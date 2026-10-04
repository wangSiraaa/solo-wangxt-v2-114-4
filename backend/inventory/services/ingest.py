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

The row-level primitives (``check_row`` / ``find_target_tree`` /
``apply_row``) are shared with the resumable survey-batch pipeline in
``inventory.services.batches`` so both paths enforce identical rules.
"""
from django.core.exceptions import ValidationError as DjValidationError
from django.db import transaction

from inventory.models import (
    MeasurementImportRow,
    Plot,
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


def check_row(raw):
    """
    Validate one raw row and convert units.

    Returns (reason, plot, species, dbh_cm, height_m); ``reason`` is None
    when the row is acceptable. Nothing is written to the database.
    """
    reason = _validate_row(raw)
    plot = Plot.objects.filter(code=raw["plot"]).first()
    species = Species.objects.filter(code=raw["species"]).first()
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
    return reason, plot, species, dbh_cm, height_m


def find_target_tree(campaign, plot, raw):
    """
    Resolve which Tree a valid row refers to, WITHOUT writing anything.

    Returns (tree_or_None, superseded_or_None):
      * ``superseded`` set  -> field-book verified renumber of that tree;
      * ``tree`` set        -> reuse that existing tree row;
      * both None           -> a genuinely new individual (caller creates).
    """
    renumber_of = raw.get("verified_renumber_of_tree")
    if renumber_of is not None:
        superseded = Tree.objects.filter(pk=renumber_of, plot=plot).first()
        # None signals a dangling renumber link; the caller rejects.
        return (superseded, superseded) if superseded else (None, None)

    for cand in Tree.objects.filter(
        plot=plot, current_field_number=raw["field_number"]
    ):
        # nearest earlier record of this label (same campaign on a
        # duplicate upload, or a previous occasion)
        prior = cand.measurements.order_by("-campaign__measured_on").first()
        if prior is None:
            return cand, None
        dist = ((prior.x_m - raw["x_m"]) ** 2
                + (prior.y_m - raw["y_m"]) ** 2) ** 0.5
        if dist <= 1.0:
            return cand, None
    # New label with no same-label history and no field-book renumber
    # link: a genuinely new individual. A new label near an old position
    # is NOT auto-attached — it is caught as a "possible_renumber"
    # conflict for human verification.
    return None, None


def apply_row(campaign, raw, plot, species, dbh_cm, height_m,
              source_batch=None):
    """
    Write one previously validated row: Tree (if new) + TreeMeasurement.

    Returns (tree, measurement, renumber_flag). Raises ValueError on a
    dangling verified-renumber link.
    """
    tree, superseded = find_target_tree(campaign, plot, raw)
    if raw.get("verified_renumber_of_tree") is not None and superseded is None:
        raise ValueError(
            f"verified_renumber_of_tree="
            f"{raw['verified_renumber_of_tree']} not found "
            f"in plot {plot.code}")

    if superseded is not None:
        tree = superseded
        tree.current_field_number = raw["field_number"]
        tree.save(update_fields=["current_field_number"])
    elif tree is None:
        tree = Tree.objects.create(
            plot=plot, species=species,
            current_field_number=raw["field_number"],
            first_campaign=campaign,
        )

    measurement, _ = TreeMeasurement.objects.update_or_create(
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
            "source_batch": source_batch,
        },
    )
    return tree, measurement, superseded is not None


@transaction.atomic
def import_campaign_rows(campaign, rows, area_tolerance):
    accepted, rejected = [], []
    for raw in rows:
        reason, plot, species, dbh_cm, height_m = check_row(raw)

        row_obj = MeasurementImportRow.objects.create(
            campaign=campaign, plot=plot or Plot.objects.first(),
            raw_payload=raw, accepted=reason is None,
            rejection_reason=reason or "",
        )
        if reason is not None:
            rejected.append({"row": raw, "reason": reason, "audit_id": row_obj.id})
            continue

        try:
            tree, _m, renumbered = apply_row(
                campaign, raw, plot, species, dbh_cm, height_m)
        except ValueError as exc:
            row_obj.accepted = False
            row_obj.rejection_reason = str(exc)
            row_obj.save()
            rejected.append({"row": raw, "reason": row_obj.rejection_reason,
                             "audit_id": row_obj.id})
            continue
        accepted.append({"tree_id": tree.id, "field_number": raw["field_number"],
                         "plot": plot.code, "renumber": renumbered})

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
