"""
Acceptance tests for the resumable survey batch sync.

Covers the station's acceptance checks:
  1. a survey uploaded as several batches merges into one complete import
     with exactly one identity scan;
  2. retransmitting the same idempotency key + payload returns the stored
     receipt and writes nothing again;
  3. the same key with a DIFFERENT payload, and overlapping tree records
     from another batch, are rejected (first writer wins, manual review);
  4. a later survey's batch arriving before the earlier survey must not
     fabricate mortality / ingrowth / identity conflicts;
  5. an interrupted apply leaves a retryable batch; retrying leaves no
     partial measurements and no double conflicts.
"""
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    BATCH_CONFLICT_OPEN,
    BatchConflict,
    Campaign,
    IdentityConflict,
    MeasurementImportRow,
    Plot,
    Species,
    Stratum,
    Survey,
    SurveyBatch,
    SurveyScan,
    Tree,
    TreeMeasurement,
)


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE = "alive_measured", "alive_not_measured", "dead"


def row(plot, num, x, y, status=AM, dbh=20.0, height=15.0, species="OAK",
        **extra):
    r = dict(plot=plot, field_number=num, species=species, x_m=x, y_m=y,
             status=status)
    if dbh is not None:
        r.update(dbh_raw=dbh, dbh_unit="cm")
    if height is not None:
        r.update(height_raw=height, height_unit="m")
    r.update(extra)
    return r


class BatchSyncTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="2019",
                                          measured_on="2019-07-01")
        self.t2 = Campaign.objects.create(code="2024",
                                          measured_on="2024-07-01")
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    # ------------------------------------------------------------- helpers
    def submit(self, survey, key, campaign, rows, expected=None):
        body = {"survey": survey, "idempotency_key": key,
                "campaign": campaign, "rows": rows}
        if expected is not None:
            body["expected_batches"] = expected
        return self.client.post("/api/batches/", body, format="json")

    def apply(self, batch_id):
        return self.client.post(f"/api/batches/{batch_id}/apply/")

    def counts(self):
        return {
            "trees": Tree.objects.count(),
            "measurements": TreeMeasurement.objects.count(),
            "audit": MeasurementImportRow.objects.count(),
            "batches": SurveyBatch.objects.count(),
            "conflicts": IdentityConflict.objects.count(),
            "batch_conflicts": BatchConflict.objects.count(),
            "scans": SurveyScan.objects.count(),
        }

    def upload_two_surveys(self):
        """2019 in two batches, 2024 in two batches; one label clash."""
        r = self.submit("s2019", "k-2019-p1", "2019", [
            row("P1", "001", 5, 5, dbh=20.0, height=15.0),
            row("P1", "008", 5, 15, dbh=27.0, height=18.0),
        ], expected=2)
        assert r.status_code == 201, r.content
        r = self.submit("s2019", "k-2019-p2", "2019", [
            row("P2", "001", 5, 5, dbh=18.0, height=12.0),
        ], expected=2)
        assert r.status_code == 201, r.content
        r = self.submit("s2024", "k-2024-p1", "2024", [
            row("P1", "001", 5, 5, dbh=21.0, height=15.3),
            # same label 008, 20 m away -> identity conflict, new tree
            row("P1", "008", 25, 15, dbh=12.0, height=10.0),
        ], expected=2)
        assert r.status_code == 201, r.content
        r = self.submit("s2024", "k-2024-p2", "2024", [
            row("P2", "001", 5, 5, dbh=19.0, height=12.5),
        ], expected=2)
        assert r.status_code == 201, r.content
        return r

    # --------------------------- 1. multi-batch merge, one complete import
    def test_multi_batch_merge_scans_exactly_once(self):
        last = self.upload_two_surveys()

        # all rows applied through the batch pipeline
        self.assertEqual(TreeMeasurement.objects.filter(
            campaign=self.t1).count(), 3)
        self.assertEqual(TreeMeasurement.objects.filter(
            campaign=self.t2).count(), 3)
        self.assertEqual(Survey.objects.filter(status="complete").count(), 2)

        # exactly ONE scan for the campaign pair, after both surveys
        # completed; the label clash produced exactly one open conflict
        self.assertEqual(SurveyScan.objects.count(), 1)
        scan = SurveyScan.objects.get()
        self.assertEqual((scan.t1_campaign, scan.t2_campaign),
                         (self.t1, self.t2))
        self.assertEqual(scan.n_conflicts, 1)
        self.assertEqual(IdentityConflict.objects.count(), 1)
        conflict = IdentityConflict.objects.get()
        self.assertEqual((conflict.plot, conflict.field_number),
                         (self.p1, "008"))

        # the final batch receipt reports the scan
        receipt = last.json()
        self.assertEqual(receipt["scan"]["status"], "scanned")

        # re-applying any batch is a no-op: no rescan, no double conflict
        batch = SurveyBatch.objects.get(idempotency_key="k-2024-p2")
        again = self.apply(batch.id)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(SurveyScan.objects.count(), 1)
        self.assertEqual(IdentityConflict.objects.count(), 1)

        # audit rows exist for every submitted row, linked to their batch
        self.assertEqual(MeasurementImportRow.objects.filter(
            batch__isnull=False, row_state="applied").count(), 6)

    # -------------------- 2. same key + same payload -> replay, no writes
    def test_same_key_same_payload_replays_receipt(self):
        rows = [row("P1", "001", 5, 5), row("P1", "002", 8, 5, dbh=22.0)]
        first = self.submit("s2019", "key-1", "2019", rows, expected=1)
        self.assertEqual(first.status_code, 201)
        receipt = first.json()
        before = self.counts()

        replay = self.submit("s2019", "key-1", "2019", rows, expected=1)
        self.assertEqual(replay.status_code, 200)
        body = replay.json()
        self.assertTrue(body["replay"])
        self.assertEqual(body["batch_id"], receipt["batch_id"])
        self.assertEqual(body["content_digest"], receipt["content_digest"])
        self.assertEqual(body["rows"], receipt["rows"])
        # nothing written again
        self.assertEqual(self.counts(), before)

    # --------------- 3a. same key + different payload -> rejected, no write
    def test_same_key_different_payload_rejected(self):
        rows = [row("P1", "001", 5, 5)]
        first = self.submit("s2019", "key-2", "2019", rows, expected=1)
        self.assertEqual(first.status_code, 201)
        before = self.counts()

        changed = [row("P1", "001", 5, 5, dbh=99.0)]
        r = self.submit("s2019", "key-2", "2019", changed, expected=1)
        self.assertEqual(r.status_code, 409)
        self.assertIn("DIFFERENT payload", r.json()["detail"])
        self.assertEqual(self.counts(), before)

    # -------------------- 3b. overlapping tree records -> blocked, manual
    def test_overlapping_tree_record_blocked_then_resolved(self):
        r = self.submit("s2024", "b1", "2024",
                        [row("P1", "001", 5, 5, dbh=20.0)], expected=2)
        self.assertEqual(r.status_code, 201)

        # a later batch contradicts the applied record: blocked, the
        # earlier measurement is NOT overwritten.
        r = self.submit("s2024", "b2", "2024",
                        [row("P1", "001", 5, 5, dbh=25.0)], expected=2)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["status"], "blocked")
        m = TreeMeasurement.objects.get(campaign=self.t2)
        self.assertEqual(m.dbh_cm, 20.0)
        self.assertEqual(BatchConflict.objects.count(), 1)
        bc = BatchConflict.objects.get()
        self.assertEqual(bc.status, BATCH_CONFLICT_OPEN)
        self.assertEqual(bc.detail["incoming"]["dbh_cm"], 25.0)
        self.assertEqual(bc.detail["existing"][0]["dbh_cm"], 20.0)

        # batch cannot continue while the conflict is open
        blocked_batch = SurveyBatch.objects.get(idempotency_key="b2")
        r = self.apply(blocked_batch.id)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(TreeMeasurement.objects.count(), 1)

        # human keeps the earlier record -> incoming row dropped
        r = self.client.post(f"/api/batch-conflicts/{bc.id}/resolve/",
                             {"action": "keep_existing",
                              "note": "first tablet is authoritative"},
                             format="json")
        self.assertEqual(r.status_code, 200)
        r = self.apply(blocked_batch.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "applied")
        m.refresh_from_db()
        self.assertEqual(m.dbh_cm, 20.0)  # still the first record
        audit = MeasurementImportRow.objects.get(batch=blocked_batch)
        self.assertEqual(audit.row_state, "dropped")
        self.assertEqual(TreeMeasurement.objects.count(), 1)

    def test_overlap_resolved_accept_incoming_is_audited_manual_override(self):
        self.submit("s2024", "b1", "2024",
                    [row("P1", "001", 5, 5, dbh=20.0)], expected=2)
        r = self.submit("s2024", "b2", "2024",
                        [row("P1", "001", 5, 5, dbh=25.0)], expected=2)
        self.assertEqual(r.status_code, 409)
        bc = BatchConflict.objects.get()
        self.client.post(f"/api/batch-conflicts/{bc.id}/resolve/",
                         {"action": "accept_incoming",
                          "note": "field check: 20.0 was a misread"},
                         format="json")
        blocked_batch = SurveyBatch.objects.get(idempotency_key="b2")
        r = self.apply(blocked_batch.id)
        self.assertEqual(r.status_code, 200)
        m = TreeMeasurement.objects.get(campaign=self.t2)
        self.assertEqual(m.dbh_cm, 25.0)  # manual, audited correction
        self.assertEqual(TreeMeasurement.objects.count(), 1)

    def test_within_batch_contradictory_rows_fail_whole_batch(self):
        r = self.submit("s2024", "b1", "2024", [
            row("P1", "001", 5, 5, dbh=20.0),
            row("P1", "001", 5.2, 5.1, dbh=33.0),  # same stem, other dbh
        ])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["status"], "failed")
        states = sorted(x["state"] for x in r.json()["rows"])
        self.assertEqual(states, ["rejected", "rejected"])
        self.assertEqual(TreeMeasurement.objects.count(), 0)
        # a failed batch cannot be applied
        batch = SurveyBatch.objects.get(idempotency_key="b1")
        self.assertEqual(self.apply(batch.id).status_code, 409)

    def test_cross_batch_exact_duplicate_folds_without_conflict(self):
        rows = [row("P1", "001", 5, 5), row("P1", "002", 8, 5, dbh=22.0)]
        r = self.submit("s2024", "b1", "2024", rows, expected=2)
        self.assertEqual(r.status_code, 201)
        before = self.counts()

        # network-recovery retransmission under a NEW key, same content
        r = self.submit("s2024", "b2", "2024", rows, expected=2)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["n_duplicates"], 2)
        after = self.counts()
        # one extra batch + its audit rows, but no new trees/measurements
        # and crucially no BatchConflict for identical content
        self.assertEqual(after["trees"], before["trees"])
        self.assertEqual(after["measurements"], before["measurements"])
        self.assertEqual(after["batch_conflicts"], 0)

    # --------- 4. later survey first: no premature mortality / ingrowth
    def test_later_survey_first_defers_scan_and_estimate(self):
        # the 2024 survey completes BEFORE any 2019 data exists
        r = self.submit("s2024", "k-2024", "2024", [
            row("P1", "001", 5, 5, dbh=21.0, height=15.3),
            row("P1", "008", 25, 15, dbh=12.0, height=10.0),
        ], expected=1)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["scan"]["status"], "deferred")
        self.assertIn("2019", r.json()["scan"]["waiting_for"])

        # no identity conflicts yet, and no estimate (which derives
        # mortality/ingrowth) may be computed against the missing survey
        self.assertEqual(IdentityConflict.objects.count(), 0)
        self.assertEqual(SurveyScan.objects.count(), 0)
        r = self.client.post("/api/estimates/", {
            "label": "premature", "t1_campaign": "2019",
            "t2_campaign": "2024", "equation_ids": [self.eq.id]},
            format="json")
        self.assertEqual(r.status_code, 409)
        self.assertIn("2019", str(r.json()["detail"]))

        # the 2019 survey arrives late: NOW the scan runs and the
        # estimate gate opens
        r = self.submit("s2019", "k-2019", "2019", [
            row("P1", "001", 5, 5, dbh=20.0, height=15.0),
            row("P1", "008", 5, 15, dbh=27.0, height=18.0),
        ], expected=1)
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["scan"]["status"], "scanned")
        self.assertEqual(SurveyScan.objects.count(), 1)
        self.assertEqual(IdentityConflict.objects.count(), 1)
        r = self.client.post("/api/estimates/", {
            "label": "complete", "t1_campaign": "2019",
            "t2_campaign": "2024", "equation_ids": [self.eq.id]},
            format="json")
        self.assertEqual(r.status_code, 201, r.content)

    # -------- 5. interrupted apply: retryable, no partial writes, no
    #             double conflicts on retry
    def test_interrupted_apply_is_retryable_and_clean(self):
        # 2019 survey fully applied first
        r = self.submit("s2019", "k-2019", "2019", [
            row("P1", "001", 5, 5, dbh=20.0, height=15.0),
            row("P1", "008", 5, 15, dbh=27.0, height=18.0),
        ], expected=1)
        self.assertEqual(r.status_code, 201)

        # the 2024 batch's apply crashes mid-transaction
        with mock.patch("inventory.services.batches.write_measurement",
                        side_effect=RuntimeError("db connection lost")):
            r = self.submit("s2024", "k-2024", "2024", [
                row("P1", "001", 5, 5, dbh=21.0, height=15.3),
                row("P1", "008", 25, 15, dbh=12.0, height=10.0),
            ], expected=1)
        # stored as retryable (202), nothing applied
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["status"], "validated")
        batch = SurveyBatch.objects.get(idempotency_key="k-2024")
        self.assertEqual(batch.status, "validated")
        self.assertIn("safe to retry", batch.error)

        # refresh shows the retryable state; audit rows are queryable and
        # NOT half-applied; no measurements leaked from the rolled-back
        # transaction
        r = self.client.get(f"/api/batches/{batch.id}/")
        self.assertTrue(r.json()["retryable"])
        self.assertEqual(TreeMeasurement.objects.filter(
            campaign=self.t2).count(), 0)
        self.assertEqual(MeasurementImportRow.objects.filter(
            batch=batch, row_state="validated").count(), 2)
        self.assertEqual(MeasurementImportRow.objects.filter(
            batch=batch, row_state="applied").count(), 0)

        # retry: applies cleanly, scan runs exactly once
        r = self.apply(batch.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "applied")
        self.assertEqual(TreeMeasurement.objects.filter(
            campaign=self.t2).count(), 2)
        self.assertEqual(SurveyScan.objects.count(), 1)
        self.assertEqual(IdentityConflict.objects.count(), 1)

        # applying again changes nothing (no double conflicts/measurements)
        before = self.counts()
        r = self.apply(batch.id)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.counts(), before)

    # ---------------- failed batch keeps its audit and never applies
    def test_failed_batch_audit_queryable_and_not_applyable(self):
        r = self.submit("s2024", "bad-1", "2024", [
            row("P1", "001", 5, 5, dbh=250.0),          # 250 cm: unit error
            row("P1", "002", 8, 5, dbh=None, height=None),  # missing dbh
        ])
        self.assertEqual(r.status_code, 422)
        self.assertEqual(r.json()["status"], "failed")
        self.assertEqual(r.json()["n_rejected"], 2)
        self.assertEqual(TreeMeasurement.objects.count(), 0)

        # audit trail exists BEFORE any apply, per row, with reasons
        batch = SurveyBatch.objects.get(idempotency_key="bad-1")
        audits = MeasurementImportRow.objects.filter(batch=batch)
        self.assertEqual(audits.count(), 2)
        self.assertTrue(all(a.row_state == "rejected" for a in audits))
        self.assertTrue(all(a.rejection_reason for a in audits))
        r = self.client.get(f"/api/import-rows/?batch={batch.id}")
        self.assertEqual(len(r.json()), 2)

        # apply refuses; the crew must resubmit corrected rows under a
        # NEW key
        self.assertEqual(self.apply(batch.id).status_code, 409)
        r = self.submit("s2024", "bad-1-fixed", "2024", [
            dict(plot="P1", field_number="001", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=250.0, dbh_unit="mm",
                 height_raw=15.0, height_unit="m"),
        ])
        self.assertEqual(r.status_code, 201)
        m = TreeMeasurement.objects.get(campaign=self.t2)
        self.assertEqual(m.dbh_cm, 25.0)  # 250 mm correctly converted

    # --------------- survey progress endpoints used by the upload UI
    def test_survey_progress_and_batch_detail_endpoints(self):
        self.submit("s2019", "k1", "2019", [row("P1", "001", 5, 5)],
                    expected=2)
        r = self.client.get("/api/surveys/?campaign=2019")
        s = r.json()[0]
        self.assertEqual((s["status"], s["n_applied"], s["expected_batches"]),
                         ("collecting", 1, 2))

        batch = SurveyBatch.objects.get(idempotency_key="k1")
        r = self.client.get(f"/api/batches/{batch.id}/")
        self.assertEqual(r.json()["receipt"]["n_applied"], 1)
        self.assertEqual(len(r.json()["receipt"]["rows"]), 1)

        # explicit finalise completes the survey and reports the scan
        # decision (deferred: 2024 has no data at all yet)
        r = self.client.post(f"/api/surveys/{s['id']}/finalize/")
        self.assertEqual(r.json()["status"], "complete")
        self.assertEqual(r.json()["scan"]["status"], "deferred")
