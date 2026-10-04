"""
Ingest pipeline for a campaign's field rows.

Responsibilities
================
* reject rows with missing/wrong dbh or height units;
* convert to canonical cm / m while retaining the raw value & unit;
* cross-check declared plot area against the boundary polygon;
* require the stem point to fall inside the plot boundary;
* create Tree + TreeMeasurement, honouring a *field-book verified*
  renumber link only (never an automatic same-number merge);
* audit every row in MeasurementImportRow.

The row-level primitives (``validate_field_row``, ``resolve_target_tree``,
``write_measurement``, ``canonical_content``) are shared with the
resumable batch-sync pipeline in ``inventory.services.batches``; the
legacy direct import below keeps its original per-row semantics.
"""
from django.core.exceptions import ValidationError as DjValidationError
from django.db import transaction

from inventory.models import (
    MeasurementImportRow,
    Plot,
    ROW_APPLIED,
    ROW_REJECTED,
    Species,
    STATUS_ALIVE_MEASURED,
    STATUS_DEAD,
    STATUS_MISSING,
    Tree,
    TreeMeasurement,
)
from inventory.services.units import (
    convert_dbh_to_cm,
    convert_height_to_m,
    point_in_ring,
    ring_area_ha,
)


# ------------------------------------------------------------- row primitives
def validate_field_row(raw, plot, species):
    """
    Validate one raw field row against plot/species reference data.

    Returns ``(reason, dbh_cm, height_m)``; ``reason`` is None when the row
    is acceptable. Unit conversion happens here so the canonical values are
    available for both writing and duplicate/conflict comparison.
    """
    reason = _validate_row(raw)
    if plot is None:
        reason = reason or f"unknown plot {raw['plot']!r}"
    if species is None:
        reason = reason or f"unknown species {raw['species']!r}"

    dbh_cm = height_m = None
    if reason is None:
        try:
            if raw["status"] == STATUS_ALIVE_MEASURED:
                dbh_cm = convert_dbh_to_cm(raw.get("dbh_raw"),
                                           raw.get("dbh_unit"))
                if raw.get("height_raw") is not None:
                    height_m = convert_height_to_m(raw["height_raw"],
                                                   raw.get("height_unit"))
                else:
                    reason = ("alive_measured row requires "
                              "height_raw/height_unit")
            else:
                # dead / not-measured / missing: dbh optional but if
                # given the unit is still mandatory and conversion is
                # still checked.
                if raw.get("dbh_raw") is not None:
                    dbh_cm = convert_dbh_to_cm(raw["dbh_raw"],
                                               raw.get("dbh_unit"))
        except DjValidationError as exc:
            reason = "; ".join(exc.messages)

    if reason is None and plot is not None:
        if not point_in_ring(raw["x_m"], raw["y_m"], plot.boundary):
            reason = ("stem coordinates outside plot boundary "
                      "(unit/CRS mix-up or wrong plot?)")
    return reason, dbh_cm, height_m


def canonical_content(raw, dbh_cm, height_m):
    """
    Canonical, comparable content of a field row (units converted).

    Used to decide whether a re-delivered record is an exact duplicate of
    an applied one or a genuine contradiction. Values are rounded so the
    comparison is insensitive to float serialisation noise.
    """

    def r4(v):
        return None if v is None else round(float(v), 4)

    return {
        "plot": raw["plot"],
        "field_number": raw["field_number"],
        "species": raw["species"],
        "x_m": r4(raw["x_m"]),
        "y_m": r4(raw["y_m"]),
        "status": raw["status"],
        "dbh_raw": r4(raw.get("dbh_raw")),
        "dbh_unit": raw.get("dbh_unit"),
        "dbh_cm": r4(dbh_cm),
        "height_raw": r4(raw.get("height_raw")),
        "height_unit": raw.get("height_unit"),
        "height_m": r4(height_m),
        "notes": raw.get("notes", "") or "",
    }


def canonical_of_measurement(measurement):
    """Canonical content of an already-applied TreeMeasurement."""
    m = measurement
    return {
        "plot": m.tree.plot.code,
        "field_number": m.field_number_seen,
        "species": m.tree.species.code,
        "x_m": round(m.x_m, 4),
        "y_m": round(m.y_m, 4),
        "status": m.status,
        "dbh_raw": None if m.dbh_raw is None else round(m.dbh_raw, 4),
        "dbh_unit": m.dbh_unit,
        "dbh_cm": None if m.dbh_cm is None else round(m.dbh_cm, 4),
        "height_raw": None if m.height_raw is None else round(m.height_raw, 4),
        "height_unit": m.height_unit,
        "height_m": None if m.height_m is None else round(m.height_m, 4),
        "notes": m.notes or "",
    }


def resolve_target_tree(campaign, plot, raw):
    """
    Decide which Tree a field row belongs to.

    Returns ``(tree, superseded, error)``:
      * ``tree`` set, ``superseded`` set  -> verified renumber: relabel and
        reuse that tree row;
      * ``tree`` set, ``superseded`` None -> reuse this existing tree row;
      * both None                         -> create a new individual;
      * ``error`` set                     -> the row's renumber link is bad.
    A new label near an old position is NEVER auto-attached; it becomes a
    new tree and the identity scan flags "possible_renumber".
    """
    renumber_of = raw.get("verified_renumber_of_tree")
    if renumber_of is not None:
        superseded = Tree.objects.filter(pk=renumber_of, plot=plot).first()
        if superseded is None:
            return None, None, (
                f"verified_renumber_of_tree={renumber_of} not found "
                f"in plot {plot.code}")
        return superseded, superseded, None

    for cand in Tree.objects.filter(
        plot=plot, current_field_number=raw["field_number"]
    ):
        # nearest earlier record of this label (same campaign on a
        # duplicate upload, or a previous occasion)
        prior = cand.measurements.order_by("-campaign__measured_on").first()
        if prior is None:
            return cand, None, None
        dist = ((prior.x_m - raw["x_m"]) ** 2
                + (prior.y_m - raw["y_m"]) ** 2) ** 0.5
        if dist <= 1.0:
            return cand, None, None
    return None, None, None


def write_measurement(tree, campaign, raw, dbh_cm, height_m):
    """Idempotent per (tree, campaign) write of one measurement."""
    measurement, _created = TreeMeasurement.objects.update_or_create(
        tree=tree, campaign=campaign,
        defaults={
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
        },
    )
    return measurement


# ------------------------------------------------------------- legacy import
@transaction.atomic
def import_campaign_rows(campaign, rows, area_tolerance):
    accepted, rejected = [], []
    for raw in rows:
        plot = Plot.objects.filter(code=raw["plot"]).first()
        species = Species.objects.filter(code=raw["species"]).first()
        reason, dbh_cm, height_m = validate_field_row(raw, plot, species)

        row_obj = MeasurementImportRow.objects.create(
            campaign=campaign, plot=plot or Plot.objects.first(),
            raw_payload=raw, accepted=reason is None,
            rejection_reason=reason or "",
            row_state=ROW_APPLIED if reason is None else ROW_REJECTED,
            canonical=(canonical_content(raw, dbh_cm, height_m)
                       if reason is None else None),
        )
        if reason is not None:
            rejected.append({"row": raw, "reason": reason, "audit_id": row_obj.id})
            continue

        tree, superseded, err = resolve_target_tree(campaign, plot, raw)
        if err is not None:
            row_obj.accepted = False
            row_obj.rejection_reason = err
            row_obj.row_state = ROW_REJECTED
            row_obj.save()
            rejected.append({"row": raw, "reason": err,
                             "audit_id": row_obj.id})
            continue

        if tree is None:
            # New label with no same-label history and no field-book
            # renumber link: a genuinely new individual. A new label
            # near an old position is NOT auto-attached — it is caught
            # as a "possible_renumber" conflict for human verification.
            tree = Tree.objects.create(
                plot=plot, species=species,
                current_field_number=raw["field_number"],
                first_campaign=campaign,
            )
        elif superseded is not None:
            tree.current_field_number = raw["field_number"]
            tree.save(update_fields=["current_field_number"])

        measurement = write_measurement(tree, campaign, raw, dbh_cm, height_m)
        row_obj.measurement = measurement
        row_obj.save(update_fields=["measurement"])
        accepted.append({"tree_id": tree.id, "field_number": raw["field_number"],
                         "plot": plot.code, "renumber": superseded is not None})

    return {
        "campaign": campaign.code,
        "n_rows": len(rows),
        "n_accepted": len(accepted),
        "n_rejected": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }


def _validate_row(raw):
    if raw["status"] == STATUS_ALIVE_MEASURED and not raw.get("dbh_raw"):
        return "alive_measured row must carry dbh_raw with explicit unit"
    if raw.get("dbh_raw") is not None and not raw.get("dbh_unit"):
        return "dbh_raw given without dbh_unit — unit declaration mandatory"
    if raw.get("height_raw") is not None and not raw.get("height_unit"):
        return "height_raw given without height_unit"
    if raw["status"] in (STATUS_MISSING, STATUS_DEAD) and raw.get("height_raw"):
        return f"{raw['status']} row must not carry a height measurement"
    return None


def verify_plot_area(plot, tolerance):
    """Raise unless polygon area matches declared area within tolerance."""
    poly_ha = ring_area_ha(plot.boundary)
    if plot.declared_area_ha <= 0:
        raise DjValidationError("declared_area_ha must be positive")
    rel = abs(poly_ha - plot.declared_area_ha) / plot.declared_area_ha
    if rel > tolerance:
        raise DjValidationError(
            f"plot {plot.code}: polygon area {poly_ha:.4f} ha disagrees with "
            f"declared {plot.declared_area_ha:.4f} ha by {rel:.2%} "
            f"(tolerance {tolerance:.2%}). Check coordinates/CRS/units."
        )
    return poly_ha
