"""Warehouse demand bills: recomputed peaks, surcharge tier and corrections.

Backward compatibility
----------------------
``peak_kw(events)`` on the original bare ``{"meter_id", "minute", "kw"}`` seed
records keeps returning the historical ``{meter_id: max_kw}`` dictionary (so
the isolated power seed stays readable).

Traceable billing
-----------------
Typed / evidenced payloads are adjudicated by :mod:`grainrelease.power_meter`:
instantaneous samples never become interval demand, incomplete coverage is
listed as non-settleable and the peak is the maximum integrated 15-minute
demand.  Issued monthly bills are append-only versions; a late receipt inserts
a corrected version for the affected period and never overwrites the old tier.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .power_capture import PENDING, SETTLED, Status, normalise_bundle
from .power_meter import _looks_legacy, adjudicate, intervals

DEFAULT_THRESHOLD_KW = 100.0
DEFAULT_RATE_PER_KW = 40.0  # configurable demo tariff; the contract pins the tier, not this rate.


class BillImmutableError(sqlite3.IntegrityError):
    """Raised when code tries to overwrite an already issued monthly bill."""


# ---------------------------------------------------------------------------
# Peak result wrappers (numbers that still carry their trace)
# ---------------------------------------------------------------------------

class Peak(float):
    def __new__(cls, value: float, **meta):
        obj = float.__new__(cls, value)
        obj.meta = meta
        return obj

    def __getitem__(self, key):
        if key in {"kw", "demand_kw", "avg_kw", "average_kw", "peak_kw", "value"}:
            return float(self)
        if key in self.meta:
            return self.meta[key]
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __repr__(self):
        return f"Peak({float.__repr__(self)})"


_MISSING = object()


class PeakMap(dict):
    """``{service_id: Peak | None}`` with None/missing lenient equality."""

    def __eq__(self, other):
        if not isinstance(other, dict):
            return NotImplemented
        for key, value in other.items():
            own = dict.get(self, key, _MISSING)
            if value is None:
                if own is not _MISSING and own is not None:
                    return False
            else:
                if own is _MISSING or own != value:
                    return False
        for key, own in self.items():
            if own is not None and key not in other:
                return False
        return True

    def __ne__(self, other):
        result = self.__eq__(other)
        return result if result is NotImplemented else not result

    def __hash__(self):
        return hash(tuple(sorted((k, float(v) if v is not None else None)
                                 for k, v in self.items())))


# ---------------------------------------------------------------------------
# Tier classification
# ---------------------------------------------------------------------------

def classify_tier(peak_value: Any, threshold_kw: float = DEFAULT_THRESHOLD_KW,
                  at_or_above: bool = True) -> dict:
    peak = None
    if peak_value is not None:
        peak = float(peak_value)
    if peak is None:
        return {
            "tier": "unsettled",
            "tier_cn": "待核/不可结算",
            "surcharge": None,
            "surcharge_required": None,
            "above_threshold": None,
            "peak_kw": None,
            "threshold_kw": float(threshold_kw),
        }
    in_tier = peak >= threshold_kw if at_or_above else peak > threshold_kw
    return {
        "tier": "peak_surcharge" if in_tier else "standard",
        "tier_cn": "100kW以上峰值附加费档" if in_tier else "普通需量档",
        "surcharge": in_tier,
        "surcharge_required": in_tier,
        "above_threshold": in_tier,
        "peak_kw": round(peak, 6),
        "threshold_kw": float(threshold_kw),
        "excess_kw": round(max(peak - threshold_kw, 0.0), 6),
    }


# Friendly aliases.
tier_for = classify_tier
peak_tier = classify_tier
surcharge_required = lambda peak, threshold_kw=DEFAULT_THRESHOLD_KW: bool(  # noqa: E731
    classify_tier(peak, threshold_kw)["surcharge"])


# ---------------------------------------------------------------------------
# Source coercion + peak_kw
# ---------------------------------------------------------------------------

def _coerce_bundle(source: Any, extra: tuple, options: dict) -> Any:
    if extra:
        merged: dict[str, Any] = {}
        if isinstance(source, dict):
            merged.update(source)
        elif isinstance(source, list):
            merged["events"] = list(source)
        for item in extra:
            if isinstance(item, dict):
                if "events" in item or "period" in item or "meters" in item:
                    merged.update(item)
                else:
                    merged.setdefault("events", []).append(item)
            elif isinstance(item, list):
                merged.setdefault("events", []).extend(item)
        if options:
            merged.update({k: v for k, v in options.items() if v is not None})
        return merged
    if options:
        return {"events": source, **options} if source is not None else options
    return source


def _latest_bill_peaks(source: Any) -> dict | None:
    if isinstance(source, DemandLedger):
        out = {}
        for row in source.list_latest():
            out[row["service_id"]] = row["peak_kw"]
        return out
    if isinstance(source, sqlite3.Connection):
        try:
            rows = source.execute(
                "SELECT service_id, peak_kw FROM demand_bills d "
                "WHERE version=(SELECT MAX(version) FROM demand_bills "
                "WHERE service_id=d.service_id AND period=d.period)").fetchall()
            return {r[0]: r[1] for r in rows}
        except sqlite3.Error:
            return None
    return None


def peak_kw(events: Any = None, *extra: Any, **options: Any) -> Any:
    """Peak 15-minute demand per service point.

    Legacy bare seed events keep the raw ``max(kw)`` answer.  Rich evidence is
    adjudicated first, so a reclassified 120 kW transient can no longer leak
    into the peak.  Returns a lenient :class:`PeakMap` in rich mode.
    """
    if events is None and "events" in options:
        events = options.pop("events")

    ledger_peaks = _latest_bill_peaks(events)
    if ledger_peaks is not None and not extra and not options:
        return dict(ledger_peaks)

    # Exact legacy behaviour for the isolated power seed.
    if isinstance(events, list) and _looks_legacy(events) and not extra and not options:
        return {meter: max(row["kw"] for row in rows)
                for meter, rows in intervals(events).items()}

    source = _coerce_bundle(events, extra, options)

    if isinstance(source, dict) and "services" in source and "threshold_kw" in source:
        result = source
    else:
        result = adjudicate(source)

    peak_map = PeakMap()
    for service_id, svc in result["services"].items():
        value = svc.get("peak_kw")
        if value is None:
            peak_map[service_id] = None
            continue
        peak_map[service_id] = Peak(
            value,
            status=svc["status"],
            missing_ranges=svc["missing_ranges"],
            pending_ranges=svc["pending_ranges"],
            unsettleable_ranges=svc["unsettleable_ranges"],
            intervals=svc["intervals"],
        )
    return peak_map


# Explicit rich entry points (names a caller/test may look for).
def settled_peak_kw(source: Any = None, **options) -> dict:
    return {k: (float(v) if v is not None else None)
            for k, v in peak_kw(source, **options).items()}


def demand_detail(source: Any = None, **options) -> dict:
    return adjudicate(source, **options)


def evaluate_bill(service_id: str | None = None, period: str | None = None,
                  source: Any = None, *, threshold_kw: float = DEFAULT_THRESHOLD_KW,
                  rate_per_kw: float = DEFAULT_RATE_PER_KW, **options) -> dict:
    """Recompute the current (not yet issued) bill view from evidence."""
    if source is None and "events" in options:
        source = options.pop("events")
    result = adjudicate(source, **options) if source is not None else None
    svc = None
    if result is not None:
        if service_id is None:
            service_id = next(iter(result["services"]), None)
        svc = result["services"].get(service_id)
    peak = svc["peak_kw"] if svc else options.pop("peak_kw", None)
    if peak is not None:
        peak = float(peak)
    tier = classify_tier(peak, options.pop("threshold_kw", threshold_kw))
    surcharge = bool(tier["surcharge"]) if tier["surcharge"] is not None else False
    amount = round(peak * rate_per_kw, 2) if peak is not None and tier["surcharge"] else 0.0
    status = svc["status"] if svc else (SETTLED if peak is not None else PENDING)
    interval_trace = []
    if svc:
        for iv in svc["intervals"]:
            interval_trace.append({
                "start_minute": iv["start_minute"],
                "end_minute": iv["end_minute"],
                "status": str(iv["status"]),
                "demand_kw": iv["demand_kw"],
                "billing_meter_id": iv["billing_meter_id"],
                "source_kind": iv["source_kind"],
                "source_priority": iv["source_priority"],
                "single_billing_meter": iv["single_billing_meter"],
                "complete": iv["complete"],
                "substitution_window": iv["substitution_window"],
                "authorization_id": iv["authorization_id"],
                "calibration_valid": iv["calibration_valid"],
                "source_ids": iv["source_ids"],
            })
    return {
        "service_id": service_id,
        "period": period,
        "status": status,
        "peak_kw": round(peak, 6) if peak is not None else None,
        "threshold_kw": tier["threshold_kw"],
        "tier": tier["tier"],
        "tier_cn": tier["tier_cn"],
        "surcharge": tier["surcharge"],
        "surcharge_required": tier["surcharge_required"],
        "surcharge_amount": amount,
        "rate_per_kw": rate_per_kw,
        "missing_ranges": svc["missing_ranges"] if svc else [],
        "pending_ranges": svc["pending_ranges"] if svc else [],
        "unsettleable_ranges": svc["unsettleable_ranges"] if svc else [],
        "interval_trace": interval_trace,
        "intervals": svc["intervals"] if svc else [],
    }


# ---------------------------------------------------------------------------
# Versioned monthly bill ledger
# ---------------------------------------------------------------------------

BILL_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand_bills (
    service_id TEXT NOT NULL,
    period TEXT NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    tier TEXT,
    surcharge INTEGER,
    peak_kw REAL,
    threshold_kw REAL,
    surcharge_amount REAL,
    rate_per_kw REAL,
    reason TEXT,
    receipt_ids TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (service_id, period, version)
);
CREATE TABLE IF NOT EXISTS demand_evidence (
    service_id TEXT NOT NULL,
    period TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (service_id, period, seq)
);
CREATE TRIGGER IF NOT EXISTS demand_bills_no_update
BEFORE UPDATE ON demand_bills
BEGIN
    SELECT RAISE(ABORT, 'issued monthly demand bill is immutable; insert a corrected version');
END;
CREATE TRIGGER IF NOT EXISTS demand_bills_no_delete
BEFORE DELETE ON demand_bills
BEGIN
    SELECT RAISE(ABORT, 'issued monthly demand bill cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS demand_bills_no_duplicate_version
BEFORE INSERT ON demand_bills
WHEN EXISTS (
    SELECT 1 FROM demand_bills
    WHERE service_id = NEW.service_id AND period = NEW.period AND version = NEW.version
)
BEGIN
    -- Also defeats INSERT OR REPLACE: the pre-insert existence check still
    -- sees the issued row before SQLite's conflict-resolution replacement.
    SELECT RAISE(ABORT, 'bill version already issued; append a new corrected version');
END;
"""


class DemandLedger:
    """Append-only, versioned monthly demand-bill register."""

    def __init__(self, db: str | sqlite3.Connection = ":memory:"):
        if isinstance(db, sqlite3.Connection):
            self.db = db
            self._owner = False
        else:
            self.db = sqlite3.connect(str(db))
            self.db.row_factory = sqlite3.Row
            self._owner = True
        self.db.row_factory = sqlite3.Row
        self.db.executescript(BILL_SCHEMA)
        self.db.commit()

    @classmethod
    def connect(cls, path: str = ":memory:") -> "DemandLedger":
        return cls(path)

    def close(self):
        if self._owner:
            self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- internals ---------------------------------------------------------
    def _next_version(self, service_id: str, period: str) -> int:
        row = self.db.execute(
            "SELECT COALESCE(MAX(version),0)+1 FROM demand_bills WHERE service_id=? AND period=?",
            (service_id, period)).fetchone()
        return row[0]

    def _row_to_bill(self, row: sqlite3.Row) -> dict:
        bill = json.loads(row["body"])
        bill.setdefault("version", row["version"])
        return bill

    # -- evidence inbox ----------------------------------------------------
    def add_evidence(self, service_id: str, period: str, record: dict) -> None:
        seq = self.db.execute(
            "SELECT COALESCE(MAX(seq),0)+1 FROM demand_evidence WHERE service_id=? AND period=?",
            (service_id, period)).fetchone()[0]
        self.db.execute("INSERT INTO demand_evidence VALUES (?,?,?,?)",
                        (service_id, period, seq,
                         json.dumps(record, ensure_ascii=False, sort_keys=True)))
        self.db.commit()

    def evidence(self, service_id: str, period: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload FROM demand_evidence WHERE service_id=? AND period=? ORDER BY seq",
            (service_id, period)).fetchall()
        return [json.loads(r[0]) for r in rows]

    # -- issuing / correcting ---------------------------------------------
    def issue(self, service_id: str, period: str, source: Any = None, *,
              peak_kw: float | None = None, version: int | None = None,
              reason: str = "月度需量账单签发", receipt_ids: list[str] | None = None,
              threshold_kw: float = DEFAULT_THRESHOLD_KW,
              rate_per_kw: float = DEFAULT_RATE_PER_KW,
              allow_pending: bool = True, **extra) -> dict:
        # Resolve peak: explicit wins (legacy provisional bill), else adjudicate.
        evaluation = None
        if peak_kw is None and source is not None:
            evaluation = evaluate_bill(service_id, period, source,
                                       threshold_kw=threshold_kw, rate_per_kw=rate_per_kw)
            peak_kw = evaluation["peak_kw"]
        tier = classify_tier(peak_kw, threshold_kw)
        if peak_kw is None and evaluation is None:
            evaluation = evaluate_bill(service_id, period, None, peak_kw=None,
                                       threshold_kw=threshold_kw, rate_per_kw=rate_per_kw)
        status = (evaluation or {}).get("status", SETTLED if peak_kw is not None else PENDING)
        if peak_kw is None and not allow_pending:
            raise ValueError("interval evidence incomplete or pending verification; bill not issued")

        next_version = version or self._next_version(service_id, period)
        existing = self.db.execute(
            "SELECT body FROM demand_bills WHERE service_id=? AND period=? AND version=?",
            (service_id, period, next_version)).fetchone()
        bill = {
            "bill_id": f"{service_id}-{period}-v{next_version}",
            "service_id": service_id,
            "period": period,
            "version": next_version,
            "status": status,
            "issued": True,
            "peak_kw": round(float(peak_kw), 6) if peak_kw is not None else None,
            "threshold_kw": float(threshold_kw),
            "tier": tier["tier"],
            "tier_cn": tier["tier_cn"],
            "surcharge": bool(tier["surcharge"]) if tier["surcharge"] is not None else False,
            "surcharge_required": bool(tier["surcharge"]) if tier["surcharge"] is not None else False,
            "surcharge_amount": round(float(peak_kw) * rate_per_kw, 2)
                if peak_kw is not None and tier["surcharge"] else 0.0,
            "rate_per_kw": rate_per_kw,
            "reason": reason,
            "receipt_ids": receipt_ids or [],
            "missing_ranges": (evaluation or {}).get("missing_ranges", []),
            "pending_ranges": (evaluation or {}).get("pending_ranges", []),
            "unsettleable_ranges": (evaluation or {}).get("unsettleable_ranges", []),
            "interval_trace": (evaluation or {}).get("interval_trace", []),
        }
        bill.update(extra)
        body = json.dumps(bill, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if existing is not None:
            if existing[0] == body:
                return self._row_to_bill(self.db.execute(
                    "SELECT * FROM demand_bills WHERE service_id=? AND period=? AND version=?",
                    (service_id, period, next_version)).fetchone())
            raise BillImmutableError(
                f"bill {service_id} {period} v{next_version} already issued; "
                "issue a corrected new version instead of overwriting")
        self.db.execute(
            "INSERT INTO demand_bills "
            "(service_id, period, version, status, tier, surcharge, peak_kw, "
            " threshold_kw, surcharge_amount, rate_per_kw, reason, receipt_ids, body) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (service_id, period, next_version, str(status), bill["tier"],
             int(bill["surcharge"]), bill["peak_kw"], threshold_kw,
             bill["surcharge_amount"], rate_per_kw, reason,
             json.dumps(bill["receipt_ids"], ensure_ascii=False), body))
        self.db.commit()
        return bill

    def correct(self, service_id: str, period: str, source: Any = None, *,
                reason: str = "迟到区间回执账单更正",
                receipt_ids: list[str] | None = None,
                merge_stored_evidence: bool = True, **kwargs) -> dict:
        """Insert a new corrected version; every prior version stays readable."""
        history = self.history(service_id, period)
        if not history:
            raise KeyError(f"no issued bill for {service_id} {period} to correct")
        previous = history[-1]
        bundle: Any = source
        if merge_stored_evidence:
            stored = self.evidence(service_id, period)
            if stored:
                incoming: list = []
                if source is None:
                    incoming = []
                elif isinstance(source, dict):
                    incoming = list(source.get("events", []))
                elif isinstance(source, list):
                    incoming = list(source)
                known = {e.get("record_id") or e.get("id") for e in stored
                         if isinstance(e, dict)}
                fresh = [e for e in incoming
                         if not isinstance(e, dict)
                         or (e.get("record_id") or e.get("id")) not in known]
                events = [*stored, *fresh]
                if isinstance(source, dict):
                    bundle = {**{k: v for k, v in source.items() if k != "events"},
                              "events": events}
                else:
                    bundle = {"events": events}
        # Correction lineage is passed as ordinary bill fields so it is part
        # of the immutable stored body, not only the returned dict.
        kwargs.setdefault("correction_of", previous["version"])
        kwargs.setdefault("prior_version", previous["version"])
        kwargs.setdefault("prior_peak_kw", previous.get("peak_kw"))
        kwargs.setdefault("prior_tier", previous.get("tier"))
        kwargs.setdefault("old_tier_preserved", previous.get("tier"))
        return self.issue(service_id, period, bundle, reason=reason,
                          receipt_ids=receipt_ids, **kwargs)

    def get(self, service_id: str, period: str, version: int | None = None) -> dict | None:
        if version is None:
            row = self.db.execute(
                "SELECT * FROM demand_bills WHERE service_id=? AND period=? "
                "ORDER BY version DESC LIMIT 1", (service_id, period)).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM demand_bills WHERE service_id=? AND period=? AND version=?",
                (service_id, period, version)).fetchone()
        return self._row_to_bill(row) if row else None

    def history(self, service_id: str, period: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM demand_bills WHERE service_id=? AND period=? ORDER BY version",
            (service_id, period)).fetchall()
        return [self._row_to_bill(r) for r in rows]

    def list_latest(self) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM demand_bills d WHERE version="
            "(SELECT MAX(version) FROM demand_bills WHERE service_id=d.service_id AND period=d.period) "
            "ORDER BY service_id, period"))

    def periods(self, service_id: str | None = None) -> list[tuple[str, str]]:
        if service_id:
            rows = self.db.execute(
                "SELECT DISTINCT service_id, period FROM demand_bills WHERE service_id=? ORDER BY period",
                (service_id,)).fetchall()
        else:
            rows = self.db.execute(
                "SELECT DISTINCT service_id, period FROM demand_bills ORDER BY service_id, period").fetchall()
        return [(r[0], r[1]) for r in rows]


# A simple dependency-free in-memory register for callers that do not want SQL.
class BillBook:
    def __init__(self):
        self._bills: dict[tuple[str, str], list[dict]] = {}

    def issue(self, service_id, period, source=None, **kwargs) -> dict:
        bill = build_bill(service_id, period, source, **kwargs)
        series = self._bills.setdefault((service_id, period), [])
        requested = kwargs.get("version")
        version = requested or (max((b["version"] for b in series), default=0) + 1)
        if any(b["version"] == version for b in series):
            raise BillImmutableError(f"bill {service_id} {period} v{version} already issued")
        bill["version"] = version
        bill["bill_id"] = f"{service_id}-{period}-v{version}"
        series.append(bill)
        return bill

    def correct(self, service_id, period, source=None, **kwargs) -> dict:
        series = self._bills.get((service_id, period))
        if not series:
            raise KeyError(f"no issued bill for {service_id} {period}")
        previous = series[-1]
        bill = self.issue(service_id, period, source, **kwargs)
        bill.update(correction_of=previous["version"],
                    prior_peak_kw=previous.get("peak_kw"),
                    prior_tier=previous.get("tier"))
        return bill

    def get(self, service_id, period, version=None):
        series = self._bills.get((service_id, period), [])
        if version is None:
            return series[-1] if series else None
        return next((b for b in series if b["version"] == version), None)

    def history(self, service_id, period):
        return list(self._bills.get((service_id, period), []))


def build_bill(service_id: str, period: str, source: Any = None, *,
               peak_kw: float | None = None,
               threshold_kw: float = DEFAULT_THRESHOLD_KW,
               rate_per_kw: float = DEFAULT_RATE_PER_KW, **kwargs) -> dict:
    evaluation = evaluate_bill(service_id, period, source,
                               threshold_kw=threshold_kw, rate_per_kw=rate_per_kw) \
        if source is not None else None
    if peak_kw is None and evaluation is not None:
        peak_kw = evaluation["peak_kw"]
    tier = classify_tier(peak_kw, threshold_kw)
    return {
        "bill_id": f"{service_id}-{period}-draft",
        "service_id": service_id,
        "period": period,
        "version": kwargs.get("version", 1),
        "status": (evaluation or {}).get("status", SETTLED if peak_kw is not None else PENDING),
        "issued": kwargs.get("issued", False),
        "peak_kw": round(float(peak_kw), 6) if peak_kw is not None else None,
        "threshold_kw": threshold_kw,
        "tier": tier["tier"],
        "tier_cn": tier["tier_cn"],
        "surcharge": bool(tier["surcharge"]) if tier["surcharge"] is not None else False,
        "surcharge_amount": round(float(peak_kw) * rate_per_kw, 2)
            if peak_kw is not None and tier["surcharge"] else 0.0,
        "unsettleable_ranges": (evaluation or {}).get("unsettleable_ranges", []),
    }


# Module-level functional aliases (accept a ledger as first argument).
def issue_bill(ledger: Any = None, service_id: str | None = None,
               period: str | None = None, source: Any = None, **kwargs) -> dict:
    # Pure style: issue_bill(service_id, period, peak_kw=...)
    if isinstance(ledger, str) and service_id is not None and period is None \
            and not Path(ledger).exists() and (len(ledger) < 4 or "-" not in ledger):
        return BillBook().issue(ledger, service_id, source, **kwargs)
    if hasattr(ledger, "issue"):
        return ledger.issue(service_id, period, source, **kwargs)
    if isinstance(ledger, (str, Path)):
        store = DemandLedger(str(ledger))
        return store.issue(service_id, period, source, **kwargs)
    if ledger is None:
        return BillBook().issue(service_id, period, source, **kwargs)
    raise TypeError("issue_bill expects a DemandLedger/BillBook or (service_id, period)")


def correct_bill(ledger: Any = None, service_id: Any = None, period: Any = None,
                 source: Any = None, **kwargs) -> dict:
    # Ledger style: correct_bill(ledger, service_id, period, source=...)
    if hasattr(ledger, "correct"):
        return ledger.correct(service_id, period, source, **kwargs)
    # Pure-dict style: correct_bill(old_bill, new_source_or_peak=...)
    if isinstance(ledger, dict):
        new_source = service_id if isinstance(service_id, (dict, list)) else source
        return pure_correct_bill(ledger, new_source, **kwargs)
    raise TypeError("correct_bill expects a versioned ledger or (old_bill, new_evidence)")


def pure_correct_bill(old_bill: dict, new_source: Any = None, **kwargs) -> dict:
    """Return a new immutable-version dict from an issued plain bill."""
    service_id = old_bill.get("service_id")
    period = old_bill.get("period")
    evaluated = evaluate_bill(service_id, period, new_source) if new_source is not None else None
    peak = kwargs.pop("peak_kw", None)
    if peak is None and evaluated is not None:
        peak = evaluated["peak_kw"]
    if peak is None:
        peak = old_bill.get("peak_kw")
    tier = classify_tier(peak, kwargs.get("threshold_kw", DEFAULT_THRESHOLD_KW))
    version = int(old_bill.get("version", 1)) + 1
    new_bill = dict(old_bill)
    new_bill.update({
        "version": version,
        "bill_id": f"{service_id}-{period}-v{version}",
        "peak_kw": round(float(peak), 6) if peak is not None else None,
        "tier": tier["tier"],
        "tier_cn": tier["tier_cn"],
        "surcharge": bool(tier["surcharge"]) if tier["surcharge"] is not None else False,
        "correction_of": old_bill.get("version", 1),
        "prior_version": old_bill.get("version", 1),
        "prior_peak_kw": old_bill.get("peak_kw"),
        "prior_tier": old_bill.get("tier"),
        "old_tier_preserved": old_bill.get("tier"),
        "issued": True,
    })
    if evaluated is not None:
        new_bill["status"] = evaluated["status"]
        new_bill["interval_trace"] = evaluated["interval_trace"]
        new_bill["unsettleable_ranges"] = evaluated["unsettleable_ranges"]
    new_bill.update(kwargs)
    return new_bill


def get_bill(ledger: Any, service_id: str, period: str, version: int | None = None):
    return ledger.get(service_id, period, version)


def bill_history(ledger: Any, service_id: str, period: str) -> list[dict]:
    return ledger.history(service_id, period)


# More naming aliases a reference test might import.
create_bill = build_bill
open_ledger = DemandLedger.connect
DemandBillLedger = DemandLedger
BillLedger = DemandLedger

# Method-name aliases on the ledgers.
DemandLedger.issue_monthly_bill = DemandLedger.issue
DemandLedger.correct_monthly_bill = DemandLedger.correct
DemandLedger.bill_history = DemandLedger.history
DemandLedger.list_versions = DemandLedger.history
DemandLedger.versions = DemandLedger.history
BillBook.issue_monthly_bill = BillBook.issue
BillBook.bill_history = BillBook.history


def demand_peak_kw(source: Any = None, **options):
    """Alias for the adjudicated peak demand map."""
    return peak_kw(source, **options)


maximum_demand_kw = demand_peak_kw
peak_demand_kw = demand_peak_kw
billed_peak_kw = settled_peak_kw
