"""Traceable demand billing: drop/overlap, two authorisations, gaps, versions.

Run from the repository root::

    python -B -m unittest discover -s tests -v

The same initial E1 records (80/120 kW offline samples, main drop at 550,
backup running from 548, two-minute overlap) must stay 待核 until one of two
contract-complete supplementary proofs arrives:

* path A: signed pre-failure seal + calibrated backup pulses -> 96 kW, standard;
* path B: signed main-meter auto-recovery continuation      -> 108 kW, surcharge.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path

from grainrelease import power_capture
from grainrelease.demand_bill import (
    BillImmutableError,
    BillBook,
    DemandLedger,
    bill_history,
    build_bill,
    classify_tier,
    correct_bill,
    get_bill,
    issue_bill,
    peak_kw,
)
from grainrelease.power_capture import CaptureStore, load_scenario
from grainrelease.power_meter import adjudicate, integrated_demand_kw, intervals

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "fixtures"


def load_fixture() -> dict:
    return json.loads((FIX / "power-switch.json").read_text(encoding="utf-8"))


def disputed_interval(bundle: dict, service: str = "E1", start: int = 540) -> dict:
    result = adjudicate(bundle)
    return next(iv for iv in result["services"][service]["intervals"]
                if iv["start_minute"] == start)


class LegacySeedCompatTest(unittest.TestCase):
    """The isolated power seed behaviour stays readable, not silently deleted."""

    def test_power_ok_seed_still_reports_raw_120(self):
        seed = json.loads((FIX / "power-ok.json").read_text(encoding="utf-8"))
        self.assertEqual(peak_kw(seed["events"]), {"E1": 120})
        self.assertEqual(
            intervals(seed["events"]),
            {"E1": [{"meter_id": "E1", "minute": 540, "kw": 80},
                    {"meter_id": "E1", "minute": 555, "kw": 120}]})

    def test_instantaneous_sample_is_not_integrated_demand(self):
        # Through the traceable engine the same two points settle nothing:
        # a boundary 120 kW point can never stand in for a full 15-minute block.
        bundle = {"period": {"start_minute": 540, "end_minute": 570},
                  "events": [
                      {"meter_id": "E1", "minute": 540, "kw": 80},
                      {"meter_id": "E1", "minute": 555, "kw": 120}]}
        result = adjudicate(bundle)
        iv540 = next(x for x in result["services"]["E1"]["intervals"] if x["start_minute"] == 540)
        self.assertFalse(iv540["settled"])
        self.assertIsNone(iv540["demand_kw"])
        self.assertNotEqual(iv540["status"], "settled")
        # samples are retained for traceability
        self.assertEqual({s["kw"] for s in iv540["samples"]}, {80, 120})


class PendingAuthorizationTest(unittest.TestCase):
    fx = load_fixture()

    def test_initial_records_are_pending_not_arbitrary_route(self):
        result = adjudicate(self.fx)
        svc = result["services"]["E1"]
        iv = next(x for x in svc["intervals"] if x["start_minute"] == 540)
        self.assertEqual(iv["status"], "pending")
        # “待核” is a first-class comparable status token.
        self.assertEqual(iv["status"], "待核")
        self.assertIn("待核", iv["status"])
        self.assertIsNone(svc["peak_kw"])
        # The naive 120 must not survive as the traceable peak.
        self.assertNotEqual(svc["peak_kw"], 120)
        self.assertEqual(peak_kw(self.fx), {})
        self.assertEqual(peak_kw(self.fx), {"E1": None})

    def test_drop_and_overlap_window_are_detected(self):
        iv = disputed_interval(self.fx)
        self.assertEqual(iv["overlap_window"], [548, 550])
        self.assertEqual(iv["authorization_id"], None)
        text = " ".join(iv["reasons"])
        self.assertIn("授权", text)

    def test_upload_order_does_not_decide_pending_state(self):
        shuffled = copy.deepcopy(self.fx)
        shuffled["events"] = list(reversed(shuffled["events"]))
        iv = disputed_interval(shuffled)
        self.assertEqual(iv["status"], "pending")


class OverlapContractOwnerTest(unittest.TestCase):
    fx = load_fixture()

    def _bundle(self, auth):
        case = self.fx["overlap_owner_case"]
        events = copy.deepcopy(case["common_events"])
        if auth is not None:
            events.append(copy.deepcopy(auth))
        return {"period": case["period"], "as_of": case["as_of"],
                "meters": case["meters"], "events": events}

    def test_without_authorization_overlap_is_pending(self):
        iv = disputed_interval(self._bundle(None))
        self.assertEqual(iv["status"], "pending")
        self.assertEqual(iv["status"], "待核")

    def test_contract_owner_not_upload_order_or_low_reading(self):
        case = self.fx["overlap_owner_case"]
        main_iv = disputed_interval(self._bundle(case["auth_main_original"]))
        backup_iv = disputed_interval(self._bundle(case["auth_backup_original"]))

        # Main reads 110 kW (higher), backup 90 kW (lower). Authorising the
        # higher-reading main as overlap original must be honoured.
        self.assertAlmostEqual(main_iv["demand_kw"],
                               case["expected_main_owner_demand_kw"], places=4)
        self.assertAlmostEqual(backup_iv["demand_kw"],
                               case["expected_backup_owner_demand_kw"], places=4)
        self.assertNotAlmostEqual(main_iv["demand_kw"], backup_iv["demand_kw"], places=4)
        self.assertGreater(main_iv["demand_kw"], backup_iv["demand_kw"])

        # The overlap minutes are attributed to the authorised owner.
        main_overlap = {(e["minute"], e["meter_id"]) for e in main_iv["excluded"]
                        if e["minute"] in (548, 549)}
        backup_overlap = {(e["minute"], e["meter_id"]) for e in backup_iv["excluded"]
                          if e["minute"] in (548, 549)}
        self.assertEqual(main_overlap, {(548, "M-BACKUP"), (549, "M-BACKUP")})
        self.assertEqual(backup_overlap, {(548, "M-MAIN"), (549, "M-MAIN")})

    def test_shuffled_uploads_give_identical_demand(self):
        case = self.fx["overlap_owner_case"]
        bundle = self._bundle(case["auth_main_original"])
        forward = disputed_interval(bundle)["demand_kw"]
        shuffled = copy.deepcopy(bundle)
        shuffled["events"] = list(reversed(shuffled["events"]))
        self.assertEqual(disputed_interval(shuffled)["demand_kw"], forward)

    def test_overlap_energy_is_counted_once(self):
        # 10 minutes main @110 + 5 minutes backup @90 = 25.8333 kWh -> 103.333 kW
        iv = disputed_interval(self._bundle(self.fx["overlap_owner_case"]["auth_main_original"]))
        total_kwh = sum(seg["kwh"] for seg in iv["segments"])
        self.assertAlmostEqual(total_kwh, 25.833333, places=4)
        self.assertAlmostEqual(iv["kwh"], total_kwh, places=5)
        # excluded rows never enter segments/counted energy
        excluded_kwh = sum(e["kw"] / 60.0 for e in iv["excluded"])
        self.assertGreater(excluded_kwh, 0)

    def test_stitched_path_a_without_receipt_still_integrates_96(self):
        path_a = copy.deepcopy(load_scenario(self.fx, "path_a"))
        path_a["events"] = [e for e in path_a["events"] if e["kind"] != "interval_receipt"]
        iv = disputed_interval(path_a)
        self.assertEqual(iv["status"], "settled")
        self.assertAlmostEqual(iv["demand_kw"], 96.0, places=5)
        overlap_excluded = {(e["minute"], e["meter_id"]) for e in iv["excluded"]
                            if e["minute"] in (548, 549)}
        self.assertEqual(overlap_excluded, {(548, "M-BACKUP"), (549, "M-BACKUP")})


class TwoAuthorizationsTest(unittest.TestCase):
    fx = load_fixture()

    def test_path_a_seal_and_calibrated_backup_gives_96_standard(self):
        bundle = load_scenario(self.fx, "path_a")
        expected = self.fx["path_a"]["expected"]
        iv = disputed_interval(bundle)
        self.assertEqual(iv["status"], "settled")
        self.assertEqual(iv["billing_meter_id"], "M-BACKUP")
        self.assertTrue(iv["single_billing_meter"])
        self.assertTrue(iv["complete"])
        self.assertTrue(iv["calibration_valid"])
        self.assertAlmostEqual(iv["demand_kw"], expected["demand_kw"], places=6)
        self.assertEqual(peak_kw(bundle), {"E1": 96})
        tier = classify_tier(96)
        self.assertEqual(tier["tier"], "standard")
        self.assertFalse(tier["surcharge"])
        bill = build_bill("E1", "2026-10", bundle, rate_per_kw=40)
        self.assertFalse(bill["surcharge"])
        self.assertEqual(bill["tier"], "standard")
        self.assertEqual(bill["surcharge_amount"], 0.0)

    def test_path_b_signed_continuation_gives_108_surcharge(self):
        bundle = load_scenario(self.fx, "path_b")
        expected = self.fx["path_b"]["expected"]
        iv = disputed_interval(bundle)
        self.assertEqual(iv["status"], "settled")
        self.assertEqual(iv["billing_meter_id"], "M-MAIN")
        self.assertTrue(iv["single_billing_meter"])
        self.assertTrue(iv["calibration_valid"])
        self.assertAlmostEqual(iv["demand_kw"], expected["demand_kw"], places=6)
        # The 120 kW transient is explicitly not the interval demand.
        self.assertNotEqual(iv["demand_kw"], 120)
        self.assertEqual(peak_kw(bundle), {"E1": 108})
        tier = classify_tier(108)
        self.assertEqual(tier["tier"], "peak_surcharge")
        self.assertTrue(tier["surcharge"])
        bill = build_bill("E1", "2026-10", bundle, rate_per_kw=40)
        self.assertTrue(bill["surcharge"])
        self.assertEqual(bill["surcharge_amount"], 4320.0)

    def test_same_initial_records_opposite_contract_outcomes(self):
        a = peak_kw(load_scenario(self.fx, "path_a"))["E1"]
        b = peak_kw(load_scenario(self.fx, "path_b"))["E1"]
        self.assertEqual(float(a), 96.0)
        self.assertEqual(float(b), 108.0)
        self.assertFalse(classify_tier(a)["surcharge"])
        self.assertTrue(classify_tier(b)["surcharge"])

    def test_threshold_boundary_is_inclusive_100kw(self):
        self.assertTrue(classify_tier(100.0)["surcharge"])
        self.assertFalse(classify_tier(99.999)["surcharge"])

    def test_energy_integration_formula(self):
        self.assertEqual(integrated_demand_kw(24.0, 15), 96.0)
        self.assertEqual(integrated_demand_kw(27.0, 15), 108.0)


class MissingIntervalsTest(unittest.TestCase):
    fx = load_fixture()

    def test_gap_is_listed_as_unsettleable_range(self):
        bundle = load_scenario(self.fx, "missing_case")
        result = adjudicate(bundle)
        svc = result["services"]["E1"]
        self.assertAlmostEqual(svc["peak_kw"], 96.0)
        self.assertEqual(len(svc["missing_ranges"]), 1)
        rng = svc["missing_ranges"][0]
        self.assertEqual((rng["start_minute"], rng["end_minute"], rng["minutes"]),
                         (555, 600, 45))
        self.assertEqual(rng["interval_starts"], [555, 570, 585])
        # Pending and missing together form the non-settleable view.
        self.assertTrue(any(r["start_minute"] == 555 for r in svc["unsettleable_ranges"]))

    def test_base_fixture_lists_later_gap_and_pending_block(self):
        svc = adjudicate(self.fx)["services"]["E1"]
        self.assertEqual([(r["start_minute"], r["end_minute"]) for r in svc["missing_ranges"]],
                         [(555, 600)])
        self.assertEqual([(r["start_minute"], r["end_minute"]) for r in svc["pending_ranges"]],
                         [(540, 555)])

    def test_instantaneous_only_interval_is_unsettleable_not_demand(self):
        bundle = {"period": {"start_minute": 600, "end_minute": 615},
                  "events": [{"meter_id": "E1", "minute": 600, "kw": 250}]}
        iv = disputed_interval(bundle, start=600)
        self.assertEqual(iv["status"], "missing")
        self.assertIsNone(iv["demand_kw"])


class CalibrationGateTest(unittest.TestCase):
    fx = load_fixture()

    def test_expired_backup_calibration_blocks_substitution(self):
        bundle = load_scenario(self.fx, "calibration_expired_case")
        iv = disputed_interval(bundle)
        self.assertNotEqual(iv["status"], "settled")
        self.assertFalse(iv["calibration_valid"])
        self.assertIsNone(iv["demand_kw"])
        self.assertEqual(peak_kw(bundle), {})

    def test_missing_calibration_keeps_backup_route_pending(self):
        case = self.fx["overlap_owner_case"]
        auth = copy.deepcopy(case["auth_backup_original"])
        meter = {"kind": "meter", "meter_id": "M-BACKUP", "service_id": "E1", "role": "backup"}
        main = {"kind": "meter", "meter_id": "M-MAIN", "service_id": "E1", "role": "main",
                "calibrated_until": "2027-12-31"}
        bundle = {"period": case["period"], "as_of": case["as_of"],
                  "meters": [main, meter],
                  "events": case["common_events"] + [auth]}
        iv = disputed_interval(bundle)
        self.assertEqual(iv["status"], "pending")
        self.assertIsNone(iv["demand_kw"])


class SourcePriorityAndCaptureTest(unittest.TestCase):
    fx = load_fixture()

    def test_receipt_carries_priority_one_and_trace_ids(self):
        iv = disputed_interval(load_scenario(self.fx, "path_a"))
        self.assertEqual(iv["source_kind"], "joint_interval_receipt")
        self.assertEqual(iv["priority"], power_capture.SOURCE_PRIORITY["joint_interval_receipt"])
        self.assertTrue(iv["source_ids"])
        self.assertEqual(iv["authorization_id"], "AUTH-A-SWITCH")

    def test_capture_store_records_arrival_order_and_dedup(self):
        store = CaptureStore(":memory:")
        ids = []
        # Deliberately capture out of business-time order.
        for record in reversed(self.fx["events"]):
            ids.append(store.capture(record)["record_id"])
        order = store.arrival_order("E1")
        # Arrival order is capture order, explicitly not business-time order.
        self.assertEqual(order, ids)
        self.assertEqual(order[0], "OFF-555-120")
        self.assertNotEqual(order, [e["record_id"] for e in self.fx["events"]])
        first = store.capture(self.fx["events"][0])
        self.assertTrue(first["idempotent"])
        # Same id with changed content must be rejected.
        changed = copy.deepcopy(self.fx["events"][0])
        changed["state"] = "online"
        with self.assertRaises(ValueError):
            store.capture(changed)
        store.close()

    def test_capture_module_connect_alias(self):
        db = power_capture.connect(":memory:")
        result = power_capture.capture_records(db, self.fx["events"])
        self.assertEqual(len(result), 4)
        rows = db.execute("SELECT COUNT(*) FROM power_evidence").fetchone()[0]
        self.assertEqual(rows, 4)
        db.close()


class VersionedMonthlyBillTest(unittest.TestCase):
    fx = load_fixture()

    def _issued_v1_120(self, ledger=None):
        ledger = ledger or DemandLedger(":memory:")
        bill = ledger.issue("E1", "2026-10", peak_kw=120,
                            reason="按离线瞬时最大值暂签", rate_per_kw=40)
        self.assertEqual(bill["version"], 1)
        return ledger, bill

    def test_issued_bill_is_immutable_at_method_and_database_layer(self):
        ledger, v1 = self._issued_v1_120()
        with self.assertRaises(BillImmutableError):
            ledger.issue("E1", "2026-10", peak_kw=1, version=1)
        with self.assertRaises(sqlite3.Error):
            ledger.db.execute("UPDATE demand_bills SET peak_kw=1 WHERE version=1")
        with self.assertRaises(sqlite3.Error):
            ledger.db.execute("DELETE FROM demand_bills WHERE version=1")
        ledger.db.rollback()
        # untouched
        self.assertEqual(get_bill(ledger, "E1", "2026-10", 1)["peak_kw"], 120.0)

    def test_path_a_correction_keeps_old_120_surcharge_readable(self):
        ledger, _ = self._issued_v1_120()
        v2 = correct_bill(ledger, "E1", "2026-10",
                          load_scenario(self.fx, "path_a"),
                          receipt_ids=["SRV-INTERVAL-A-540"], rate_per_kw=40)
        self.assertEqual(v2["version"], 2)
        self.assertAlmostEqual(v2["peak_kw"], 96.0)
        self.assertEqual(v2["tier"], "standard")
        self.assertFalse(v2["surcharge"])
        self.assertEqual(v2["surcharge_amount"], 0.0)
        self.assertEqual(v2["prior_peak_kw"], 120.0)
        self.assertEqual(v2["prior_tier"], "peak_surcharge")
        # old monthly bill still reads exactly as issued, with its old tier.
        old = get_bill(ledger, "E1", "2026-10", 1)
        self.assertEqual(old["peak_kw"], 120.0)
        self.assertEqual(old["tier"], "peak_surcharge")
        self.assertTrue(old["surcharge"])
        self.assertEqual(old["surcharge_amount"], 4800.0)
        history = bill_history(ledger, "E1", "2026-10")
        self.assertEqual([b["version"] for b in history], [1, 2])
        self.assertEqual(history[0]["tier"], "peak_surcharge")
        self.assertEqual(history[-1]["tier"], "standard")

    def test_path_b_correction_recomputes_to_108_surcharge(self):
        ledger, _ = self._issued_v1_120()
        v2 = ledger.correct("E1", "2026-10", load_scenario(self.fx, "path_b"),
                            rate_per_kw=40)
        self.assertAlmostEqual(v2["peak_kw"], 108.0)
        self.assertEqual(v2["tier"], "peak_surcharge")
        self.assertTrue(v2["surcharge"])
        self.assertEqual(v2["surcharge_amount"], 4320.0)
        # recomputed basis, not a money-only adjustment: 120 transient is gone
        latest = ledger.get("E1", "2026-10")
        self.assertNotEqual(latest["peak_kw"], 120.0)
        self.assertEqual(latest["peak_kw"], 108.0)
        # old tier remains on v1
        self.assertEqual(ledger.get("E1", "2026-10", 1)["tier"], "peak_surcharge")

    def test_late_receipt_changes_only_affected_period(self):
        ledger, _ = self._issued_v1_120()
        ledger.issue("E1", "2026-09", peak_kw=88, rate_per_kw=40)
        ledger.correct("E1", "2026-10", load_scenario(self.fx, "path_a"))
        sep = ledger.get("E1", "2026-09")
        self.assertEqual(sep["version"], 1)
        self.assertEqual(sep["peak_kw"], 88.0)
        self.assertEqual(sep["tier"], "standard")
        self.assertEqual(len(ledger.history("E1", "2026-09")), 1)
        self.assertEqual(len(ledger.history("E1", "2026-10")), 2)

    def test_pending_evidence_does_not_overwrite_and_book_keeps_status(self):
        ledger, _ = self._issued_v1_120()
        v2 = ledger.correct("E1", "2026-10", self.fx)  # still 待核 base records
        self.assertEqual(v2["version"], 2)
        self.assertIsNone(v2["peak_kw"])
        self.assertEqual(v2["status"], "pending")
        self.assertEqual(v2["status"], "待核")
        # v1 remains the last settled/issued numeric bill
        self.assertEqual(ledger.get("E1", "2026-10", 1)["peak_kw"], 120.0)

    def test_in_memory_billbook_alias_flow(self):
        book = BillBook()
        issue_bill(book, "E1", "2026-10", peak_kw=120)
        correct_bill(book, "E1", "2026-10", peak_kw=96, tier="standard",
                     surcharge=False)
        self.assertEqual([b["version"] for b in bill_history(book, "E1", "2026-10")], [1, 2])
        self.assertEqual(get_bill(book, "E1", "2026-10", 1)["peak_kw"], 120)
        with self.assertRaises(BillImmutableError):
            book.issue("E1", "2026-10", peak_kw=1, version=1)


class MainBusinessIsolationTest(unittest.TestCase):
