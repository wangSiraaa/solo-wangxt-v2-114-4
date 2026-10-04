"""
Acceptance tests for the permanent-plot station.

Covers the acceptance checks specified by the station:
  A. remeasurement renumber handling;
  B. unequal plot areas in population expansion;
  C. measurement-unit mistakes rejected at ingest;
  D. real zero growth vs missing data vs mortality kept distinct;
  E. same number + contradictory position never auto-merged;
  F. confirmed estimate editions cannot be silently changed by a new
     allometric equation.
"""
import json

from django.conf import settings
from django.core.exceptions import ValidationError as DjValidationError
from django.test import TestCase
from rest_framework.test import APIClient

from inventory.models import (
    AllometricEquation,
    Campaign,
    CONFLICT_OPEN,
    EstimateVersion,
    Plot,
    Species,
    Stratum,
    Tree,
    TreeMeasurement,
)
from inventory.services.estimator import (
    build_measurement_table,
    estimate,
    resolved_identity_pairs,
)
from inventory.services.identity import pair_measurements
from inventory.services.ingest import import_campaign_rows, verify_plot_area
from inventory.services.units import convert_dbh_to_cm, ring_area_ha


def rect(ox, oy, w, d):
    return [[ox, oy], [ox + w, oy], [ox + w, oy + d], [ox, oy + d],
            [ox, oy]]


AM, AN, DE = "alive_measured", "alive_not_measured", "dead"


class EstimatorAcceptanceTests(TestCase):
    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        # Unequal plot areas: 0.10 ha and 0.25 ha.
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)

    def _run(self):
        t1t, t2t, equations, plots, strata = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        ren, dist = resolved_identity_pairs(self.t1, self.t2)
        design = dict(t1_code="t1", t2_code="t2", interval_years=5.0,
                      dbh_sd_cm=0.1, height_sd_m=0.3, zero_tol_cm=0.15,
                      recruitment_cm=5.0, fpc=False, crs_epsg=32650)
        return estimate(t1t, t2t, equations, plots, strata, design, ren, dist)

    # ---------- C. unit mistakes ------------------------------------------------
    def test_dbh_unit_must_be_explicit(self):
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(25.0, None)

    def test_mm_entered_as_cm_is_rejected_by_range(self):
        # 250 (mm) typed as cm -> 250 cm beyond the accepted demo range.
        with self.assertRaises(DjValidationError):
            convert_dbh_to_cm(250.0, "cm")

    def test_mm_value_correctly_converted(self):
        self.assertAlmostEqual(convert_dbh_to_cm(250.0, "mm"), 25.0)

    def test_import_rejects_bad_unit_rows(self):
        rows = [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=10, y_m=10, status=AM,
                 dbh_raw=250.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P1", field_number="2", species="OAK",
                 x_m=12, y_m=12, status=AM,
                 dbh_raw=20.0, height_raw=15.0, height_unit="m"),
        ]
        r = import_campaign_rows(self.t2, rows, 0.01)
        self.assertEqual(r["n_rejected"], 2)
        self.assertEqual(TreeMeasurement.objects.count(), 0)

    # ---------- B. unequal plot areas ------------------------------------------
    def test_unequal_plot_areas_expanded_per_plot(self):
        # 100 kg growth on each plot; per-ha: 1000 vs 400 kg/ha.
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        # choose t2 dbh giving +100 kg growth per tree with a=0.1,b=2,c=.5
        def b_at(d):
            return 0.1 * d ** 2 * 15 ** 0.5
        import math
        d2 = math.sqrt((b_at(20.0) + 100.0) / (0.1 * 15 ** 0.5))
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
            dict(plot="P2", field_number="1", species="OAK",
                 x_m=5, y_m=5, status=AM, dbh_raw=d2, dbh_unit="cm",
                 height_raw=15.0, height_unit="m"),
        ], 0.01)
        res = self._run()
        # per-ha mean = (1000 + 400)/2 = 700 kg/ha; x 100 ha = 70 000 kg
        self.assertAlmostEqual(
            res["components"]["survivor_growth"]["total_kg"],
            70_000.0, delta=1e-6)
        # The naive "mean tree * area" would give 100 kg * (100/0.175 avg?)
        # and clearly differs; the provenance carries per-plot values.
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual(p1["area_ha"], 0.10)

    def test_plot_area_polygon_crosscheck(self):
        bad = Plot(code="BAD", stratum=self.sA, x_m=0, y_m=0,
                   declared_area_ha=0.50, boundary=rect(0, 0, 50, 20),
                   area_polygon_ha=0.10)
        with self.assertRaises(DjValidationError):
            verify_plot_area(bad, 0.01)

    # ---------- D. zero / missing / dead ---------------------------------------
    def test_zero_growth_missing_and_dead_are_distinct(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m"),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=AM, dbh_raw=22.0, dbh_unit="cm",
                 height_raw=16.0, height_unit="m"),
        ], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="z", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=18.0, dbh_unit="cm",
                 height_raw=13.0, height_unit="m",
                 notes="verified zero growth"),
            dict(plot="P1", field_number="m", species="OAK", x_m=8, y_m=8,
                 status=AN),
            dict(plot="P1", field_number="d", species="OAK", x_m=11, y_m=11,
                 status=DE),
        ], 0.01)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([z["tree"] for z in p1["verified_zero_growth"]],
                         ["P1/z"])
        self.assertEqual([m["tree"] for m in p1["alive_not_measured"]],
                         ["P1/m"])
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/d"])
        # missing survivor did NOT silently become zero growth:
        self.assertTrue(p1["imputed_survivor_growth_kg"] >= 0)

    # ---------- A + E. renumber / same-number contradiction --------------------
    def test_renumber_keeps_one_individual(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="007", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        tree = Tree.objects.get(current_field_number="007")
        tree.current_field_number = "017"
        tree.save(update_fields=["current_field_number"])
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="017", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        t1t, t2t, *_ = build_measurement_table(
            self.t1, self.t2, AllometricEquation.objects.all())
        pairing = pair_measurements(t1t, t2t)
        self.assertEqual(len(pairing["pairs"]), 1)
        self.assertEqual(pairing["pairs"][0]["kind"], "renumber")

    def test_same_number_position_contradiction_is_excluded(self):
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="008", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        # new tree row, same label, 15 m away
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="008", species="OAK", x_m=15, y_m=5,
                 status=AM, dbh_raw=12.0, dbh_unit="cm",
                 height_raw=10.0, height_unit="m")], 0.01)
        self.assertEqual(
            Tree.objects.filter(plot=self.p1,
                                current_field_number="008").count(), 2)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        excluded = [c["tree"] for c in p1["excluded_identity_conflicts"]]
        self.assertIn("P1/008", excluded)
        # not counted as growth, nor as mortality, nor as ingrowth
        self.assertEqual(p1["kg"]["survivor_growth"], 0.0)
        self.assertEqual(p1["mortality"], [])
        self.assertEqual(p1["ingrowth"], [])

    def test_distinct_resolution_counts_removal_and_ingrowth(self):
        from inventory.services.conflicts import scan_conflicts
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="009", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=16.0, dbh_unit="cm",
                 height_raw=12.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="009", species="OAK", x_m=25, y_m=5,
                 status=AM, dbh_raw=8.0, dbh_unit="cm",
                 height_raw=8.0, height_unit="m")], 0.01)
        found = scan_conflicts(self.t1, self.t2)
        self.assertTrue(found)
        client = APIClient()
        cid = found[0]["id"]
        resp = client.post(f"/api/conflicts/{cid}/resolve/",
                           {"status": "distinct", "note": "new recruit"})
        self.assertEqual(resp.status_code, 200)
        res = self._run()
        p1 = next(p for p in res["provenance"]["plots"] if p["plot"] == "P1")
        self.assertEqual([m["tree"] for m in p1["mortality"]], ["P1/009"])
        self.assertEqual([m["tree"] for m in p1["ingrowth"]], ["P1/009"])

    # ---------- F. confirmed edition immutability ------------------------------
    def test_confirmed_estimate_is_frozen_against_new_equation(self):
        client = APIClient()
        import_campaign_rows(self.t1, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=20.0, dbh_unit="cm",
                 height_raw=15.0, height_unit="m")], 0.01)
        import_campaign_rows(self.t2, [
            dict(plot="P1", field_number="1", species="OAK", x_m=5, y_m=5,
                 status=AM, dbh_raw=21.0, dbh_unit="cm",
                 height_raw=15.3, height_unit="m")], 0.01)
        body = dict(label="v1", t1_campaign="t1", t2_campaign="t2",
                    equation_ids=[self.eq.id], fpc=False)
        r = client.post("/api/estimates/", body, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        vid = r.json()["id"]
        before = r.json()["result_payload"]["components"]["survivor_growth"]

        rc = client.post(f"/api/estimates/{vid}/confirm/")
        self.assertEqual(rc.status_code, 200, rc.content)

        # 1) the JSON result stays byte-stable
        again = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(
            again["result_payload"]["components"]["survivor_growth"], before)

        # 2) the equation is locked: coefficient change refused
        self.eq.refresh_from_db()
        self.eq.a = 0.999
        with self.assertRaises(PermissionError):
            self.eq.save()

        # 3) the edition row itself cannot be mutated
        version = EstimateVersion.objects.get(pk=vid)
        version.label = "tampered"
        with self.assertRaises(PermissionError):
            version.save()

        # 4) a new equation must be issued as a NEW equation row/version
        eq2 = AllometricEquation.objects.create(
            code="OAK", version="2", status="draft",
            a=0.2, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional revised")
        eq2.species.add(self.oak)
        r2 = client.post("/api/estimates/",
                         dict(label="v2-new-equation",
                              t1_campaign="t1", t2_campaign="t2",
                              equation_ids=[eq2.id], fpc=False),
                         format="json")
        self.assertEqual(r2.status_code, 201)
        self.assertNotEqual(r2.json()["id"], vid)
        # old edition unchanged
        old = client.get(f"/api/estimates/{vid}/").json()
        self.assertEqual(old["label"], "v1")
        self.assertEqual(
            old["result_payload"]["components"]["survivor_growth"], before)

    def test_result_payload_records_units_and_sources(self):
        res = self._run()
        self.assertEqual(res["units"]["dbh"],
                         "cm (converted at ingest; raw unit retained)")
        self.assertEqual(res["units"]["height"], "m")
        self.assertIn("estimator", res["design"])
        self.assertTrue(res["uncertainty_assumptions"])
        self.assertIn("OAK", res["equations_used"])


# =============================================================================
# Survey-batch synchronisation (offline tablets, multi-batch upload)
# =============================================================================
from unittest import mock  # noqa: E402

from inventory.models import (  # noqa: E402
    BATCH_APPLIED,
    BATCH_BLOCKED,
    BATCH_DISCARDED,
    BATCH_FAILED,
    BATCH_VALIDATED,
    IdentityConflict,
    ImportBatch,
    MeasurementImportRow,
)
from inventory.services.batches import (  # noqa: E402
    apply_batch,
    campaign_progress,
    content_digest,
    scan_if_adjacent_complete,
    submit_batch,
)


class BatchSyncAcceptanceTests(TestCase):
    """
    Acceptance for the resumable survey-batch sync:
      1. several batches merge into ONE complete import + conflict scan;
      2. same key + same payload replays the original receipt, no re-write;
      3. same key + different payload, or overlapping tree records, are
         rejected / blocked for humans (first-arrived data wins);
      4. an early later-campaign batch must not fabricate mortality or
         ingrowth before the adjacent survey is complete;
      5. an interrupted apply is retryable and leaves neither partial
         measurements nor duplicated conflicts.
    """

    def setUp(self):
        self.sA = Stratum.objects.create(code="A", name="A", area_ha=100.0)
        self.oak = Species.objects.create(code="OAK", name="Oak")
        self.eq = AllometricEquation.objects.create(
            code="OAK", version="1", status="confirmed",
            a=0.1, b=2.0, c=0.5, dbh_min_cm=5.0, dbh_max_cm=100.0,
            residual_sigma=0.1, citation="fictional")
        self.eq.species.add(self.oak)
        self.t1 = Campaign.objects.create(code="t1", measured_on="2019-01-01")
        self.t2 = Campaign.objects.create(code="t2", measured_on="2024-01-01")
        self.p1 = Plot.objects.create(
            code="P1", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.10, boundary=rect(0, 0, 50, 20),
            area_polygon_ha=0.10)
        self.p2 = Plot.objects.create(
            code="P2", stratum=self.sA, x_m=0, y_m=0,
            declared_area_ha=0.25, boundary=rect(0, 0, 50, 50),
            area_polygon_ha=0.25)
        self.client = APIClient()

    # -- row factories ------------------------------------------------------
    @staticmethod
    def am(plot, num, x, y, dbh, h):
        return dict(plot=plot, field_number=num, species="OAK",
                    x_m=x, y_m=y, status=AM,
                    dbh_raw=dbh, dbh_unit="cm",
                    height_raw=h, height_unit="m")

    def _t1_rows(self):
        return [
            self.am("P1", "1", 10, 10, 20.0, 15.0),
            self.am("P1", "2", 20, 10, 18.0, 13.0),
            self.am("P2", "1", 10, 10, 20.0, 15.0),
            self.am("P1", "8", 30, 10, 20.0, 15.0),
        ]

    def _t2_rows(self):
        return [
            self.am("P1", "1", 10, 10, 21.0, 15.3),
            self.am("P1", "2", 20, 10, 18.0, 13.0),   # verified zero growth
            self.am("P2", "1", 10, 10, 21.0, 15.3),
            # same label "8" but 15+ m away -> identity conflict, excluded
            self.am("P1", "8", 45, 15, 12.0, 10.0),
            self.am("P1", "9", 40, 15, 8.0, 7.0),    # ingrowth
        ]

    def _submit(self, campaign, key, rows):
        return self.client.post(
            "/api/batches/",
            {"campaign": campaign, "client_key": key, "rows": rows},
            format="json")

    def _estimate_payload(self):
        r = self.client.post("/api/estimates/",
                             dict(label="chk", t1_campaign="t1",
                                  t2_campaign="t2", equation_ids=[self.eq.id],
                                  fpc=False), format="json")
        self.assertEqual(r.status_code, 201, r.content)
        return r.json()["result_payload"]

    def _upload_all_in_batches(self):
        """The survey split into four batches, t2 batch arriving FIRST."""
        r = self._submit("t2", "t2-a", self._t2_rows()[:3])
        self.assertEqual(r.status_code, 201, r.content)
        r = self._submit("t1", "t1-a", self._t1_rows()[:2])
        self.assertEqual(r.status_code, 201, r.content)
        r = self._submit("t1", "t1-b", self._t1_rows()[2:])
        self.assertEqual(r.status_code, 201, r.content)
        return self._submit("t2", "t2-b", self._t2_rows()[3:])

    # -- 1. multi-batch merge = one complete import + one conflict scan -----
    def test_multi_batch_merge_equals_single_import(self):
        r = self._upload_all_in_batches()
        self.assertEqual(r.status_code, 201, r.content)
        # the final batch's response carries the identity scan
        self.assertEqual(len(r.json()["conflicts"]), 1)

        self.assertEqual(TreeMeasurement.objects.filter(
            campaign=self.t1).count(), 4)
        self.assertEqual(TreeMeasurement.objects.filter(
            campaign=self.t2).count(), 5)
        self.assertEqual(ImportBatch.objects.filter(
            status=BATCH_APPLIED).count(), 4)
        # exactly ONE conflict record from the merged scan
        self.assertEqual(IdentityConflict.objects.count(), 1)
        self.assertEqual(IdentityConflict.objects.get().field_number, "8")
        # every row is audited against its batch
        self.assertEqual(
            MeasurementImportRow.objects.filter(
                accepted=True, batch__isnull=False).count(), 9)

        via_batches = self._estimate_payload()

        # reset and import the very same rows the legacy one-shot way
        for m in (EstimateVersion, IdentityConflict, MeasurementImportRow,
                  TreeMeasurement, Tree, ImportBatch):
            m.objects.all().delete()
        import_campaign_rows(self.t1, self._t1_rows(), 0.01)
        import_campaign_rows(self.t2, self._t2_rows(), 0.01)
        from inventory.services.conflicts import scan_conflicts
        self.assertEqual(len(scan_conflicts(self.t1, self.t2)), 1)
        legacy = self._estimate_payload()

        for comp in ("survivor_growth", "mortality", "ingrowth"):
            self.assertAlmostEqual(
                via_batches["components"][comp]["total_kg"],
                legacy["components"][comp]["total_kg"], places=9)
        self.assertEqual(via_batches["provenance"]["pairs_same_number"],
                         legacy["provenance"]["pairs_same_number"])
        self.assertEqual(len(via_batches["provenance"]["open_conflicts"]), 1)

    # -- 2. same key + same payload -> original receipt, no re-write --------
    def test_same_key_same_payload_replays_receipt(self):
        rows = self._t1_rows()[:2]
        r1 = self._submit("t1", "key-1", rows)
        self.assertEqual(r1.status_code, 201, r1.content)
        receipt1 = r1.json()["receipt"]

        counts = (TreeMeasurement.objects.count(),
                  Tree.objects.count(),
                  MeasurementImportRow.objects.count(),
                  ImportBatch.objects.count())
        r2 = self._submit("t1", "key-1", rows)
        self.assertEqual(r2.status_code, 200, r2.content)
        self.assertTrue(r2.json()["replayed"])
        self.assertEqual(r2.json()["receipt"], receipt1)
        self.assertEqual(counts, (TreeMeasurement.objects.count(),
                                  Tree.objects.count(),
                                  MeasurementImportRow.objects.count(),
                                  ImportBatch.objects.count()))

    # -- 3a. same key + different payload -> rejected ------------------------
    def test_same_key_different_payload_is_rejected(self):
        rows = self._t1_rows()[:1]
        r1 = self._submit("t1", "key-1", rows)
        self.assertEqual(r1.status_code, 201, r1.content)

        tampered = [dict(rows[0], dbh_raw=33.0)]
        r2 = self._submit("t1", "key-1", tampered)
        self.assertEqual(r2.status_code, 409)
        # first-arrived measurement untouched
        m = TreeMeasurement.objects.get()
        self.assertEqual(m.dbh_cm, 20.0)
        self.assertEqual(ImportBatch.objects.count(), 1)

    # -- 3b. overlapping tree records -> blocked for humans, no overwrite ---
    def test_overlapping_tree_records_are_blocked_not_overwritten(self):
        r1 = self._submit("t1", "key-1", self._t1_rows()[:1])
        self.assertEqual(r1.status_code, 201, r1.content)

        # a second batch (different key) re-measures the same tree/label
        clash = [self.am("P1", "1", 10, 10, 25.0, 16.0)]
        r2 = self._submit("t1", "key-2", clash)
        self.assertEqual(r2.status_code, 202, r2.content)
        batch2 = ImportBatch.objects.get(client_key="key-2")
        self.assertEqual(batch2.status, BATCH_BLOCKED)
        self.assertEqual(batch2.overlaps[0]["existing_batch"], "key-1")
        # first-arrived data NOT overwritten
        self.assertEqual(TreeMeasurement.objects.get().dbh_cm, 20.0)

        # shows up in the human-review endpoint and can only be discarded
        rc = self.client.get("/api/batches/conflicts/")
        self.assertEqual([b["client_key"] for b in rc.json()], ["key-2"])
        rr = self.client.post(f"/api/batches/{batch2.id}/resolve/",
                              {"decision": "discard",
                               "note": "duplicate of key-1, keep first"},
                              format="json")
        self.assertEqual(rr.status_code, 200, rr.content)
        batch2.refresh_from_db()
        self.assertEqual(batch2.status, BATCH_DISCARDED)
        self.assertEqual(TreeMeasurement.objects.get().dbh_cm, 20.0)
        self.assertEqual(TreeMeasurement.objects.count(), 1)

    def test_duplicate_label_within_one_batch_fails_validation(self):
        rows = [self.am("P1", "1", 10, 10, 20.0, 15.0),
                self.am("P1", "1", 12, 10, 21.0, 15.0)]
        r = self._submit("t1", "key-dup", rows)
        self.assertEqual(r.status_code, 207, r.content)
        self.assertEqual(TreeMeasurement.objects.count(), 0)
        batch = ImportBatch.objects.get(client_key="key-dup")
        self.assertEqual(batch.status, BATCH_FAILED)
        # the rejection is audited before any apply
        self.assertEqual(MeasurementImportRow.objects.filter(
            batch=batch, accepted=False).count(), 1)

    # -- 4. early later batch must not fabricate mortality / ingrowth -------
    def test_early_later_batch_defers_mortality_ingrowth_and_conflicts(self):
        # t2 arrives while t1 is still on the tablet
        r = self._submit("t2", "t2-early", self._t2_rows())
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(r.json()["conflicts"], [])       # scan deferred
        self.assertEqual(IdentityConflict.objects.count(), 0)

        res = self._estimate_payload()
        self.assertEqual(res["components"]["mortality"]["total_kg"], 0.0)
        self.assertEqual(res["components"]["ingrowth"]["total_kg"], 0.0)
        pending = res["provenance"]["pending_adjacent_data"]
        self.assertEqual(pending["plots"], ["P1", "P2"])
        self.assertEqual(len(pending["held_t2_rows"]), 5)

        # once the adjacent survey arrives, components and conflicts appear
        self._submit("t1", "t1-a", self._t1_rows()[:2])
        r = self._submit("t1", "t1-b", self._t1_rows()[2:])
        self.assertEqual(len(r.json()["conflicts"]), 1)   # scan ran now
        res = self._estimate_payload()
        self.assertGreater(res["components"]["survivor_growth"]["total_kg"], 0)
        self.assertGreater(res["components"]["ingrowth"]["total_kg"], 0)
        self.assertEqual(
            res["provenance"]["pending_adjacent_data"]["plots"], [])

    # -- 5. interrupted apply -> retryable, no partial writes, no dupes -----
    def test_interrupted_apply_is_retryable_and_clean(self):
        rows = self._t1_rows()
        with mock.patch("inventory.services.batches.apply_row",
                        side_effect=RuntimeError("power loss mid-apply")):
            with self.assertRaises(RuntimeError):
                submit_batch(self.t1, "key-1", rows, 0.01)

        batch = ImportBatch.objects.get(client_key="key-1")
        # refresh shows a retryable state, nothing half-written
        self.assertEqual(batch.status, BATCH_VALIDATED)
        self.assertEqual(TreeMeasurement.objects.count(), 0)
        self.assertEqual(Tree.objects.count(), 0)
        self.assertEqual(MeasurementImportRow.objects.filter(
            accepted=True).count(), 0)

        # retry via the API: applies cleanly and completely
        r = self.client.post(f"/api/batches/{batch.id}/apply/")
        self.assertEqual(r.status_code, 201, r.content)
        batch.refresh_from_db()
        self.assertEqual(batch.status, BATCH_APPLIED)
        self.assertEqual(TreeMeasurement.objects.count(), 4)
        self.assertEqual(batch.n_applied, 4)

        # applying again is an idempotent no-op
        r = self.client.post(f"/api/batches/{batch.id}/apply/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(TreeMeasurement.objects.count(), 4)

        # finish the survey; the scan must not duplicate conflicts on retry
        self._submit("t2", "t2-a", self._t2_rows())
        self.assertEqual(IdentityConflict.objects.count(), 1)
        scan_if_adjacent_complete(self.t1)
        scan_if_adjacent_complete(self.t2)
        self.assertEqual(IdentityConflict.objects.count(), 1)

    def test_resend_of_interrupted_batch_resumes_instead_of_replaying(self):
        rows = self._t1_rows()[:2]
        with mock.patch("inventory.services.batches.apply_row",
                        side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                submit_batch(self.t1, "key-1", rows, 0.01)
        # tablet re-sends the identical batch after the network returns
        r = self._submit("t1", "key-1", rows)
        self.assertEqual(r.status_code, 201, r.content)
        self.assertFalse(r.json()["replayed"])
        self.assertEqual(TreeMeasurement.objects.count(), 2)
        self.assertEqual(ImportBatch.objects.count(), 1)

    # -- progress + audit queryability ---------------------------------------
    def test_progress_and_per_batch_audit_trail(self):
        self._upload_all_in_batches()
        prog = {p["campaign"]: p for p in self.client.get(
            "/api/batches/progress/").json()}
        self.assertEqual(prog["t1"]["rows_applied"], 4)
        self.assertEqual(prog["t2"]["rows_applied"], 5)
        self.assertTrue(prog["t1"]["complete"])

        batch = ImportBatch.objects.get(client_key="t1-a")
        detail = self.client.get(f"/api/batches/{batch.id}/").json()
        self.assertEqual(len(detail["receipt"]["rows"]), 2)
        self.assertTrue(all(r["result"] == "accepted"
                            for r in detail["receipt"]["rows"]))
        # audit rows are linked to their batch, before and after apply
        self.assertEqual(MeasurementImportRow.objects.filter(
            batch=batch, accepted=True).count(), 2)
        self.assertEqual(MeasurementImportRow.objects.filter(
            campaign=self.t1).count(), 4)
