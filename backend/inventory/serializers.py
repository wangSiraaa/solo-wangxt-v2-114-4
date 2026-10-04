from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    BatchConflict,
    Campaign,
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
)


class StratumSerializer(serializers.ModelSerializer):
    class Meta:
        model = Stratum
        fields = ["id", "code", "name", "area_ha"]


class SpeciesSerializer(serializers.ModelSerializer):
    class Meta:
        model = Species
        fields = ["id", "code", "name", "family"]


class CampaignSerializer(serializers.ModelSerializer):
    class Meta:
        model = Campaign
        fields = ["id", "code", "measured_on", "description"]


class PlotSerializer(serializers.ModelSerializer):
    stratum_code = serializers.CharField(source="stratum.code", read_only=True)
    stratum_name = serializers.CharField(source="stratum.name", read_only=True)
    crs_epsg = serializers.SerializerMethodField()

    class Meta:
        model = Plot
        fields = [
            "id", "code", "stratum", "stratum_code", "stratum_name",
            "x_m", "y_m", "declared_area_ha", "area_polygon_ha",
            "boundary", "crs_epsg",
        ]

    def get_crs_epsg(self, _obj):
        from django.conf import settings
        return settings.SURVEY_CRS_EPSG


class EquationSerializer(serializers.ModelSerializer):
    species_codes = serializers.SlugRelatedField(
        many=True, read_only=True, slug_field="code", source="species"
    )

    class Meta:
        model = AllometricEquation
        fields = [
            "id", "code", "version", "species_codes", "status", "form",
            "a", "b", "c", "dbh_min_cm", "dbh_max_cm",
            "height_required", "residual_sigma", "citation", "created_at",
        ]


class TreeSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    species_code = serializers.CharField(source="species.code", read_only=True)
    supersedes = serializers.PrimaryKeyRelatedField(
        source="superseded_tree", read_only=True
    )

    class Meta:
        model = Tree
        fields = [
            "id", "plot", "plot_code", "species_code",
            "current_field_number", "first_campaign", "supersedes",
        ]


class MeasurementSerializer(serializers.ModelSerializer):
    plot_code = serializers.CharField(source="tree.plot.code", read_only=True)
    field_number = serializers.CharField(source="field_number_seen")

    class Meta:
        model = TreeMeasurement
        fields = [
            "id", "tree", "campaign", "plot_code", "field_number",
            "x_m", "y_m", "status",
            "dbh_raw", "dbh_unit", "dbh_cm",
            "height_raw", "height_unit", "height_m", "notes",
        ]


class ConflictSerializer(serializers.ModelSerializer):
    class Meta:
        model = IdentityConflict
        fields = [
            "id", "plot", "field_number", "t1_campaign", "t2_campaign",
            "t1_measurement", "t2_measurement", "distance_m",
            "status", "resolution_note", "resolved_at",
        ]
        read_only_fields = ["distance_m", "resolved_at"]


class ConflictResolveSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["renumber", "distinct"])
    note = serializers.CharField(required=False, allow_blank=True)


class EstimateVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = EstimateVersion
        fields = [
            "id", "label", "t1_campaign", "t2_campaign", "status",
            "design_snapshot", "result_payload", "equation_checksum",
            "created_at", "confirmed_at",
        ]
        read_only_fields = [
            "status", "design_snapshot", "result_payload",
            "equation_checksum", "confirmed_at",
        ]


class MeasurementImportRowSerializer(serializers.Serializer):
    """One raw field row. Units are mandatory with every value."""

    plot = serializers.CharField()
    field_number = serializers.CharField()
    species = serializers.CharField()
    x_m = serializers.FloatField()
    y_m = serializers.FloatField()
    status = serializers.ChoiceField(
        choices=["alive_measured", "alive_not_measured", "dead", "missing_tree"]
    )
    dbh_raw = serializers.FloatField(required=False, allow_null=True)
    dbh_unit = serializers.ChoiceField(choices=["cm", "mm", "in"],
                                       required=False, allow_null=True)
    height_raw = serializers.FloatField(required=False, allow_null=True)
    height_unit = serializers.ChoiceField(choices=["m"],
                                          required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    # Used ONLY to record a field-book verified renumber. Never inferred.
    verified_renumber_of_tree = serializers.IntegerField(
        required=False, allow_null=True
    )


class MeasurementImportSerializer(serializers.Serializer):
    campaign = serializers.CharField()
    rows = MeasurementImportRowSerializer(many=True)


# ------------------------------------------------------------- batch sync
class BatchSubmitSerializer(serializers.Serializer):
    """One survey batch uploaded by a field tablet."""

    campaign = serializers.CharField()
    survey = serializers.CharField(
        help_text="Client-side survey identifier shared by all batches "
                  "of this survey.")
    idempotency_key = serializers.CharField(
        max_length=80,
        help_text="Client-generated unique key; retransmission with the "
                  "same key and payload returns the stored receipt.")
    expected_batches = serializers.IntegerField(
        required=False, allow_null=True, min_value=1,
        help_text="Total number of batches this survey will arrive in.")
    client_meta = serializers.DictField(required=False)
    rows = MeasurementImportRowSerializer(many=True)


class SurveySerializer(serializers.ModelSerializer):
    campaign_code = serializers.CharField(source="campaign.code",
                                          read_only=True)
    n_batches = serializers.SerializerMethodField()
    n_applied = serializers.SerializerMethodField()
    n_blocked = serializers.SerializerMethodField()
    n_failed = serializers.SerializerMethodField()

    class Meta:
        model = Survey
        fields = [
            "id", "code", "campaign", "campaign_code", "expected_batches",
            "status", "finalized_at", "created_at",
            "n_batches", "n_applied", "n_blocked", "n_failed",
        ]

    def _count(self, obj, status):
        return obj.batches.filter(status=status).count()

    def get_n_batches(self, obj):
        return obj.batches.count()

    def get_n_applied(self, obj):
        return self._count(obj, "applied")

    def get_n_blocked(self, obj):
        return self._count(obj, "blocked")

    def get_n_failed(self, obj):
        return self._count(obj, "failed")


class SurveyBatchSerializer(serializers.ModelSerializer):
    survey_code = serializers.CharField(source="survey.code", read_only=True)
    campaign_code = serializers.CharField(source="campaign.code",
                                          read_only=True)
    retryable = serializers.SerializerMethodField()

    class Meta:
        model = SurveyBatch
        fields = [
            "id", "survey", "survey_code", "campaign", "campaign_code",
            "idempotency_key", "content_digest", "status", "row_count",
            "client_meta", "error", "attempt_count", "retryable",
            "created_at", "updated_at", "applied_at",
        ]

    def get_retryable(self, obj):
        return obj.status in ("staged", "validated")


class SurveyBatchDetailSerializer(SurveyBatchSerializer):
    receipt = serializers.SerializerMethodField()
    conflicts = serializers.SerializerMethodField()

    class Meta(SurveyBatchSerializer.Meta):
        fields = SurveyBatchSerializer.Meta.fields + [
            "receipt", "conflicts",
        ]

    def get_receipt(self, obj):
        from inventory.services.batches import build_receipt
        return obj.receipt or build_receipt(obj)

    def get_conflicts(self, obj):
        return BatchConflictSerializer(obj.conflicts.all(), many=True).data


class BatchConflictSerializer(serializers.ModelSerializer):
    idempotency_key = serializers.CharField(source="batch.idempotency_key",
                                            read_only=True)
    campaign_code = serializers.CharField(source="batch.campaign.code",
                                          read_only=True)
    field_number = serializers.SerializerMethodField()
    plot = serializers.SerializerMethodField()

    class Meta:
        model = BatchConflict
        fields = [
            "id", "batch", "idempotency_key", "campaign_code",
            "audit_row", "field_number", "plot",
            "existing_measurement", "existing_batch", "detail",
            "status", "resolution_note", "resolved_at", "created_at",
        ]
        read_only_fields = ["detail", "resolved_at"]

    def get_field_number(self, obj):
        return obj.audit_row.raw_payload.get("field_number")

    def get_plot(self, obj):
        return obj.audit_row.raw_payload.get("plot")


class BatchConflictResolveSerializer(serializers.Serializer):
    action = serializers.ChoiceField(
        choices=["keep_existing", "accept_incoming", "accept_as_new"])
    note = serializers.CharField(required=False, allow_blank=True)


class MeasurementImportRowAuditSerializer(serializers.ModelSerializer):
    """Queryable audit of every imported row, before and after apply."""

    campaign_code = serializers.CharField(source="campaign.code",
                                          read_only=True)
    plot_code = serializers.CharField(source="plot.code", read_only=True)
    idempotency_key = serializers.CharField(source="batch.idempotency_key",
                                            read_only=True, default=None)

    class Meta:
        model = MeasurementImportRow
        fields = [
            "id", "campaign", "campaign_code", "plot", "plot_code",
            "batch", "idempotency_key", "seq", "row_state", "accepted",
            "rejection_reason", "raw_payload", "canonical",
            "measurement", "created_at",
        ]
