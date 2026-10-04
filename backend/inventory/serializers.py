from rest_framework import serializers

from inventory.models import (
    AllometricEquation,
    Campaign,
    EstimateVersion,
    IdentityConflict,
    ImportBatch,
    Plot,
    Species,
    Stratum,
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


class BatchSubmitSerializer(serializers.Serializer):
    """One survey batch uploaded by a field tablet."""

    campaign = serializers.CharField()
    client_key = serializers.CharField(max_length=64)
    rows = MeasurementImportRowSerializer(many=True)

    def validate_client_key(self, value):
        if not value.strip():
            raise serializers.ValidationError("client_key must not be blank")
        return value


class BatchSerializer(serializers.ModelSerializer):
    """Batch summary for lists and progress views (no row payloads)."""

    campaign_code = serializers.CharField(source="campaign.code",
                                          read_only=True)

    class Meta:
        model = ImportBatch
        fields = [
            "id", "campaign", "campaign_code", "client_key", "content_hash",
            "status", "n_rows", "n_applied", "error", "resolution_note",
            "created_at", "updated_at", "applied_at", "resolved_at",
        ]


class BatchDetailSerializer(BatchSerializer):
    """Full batch detail: per-row receipts and (if blocked) overlaps."""

    class Meta(BatchSerializer.Meta):
        fields = BatchSerializer.Meta.fields + ["receipt", "overlaps"]


class BatchResolveSerializer(serializers.Serializer):
    # A later batch may NEVER overwrite first-arrived data; the only
    # human decision is to discard the blocked batch.
    decision = serializers.ChoiceField(choices=["discard"])
    note = serializers.CharField(required=False, allow_blank=True)
