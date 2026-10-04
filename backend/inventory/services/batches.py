"""
Resumable survey batch synchronisation.

Field tablets work offline and upload ONE survey as several batches; a
later survey's batch (e.g. 2024) may arrive before an earlier one (2019),
and batches may be retransmitted when the network recovers.

Guarantees
==========
* **Idempotent submission** — ``idempotency_key`` is unique. Same key +
  same content digest returns the stored receipt without any write; same
  key + different digest is rejected (HTTP 409).
* **All-or-nothing apply** — a batch is validated row by row at staging;
  only when every row passes are measurements written, in ONE atomic
  transaction together with the identity scan bookkeeping. An interrupted
  apply rolls everything back: the batch stays ``validated`` (retryable)
  and no partial measurements survive.
* **First writer wins** — a row that contradicts an already-applied
  record of the same campaign/plot/field number never overwrites it. The
  batch is ``blocked`` and a :class:`BatchConflict` is opened for manual
  handling (keep existing / accept as correction / accept as new
  individual). Exact re-deliveries are folded to ``duplicate``.
* **Adjacent-data gating** — identity conflicts (and, via the estimate
  endpoint, mortality / ingrowth) are only computed once the survey data
  of BOTH adjacent campaigns is complete, so an early 2024 batch cannot
  fabricate deaths or ingrowth before the 2019 survey has arrived.

State machine
=============
``staged`` -> ``validated`` -> ``applied`` | ``blocked`` | ``failed``

* ``staged``     rows stored, validation pending (crash-safe, redoable)
* ``validated``  all rows passed; apply may be (re)attempted
* ``applied``    measurements written atomically; terminal
* ``blocked``    open BatchConflicts; resolvable, then re-apply
* ``failed``     validation errors; correct the data and resubmit under a
                 NEW idempotency key
"""
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from inventory.models import (
    BATCH_APPLIED,
    BATCH_BLOCKED,
    BATCH_CONFLICT_ACCEPT,
    BATCH_CONFLICT_AS_NEW,
    BATCH_CONFLICT_KEEP,
    BATCH_CONFLICT_OPEN,
    BATCH_FAILED,
    BATCH_STAGED,
    BATCH_VALIDATED,
    BatchConflict,
    Campaign,
    MeasurementImportRow,
    Plot,
    ROW_APPLIED,
    ROW_CONFLICTED,
    ROW_DROPPED,
    ROW_DUPLICATE,
    ROW_REJECTED,
    ROW_STAGED,
    ROW_VALIDATED,
    Species,
    SURVEY_COMPLETE,
    Survey,
    SurveyBatch,
    SurveyScan,
    Tree,
    TreeMeasurement,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.ingest import (
    canonical_content,
    canonical_of_measurement,
    resolve_target_tree,
    validate_field_row,
    write_measurement,
)


class BatchStateError(Exception):
    """Raised when an apply is requested for a non-applyable batch."""


# ------------------------------------------------------------------- digests
def _canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)


def batch_content_digest(campaign_code, survey_code, rows):
    """SHA-256 over the canonical submission payload."""
    payload = {
        "campaign": campaign_code,
        "survey": survey_code,
        "rows": rows,
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


# ---------------------------------------------------------------- submission
def get_or_create_survey(campaign, survey_code, expected_batches=None):
    """
    Fetch the survey for ``survey_code`` or create it for ``campaign``.
    Raises BatchStateError if the code is bound to another campaign or the
    declared batch count disagrees with an earlier declaration.
    """
    survey = Survey.objects.filter(code=survey_code).first()
    if survey is None:
        survey = Survey.objects.create(
            code=survey_code, campaign=campaign,
            expected_batches=expected_batches,
        )
        return survey
    if survey.campaign_id != campaign.id:
        raise BatchStateError(
            f"survey {survey_code!r} belongs to campaign "
            f"{survey.campaign.code}, not {campaign.code}")
    if expected_batches is not None:
        if survey.expected_batches is None:
            survey.expected_batches = expected_batches
            survey.save(update_fields=["expected_batches"])
        elif survey.expected_batches != expected_batches:
            raise BatchStateError(
                f"survey {survey_code!r} already declared "
                f"{survey.expected_batches} expected batches; got "
                f"{expected_batches}")
    return survey


def find_replay(idempotency_key, digest):
    """
    Returns ``(batch, replay)``:
      * batch + replay=True   -> same key, same payload: answer from store
      * batch + replay=False  -> same key, DIFFERENT payload: reject
      * (None, False)         -> new submission
    """
    batch = SurveyBatch.objects.filter(
        idempotency_key=idempotency_key).first()
    if batch is None:
        return None, False
    return batch, batch.content_digest == digest


def submit_batch(campaign, survey_code, idempotency_key, rows,
                 expected_batches=None, client_meta=None,
                 area_tolerance=0.01):
    """
    Stage, validate and (if clean) apply one batch.

    Returns the stored receipt. Raises BatchStateError for survey binding
    problems. Idempotent replays are handled by the caller via
    :func:`find_replay`.
    """
    digest = batch_content_digest(campaign.code, survey_code, rows)
    with transaction.atomic():
        survey = get_or_create_survey(campaign, survey_code, expected_batches)
        batch = SurveyBatch.objects.create(
            survey=survey, campaign=campaign,
            idempotency_key=idempotency_key, content_digest=digest,
            status=BATCH_STAGED, row_count=len(rows),
            client_meta=client_meta or {},
        )
        _stage_and_validate(batch, rows)
        if batch.status == BATCH_FAILED:
            batch.receipt = build_receipt(batch)
            batch.save(update_fields=["receipt", "updated_at"])
            return batch.receipt
        batch.status = BATCH_VALIDATED
        batch.save(update_fields=["status", "updated_at"])
    # Apply outside the staging transaction so a crash between the two
    # leaves a clean, retryable `validated` batch.
    try:
        return apply_batch(batch.pk, area_tolerance=area_tolerance)
    except BatchStateError:
        raise
    except Exception:
        # apply_batch already recorded the interruption on the batch;
        # answer with the retryable receipt instead of losing the batch.
        batch.refresh_from_db()
        return build_receipt(batch)


def _stage_and_validate(batch, rows):
    """
    Validate every row and write the audit trail (queryable BEFORE apply).

    Within one batch, rows sharing (plot, field_number):
      * identical canonical content -> folded to ``duplicate`` (applied
        once), e.g. a row repeated by the tablet;
      * different content at the SAME position (<= 1 m) -> both rejected:
        two contradictory descriptions of one stem is a data error the
        crew must fix; the batch fails and nothing is applied;
      * different content at DIFFERENT positions -> allowed: two
        individuals may legitimately share a tag number — they become two
        trees and the identity scan (not the batch gate) judges them.

    Any rejected row fails the whole batch (all-or-nothing apply).
    """
    audits = []
    n_bad = 0
    for seq, raw in enumerate(rows):
        plot = Plot.objects.filter(code=raw["plot"]).first()
        species = Species.objects.filter(code=raw["species"]).first()
        reason, dbh_cm, height_m = validate_field_row(raw, plot, species)
        canonical = None
        state = ROW_REJECTED
        if reason is None:
            canonical = canonical_content(raw, dbh_cm, height_m)
            state = ROW_VALIDATED
        if state == ROW_REJECTED:
            n_bad += 1
        audits.append(MeasurementImportRow.objects.create(
            campaign=batch.campaign, plot=plot or Plot.objects.first(),
            raw_payload=raw, accepted=state != ROW_REJECTED,
            rejection_reason=reason or "",
            batch=batch, seq=seq, row_state=state, canonical=canonical,
        ))

    groups = {}
    for ar in audits:
        if ar.row_state != ROW_VALIDATED:
            continue
        key = (ar.canonical["plot"], ar.canonical["field_number"])
        fingerprint = _canonical_json(ar.canonical)
        group = groups.setdefault(key, [])
        if any(g["fingerprint"] == fingerprint for g in group):
            ar.row_state = ROW_DUPLICATE
            ar.rejection_reason = "duplicate row within batch"
            ar.save(update_fields=["row_state", "rejection_reason"])
            continue
        clash = next((g for g in group
                      if _position_distance(ar.canonical, g["canonical"])
                      <= 1.0), None)
        if clash is not None:
            reason = (f"contradicts row {clash['seq']} of this batch: same "
                      f"plot/field number at the same position but "
                      f"different content")
            for victim in (ar, clash["audit"]):
                if victim.row_state == ROW_VALIDATED:
                    victim.row_state = ROW_REJECTED
                    victim.accepted = False
                    victim.rejection_reason = reason
                    victim.save(update_fields=["row_state", "accepted",
                                               "rejection_reason"])
                    n_bad += 1
            continue
        group.append({"seq": ar.seq, "fingerprint": fingerprint,
                      "canonical": ar.canonical, "audit": ar})

    if n_bad:
        batch.status = BATCH_FAILED
        batch.error = (f"{n_bad} row(s) failed validation; correct the "
                       f"data and resubmit under a NEW idempotency key")
        batch.save(update_fields=["status", "error", "updated_at"])


def _position_distance(c1, c2):
    return ((c1["x_m"] - c2["x_m"]) ** 2
            + (c1["y_m"] - c2["y_m"]) ** 2) ** 0.5


# -------------------------------------------------------------------- apply
def apply_batch(batch_id, area_tolerance=0.01):
    """
    (Re)apply a validated/blocked batch. Safe to call repeatedly:
    applied batches return their stored receipt; an interruption rolls
    back to ``validated`` with no partial measurements.
    """
    try:
        with transaction.atomic():
            batch = (SurveyBatch.objects.select_for_update()
                     .get(pk=batch_id))
            if batch.status == BATCH_APPLIED:
                return batch.receipt
            if batch.status == BATCH_FAILED:
                raise BatchStateError(
                    "batch failed validation; correct the rows and "
                    "resubmit under a NEW idempotency key")
            if batch.status == BATCH_STAGED:
                # crash between staging and validation: redo validation
                rows = [ar.raw_payload for ar in
                        batch.audit_rows.order_by("seq")]
                batch.audit_rows.all().delete()
                _stage_and_validate(batch, rows)
                if batch.status == BATCH_FAILED:
                    batch.receipt = build_receipt(batch)
                    batch.save(update_fields=["receipt", "updated_at"])
                    return batch.receipt
                batch.status = BATCH_VALIDATED
            if batch.status == BATCH_BLOCKED:
                open_conflicts = batch.conflicts.filter(
                    status=BATCH_CONFLICT_OPEN).count()
                if open_conflicts:
                    raise BatchStateError(
                        f"{open_conflicts} overlapping tree record(s) still "
                        f"await manual review")
                batch.status = BATCH_VALIDATED
            batch.attempt_count += 1
            batch.error = ""
            applied = _apply_validated(batch)
            if not applied:
                # newly discovered overlaps: batch is blocked again; the
                # receipt (stored by _apply_validated) lists the conflicts.
                return batch.receipt
            batch.status = BATCH_APPLIED
            batch.applied_at = timezone.now()
            batch.receipt = build_receipt(batch)
            batch.save()
    except BatchStateError:
        raise
    except Exception as exc:
        # The atomic block rolled back: no partial measurements. Record
        # the interruption so a refresh shows a retryable state.
        batch = SurveyBatch.objects.get(pk=batch_id)
        batch.error = f"apply interrupted, safe to retry: {exc}"
        batch.save(update_fields=["error", "updated_at"])
        raise

    # Post-commit: survey completion + gated identity scan. These are
    # idempotent and run outside the apply transaction.
    batch = SurveyBatch.objects.get(pk=batch_id)
    maybe_complete_survey(batch.survey)
    scan_info = maybe_scan_for_campaign(batch.campaign,
                                        trigger=batch.survey.code)
    receipt = dict(batch.receipt)
    receipt["scan"] = scan_info
    batch.receipt = receipt
    batch.save(update_fields=["receipt", "updated_at"])
    return receipt


def _apply_validated(batch):
    """
    Write measurements for all pending rows of a validated batch.

    Runs inside the caller's atomic block. Overlaps with OTHER batches'
    applied records open BatchConflicts and block the batch; rows applied
    earlier in THIS run are tracked so a legitimate same-batch label
    sharing (two individuals, one tag number) is not self-flagged.

    Returns True when every row was written (batch may be marked
    applied), False when the batch was blocked on new conflicts.
    """
    campaign = batch.campaign
    own_measurement_ids = set()
    new_conflicts = []

    pending = list(batch.audit_rows.filter(
        row_state__in=[ROW_VALIDATED, ROW_CONFLICTED]).order_by("seq"))

    # ---- pass 1: overlap detection against OTHER batches' applied data
    for ar in pending:
        resolution = batch.conflicts.filter(audit_row=ar).first()
        if resolution is not None and resolution.status != BATCH_CONFLICT_OPEN:
            continue  # human decision executed in pass 2
        existing = _existing_records(campaign, ar, own_measurement_ids)
        if not existing:
            continue
        if any(_canonical_equal(m, ar) for m in existing):
            # exact re-delivery (e.g. network-recovery retransmission
            # under a new key): fold, never write twice.
            ar.row_state = ROW_DUPLICATE
            ar.rejection_reason = "identical record already applied"
            ar.measurement = next(m for m in existing
                                  if _canonical_equal(m, ar))
            ar.save(update_fields=["row_state", "rejection_reason",
                                   "measurement"])
            continue
        conflict, created = BatchConflict.objects.get_or_create(
            batch=batch, audit_row=ar,
            defaults={
                "existing_measurement": existing[0],
                "existing_batch": _batch_of(existing[0]),
                "detail": {
                    "incoming": ar.canonical,
                    "existing": [canonical_of_measurement(m)
                                 for m in existing],
                },
            },
        )
        if conflict.status == BATCH_CONFLICT_OPEN:
            ar.row_state = ROW_CONFLICTED
            ar.rejection_reason = (
                "overlaps an already-applied record; awaiting manual review")
            ar.save(update_fields=["row_state", "rejection_reason"])
            new_conflicts.append(conflict)

    if new_conflicts:
        batch.status = BATCH_BLOCKED
        batch.error = (f"{len(new_conflicts)} overlapping tree record(s) "
                       f"need manual review; earlier data kept")
        batch.receipt = build_receipt(batch)
        batch.save(update_fields=["status", "error", "receipt",
                                  "updated_at"])
        return False

    # ---- pass 2: write measurements
    own_tree_ids = set()
    for ar in pending:
        if ar.row_state == ROW_DUPLICATE:
            continue
        resolution = batch.conflicts.filter(audit_row=ar).first()
        raw = ar.raw_payload
        dbh_cm = ar.canonical["dbh_cm"] if ar.canonical else None
        height_m = ar.canonical["height_m"] if ar.canonical else None
        plot = Plot.objects.get(code=raw["plot"])
        species = Species.objects.get(code=raw["species"])

        if resolution is not None:
            if resolution.status == BATCH_CONFLICT_KEEP:
                ar.row_state = ROW_DROPPED
                ar.rejection_reason = (
                    "reviewer kept the earlier applied record")
                ar.measurement = resolution.existing_measurement
                ar.save(update_fields=["row_state", "rejection_reason",
                                       "measurement"])
                continue
            if resolution.status == BATCH_CONFLICT_ACCEPT:
                # verified correction: update the existing measurement in
                # place (manual, audited — never automatic).
                measurement = resolution.existing_measurement
                _overwrite_measurement(measurement, raw, dbh_cm, height_m)
                ar.row_state = ROW_APPLIED
                ar.measurement = measurement
                ar.save(update_fields=["row_state", "measurement"])
                continue
            if resolution.status == BATCH_CONFLICT_AS_NEW:
                tree = Tree.objects.create(
                    plot=plot, species=species,
                    current_field_number=raw["field_number"],
                    first_campaign=campaign,
                )
                measurement = write_measurement(tree, campaign, raw,
                                                dbh_cm, height_m)
                own_measurement_ids.add(measurement.id)
                ar.row_state = ROW_APPLIED
                ar.measurement = measurement
                ar.save(update_fields=["row_state", "measurement"])
                continue
            # open conflict: unreachable (guarded in apply_batch)

        tree, superseded, err = resolve_target_tree(campaign, plot, raw)
        if err is not None:
            # renumber target vanished between staging and apply
            raise BatchStateError(f"row {ar.seq}: {err}")
        if tree is not None and tree.id in own_tree_ids:
            # two rows of this batch claim the same tracked tree (e.g. a
            # duplicated verified_renumber_of_tree link): refuse the
            # silent overwrite; the batch stays retryable for review.
            raise BatchStateError(
                f"rows of this batch collide on tree {tree.id} "
                f"({plot.code}/{raw['field_number']}); correct and "
                f"resubmit under a NEW idempotency key")
        if tree is None:
            tree = Tree.objects.create(
                plot=plot, species=species,
                current_field_number=raw["field_number"],
                first_campaign=campaign,
            )
        elif superseded is not None:
            tree.current_field_number = raw["field_number"]
            tree.save(update_fields=["current_field_number"])
        own_tree_ids.add(tree.id)
        measurement = write_measurement(tree, campaign, raw,
                                        dbh_cm, height_m)
        own_measurement_ids.add(measurement.id)
        ar.row_state = ROW_APPLIED
        ar.measurement = measurement
        ar.save(update_fields=["row_state", "measurement"])
    return True


def _existing_records(campaign, audit_row, own_measurement_ids):
    """
    Already-applied measurements of OTHER batches that claim the same
    tree record: same campaign + plot + field number, or the tracked tree
    this row resolves to. Rows written by the current apply run are
    excluded so one batch may legitimately carry two individuals sharing
    a label (the identity scan, not the batch gate, judges those).
    """
    raw = audit_row.raw_payload
    qs = (TreeMeasurement.objects
          .filter(campaign=campaign,
                  tree__plot__code=raw["plot"],
                  field_number_seen=raw["field_number"])
          .select_related("tree", "tree__plot", "tree__species"))
    found = [m for m in qs if m.id not in own_measurement_ids]
    tree, _sup, err = resolve_target_tree(
        campaign, Plot.objects.get(code=raw["plot"]), raw)
    if err is None and tree is not None:
        for m in (TreeMeasurement.objects
                  .filter(campaign=campaign, tree=tree)
                  .select_related("tree", "tree__plot", "tree__species")):
            if m.id not in own_measurement_ids and m not in found:
                found.append(m)
    return found


def _canonical_equal(measurement, audit_row):
    return canonical_of_measurement(measurement) == audit_row.canonical


def _batch_of(measurement):
    audit = (MeasurementImportRow.objects
             .filter(measurement=measurement, batch__isnull=False)
             .order_by("id").first())
    return audit.batch if audit else None


def _overwrite_measurement(measurement, raw, dbh_cm, height_m):
    for field, value in {
        "field_number_seen": raw["field_number"],
        "x_m": raw["x_m"], "y_m": raw["y_m"],
        "status": raw["status"],
        "dbh_raw": raw.get("dbh_raw"),
        "dbh_unit": raw.get("dbh_unit"),
        "dbh_cm": dbh_cm,
        "height_raw": raw.get("height_raw"),
        "height_unit": raw.get("height_unit"),
        "height_m": height_m,
        "notes": raw.get("notes", ""),
    }.items():
        setattr(measurement, field, value)
    measurement.save()


# ------------------------------------------------- survey completion & scan
def maybe_complete_survey(survey):
    """Auto-complete once the declared number of batches has applied."""
    if survey.status == SURVEY_COMPLETE:
        return False
    if (survey.expected_batches is not None
            and survey.batches.filter(status=BATCH_APPLIED).count()
            >= survey.expected_batches):
        survey.status = SURVEY_COMPLETE
        survey.finalized_at = timezone.now()
        survey.save(update_fields=["status", "finalized_at"])
        return True
    return False


def finalize_survey(survey):
    """Coordinator marks every batch accounted for; triggers the scan."""
    if survey.status != SURVEY_COMPLETE:
        survey.status = SURVEY_COMPLETE
        survey.finalized_at = timezone.now()
        survey.save(update_fields=["status", "finalized_at"])
    return maybe_scan_for_campaign(survey.campaign, trigger=survey.code)


def campaign_data_ready(campaign):
    """
    Is this campaign's survey data complete enough to derive cross-survey
    products (identity conflicts, mortality, ingrowth)?

    Batch-managed campaigns need a COMPLETE survey; campaigns whose data
    arrived through the legacy direct import count as ready once they
    hold measurements.
    """
    surveys = list(campaign.surveys.all())
    if surveys:
        return any(s.status == SURVEY_COMPLETE for s in surveys)
    return campaign.measurements.exists()


def adjacent_campaigns(campaign):
    """Immediate predecessor/successor campaigns by measurement date."""
    ordered = list(Campaign.objects.order_by("measured_on", "id"))
    idx = next((i for i, c in enumerate(ordered) if c.id == campaign.id),
               None)
    if idx is None:
        return []
    neighbours = []
    if idx > 0:
        neighbours.append(ordered[idx - 1])
    if idx < len(ordered) - 1:
        neighbours.append(ordered[idx + 1])
    return neighbours


def estimate_prerequisites(t1_campaign, t2_campaign):
    """
    Problems that forbid computing mortality/ingrowth for this pair, or
    [] when adjacent survey data is complete on both sides.
    """
    problems = []
    for c in (t1_campaign, t2_campaign):
        if not campaign_data_ready(c):
            problems.append(
                f"campaign {c.code}: survey data incomplete — batches are "
                f"still being collected (or none arrived); mortality, "
                f"ingrowth and identity conflicts are computed only after "
                f"adjacent survey data is complete")
    return problems


def maybe_scan_for_campaign(campaign, trigger=""):
    """
    Run the identity scan for every adjacent campaign pair whose survey
    data is complete on BOTH sides. Idempotent: conflicts are upserted
    and a pair is re-scanned only when newer batches were applied.
    """
    pairs_scanned, waiting = [], []
    for neighbour in adjacent_campaigns(campaign):
        t1, t2 = sorted([campaign, neighbour],
                        key=lambda c: (c.measured_on, c.id))
        if not (campaign_data_ready(t1) and campaign_data_ready(t2)):
            other = t2 if t1.id == campaign.id else t1
            waiting.append(other.code)
            continue
        last_scan = SurveyScan.objects.filter(
            t1_campaign=t1, t2_campaign=t2).first()
        latest_apply = _latest_apply([t1, t2])
        if (last_scan is not None and latest_apply is not None
                and last_scan.scanned_at >= latest_apply):
            pairs_scanned.append({
                "t1": t1.code, "t2": t2.code,
                "scan_id": last_scan.id,
                "n_conflicts": last_scan.n_conflicts,
                "rerun": False,
            })
            continue
        found = scan_conflicts(t1, t2)
        scan, _created = SurveyScan.objects.update_or_create(
            t1_campaign=t1, t2_campaign=t2,
            defaults={"scanned_at": timezone.now(),
                      "n_conflicts": len(found), "trigger": trigger},
        )
        pairs_scanned.append({
            "t1": t1.code, "t2": t2.code, "scan_id": scan.id,
            "n_conflicts": len(found), "rerun": True,
        })
    if pairs_scanned:
        return {"status": "scanned", "pairs": pairs_scanned,
                "waiting_for": waiting}
    if waiting:
        return {"status": "deferred", "waiting_for": waiting,
                "detail": "identity scan deferred until adjacent survey "
                          "data is complete"}
    return {"status": "no_adjacent_campaign", "pairs": []}


def _latest_apply(campaigns):
    ts = (SurveyBatch.objects
          .filter(campaign__in=campaigns, status=BATCH_APPLIED,
                  applied_at__isnull=False)
          .order_by("-applied_at")
          .values_list("applied_at", flat=True).first())
    return ts


# ------------------------------------------------------------------ receipt
def build_receipt(batch):
    """Per-row receipt stored on the batch and returned on replay."""
    rows = []
    counts = {"applied": 0, "duplicate": 0, "rejected": 0,
              "conflicted": 0, "dropped": 0, "pending": 0}
    for ar in batch.audit_rows.order_by("seq"):
        raw = ar.raw_payload
        state = ar.row_state
        if state in (ROW_STAGED, ROW_VALIDATED):
            counts["pending"] += 1
        elif state in counts:
            counts[state] += 1
        rows.append({
            "seq": ar.seq,
            "plot": raw.get("plot"),
            "field_number": raw.get("field_number"),
            "state": state,
            "reason": ar.rejection_reason,
            "audit_id": ar.id,
            "measurement_id": ar.measurement_id,
            "tree_id": (ar.measurement.tree_id
                        if ar.measurement_id else None),
        })
    conflicts = [{
        "id": c.id,
        "audit_id": c.audit_row_id,
        "field_number": c.audit_row.raw_payload.get("field_number"),
        "plot": c.audit_row.raw_payload.get("plot"),
        "existing_measurement_id": c.existing_measurement_id,
        "existing_batch_id": c.existing_batch_id,
        "status": c.status,
    } for c in batch.conflicts.select_related("audit_row")]
    return {
        "batch_id": batch.id,
        "idempotency_key": batch.idempotency_key,
        "survey": batch.survey.code,
        "campaign": batch.campaign.code,
        "status": batch.status,
        "content_digest": batch.content_digest,
        "n_rows": batch.row_count,
        "n_applied": counts["applied"],
        "n_duplicates": counts["duplicate"],
        "n_rejected": counts["rejected"],
        "n_conflicts": len([c for c in conflicts
                            if c["status"] == BATCH_CONFLICT_OPEN]),
        "n_dropped": counts["dropped"],
        "attempt_count": batch.attempt_count,
        "error": batch.error,
        "rows": rows,
        "conflicts": conflicts,
    }
