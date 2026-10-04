"""
Domain model for repeated-measure permanent forest plots.

Explicit design choices
=======================
* Every measured quantity carries its unit — the database never stores a
  "dbh number" without dbh_unit next to it. Accepted units: cm/mm/in for
  dbh, m for height. Unit conversion happens at ingest; canonical storage
  is centimetres (dbh) and metres (height).
* Tree coordinates are projected (x_m, y_m) in SURVEY_CRS_EPSG; plot
  boundaries are GeoJSON polygons in the same CRS. With PostGIS,
  deploy/postgis.sql adds generated geometry columns + GiST indexes.
* Individual tree identity across surveys is NOT assumed from equal tree
  numbers. Same number + contradictory position opens an IdentityConflict
  and the pair stays out of growth estimation until a human resolves it.
* Missing measurements (alive but not measured) are distinct from real
  zero growth (measured, |Δdbh| <= tolerance, cross-checked) and from
  mortality (a mortality observation exists).
* Allometric equations are versioned. EstimateVersion freezes the
  equations and a SHA-256 checksum; confirmed versions are locked and a
  new equation can never silently alter a published estimate.
"""
from django.db import models


# ---------------------------------------------------------------- constants
DBH_UNITS = ["cm", "mm", "in"]
HEIGHT_UNITS = ["m"]

DBH_TO_CM = {"cm": 1.0, "mm": 0.1, "in": 2.54}

# Tree measurement status — three mutually exclusive situations that a
# careless field database tends to collapse into "dbh = 0".
STATUS_ALIVE_MEASURED = "alive_measured"
STATUS_ALIVE_NOT_MEASURED = "alive_not_measured"   # missing, NOT zero
STATUS_DEAD = "dead"                                # mortality
STATUS_MISSING = "missing_tree"                     # not located at all
TREE_STATUS_CHOICES = [
    (STATUS_ALIVE_MEASURED, "Alive and remeasured"),
    (STATUS_ALIVE_NOT_MEASURED, "Alive but not measured (missing data)"),
    (STATUS_DEAD, "Dead (mortality observation)"),
    (STATUS_MISSING, "Tree not located"),
]

CONFLICT_OPEN = "open"
CONFLICT_RENUMBER = "renumber"          # same tree, new number
CONFLICT_DISTINCT = "distinct"          # genuinely different individuals
CONFLICT_CHOICES = [
    (CONFLICT_OPEN, "Unverified — excluded from estimates"),
    (CONFLICT_RENUMBER, "Verified renumber (same individual)"),
    (CONFLICT_DISTINCT, "Verified distinct individuals"),
]

EQUATION_DRAFT = "draft"
EQUATION_CONFIRMED = "confirmed"
EQUATION_RETIRED = "retired"
EQUATION_STATUS_CHOICES = [
    (EQUATION_DRAFT, "Draft (not usable for estimates)"),
    (EQUATION_CONFIRMED, "Confirmed (locked when used)"),
    (EQUATION_RETIRED, "Retired"),
]

# ---------------------------------------------------------------- batch sync
# A field tablet uploads ONE survey as several batches (offline work,
# flaky network). Each batch carries a client idempotency key and moves
# through staged -> validated -> applied / blocked / failed.
SURVEY_COLLECTING = "collecting"
SURVEY_COMPLETE = "complete"
SURVEY_STATUS_CHOICES = [
    (SURVEY_COLLECTING, "Collecting batches"),
    (SURVEY_COMPLETE, "Complete — all batches accounted for"),
]

BATCH_STAGED = "staged"          # rows received, validation pending/redoable
BATCH_VALIDATED = "validated"    # all rows passed; apply is retryable
BATCH_APPLIED = "applied"        # atomically written to measurements
BATCH_BLOCKED = "blocked"        # overlaps applied data; manual handling
BATCH_FAILED = "failed"          # row validation failed; resubmit new key
BATCH_STATUS_CHOICES = [
    (BATCH_STAGED, "Staged — received, not yet validated"),
    (BATCH_VALIDATED, "Validated — ready to (re)apply"),
    (BATCH_APPLIED, "Applied — measurements written atomically"),
    (BATCH_BLOCKED, "Blocked — conflicting tree records, manual review"),
    (BATCH_FAILED, "Failed — row validation errors, not applied"),
]

# Per-row audit states (MeasurementImportRow.row_state).
ROW_STAGED = "staged"
ROW_VALIDATED = "validated"
ROW_APPLIED = "applied"
ROW_REJECTED = "rejected"        # failed validation, never applied
ROW_DUPLICATE = "duplicate"      # identical record already applied
ROW_CONFLICTED = "conflicted"    # overlaps applied data, open BatchConflict
ROW_DROPPED = "dropped"          # human kept the earlier record
ROW_STATE_CHOICES = [
    (ROW_STAGED, "Staged"),
    (ROW_VALIDATED, "Validated"),
    (ROW_APPLIED, "Applied"),
    (ROW_REJECTED, "Rejected"),
    (ROW_DUPLICATE, "Duplicate — identical record already applied"),
    (ROW_CONFLICTED, "Conflicted — awaiting manual review"),
    (ROW_DROPPED, "Dropped — earlier record kept by reviewer"),
]

BATCH_CONFLICT_OPEN = "open"
BATCH_CONFLICT_KEEP = "keep_existing"      # earlier applied record wins
BATCH_CONFLICT_ACCEPT = "accept_incoming"  # verified correction, overwrite
BATCH_CONFLICT_AS_NEW = "accept_as_new"    # distinct individual, same label
BATCH_CONFLICT_CHOICES = [
    (BATCH_CONFLICT_OPEN, "Open — earlier data untouched"),
    (BATCH_CONFLICT_KEEP, "Keep existing — incoming row dropped"),
    (BATCH_CONFLICT_ACCEPT, "Accept incoming — verified correction"),
    (BATCH_CONFLICT_AS_NEW, "Accept as new individual sharing the label"),
]

VERSION_DRAFT = "draft"
VERSION_CONFIRMED = "confirmed"
VERSION_SUPERSEDED = "superseded"
VERSION_STATUS_CHOICES = [
    (VERSION_DRAFT, "Draft — recomputes with current data/equations"),
    (VERSION_CONFIRMED, "Confirmed — frozen, immutable"),
    (VERSION_SUPERSEDED, "Superseded by a newer confirmed version"),
]


class Stratum(models.Model):
    """Sampling stratum with known land area (the sampling frame)."""

    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=120)
    area_ha = models.FloatField(
        help_text="Known stratum land area in hectares (sampling frame)."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} ({self.name})"


class Plot(models.Model):
    """A permanent sample plot. Area is per plot, not assumed constant."""

    code = models.CharField(max_length=16, unique=True)
    stratum = models.ForeignKey(
        Stratum, on_delete=models.PROTECT, related_name="plots"
    )
    # Projected coordinates of plot centre, metres.
    x_m = models.FloatField(help_text="Plot centre X, projected CRS [m].")
    y_m = models.FloatField(help_text="Plot centre Y, projected CRS [m].")
    declared_area_ha = models.FloatField(
        help_text="Declared plot area in hectares. Plots may differ in size."
    )
    # GeoJSON-like ring: [[x, y], ...] in projected metres.
    boundary = models.JSONField(
        help_text="Boundary ring [[x_m, y_m], ...] in projected CRS."
    )
    area_polygon_ha = models.FloatField(
        help_text="Polygon area in ha, computed at ingest and cross-checked."
    )

    class Meta:
        ordering = ["code"]

    def __str__(self):
        return self.code


class Species(models.Model):
    code = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=160)
    family = models.CharField(max_length=120, blank=True)

    class Meta:
        verbose_name_plural = "species"
        ordering = ["code"]

    def __str__(self):
        return f"{self.code} — {self.name}"


class AllometricEquation(models.Model):
    """
    Versioned allometric biomass equation.

        agb_kg = a * (dbh_cm ** b) * (height_m ** c)

    Applicability is explicit (species list, dbh range, source/citation).
    Residual error is stored as a dimensionless multiplicative sigma
    (agb * exp(eps), eps ~ N(0, residual_sigma^2)) and propagated into
    equation-error components of uncertainty.
    """

    code = models.CharField(max_length=32)
    version = models.CharField(max_length=16)
    species = models.ManyToManyField(Species, related_name="equations")
    status = models.CharField(
        max_length=16, choices=EQUATION_STATUS_CHOICES, default=EQUATION_DRAFT
    )
    form = models.CharField(
        max_length=64,
        default="agb = a * dbh_cm^b * height_m^c",
        help_text="Human-readable equation form.",
    )
    a = models.FloatField()
    b = models.FloatField()
    c = models.FloatField()
    dbh_min_cm = models.FloatField()
    dbh_max_cm = models.FloatField()
    height_required = models.BooleanField(default=True)
    residual_sigma = models.FloatField(
        help_text="Multiplicative residual SD of ln(agb) [dimensionless]."
    )
    citation = models.CharField(max_length=240)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["code", "version"]
        unique_together = [("code", "version")]

    @property
    def coefficient_checksum_source(self):
        return (
            f"{self.code}|{self.version}|{self.a:.10g}|{self.b:.10g}|"
            f"{self.c:.10g}|{self.dbh_min_cm:.6g}|{self.dbh_max_cm:.6g}|"
            f"{self.residual_sigma:.6g}"
        )

    _FROZEN_FIELDS = ("code", "version", "a", "b", "c", "dbh_min_cm",
                      "dbh_max_cm", "residual_sigma", "form",
                      "height_required")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == EQUATION_CONFIRMED:
                changed = [
                    f for f in self._FROZEN_FIELDS
                    if getattr(original, f) != getattr(self, f)
                ]
                if changed:
                    raise PermissionError(
                        f"Equation {self.code} v{self.version} is locked by a "
                        f"confirmed estimate; changed fields {changed}. Issue "
                        "a new equation code/version instead."
                    )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.code} v{self.version} [{self.status}]"


class Campaign(models.Model):
    """One measurement occasion (survey edition)."""

    code = models.CharField(max_length=16, unique=True)
    measured_on = models.DateField()
    description = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["measured_on"]

    def __str__(self):
        return f"{self.code} ({self.measured_on})"


class Tree(models.Model):
    """
    A tracked individual. The field number is a label, not a primary key:
    renumbers keep the same Tree row (current_field_number changes), while
    a same-number/different-location finding becomes a second Tree row plus
    an IdentityConflict until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT, related_name="trees")
    current_field_number = models.CharField(max_length=16)
    species = models.ForeignKey(
        Species, on_delete=models.PROTECT, related_name="trees"
    )
    first_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="trees_first"
    )
    superseded_tree = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="successors",
        help_text="Set when this row re-numbers an earlier tree (verified).",
    )

    class Meta:
        # Field numbers are LABELS, not identities: a renumber keeps one
        # Tree row, a same-number/different-position finding creates two
        # Tree rows carrying the same label in one plot. The authoritative
        # label per occasion is TreeMeasurement.field_number_seen.
        indexes = [
            models.Index(fields=["plot", "current_field_number"]),
        ]
        ordering = ["plot__code", "current_field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.current_field_number}"


class TreeMeasurement(models.Model):
    """One tree measured (or sought and found dead/missing) at one campaign."""

    tree = models.ForeignKey(
        Tree, on_delete=models.PROTECT, related_name="measurements"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="measurements"
    )
    field_number_seen = models.CharField(
        max_length=16,
        help_text="Number physically on the tag at this campaign.",
    )
    x_m = models.FloatField(help_text="Stem position X, projected CRS [m].")
    y_m = models.FloatField(help_text="Stem position Y, projected CRS [m].")
    status = models.CharField(max_length=20, choices=TREE_STATUS_CHOICES)

    # Raw entry keeps the original unit; canonical *_cm / *_m columns are
    # converted at ingest. Both are retained so a unit error is auditable.
    dbh_raw = models.FloatField(null=True, blank=True)
    dbh_unit = models.CharField(max_length=2, null=True, blank=True,
                                choices=[(u, u) for u in DBH_UNITS])
    dbh_cm = models.FloatField(null=True, blank=True)

    height_raw = models.FloatField(null=True, blank=True)
    height_unit = models.CharField(max_length=2, null=True, blank=True,
                                   choices=[(u, u) for u in HEIGHT_UNITS])
    height_m = models.FloatField(null=True, blank=True)

    notes = models.CharField(max_length=240, blank=True)

    class Meta:
        unique_together = [("tree", "campaign")]
        ordering = ["tree__plot__code", "tree__current_field_number"]

    def __str__(self):
        return f"{self.tree} @ {self.campaign.code}: {self.status}"


class IdentityConflict(models.Model):
    """
    Same field number at both campaigns, but positions contradict the
    hypothesis "same individual". Never auto-merged: stays open (and the
    trees are excluded from survivor growth) until verified.
    """

    plot = models.ForeignKey(Plot, on_delete=models.PROTECT)
    field_number = models.CharField(max_length=16)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="conflicts_t2"
    )
    t1_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t1"
    )
    t2_measurement = models.ForeignKey(
        TreeMeasurement, on_delete=models.PROTECT, related_name="conflicts_as_t2"
    )
    distance_m = models.FloatField(
        help_text="Distance between the two reported positions [m]."
    )
    status = models.CharField(
        max_length=10, choices=CONFLICT_CHOICES, default=CONFLICT_OPEN
    )
    resolution_note = models.CharField(max_length=240, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["plot__code", "field_number"]

    def __str__(self):
        return f"{self.plot.code}/{self.field_number}: {self.status}"


class Survey(models.Model):
    """
    One field survey of a campaign, uploaded as several batches.

    A survey is ``collecting`` while tablets are still sending batches and
    becomes ``complete`` either explicitly (coordinator finalises) or when
    the client-declared ``expected_batches`` have all applied. Derived
    products (identity scan, and through it mortality/ingrowth) are only
    computed once the surveys of BOTH adjacent campaigns are complete —
    a 2024 batch arriving before any 2019 data must not fabricate deaths
    or ingrowth.
    """

    code = models.CharField(
        max_length=64, unique=True,
        help_text="Client-side survey identifier shared by its batches.",
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="surveys"
    )
    expected_batches = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Client-declared number of batches; reaching this many "
                  "applied batches completes the survey automatically.",
    )
    status = models.CharField(
        max_length=10, choices=SURVEY_STATUS_CHOICES,
        default=SURVEY_COLLECTING,
    )
    finalized_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["campaign__measured_on", "code"]

    def __str__(self):
        return f"{self.code} [{self.status}]"


class SurveyBatch(models.Model):
    """
    One uploaded batch of field rows with a client idempotency key.

    * ``idempotency_key`` is unique: a retransmission with the SAME key and
      the SAME content digest returns the stored receipt without any write;
      the same key with a DIFFERENT digest is rejected.
    * ``receipt`` stores the per-row outcome so a replayed submission can
      be answered byte-identically.
    * The batch only becomes ``applied`` when every row passed validation
      and the measurements + identity scan were written in ONE atomic
      transaction; an interrupted apply leaves the batch ``validated``
      (retryable) with no partial measurements.
    """

    survey = models.ForeignKey(
        Survey, on_delete=models.PROTECT, related_name="batches"
    )
    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="batches"
    )
    idempotency_key = models.CharField(max_length=80, unique=True)
    content_digest = models.CharField(
        max_length=64,
        help_text="SHA-256 over the canonical row payload.",
    )
    status = models.CharField(
        max_length=10, choices=BATCH_STATUS_CHOICES, default=BATCH_STAGED
    )
    row_count = models.PositiveIntegerField(default=0)
    client_meta = models.JSONField(
        default=dict, blank=True,
        help_text="Optional device/crew metadata supplied by the tablet.",
    )
    receipt = models.JSONField(
        null=True, blank=True,
        help_text="Stored per-row receipt returned for replays.",
    )
    error = models.CharField(
        max_length=300, blank=True,
        help_text="Last failure/interruption note (state stays retryable).",
    )
    attempt_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"batch {self.idempotency_key} [{self.status}]"


class MeasurementImportRow(models.Model):
    """
    Audit trail for raw imported rows, including rejected ones.

    Rows of a batch are written at STAGING time (before any measurement
    exists) and transition staged -> validated -> applied / rejected /
    duplicate / conflicted / dropped, so the audit is queryable before and
    after the batch is applied. Legacy direct imports (batch=NULL) are
    recorded as applied/rejected immediately.
    """

    campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="import_rows"
    )
    plot = models.ForeignKey(
        Plot, on_delete=models.PROTECT, related_name="import_rows"
    )
    raw_payload = models.JSONField()
    accepted = models.BooleanField()
    rejection_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    batch = models.ForeignKey(
        SurveyBatch, on_delete=models.CASCADE,
        null=True, blank=True, related_name="audit_rows",
    )
    seq = models.PositiveIntegerField(
        null=True, blank=True,
        help_text="Row index within the submitting batch.",
    )
    row_state = models.CharField(
        max_length=12, choices=ROW_STATE_CHOICES, default=ROW_APPLIED
    )
    canonical = models.JSONField(
        null=True, blank=True,
        help_text="Canonical (unit-converted) content used for duplicate "
                  "and conflict detection.",
    )
    measurement = models.ForeignKey(
        "TreeMeasurement", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="import_rows",
        help_text="Measurement written when this row was applied.",
    )

    class Meta:
        ordering = ["id"]


class BatchConflict(models.Model):
    """
    A batch row that contradicts an already-APPLIED measurement of the
    same campaign / plot / field number (or same tracked tree).

    The earlier applied data is NEVER overwritten automatically: the
    batch is blocked until a human picks keep_existing / accept_incoming
    / accept_as_new, after which the batch can be re-applied.
    """

    batch = models.ForeignKey(
        SurveyBatch, on_delete=models.CASCADE, related_name="conflicts"
    )
    audit_row = models.ForeignKey(
        MeasurementImportRow, on_delete=models.PROTECT,
        related_name="batch_conflicts",
    )
    existing_measurement = models.ForeignKey(
        "TreeMeasurement", on_delete=models.PROTECT,
        related_name="batch_conflicts",
    )
    existing_batch = models.ForeignKey(
        SurveyBatch, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="conflicts_against",
        help_text="Batch that originally applied the existing record.",
    )
    detail = models.JSONField(
        default=dict,
        help_text="Canonical incoming vs existing content for review.",
    )
    status = models.CharField(
        max_length=16, choices=BATCH_CONFLICT_CHOICES,
        default=BATCH_CONFLICT_OPEN,
    )
    resolution_note = models.CharField(max_length=240, blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("batch", "audit_row")]
        ordering = ["batch_id", "id"]

    def __str__(self):
        return (f"batch#{self.batch_id} row {self.audit_row_id}: "
                f"{self.status}")


class SurveyScan(models.Model):
    """
    Record of an identity-conflict scan between two adjacent campaigns.

    Written at most once per campaign pair per new applied data: the scan
    runs when both sides' survey data is complete, and re-runs only if
    newer batches were applied since the last scan. Identity conflicts
    themselves are upserted, so a retry never creates doubles.
    """

    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="scans_as_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="scans_as_t2"
    )
    scanned_at = models.DateTimeField()
    n_conflicts = models.PositiveIntegerField(default=0)
    trigger = models.CharField(
        max_length=64, blank=True,
        help_text="Survey code (or caller) that triggered this scan.",
    )

    class Meta:
        unique_together = [("t1_campaign", "t2_campaign")]
        ordering = ["t1_campaign__measured_on"]

    def __str__(self):
        return (f"scan {self.t1_campaign.code}↔{self.t2_campaign.code} "
                f"@ {self.scanned_at:%Y-%m-%d %H:%M}")


class EstimateVersion(models.Model):
    """
    An immutable edition of the population estimate.

    On confirmation:
      * status -> confirmed (edits to the row are blocked in save());
      * equations referenced get locked;
      * result_payload + equation_checksum are frozen.
    A later new allometric equation creates a NEW version; the confirmed
    one can never be silently changed.
    """

    label = models.CharField(max_length=120)
    t1_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t1"
    )
    t2_campaign = models.ForeignKey(
        Campaign, on_delete=models.PROTECT, related_name="estimate_t2"
    )
    equations = models.ManyToManyField(AllometricEquation, related_name="estimates")
    status = models.CharField(
        max_length=12, choices=VERSION_STATUS_CHOICES, default=VERSION_DRAFT
    )
    design_snapshot = models.JSONField(
        help_text="Stratum areas, expansion design, thresholds, CRS, "
                  "uncertainty assumptions used for this run."
    )
    result_payload = models.JSONField(
        null=True, blank=True,
        help_text="Full component breakdown + provenance + uncertainty."
    )
    equation_checksum = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.get(pk=self.pk)
            if original.status == VERSION_CONFIRMED:
                raise PermissionError(
                    "EstimateVersion is confirmed/frozen; create a new "
                    "version instead of mutating this one."
                )
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.label} [{self.status}]"
