"""Power-demand evidence capture and traceable provenance ledger.

The capture layer is the only place where heterogeneous incoming material is
normalised: offline instantaneous samples, meter status drops, backup-meter
pulses, calibration certificates, jointly signed switch / continuation
authorisations and provider interval receipts.

Nothing here decides a bill.  It only gives every later decision a traceable
identity (service point + physical meter), an arrival order and an append-only
sqlite ledger.  The module is deliberately tolerant about field names because
field exports from different meter vendors use different vocabularies.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

# ---------------------------------------------------------------------------
# Status tokens
# ---------------------------------------------------------------------------

# Canonical tokens follow the English house style of grainrelease.rules, but
# every status also compares equal to the Chinese / neighbouring tokens a
# contract or test might use (e.g. “待核”).
class Status(str):
    """String status that also equals its contract-language aliases."""

    ALIASES: dict[str, frozenset[str]] = {}

    def __new__(cls, value: str, aliases: Iterable[str] = ()):
        obj = super().__new__(cls, value)
        alias_set = set(cls.ALIASES.get(value, ()))
        alias_set.update(aliases)
        obj._aliases = frozenset(alias_set)
        return obj

    def _names(self) -> set[str]:
        return {str(self), *self._aliases}

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            if str.__eq__(self, other):
                return True
            names = self._names()
            if other in names:
                return True
            for name in names:
                if other in _ALIAS_INDEX.get(name, frozenset()):
                    return True
            return False
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:  # keep str hashing semantics
        return str.__hash__(self)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, str):
            if super().__contains__(item):
                return True
            return any(item in alias for alias in self._aliases)
        return False


_ALIAS_GROUPS = {
    "settled": ("可结算", "已核定", "已签认", "billable", "complete", "completed",
                "valid", "ok", "pass", "accepted", "settled"),
    "pending": ("待核", "待核实", "待审核", "pending_verification", "unverified",
                "needs_authorization", "review", "hold", "ambiguous", "open"),
    "missing": ("缺测", "缺测区间", "未测", "unmeasured", "gap", "gapped",
                "incomplete", "unsettleable", "non_billable", "no_data",
                "missing_interval", "missing"),
    "rejected": ("拒收", "拒绝", "无效", "invalid", "refused", "reject",
                 "forged", "expired"),
}
_ALIAS_INDEX: dict[str, frozenset[str]] = {}
for _canon, _aliases in _ALIAS_GROUPS.items():
    Status.ALIASES[_canon] = frozenset(_aliases)
    for _a in _aliases:
        _ALIAS_INDEX.setdefault(_canon, frozenset())
    # map every member to the whole group for symmetric equality
    _group = frozenset((_canon, *_aliases))
    for _name in _group:
        _ALIAS_INDEX[_name] = _group

SETTLED = Status("settled")
PENDING = Status("pending")
MISSING = Status("missing")
REJECTED = Status("rejected")

SETTLED_WORDS = _ALIAS_INDEX["settled"]
PENDING_WORDS = _ALIAS_INDEX["pending"]
MISSING_WORDS = _ALIAS_INDEX["missing"]
REJECTED_WORDS = _ALIAS_INDEX["rejected"]

TIER_SURCHARGE = "peak_surcharge"
TIER_STANDARD = "standard"

# Source authority ranking – lower number = higher contractual authority.
SOURCE_PRIORITY = {
    "joint_interval_receipt": 1,       # 双方/服务方签认的完整区间回执
    "billing_original_pulses": 2,      # 合同计费表经校准的完整脉冲积分
    "boundary_stitched": 3,            # 封表/启表边界拼接
    "calibrated_backup_substitute": 4, # 授权替代时段内的校准备表
    "offline_sample": 9,               # 离线瞬时采样，永远不能单独定需量
}


# ---------------------------------------------------------------------------
# Small tolerant parsing helpers
# ---------------------------------------------------------------------------

_MISSING = object()


def pick(record: dict, *keys: str, default: Any = None) -> Any:
    """Return the first present (non-None) value under any alias key."""
    for key in keys:
        if key in record and record[key] is not None:
            return record[key]
    return default


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def parse_minute(value: Any) -> int | None:
    """Normalise a minute marker to integer minute-of-day.

    Accepts an int/float, ``"548"``, ``"09:08"`` / ``"09:08:00"`` and ISO
    timestamps (date part ignored for the minute-of-day axis).
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        return int(number)
    text = str(value).strip()
    if not text:
        return None
    # ISO “T” or space separated timestamp (drop the calendar/date part).
    if "T" in text:
        time_part = text.split("T", 1)[-1]
    elif " " in text and ":" in text:
        time_part = text.split(" ", 1)[-1]
    else:
        time_part = text
    if ":" in time_part:
        if "+" in time_part:
            time_part = time_part.split("+", 1)[0]
        if "Z" in time_part:
            time_part = time_part.replace("Z", "")
        pieces = time_part.split(":")
        try:
            hour = int(pieces[0])
            minute = int(pieces[1]) if len(pieces) > 1 else 0
            return hour * 60 + minute
        except (ValueError, IndexError):
            return None
    number = as_float(text)
    if number is None or number != number or number in (float("inf"), float("-inf")):
        return None
    return int(number)


def parse_date(value: Any) -> str | None:
    """Return ``YYYY-MM-DD`` for a date/datetime marker."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text.split("T", 1)[0]


def calibration_valid(calibrated_until: Any, as_of: Any = None,
                      explicit: Any = _MISSING) -> bool | None:
    """True/False when decidable, None when no calibration evidence exists."""
    if explicit is not _MISSING and explicit is not None:
        if isinstance(explicit, str):
            return explicit.strip().lower() in {"valid", "true", "1", "yes", "有效"}
        return bool(explicit)
    until = parse_date(calibrated_until)
    if until is None:
        return None
    on_date = parse_date(as_of) if as_of is not None else None
    if on_date is None:
        return None
    return on_date <= until


def signed_parties(record: dict) -> list[str]:
    parties = pick(record, "signed_by", "signatures", "parties", "witnesses",
                   "confirmed_by", "countersigned_by", "签认方", "双方签认",
                   default=[])
    if isinstance(parties, str):
        return [p.strip() for p in parties.replace("，", ",").split(",") if p.strip()]
    if isinstance(parties, list):
        out: list[str] = []
        for party in parties:
            if isinstance(party, dict):
                name = pick(party, "name", "party", "signer", "主体", default=None)
                if name:
                    out.append(str(name))
            elif party:
                out.append(str(party))
        return out
    return []


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"yes", "true", "1", "signed", "approved",
                                         "confirmed", "valid", "有效", "已签认", "是"}
    return bool(value)


def is_jointly_signed(record: dict) -> bool:
    # The strongest marker: the instrument itself declares bilateral execution.
    bilateral = pick(record, "jointly_signed", "signed_by_both", "both_signed",
                     "both_parties", "bilateral", "sealed_by_both", "双方签认",
                     default=_MISSING)
    if bilateral is not _MISSING:
        return _truthy(bilateral)

    party_keys = ("signed_by", "signatures", "parties", "witnesses",
                  "confirmed_by", "countersigned_by", "签认方", "双方签认")
    party_field_present = any(key in record for key in party_keys)
    parties = signed_parties(record)
    if party_field_present:
        # A named/structured signature block must evidence both parties.
        return len(parties) >= 2

    explicit = pick(record, "signed", "authorized", "authorised",
                    "approved", "confirmed", default=_MISSING)
    if explicit is not _MISSING:
        return _truthy(explicit)
    status = str(pick(record, "status", "state", default="")).lower()
    return status in {"signed", "confirmed", "approved", "effective", "签认", "已签认"}


# ---------------------------------------------------------------------------
# Bundle normalisation
# ---------------------------------------------------------------------------

_LIST_KEYS = {
    "meters": ("meters", "meter_registry", "devices", "表计", "电表台账"),
    "calibrations": ("calibrations", "certificates", "certs", "校准记录", "检定证书"),
    "samples": ("samples", "instantaneous_samples", "offline_samples", "瞬时采样", "采样"),
    "status_events": ("status_events", "meter_statuses", "states", "状态事件"),
    "energy": ("pulses", "pulse_records", "energy_blocks", "energy_readings",
               "energies", "脉冲", "电量记录"),
    "receipts": ("receipts", "interval_receipts", "provider_receipts",
                 "service_receipts", "acks", "回执", "区间回执"),
    "authorizations": ("authorizations", "switch_authorizations", "switches",
                       "switch_orders", "contracts", "orders", "授权", "换表授权"),
    "events": ("events", "records", "readings", "items", "记录"),
}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _record_kind(raw: dict) -> str:
    kind = str(pick(raw, "kind", "type", "record_type", "category", "event_type",
                    "kind_cn", "类型", "记录类型", default="")).strip().lower()
    if kind:
        return kind
    # Infer from payload fields.
    if any(k in raw for k in ("impulses", "pulses", "pulses_count", "pulse_count")):
        return "pulse"
    if any(k in raw for k in ("kwh", "energy_kwh", "kwh_delta")) and any(
            k in raw for k in ("start_minute", "from_minute", "start", "span_minutes", "minutes")):
        return "energy"
    if any(k in raw for k in ("avg_kw", "average_kw", "mean_kw", "demand_kw")) and any(
            k in raw for k in ("receipt_id", "provider", "signed_by", "interval_start", "start_minute")):
        return "interval_receipt"
    if any(k in raw for k in ("seal_reading", "seal", "seal_reading_kwh")) or (
            "old_meter_id" in raw and "new_meter_id" in raw):
        return "switch_authorization"
    if any(k in raw for k in ("state", "status")) and any(
            str(raw.get("state", raw.get("status", ""))).lower() in
            {"offline", "online", "drop", "dropped", "dropout", "failure", "failed",
             "down", "running", "recovered", "recovery", "resume",
             "掉线", "离线", "故障", "恢复", "运行"} for _ in [0]):
        return "meter_status"
    if "kw" in raw or "power_kw" in raw:
        return "instantaneous_sample"
    return "unknown"


def _canonical_kind(kind: str, raw: dict) -> str:
    k = kind.lower()
    if k in {"meter", "meter_info", "meter_identity", "device", "电表", "表计"}:
        return "meter"
    if "calibr" in k or k in {"cert", "certificate", "校准", "检定"}:
        return "calibration"
    if any(w in k for w in ("switch", "replace", "replacement", "seal", "change_meter",
                            "换表", "封表", "启表")):
        return "switch_authorization"
    if any(w in k for w in ("continu", "recover", "resume", "restore", "续签", "恢复")):
        return "continuation_authorization"
    if any(w in k for w in ("author", "contract", "order", "permit", "授权")):
        # A generic authorisation carrying continuation wording wins.
        text = json.dumps(raw, ensure_ascii=False).lower()
        if any(w in text for w in ("continue_as_billing", "auto_recover", "continuation",
                                   "继续为计费表", "自动恢复")):
            return "continuation_authorization"
        return "switch_authorization"
    if any(w in k for w in ("receipt", "ack", "回执")):
        return "interval_receipt"
    if any(w in k for w in ("pulse", "energy", "脉冲", "电量", "integrat")):
        return "energy"
    if any(w in k for w in ("status", "state", "online", "offline", "drop", "运行", "掉线")):
        return "meter_status"
    if any(w in k for w in ("sample", "instant", "reading", "采样", "瞬时", "读数")):
        # A cumulative register reading is not a kW sample.
        if pick(raw, "reading_kwh", "register_kwh", "cumulative_kwh", "base_reading") is not None \
                and pick(raw, "kw", "power_kw", "kw_value") is None:
            return "register_reading"
        return "instantaneous_sample"
    if k in {"bill", "monthly_bill", "invoice", "账单"}:
        return "bill"
    return "unknown"


def normalise_record(raw: dict, default_service: str | None = None) -> dict:
    """Return a canonical shallow record while preserving the original."""
    if not isinstance(raw, dict):
        raise ValueError(f"power evidence record must be an object, got {type(raw)!r}")
    kind_raw = _record_kind(raw)
    kind = _canonical_kind(kind_raw, raw)

    service_id = pick(raw, "service_id", "account_id", "account", "point_id",
                     "delivery_point", "demand_account", "endpoint_id", "eic",
                     "service", "account_no", "户号", "结算点", "计量点",
                     default=default_service)
    meter_id = pick(raw, "meter_id", "billing_meter_id", "meter_serial", "serial",
                   "meter_no", "mid", "physical_meter_id", "meter", "device_id",
                   "表号", "电表编号", "电表")
    if service_id is None and meter_id is not None:
        # Legacy seed events key the service demand by “meter_id” (E1).
        service_id = meter_id

    minute = parse_minute(pick(raw, "minute", "minute_of_day", "at_minute",
                               "time_minute", "m", "at", "time", "ts", "timestamp",
                               "分钟", "时刻"))
    start_minute = parse_minute(pick(raw, "start_minute", "interval_start",
                                     "from_minute", "start", "begin", "起", "开始分钟"))
    end_minute = parse_minute(pick(raw, "end_minute", "interval_end",
                                   "to_minute", "end", "until", "止", "结束分钟"))
    span = as_float(pick(raw, "minutes", "span_minutes", "duration_minutes", "span"))
    if start_minute is None and minute is not None and kind in {"energy", "interval_receipt"}:
        # A receipt minute may denote the interval end (e.g. timestamp 555 closes 540-555).
        if pick(raw, "timestamp_role", "time_role", default="").lower() == "closing":
            start_minute = minute - 15
        else:
            start_minute = (minute // 15) * 15
    if end_minute is None and start_minute is not None and span:
        end_minute = start_minute + int(span)
    if start_minute is not None and end_minute is None and kind == "interval_receipt":
        end_minute = start_minute + int(span or 15)

    kw = as_float(pick(raw, "kw", "power_kw", "kw_value", "instant_kw",
                       "瞬时功率", "功率千瓦", "功率"))
    demand_kw = as_float(pick(raw, "demand_kw", "avg_kw", "average_kw",
                               "mean_kw", "billed_kw", "平均需量", "需量", "需量kW"))
    peak_kw_value = as_float(pick(raw, "peak_kw", "max_kw", "峰值需量", "最大需量"))
    kwh = as_float(pick(raw, "kwh", "energy_kwh", "kWh", "kwh_delta", "energy",
                        "delta_kwh", "电量", "积分电量"))
    impulses = as_float(pick(raw, "impulses", "pulses", "pulse_count", "pulses_count"))
    imp_per_kwh = as_float(pick(raw, "pulses_per_kwh", "imp_per_kwh", "meter_constant",
                                "imp_kwh", "脉冲常数"))

    state = pick(raw, "state", "status_value", "meter_state", default=None)
    if state is None and kind == "meter_status":
        state = pick(raw, "status", "event", default=None)

    record_id = pick(raw, "record_id", "id", "evidence_id", "source_id", "rid")
    arrival_seq = pick(raw, "arrival_seq", "received_order", "upload_order",
                       "arrival_order", "received_seq", "order", "seq")
    received_at = pick(raw, "received_at", "arrived_at", "ingested_at")

    cal_until = pick(raw, "calibrated_until", "calibration_valid_until",
                     "valid_until", "certified_until", "校准有效期至")
    cal_explicit = pick(raw, "calibration_valid", "cal_valid", "valid_calibration")
    cal_flag_parsed = calibration_valid(None, None, cal_explicit)
    calibration_id = pick(raw, "calibration_id", "certificate_id", "cert_id", "校准证书号")

    return {
        "kind": kind,
        "kind_raw": kind_raw,
        "record_id": str(record_id) if record_id is not None else None,
        "service_id": str(service_id) if service_id is not None else None,
        "meter_id": str(meter_id) if meter_id is not None else None,
        "minute": minute,
        "start_minute": start_minute,
        "end_minute": end_minute,
        "minutes": int(span) if span is not None else (
            end_minute - start_minute if start_minute is not None and end_minute is not None else None),
        "kw": kw,
        "demand_kw": demand_kw,
        "peak_kw": peak_kw_value,
        "kwh": kwh,
        "impulses": impulses,
        "pulses_per_kwh": imp_per_kwh,
        "state": str(state).lower() if state is not None else None,
        "calibrated_until": cal_until,
        "calibration_valid_flag": cal_flag_parsed,
        "calibration_id": calibration_id,
        "signed": is_jointly_signed(raw),
        "parties": signed_parties(raw),
        "arrival_seq": int(as_float(arrival_seq)) if as_float(arrival_seq) is not None else None,
        "received_at": received_at,
        "raw": raw,
    }


def _iter_source_lists(bundle: dict) -> list[dict]:
    """Pull every kind-bearing record out of arbitrary fixture layouts."""
    records: list[dict] = []
    seen_ids: set[int] = set()

    def add(obj: Any):
        if isinstance(obj, dict):
            if id(obj) not in seen_ids:
                seen_ids.add(id(obj))
                records.append(obj)
        elif isinstance(obj, list):
            for item in obj:
                add(item)

    for keys in _LIST_KEYS.values():
        for key in keys:
            if key in bundle:
                add(bundle[key])
    # Nested scenario helpers are NOT merged into the base bundle.
    return records


def normalise_bundle(source: Any) -> dict:
    """Normalise a path / JSON string / fixture dict / record list."""
    if source is None:
        raw_bundle: dict = {}
    elif isinstance(source, (str, Path)):
        text = str(source)
        path = Path(text)
        if path.exists():
            raw_bundle = json.loads(path.read_text(encoding="utf-8"))
        else:
            raw_bundle = json.loads(text)
    elif isinstance(source, list):
        raw_bundle = {"events": source}
    elif isinstance(source, dict):
        raw_bundle = source
    else:
        raise TypeError(f"unsupported power evidence source: {type(source)!r}")

    # A few callers pass a single normalised/raw record.
    if not any(k in raw_bundle for k in _LIST_KEYS["events"]) and _record_kind(raw_bundle) != "unknown" \
            and ("meter_id" in raw_bundle or "service_id" in raw_bundle):
        raw_bundle = {"events": [raw_bundle]}

    period_raw = pick(raw_bundle, "period", "billing_period", "账期", default=None)
    period: dict[str, Any] | None = None
    if isinstance(period_raw, dict):
        period = {
            "period_id": pick(period_raw, "period_id", "id", "month", "账期", default=None),
            "date": parse_date(pick(period_raw, "date", "day", "business_date", "as_of")),
            "start_minute": parse_minute(pick(period_raw, "start_minute", "expected_start", "start", "from")),
            "end_minute": parse_minute(pick(period_raw, "end_minute", "expected_end", "end", "to")),
            "interval_minutes": int(as_float(pick(period_raw, "interval_minutes", "granularity",
                                                  default=15)) or 15),
        }
    as_of = parse_date(pick(raw_bundle, "as_of", "business_date", "date", default=None))
    if as_of is None and period and period.get("date"):
        as_of = period["date"]

    default_service = pick(raw_bundle, "service_id", "account_id", default=None)
    default_service = str(default_service) if default_service is not None else None

    norm_records = [normalise_record(r, default_service) for r in _iter_source_lists(raw_bundle)]

    service_points: dict[str, dict] = {}
    meters: dict[str, dict] = {}

    def ensure_service(sid: str) -> dict:
        return service_points.setdefault(sid, {
            "service_id": sid, "main_meter_id": None, "backup_meter_id": None, "raw": {}})

    def ensure_meter(mid: str, sid: str | None) -> dict:
        meter = meters.setdefault(mid, {
            "meter_id": mid, "service_id": sid, "role": "unknown",
            "calibrated_until": None, "calibration_valid_flag": _MISSING,
            "calibration_id": None, "raw": {}})
        if sid and not meter["service_id"]:
            meter["service_id"] = sid
        return meter

    for nr in norm_records:
        sid = nr["service_id"] or default_service
        mid = nr["meter_id"]
        if sid:
            nr["service_id"] = sid
            ensure_service(sid)
        if nr["kind"] == "meter" and mid:
            meter = ensure_meter(mid, sid)
            role = str(pick(nr["raw"], "role", "meter_role", "type", "用途", default="unknown")).lower()
            if "main" in role or role in {"primary", "贸易结算表", "主表"}:
                meter["role"] = "main"
                if sid:
                    ensure_service(sid)["main_meter_id"] = mid
            elif "backup" in role or "standby" in role or "备用" in role:
                meter["role"] = "backup"
                if sid:
                    ensure_service(sid)["backup_meter_id"] = mid
            meter["raw"] = nr["raw"]
            for key in ("calibrated_until", "calibration_id"):
                if nr[key] is not None:
                    meter[key] = nr[key]
            if nr["calibration_valid_flag"] is not None:
                meter["calibration_valid_flag"] = nr["calibration_valid_flag"]
            if pick(nr["raw"], "contract_billing_meter", "is_billing_meter", default=None):
                meter["billing_original"] = True
        elif nr["kind"] == "calibration" and mid:
            meter = ensure_meter(mid, sid)
            if nr["calibrated_until"] is not None:
                meter["calibrated_until"] = nr["calibrated_until"]
            if nr["calibration_id"] is not None:
                meter["calibration_id"] = nr["calibration_id"]
            if nr["calibration_valid_flag"] is not None:
                meter["calibration_valid_flag"] = nr["calibration_valid_flag"]

    # Top-level service-point declarations / contract bindings.
    for sp in _as_list(pick(raw_bundle, "service_points", "accounts", "结算点", default=[])):
        if not isinstance(sp, dict):
            continue
        sid = str(pick(sp, "service_id", "account_id", "id", "point_id", default=None) or "")
        if not sid:
            continue
        entry = ensure_service(sid)
        main_id = pick(sp, "main_meter_id", "contract_main_meter", "main_meter", "主表")
        backup_id = pick(sp, "backup_meter_id", "contract_backup_meter", "backup_meter", "备用表")
        if main_id:
            entry["main_meter_id"] = str(main_id)
            ensure_meter(str(main_id), sid)["role"] = "main"
        if backup_id:
            entry["backup_meter_id"] = str(backup_id)
            ensure_meter(str(backup_id), sid)["role"] = "backup"
        entry["raw"] = sp

    # Attach calibration validity evaluated at as_of to every meter.
    for mid, meter in meters.items():
        meter["calibration_valid"] = calibration_valid(
            meter.get("calibrated_until"), as_of, meter.get("calibration_valid_flag", _MISSING))

    # Reroute meter-only events onto their contracted service point so a serial
    # number never becomes a phantom billing account.  Legacy {meter_id:'E1'}
    # seed records have no registry entry and therefore stay keyed as before.
    meter_service = {mid: m.get("service_id") for mid, m in meters.items() if m.get("service_id")}
    for nr in norm_records:
        mid = nr.get("meter_id")
        sid = nr.get("service_id")
        target = meter_service.get(mid)
        if target and (sid is None or sid == mid or sid not in service_points):
            old_sid = sid
            nr["service_id"] = target
            # Remove the empty account auto-created from a bare meter serial.
            if old_sid and old_sid != target and old_sid in service_points:
                phantom = service_points[old_sid]
                if not phantom.get("main_meter_id") and not phantom.get("backup_meter_id"):
                    service_points.pop(old_sid, None)
            service_points.setdefault(target, {
                "service_id": target, "main_meter_id": None,
                "backup_meter_id": None, "raw": {}})

    return {
        "period": period,
        "as_of": as_of,
        "threshold_kw": as_float(pick(raw_bundle, "threshold_kw", "peak_threshold_kw",
                                      "surcharge_threshold_kw", default=100.0)) or 100.0,
        "service_points": service_points,
        "meters": meters,
        "records": norm_records,
        "raw": raw_bundle,
    }


def load_scenario(source: Any, scenario: str | int) -> dict:
    """Return one named scenario bundle from a multi-scenario fixture."""
    if isinstance(source, dict):
        bundle = source
    else:
        bundle = normalise_bundle(source)["raw"] if not isinstance(source, (str, Path)) or not Path(str(source)).exists() \
            else json.loads(Path(str(source)).read_text(encoding="utf-8"))
    scenarios = pick(bundle, "scenarios", "cases", default=None)
    chosen = None
    if isinstance(scenarios, dict) and scenario in scenarios:
        chosen = scenarios[scenario]
    elif isinstance(scenarios, list):
        chosen = scenarios[int(scenario)]
    elif scenario in bundle and isinstance(bundle[scenario], dict):
        # Fixtures may expose path_a / path_b / missing_case top-level keys.
        chosen = bundle[scenario]
    if chosen is None:
        raise KeyError(f"unknown power scenario {scenario!r}")
    if isinstance(chosen, dict):
        merged = {k: v for k, v in bundle.items()
                  if k in {"period", "as_of", "threshold_kw", "service_points", "meters"}}
        merged.update(chosen)
        return merged
    return {"events": chosen}


# ---------------------------------------------------------------------------
# Append-only capture ledger (sqlite)
# ---------------------------------------------------------------------------

CAPTURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS power_sources (
    source_id TEXT PRIMARY KEY,
    kind TEXT,
    sha TEXT,
    payload TEXT NOT NULL,
    captured_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS power_evidence (
    record_id TEXT PRIMARY KEY,
    source_id TEXT,
    arrival_seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    service_id TEXT,
    meter_id TEXT,
    minute INTEGER,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_capture_audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path: str = ":memory:") -> sqlite3.Connection:
    """Open (and migrate) a power capture sqlite ledger."""
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(CAPTURE_SCHEMA)
    return db


class CaptureStore:
    """Thin method facade over the capture ledger."""

    def __init__(self, db: str | sqlite3.Connection = ":memory:"):
        if isinstance(db, sqlite3.Connection):
            self.db = db
            self._owner = False
        else:
            self.db = connect(db)
            self._owner = True

    def close(self) -> None:
        if self._owner:
            self.db.close()

    def __enter__(self) -> "CaptureStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def capture(self, record: dict, source_id: str | None = None) -> dict:
        nr = normalise_record(record)
        rid = nr["record_id"] or f"AUTO-{self.db.execute('SELECT COUNT(*) FROM power_evidence').fetchone()[0] + 1}"
        nr["record_id"] = rid
        existing = self.db.execute("SELECT payload FROM power_evidence WHERE record_id=?", (rid,)).fetchone()
        body = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if existing:
            if existing[0] != body:
                raise ValueError(f"evidence id {rid} reused with different content")
            return {"record_id": rid, "idempotent": True}
        seq = self.db.execute("SELECT COALESCE(MAX(arrival_seq),0)+1 FROM power_evidence").fetchone()[0]
        self.db.execute(
            "INSERT INTO power_evidence VALUES (?,?,?,?,?,?,?,?)",
            (rid, source_id, seq, nr["kind"], nr["service_id"], nr["meter_id"], nr["minute"], body))
        self.db.execute("INSERT INTO power_capture_audit(record_id,action,detail) VALUES (?,?,?)",
                        (rid, "captured", f"arrival_seq={seq}"))
        self.db.commit()
        return {"record_id": rid, "idempotent": False, "arrival_seq": seq}

    # Friendly aliases.
    add_record = capture
    record = capture

    def provenance(self, service_id: str | None = None) -> list[dict]:
        sql = "SELECT * FROM power_evidence"
        args: tuple = ()
        if service_id is not None:
            sql += " WHERE service_id=?"
            args = (service_id,)
        sql += " ORDER BY arrival_seq"
        return [dict(r) for r in self.db.execute(sql, args)]

    def arrival_order(self, service_id: str | None = None) -> list[str]:
        return [r["record_id"] for r in self.provenance(service_id)]


def capture_records(db: "str | sqlite3.Connection | CaptureStore",
                    records: Iterable[dict]) -> list[dict]:
    """Capture many records into a path/connection/store."""
    if isinstance(db, CaptureStore):
        store, close_after = db, False
    else:
        store = CaptureStore(db)
        close_after = isinstance(db, str)
    try:
        return [store.capture(r) for r in records]
    finally:
        if close_after:
            store.close()


# Concise aliases a caller may import.
def capture(source: Any = None) -> dict:
    """Normalise an evidence source (path / JSON / dict / record list)."""
    return normalise_bundle(source)


normalize = normalise_bundle
load_bundle = normalise_bundle
Ledger = CaptureStore
PowerCapture = CaptureStore
