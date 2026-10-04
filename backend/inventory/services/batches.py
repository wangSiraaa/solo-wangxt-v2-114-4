"""
Resumable survey-batch synchronisation.

Field tablets work offline and upload one survey in several batches; batches
may arrive out of order (a 2024 batch before a 2019 one) or be re-sent when
the network flaps. This module makes that safe:

* **Idempotency** — every batch carries the tablet's ``client_key`` and a
  SHA-256 ``content_hash`` of its rows. Same key + same content returns the
  stored receipt and writes nothing. Same key + different content is
  rejected (the key no longer identifies this payload).
* **All-or-nothing apply** — a batch is validated as a whole; measurements
  and audit rows are written in ONE transaction. An interrupted apply rolls
  back completely, so a ``staged``/``validated`` batch is always safe to
  resume and never leaves partial measurements.
* **First-arrived data wins** — a batch whose rows overlap tree records
  already applied for the same campaign is *blocked* for human review. A
  later batch NEVER overwrites earlier measurements; the reviewer may only
  discard the later batch.
* **Adjacent-survey gating** — the identity-conflict scan runs only once
  both neighbouring campaigns hold measurements, and the estimator defers
  mortality/ingrowth for plots whose adjacent occasion has not arrived
  (see ``inventory.services.estimator``).
"""
import hashlib
import json

from django.db import transaction
from django.utils import timezone

from inventory.models import (
    BATCH_APPLIED,
    BATCH_BLOCKED,
    BATCH_DISCARDED,
    BATCH_FAILED,
    BATCH_STAGED,
    BATCH_VALIDATED,
    Campaign,
    ImportBatch,
    MeasurementImportRow,
    Plot,
    Species,
    Tree,
    TreeMeasurement,
)
from inventory.services.conflicts import scan_conflicts
from inventory.services.ingest import apply_row, check_row, find_target_tree


class BatchKeyReuseError(Exception):
    """Same client_key re-used with a DIFFERENT payload."""


class BatchStateError(Exception):
    """The requested transition is not allowed from the batch's state."""


# ------------------------------------------------------------------ digest
def _canon(obj):
    """Canonicalise so 10 and 10.0 (JSON int/float) hash identically."""
    if isinstance(obj, float) and obj.is_integer():
        return int(obj)
    if isinstance(obj, dict):
        return {k: _canon(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_canon(v) for v in obj]
    return obj


def content_digest(rows):
    """Stable SHA-256 over the canonicalised row payload."""
    payload = json.dumps(_canon(rows), sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ submit
def submit_batch(campaign, client_key, rows, area_tolerance):
    """
    Register a batch and drive it to a terminal-ish state.

    Returns (batch, replayed). ``replayed`` is True when the same key and
    content were seen before: the stored receipt is returned and NOTHING is
    written again.
    """
    digest = content_digest(rows)
    existing = ImportBatch.objects.filter(
        campaign=campaign, client_key=client_key).first()
    if existing is not None:
        if existing.content_hash != digest:
            raise BatchKeyReuseError(
                f"client_key {client_key!r} was already used for a "
                f"different payload (content "
                f"{existing.content_hash[:12]}… vs {digest[:12]}…). "
                "Choose a new key for corrected data; the original batch "
                "is untouched.")
        if existing.status in (BATCH_STAGED, BATCH_VALIDATED):
            # The previous attempt was interrupted before finishing (the
            # tablet never saw a receipt and re-sent). Resume it — the
            # apply is transactional, so this cannot double-write.
            validate_batch(existing, area_tolerance)
            if existing.status == BATCH_VALIDATED:
                apply_batch(existing, area_tolerance)
            return existing, False
        return existing, True

    batch = ImportBatch.objects.create(
        campaign=campaign, client_key=client_key, content_hash=digest,
        payload=rows, n_rows=len(rows), status=BATCH_STAGED)
    validate_batch(batch, area_tolerance)
    if batch.status == BATCH_VALIDATED:
        apply_batch(batch, area_tolerance)
    return batch, False


# ---------------------------------------------------------------- validate
@transaction.atomic
def validate_batch(batch, area_tolerance):
    """
    staged -> validated | failed.

    Every row is checked (units, plot/species, boundary, renumber link,
    in-batch duplicates). Rejected rows are audited immediately in
    MeasurementImportRow so the pre-apply trail is queryable. Nothing
    touches Tree/TreeMeasurement here.
    """
    if batch.status != BATCH_STAGED:
        return batch

    rows = batch.payload
    receipts = []
    n_rejected = 0
    seen_labels = {}
    for idx, raw in enumerate(rows):
        reason, plot, species, dbh_cm, height_m = check_row(raw)

        label_key = (raw["plot"], raw["field_number"])
        if reason is None and label_key in seen_labels:
            reason = (f"duplicate of row {seen_labels[label_key]} in this "
                      f"batch (same plot/field_number)")
        if reason is None:
            seen_labels[label_key] = idx

        renumber_of = raw.get("verified_renumber_of_tree")
        if reason is None and renumber_of is not None and not Tree.objects.filter(
                pk=renumber_of, plot=plot).exists():
            reason = (f"verified_renumber_of_tree={renumber_of} not found "
                      f"in plot {plot.code}")

        entry = {
            "index": idx,
            "plot": raw["plot"],
            "field_number": raw["field_number"],
            "result": "accepted" if reason is None else "rejected",
        }
        if reason is None:
            entry.update({"dbh_cm": dbh_cm, "height_m": height_m})
        else:
            n_rejected += 1
            entry["reason"] = reason
            audit = MeasurementImportRow.objects.create(
                campaign=batch.campaign, plot=plot or Plot.objects.first(),
                batch=batch, raw_payload=raw, accepted=False,
                rejection_reason=reason)
            entry["audit_id"] = audit.id
        receipts.append(entry)

    batch.receipt = {
        "campaign": batch.campaign.code,
        "client_key": batch.client_key,
        "content_hash": batch.content_hash,
        "n_rows": len(rows),
        "n_accepted": len(rows) - n_rejected,
        "n_rejected": n_rejected,
        "rows": receipts,
    }
    if n_rejected:
        batch.status = BATCH_FAILED
        batch.error = (f"{n_rejected} of {len(rows)} rows failed validation; "
                       "nothing was applied. Correct the payload and resend "
                       "under a NEW client_key.")
    else:
        batch.status = BATCH_VALIDATED
        batch.error = ""
    batch.save()
    return batch


# ------------------------------------------------------------------- apply
def apply_batch(batch, area_tolerance):
    """
    validated -> applied | blocked. Idempotent and resumable.

    The whole apply is one transaction: either every measurement and its
    audit row exists, or none does. Overlap with already-applied tree
    records blocks the batch for human review instead of overwriting.
    """
    if batch.status == BATCH_APPLIED:
        return batch  # idempotent: stored receipt is the answer
    if batch.status == BATCH_STAGED:
        validate_batch(batch, area_tolerance)
    if batch.status == BATCH_FAILED:
        raise BatchStateError(
            "batch failed validation; correct the payload and resend under "
            "a new client_key.")
    if batch.status == BATCH_BLOCKED:
        raise BatchStateError(
            "batch overlaps already-applied data and awaits human review; "
            "use the resolve endpoint.")
    if batch.status == BATCH_DISCARDED:
        raise BatchStateError("batch was discarded by a reviewer.")

    campaign = batch.campaign
    valid_rows = [r for r in batch.receipt["rows"] if r["result"] == "accepted"]
    raw_by_index = {i: raw for i, raw in enumerate(batch.payload)}

    with transaction.atomic():
        # Serialise concurrent applies of the same campaign: the overlap
        # check below must not race a second batch applying right now.
        Campaign.objects.select_for_update().get(pk=campaign.pk)

        planned, overlaps = [], []
        claimed_trees = {}
        for entry in valid_rows:
            raw = raw_by_index[entry["index"]]
            plot = Plot.objects.get(code=raw["plot"])
            tree, _superseded = find_target_tree(campaign, plot, raw)
            if tree is not None:
                prior = TreeMeasurement.objects.filter(
                    tree=tree, campaign=campaign).first()
                if prior is not None:
                    overlaps.append(_overlap_entry(
                        entry, tree, prior,
                        "tree already has a measurement for this campaign"))
                    continue
                if tree.id in claimed_trees:
                    overlaps.append(_overlap_entry(
                        entry, tree, None,
                        f"row {claimed_trees[tree.id]} in this same batch "
                        "already claims this tree"))
                    continue
                claimed_trees[tree.id] = entry["index"]
            planned.append((entry, raw, plot))

        if overlaps:
            batch.status = BATCH_BLOCKED
            batch.overlaps = overlaps
            batch.error = (
                f"{len(overlaps)} row(s) overlap tree records already "
                "applied for this campaign. First-arrived data is kept; a "
                "human must review this batch.")
            batch.save()
            return batch

        applied_entries = []
        for entry, raw, plot in planned:
            tree, measurement, renumbered = apply_row(
                campaign, raw, plot,
                species=Species.objects.get(code=raw["species"]),
                dbh_cm=entry["dbh_cm"], height_m=entry["height_m"],
                source_batch=batch)
            audit = MeasurementImportRow.objects.create(
                campaign=campaign, plot=plot, batch=batch,
                raw_payload=raw, accepted=True)
            entry.update({
                "tree_id": tree.id,
                "measurement_id": measurement.id,
                "audit_id": audit.id,
                "renumber": renumbered,
            })
            applied_entries.append(entry)

        batch.receipt = {**batch.receipt, "rows": [
            *applied_entries,
            *[r for r in batch.receipt["rows"] if r["result"] != "accepted"],
        ]}
        batch.status = BATCH_APPLIED
        batch.n_applied = len(applied_entries)
        batch.applied_at = timezone.now()
        batch.error = ""
        batch.save()

    # Identity scan is part of applying the batch — but only once the
    # adjacent campaign's data are complete (deferred otherwise).
    batch.receipt = {**batch.receipt,
                     "conflicts": scan_if_adjacent_complete(campaign)}
    batch.save(update_fields=["receipt", "updated_at"])
    return batch


def _overlap_entry(entry, tree, prior_measurement, reason):
    source = None
    if prior_measurement is not None and prior_measurement.source_batch_id:
        source = ImportBatch.objects.filter(
            pk=prior_measurement.source_batch_id
        ).values_list("client_key", flat=True).first()
    return {
        "index": entry["index"],
        "plot": entry["plot"],
        "field_number": entry["field_number"],
        "tree_id": tree.id,
        "existing_measurement_id": (
            prior_measurement.id if prior_measurement else None),
        "existing_batch": source,
        "reason": reason,
    }


def scan_if_adjacent_complete(campaign):
    """
    Run the identity-conflict scan only once BOTH neighbouring campaigns
    hold measurements — an early 2024 batch must not manufacture conflicts
    (or, via the estimator, mortality/ingrowth) against an empty 2019.
    """
    other = Campaign.objects.exclude(pk=campaign.pk).order_by(
        "measured_on").first()
    if other is None:
        return []
    # measured_on may be a str on unsaved instances; ISO strings and
    # dates sort identically, so normalise to str for the comparison.
    t1, t2 = sorted([campaign, other], key=lambda c: str(c.measured_on))
    have_t1 = TreeMeasurement.objects.filter(campaign=t1).exists()
    have_t2 = TreeMeasurement.objects.filter(campaign=t2).exists()
    if not (have_t1 and have_t2):
        return []
    return scan_conflicts(t1, t2)


# ----------------------------------------------------------------- resolve
def resolve_batch(batch, decision, note=""):
    """
    Human handling of a blocked batch. The ONLY decision is ``discard``:
    the later batch is dropped and first-arrived data stays untouched.
    """
    if batch.status != BATCH_BLOCKED:
        raise BatchStateError(
            f"only blocked batches can be resolved (status is "
            f"{batch.status}).")
    if decision != "discard":
        raise BatchStateError(
            "unsupported decision; a later batch may never overwrite "
            "first-arrived data, so only 'discard' is allowed.")
    batch.status = BATCH_DISCARDED
    batch.resolution_note = note
    batch.resolved_at = timezone.now()
    batch.save()
    return batch


# ---------------------------------------------------------------- progress
def campaign_progress():
    """Per-campaign upload progress for the dashboard."""
    out = []
    campaigns = Campaign.objects.all()
    for c in campaigns:
        batches = list(c.import_batches.all())
        by_status = {}
        for b in batches:
            by_status[b.status] = by_status.get(b.status, 0) + 1
        rows_applied = sum(b.n_applied for b in batches)
        rows_total = sum(b.n_rows for b in batches
                         if b.status != BATCH_DISCARDED)
        out.append({
            "campaign": c.code,
            "measured_on": c.measured_on,
            "n_batches": len(batches),
            "by_status": by_status,
            "rows_applied": rows_applied,
            "rows_total": rows_total,
            "n_measurements": TreeMeasurement.objects.filter(
                campaign=c).count(),
            "complete": bool(batches) and all(
                b.status == BATCH_APPLIED for b in batches),
        })
    return out
