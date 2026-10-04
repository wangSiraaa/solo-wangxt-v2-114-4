"""
REST API:

GET  /plots/                        plot positions + boundaries (React map)
GET  /trees/?campaign=CODE          individuals and remeasurement status
GET  /conflicts/                    same-number position contradictions
POST /conflicts/{id}/resolve/       human verification only
POST /imports/                      ingest a campaign's field rows (legacy)
POST /batches/                      submit one survey batch (idempotent)
GET  /batches/                      upload progress per batch
GET  /batches/{id}/                 batch detail + per-row receipt
POST /batches/{id}/apply/           continue/apply a validated|blocked batch
GET  /surveys/                      survey collection progress
POST /surveys/{id}/finalize/        mark a survey complete (triggers scan)
GET  /batch-conflicts/              overlapping tree records, manual queue
POST /batch-conflicts/{id}/resolve/ keep existing / accept incoming / as new
GET  /import-rows/                  row-level audit, before and after apply
POST /estimates/                    run (or rerun) a DRAFT estimate
POST /estimates/{id}/confirm/       freeze forever; locks equations
GET  /estimates/{id}/               frozen result with provenance
"""
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from inventory.models import (
    AllometricEquation,
    BATCH_APPLIED,
    BATCH_BLOCKED,
    BATCH_CONFLICT_OPEN,
    BATCH_FAILED,
    BatchConflict,
    Campaign,
    CONFLICT_DISTINCT,
    CONFLICT_OPEN,
    CONFLICT_RENUMBER,
    EstimateVersion,
    IdentityConflict,
    MeasurementImportRow,
    Plot,
    Species,
    Stratum,
    Survey,
    SurveyBatch,
    Tree,
    TreeMeasurement,
    VERSION_CONFIRMED,
)
from inventory.serializers import (
    BatchConflictResolveSerializer,
    BatchConflictSerializer,
    BatchSubmitSerializer,
    CampaignSerializer,
    ConflictResolveSerializer,
    ConflictSerializer,
    EquationSerializer,
    EstimateVersionSerializer,
    MeasurementImportRowAuditSerializer,
    MeasurementImportSerializer,
    MeasurementSerializer,
    PlotSerializer,
    SpeciesSerializer,
    StratumSerializer,
    SurveyBatchDetailSerializer,
    SurveyBatchSerializer,
    SurveySerializer,
    TreeSerializer,
)
from inventory.services.batches import (
    BatchStateError,
    apply_batch,
    batch_content_digest,
    build_receipt,
    estimate_prerequisites,
    finalize_survey,
    find_replay,
    submit_batch,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    equation_checksum,
    resolved_identity_pairs,
)
from inventory.services.ingest import import_campaign_rows


class StratumViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Stratum.objects.all()
    serializer_class = StratumSerializer


class SpeciesViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Species.objects.all()
    serializer_class = SpeciesSerializer


class CampaignViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Campaign.objects.all()
    serializer_class = CampaignSerializer


class EquationViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = AllometricEquation.objects.prefetch_related("species").all()
    serializer_class = EquationSerializer


class PlotViewSet(viewsets.ReadOnlyModelViewSet):
    queryset = Plot.objects.select_related("stratum").all()
    serializer_class = PlotSerializer


class TreeViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = TreeSerializer

    def get_queryset(self):
        qs = Tree.objects.select_related("plot", "species", "superseded_tree")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(measurements__campaign__code=campaign).distinct()
        plot = self.request.query_params.get("plot")
        if plot:
            qs = qs.filter(plot__code=plot)
        return qs


class MeasurementViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = MeasurementSerializer

    def get_queryset(self):
        qs = TreeMeasurement.objects.select_related("tree", "tree__plot",
                                                    "campaign")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        return qs


class ConflictViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = ConflictSerializer

    def get_queryset(self):
        qs = IdentityConflict.objects.select_related("plot")
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """Human-in-the-loop resolution. Nothing here is automatic."""
        conflict = self.get_object()
        ser = ConflictResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if conflict.status != CONFLICT_OPEN:
            return Response(
                {"detail": f"conflict already resolved as {conflict.status}; "
                           "verification cannot be undone here."},
                status=status.HTTP_409_CONFLICT,
            )
        decision = ser.validated_data["status"]
        with transaction.atomic():
            if decision == CONFLICT_RENUMBER:
                # same individual: t2's tree row becomes a successor of t1's
                t2_tree = conflict.t2_measurement.tree
                t1_tree = conflict.t1_measurement.tree
                if t2_tree != t1_tree:
                    t2_tree.superseded_tree = t1_tree
                    t2_tree.current_field_number = (
                        conflict.t2_measurement.field_number_seen
                    )
                    t2_tree.save(update_fields=["superseded_tree",
                                               "current_field_number"])
            # distinct: do nothing — t1 and t2 rows stay separate and enter
            # mortality / ingrowth candidates respectively.
            conflict.status = decision
            conflict.resolution_note = ser.validated_data.get("note", "")
            conflict.resolved_at = timezone.now()
            conflict.save()
        return Response(ConflictSerializer(conflict).data)


class ImportViewSet(viewsets.ViewSet):
    def create(self, request):
        ser = MeasurementImportSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        campaign = Campaign.objects.filter(
            code=ser.validated_data["campaign"]
        ).first()
        if campaign is None:
            return Response({"detail": "unknown campaign"},
                            status=status.HTTP_404_NOT_FOUND)
        result = import_campaign_rows(
            campaign, ser.validated_data["rows"],
            area_tolerance=settings.PLOT_AREA_TOLERANCE,
        )

        # re-scan identity contradictions against the other campaign
        other = Campaign.objects.exclude(pk=campaign.pk).order_by(
            "measured_on").first()
        if other:
            t1, t2 = sorted([campaign, other], key=lambda c: c.measured_on)
            result["conflicts"] = scan_conflicts(t1, t2)
        return Response(result,
                        status=status.HTTP_207_MULTI_STATUS if result["rejected"]
                        else status.HTTP_200_OK)


class SurveyViewSet(viewsets.ReadOnlyModelViewSet):
    """Upload progress per survey (batches applied / expected)."""

    serializer_class = SurveySerializer

    def get_queryset(self):
        qs = Survey.objects.select_related("campaign").prefetch_related(
            "batches")
        campaign = self.request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return qs

    @action(detail=True, methods=["post"])
    def finalize(self, request, pk=None):
        """
        Coordinator declares every batch of this survey accounted for.
        Completing a survey triggers the identity scan against adjacent
        campaigns whose survey data is also complete.
        """
        survey = self.get_object()
        scan = finalize_survey(survey)
        data = SurveySerializer(survey).data
        data["scan"] = scan
        return Response(data)


class BatchViewSet(viewsets.ViewSet):
    """
    Resumable survey batch sync.

    create: idempotent submit — same key + same payload replays the
    stored receipt without writing; same key + different payload is
    rejected; overlapping tree records block the batch for manual review.
    apply:  continue a validated/blocked batch (crash-safe, retryable).
    """

    def create(self, request):
        ser = BatchSubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        data = ser.validated_data
        campaign = Campaign.objects.filter(code=data["campaign"]).first()
        if campaign is None:
            return Response({"detail": "unknown campaign"},
                            status=status.HTTP_404_NOT_FOUND)

        digest = batch_content_digest(campaign.code, data["survey"],
                                      data["rows"])
        existing, replay = find_replay(data["idempotency_key"], digest)
        if existing is not None:
            if replay:
                # network-recovery retransmission: original receipt,
                # zero writes.
                receipt = existing.receipt or build_receipt(existing)
                return Response({**receipt, "replay": True},
                                status=status.HTTP_200_OK)
            return Response(
                {"detail": "idempotency key already used with a DIFFERENT "
                           "payload; rejected without writing. Use a new "
                           "key for corrected data.",
                 "batch_id": existing.id,
                 "stored_digest": existing.content_digest,
                 "received_digest": digest},
                status=status.HTTP_409_CONFLICT)

        try:
            receipt = submit_batch(
                campaign, data["survey"], data["idempotency_key"],
                data["rows"],
                expected_batches=data.get("expected_batches"),
                client_meta=data.get("client_meta"),
                area_tolerance=settings.PLOT_AREA_TOLERANCE,
            )
        except BatchStateError as exc:
            return Response({"detail": str(exc)},
                            status=status.HTTP_409_CONFLICT)
        except IntegrityError:
            # concurrent submit raced us on the unique idempotency key:
            # answer from the stored batch exactly like a replay check.
            batch = SurveyBatch.objects.get(
                idempotency_key=data["idempotency_key"])
            if batch.content_digest == digest:
                receipt = batch.receipt or build_receipt(batch)
                return Response({**receipt, "replay": True},
                                status=status.HTTP_200_OK)
            return Response(
                {"detail": "idempotency key already used with a DIFFERENT "
                           "payload; rejected without writing."},
                status=status.HTTP_409_CONFLICT)

        batch_status = receipt["status"]
        if batch_status == BATCH_APPLIED:
            code = status.HTTP_201_CREATED
        elif batch_status == BATCH_BLOCKED:
            code = status.HTTP_409_CONFLICT
        elif batch_status == BATCH_FAILED:
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        else:
            code = status.HTTP_202_ACCEPTED
        return Response(receipt, status=code)

    def list(self, request):
        qs = (SurveyBatch.objects
              .select_related("survey", "campaign").order_by("-created_at"))
        survey = request.query_params.get("survey")
        if survey:
            qs = qs.filter(survey__code=survey)
        campaign = request.query_params.get("campaign")
        if campaign:
            qs = qs.filter(campaign__code=campaign)
        state = request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        return Response(SurveyBatchSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        from django.shortcuts import get_object_or_404
        batch = get_object_or_404(
            SurveyBatch.objects.select_related("survey", "campaign")
                               .prefetch_related("conflicts"), pk=pk)
        return Response(SurveyBatchDetailSerializer(batch).data)

    @action(detail=True, methods=["post"])
    def apply(self, request, pk=None):
        """Continue applying a validated/blocked batch. Idempotent."""
        from django.shortcuts import get_object_or_404
        batch = get_object_or_404(SurveyBatch, pk=pk)
        try:
            receipt = apply_batch(
                batch.pk, area_tolerance=settings.PLOT_AREA_TOLERANCE)
        except BatchStateError as exc:
            return Response(
                {"detail": str(exc),
                 "receipt": build_receipt(batch)},
                status=status.HTTP_409_CONFLICT)
        except Exception as exc:  # interrupted mid-transaction: rolled back
            batch.refresh_from_db()
            return Response(
                {"detail": f"apply interrupted and rolled back; safe to "
                           f"retry. ({exc})",
                 "receipt": build_receipt(batch)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR)
        code = (status.HTTP_200_OK if receipt["status"] == BATCH_APPLIED
                else status.HTTP_409_CONFLICT)
        return Response(receipt, status=code)


class BatchConflictViewSet(viewsets.ReadOnlyModelViewSet):
    """Overlapping tree records awaiting manual handling."""

    serializer_class = BatchConflictSerializer

    def get_queryset(self):
        qs = (BatchConflict.objects
              .select_related("batch", "batch__campaign", "audit_row",
                              "existing_measurement"))
        state = self.request.query_params.get("status")
        if state:
            qs = qs.filter(status=state)
        batch = self.request.query_params.get("batch")
        if batch:
            qs = qs.filter(batch_id=batch)
        return qs

    @action(detail=True, methods=["post"])
    def resolve(self, request, pk=None):
        """
        Human decision on an overlap. The earlier applied record is kept
        unless the reviewer explicitly accepts the incoming one; nothing
        is overwritten automatically. Re-apply the batch afterwards.
        """
        conflict = self.get_object()
        ser = BatchConflictResolveSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        if conflict.status != BATCH_CONFLICT_OPEN:
            return Response(
                {"detail": f"conflict already resolved as "
                           f"{conflict.status}."},
                status=status.HTTP_409_CONFLICT)
        conflict.status = ser.validated_data["action"]
        conflict.resolution_note = ser.validated_data.get("note", "")
        conflict.resolved_at = timezone.now()
        conflict.save()
        return Response(BatchConflictSerializer(conflict).data)


class ImportRowViewSet(viewsets.ReadOnlyModelViewSet):
    """Row-level import audit, queryable before and after batch apply."""

    serializer_class = MeasurementImportRowAuditSerializer

    def get_queryset(self):
        qs = (MeasurementImportRow.objects
              .select_related("campaign", "plot", "batch"))
        params = self.request.query_params
        if params.get("campaign"):
            qs = qs.filter(campaign__code=params["campaign"])
        if params.get("batch"):
            qs = qs.filter(batch_id=params["batch"])
        if params.get("state"):
            qs = qs.filter(row_state=params["state"])
        if params.get("accepted") in ("true", "false"):
            qs = qs.filter(accepted=params["accepted"] == "true")
        return qs


class EstimateViewSet(viewsets.ViewSet):
    def list(self, request):
        qs = EstimateVersion.objects.all().order_by("-created_at")
        return Response(EstimateVersionSerializer(qs, many=True).data)

    def retrieve(self, request, pk=None):
        return Response(
            EstimateVersionSerializer(_get_version(pk)).data
        )

    def create(self, request):
        """
        Body: {"label": ..., "t1_campaign": CODE, "t2_campaign": CODE,
               "equation_ids": [...], "fpc": true}
        Creates (or recomputes) a DRAFT. Confirmation is a separate action.
        """
        label = request.data.get("label", "draft estimate")
        t1 = Campaign.objects.filter(
            code=request.data.get("t1_campaign")).first()
        t2 = Campaign.objects.filter(
            code=request.data.get("t2_campaign")).first()
        if not t1 or not t2 or t1.measured_on >= t2.measured_on:
            return Response(
                {"detail": "need t1 earlier than t2 campaign codes"},
                status=status.HTTP_400_BAD_REQUEST)
        # Mortality/ingrowth are derived only once adjacent survey data is
        # complete: a half-arrived survey (e.g. 2024 batches present, 2019
        # still offline) must not fabricate deaths or recruits.
        problems = estimate_prerequisites(t1, t2)
        if problems:
            return Response({"detail": problems},
                            status=status.HTTP_409_CONFLICT)
        eq_ids = request.data.get("equation_ids", [])
        equations_qs = AllometricEquation.objects.filter(
            id__in=eq_ids
        ).prefetch_related("species")
        if equations_qs.count() != len(eq_ids) or not eq_ids:
            return Response({"detail": "equation_ids invalid/empty"},
                            status=status.HTTP_400_BAD_REQUEST)

        table_t1, table_t2, equations, plots, strata = (
            build_measurement_table(t1, t2, equations_qs)
        )
        uncovered = sorted({
            r["species"] for r in table_t1 + table_t2
            if r["species"] not in equations
        })
        renumber, distinct = resolved_identity_pairs(t1, t2)

        interval = round(
            (t2.measured_on - t1.measured_on).days / 365.25, 3)
        design = {
            "t1_code": t1.code, "t2_code": t2.code,
            "interval_years": interval,
            "dbh_sd_cm": settings.DBH_MEASUREMENT_SD_CM,
            "height_sd_m": settings.HEIGHT_MEASUREMENT_SD_M,
            "zero_tol_cm": settings.ZERO_GROWTH_TOL_CM,
            "recruitment_cm": settings.RECRUITMENT_DBH_CM,
            "fpc": bool(request.data.get("fpc", True)),
            "crs_epsg": settings.SURVEY_CRS_EPSG,
        }
        result = estimate(table_t1, table_t2, equations, plots, strata,
                          design,
                          resolved_renumber_pairs=renumber,
                          resolved_distinct_pairs=distinct)
        result["species_without_equation"] = uncovered
        checksum = equation_checksum(equations)

        snap_strata = {code: {**s, "plot_codes": list(s["plot_codes"])}
                       for code, s in strata.items()}
        design_snapshot = {**design,
                           "strata": snap_strata,
                           "equation_ids": sorted(eq_ids),
                           "equation_codes": {sp: e["code"] + "@" + e["version"]
                                              for sp, e in equations.items()},
                           "area_tolerance": settings.PLOT_AREA_TOLERANCE}

        version = EstimateVersion.objects.create(
            label=label, t1_campaign=t1, t2_campaign=t2,
            design_snapshot=design_snapshot,
            result_payload=result, equation_checksum=checksum,
        )
        version.equations.set(equations_qs)
        return Response(EstimateVersionSerializer(version).data,
                        status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["post"])
    def confirm(self, request, pk=None):
        """Freeze the edition forever and lock its equations."""
        version = _get_version(pk)
        if version.status == VERSION_CONFIRMED:
            return Response({"detail": "already confirmed"},
                            status=status.HTTP_409_CONFLICT)
        with transaction.atomic():
            # re-verify checksum: equations must not have drifted since run
            from inventory.services.estimator import build_measurement_table
            eqs = version.equations.all().prefetch_related("species")
            equations = {}
            for e in eqs:
                for sp in e.species.all():
                    equations[sp.code] = {
                        "code": e.code, "version": e.version,
                        "a": e.a, "b": e.b, "c": e.c,
                        "dbh_min_cm": e.dbh_min_cm,
                        "dbh_max_cm": e.dbh_max_cm,
                        "height_required": e.height_required,
                        "residual_sigma": e.residual_sigma,
                        "citation": e.citation,
                    }
            current = equation_checksum(equations)
            if current != version.equation_checksum:
                return Response(
                    {"detail": "equations changed since the run; create a "
                               "new version rather than confirming stale "
                               "numbers."},
                    status=status.HTTP_409_CONFLICT)
            version.status = VERSION_CONFIRMED
            version.confirmed_at = timezone.now()
            version.save()
            # Lock the equations: a confirmed edition's equation is frozen
            # and a new coefficient set must be issued as a new equation row.
            from inventory.models import EQUATION_CONFIRMED
            eqs.update(status=EQUATION_CONFIRMED)
        return Response(EstimateVersionSerializer(version).data)


def _get_version(pk):
    from django.shortcuts import get_object_or_404
    return get_object_or_404(
        EstimateVersion.objects.prefetch_related("equations"), pk=pk)
