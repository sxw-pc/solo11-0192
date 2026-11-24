"""Traceable meter identity, switch boundaries and 15-minute energy integration.

The historical :func:`intervals` only grouped raw event dicts by
``meter_id`` and let :func:`grainrelease.demand_bill.peak_kw` take the largest
instantaneous ``kw``.  That is wrong for demand billing:

* an instantaneous sample is not the demand of a complete 15-minute interval;
* a meter replacement needs jointly signed seal / start readings and a
  contractual owner for the overlap;
* a backup meter may substitute only inside the authorised window while its
  calibration is valid;
* missing coverage is reported as a non-settleable range instead of being
  filled with a point reading.

The public :func:`intervals` keeps its legacy ``{meter_id: [raw events]}``
return for the original bare seed records.  Any typed / evidenced payload goes
through :func:`adjudicate` and is returned as ``{service_id: [interval]}``.
"""
from __future__ import annotations

from typing import Any

from .power_capture import (
    MISSING,
    PENDING,
    REJECTED,
    SETTLED,
    SOURCE_PRIORITY,
    Status,
    as_float,
    normalise_bundle,
)

INTERVAL_MINUTES = 15

_OFFLINE_WORDS = {"offline", "drop", "dropped", "dropout", "failure", "failed",
                  "down", "disconnected", "掉线", "离线", "故障", "中断"}
_ONLINE_WORDS = {"online", "recovered", "recovery", "resume", "restored",
                 "reconnect", "恢复", "复机", "上线"}
_RUNNING_WORDS = {"running", "run", "active", "started", "start", "运行", "启动"}


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------

def _looks_legacy(events: Any) -> bool:
    if not isinstance(events, list) or not events:
        return False
    rich_keys = {"kind", "type", "record_type", "service_id", "pulses", "impulses",
                 "kwh", "energy_kwh", "receipt_id", "signed_by", "authorization",
                 "calibrated_until", "start_minute", "interval_start", "state"}
    for event in events:
        if not isinstance(event, dict):
            return False
        if rich_keys & set(event):
            return False
        if "meter_id" not in event or "kw" not in event:
            return False
    return True


def _legacy_intervals(events: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for event in events:
        result.setdefault(event["meter_id"], []).append(event)
    return result


# ---------------------------------------------------------------------------
# Small value objects
# ---------------------------------------------------------------------------

class Interval(dict):
    """Dict row carrying a few convenience aliases."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:  # pragma: no cover - trivial
            raise AttributeError(name) from exc


class IntervalMap(dict):
    """``{service_id: [Interval]}`` plus whole-adjudication views."""

    def __init__(self, *args, adjudication: dict | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.adjudication = adjudication or {}

    @property
    def missing_ranges(self):
        return self.adjudication.get("missing_ranges", [])

    @property
    def pending_ranges(self):
        return self.adjudication.get("pending_ranges", [])

    @property
    def unsettleable_ranges(self):
        return self.adjudication.get("unsettleable_ranges", [])

    @property
    def detail(self):
        return self.adjudication


# ---------------------------------------------------------------------------
# Evidence selection helpers
# ---------------------------------------------------------------------------

def _state_canonical(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip().lower()
    if text in _OFFLINE_WORDS or any(w in text for w in ("offline", "fail", "drop", "掉线", "故障", "离线")):
        return "offline"
    if text in _ONLINE_WORDS or any(w in text for w in ("recov", "resume", "restor", "恢复")):
        return "online"
    if text in _RUNNING_WORDS or "run" in text or "运行" in text:
        return "running"
    return text


def _records_for(norm: dict, service_id: str) -> list[dict]:
    out = []
    for record in norm["records"]:
        if record["service_id"] == service_id or (
                record["service_id"] is None and record["meter_id"]):
            out.append(record)
    return out


def _meter_calibration(norm: dict, meter_id: str | None, records: list[dict],
                       as_of: str | None):
    """Return (valid: bool|None, calibration_id, until)."""
    if not meter_id:
        return None, None, None
    meter = norm["meters"].get(meter_id)
    if meter is not None:
        return meter.get("calibration_valid"), meter.get("calibration_id"), meter.get("calibrated_until")
    # Evidence may carry calibration inline even without a registry entry.
    for record in records:
        if record["meter_id"] == meter_id and record["kind"] == "calibration":
            from .power_capture import calibration_valid
            valid = calibration_valid(record["calibrated_until"], as_of,
                                      record["calibration_valid_flag"])
            return valid, record["calibration_id"], record["calibrated_until"]
    for record in records:
        if record["meter_id"] == meter_id and record["calibration_valid_flag"] is not None:
            return bool(record["calibration_valid_flag"]), record["calibration_id"], record["calibrated_until"]
    return None, None, None


def _energy_blocks(records: list[dict], meter_id: str) -> list[tuple[int, int, float, str]]:
    """Return ``(start, end, kwh, source_id)`` energy blocks for a meter."""
    blocks = []
    for record in records:
        if record["meter_id"] != meter_id or record["kind"] != "energy":
            continue
        sid = record["record_id"] or "energy"
        kwh = record["kwh"]
        start, end = record["start_minute"], record["end_minute"]
        if kwh is None and record["impulses"] is not None:
            constant = record["pulses_per_kwh"] or 1000.0
            kwh = record["impulses"] / constant
        demand = record["demand_kw"]
        if kwh is None and demand is not None and start is not None and record["minutes"]:
            kwh = demand * record["minutes"] / 60.0
        if kwh is None or start is None:
            continue
        if end is None:
            minutes = record["minutes"] or 15
            end = start + int(minutes)
        if end <= start or kwh < 0:
            continue
        blocks.append((start, end, float(kwh), sid))
    return blocks


def _kw_profile(blocks: list[tuple[int, int, float, str]]) -> dict[int, float]:
    """Minute -> kW assuming constant power over each energy block."""
    profile, _ = _profile_and_conflicts(blocks)
    return profile


def _profile_and_conflicts(blocks):
    """Return minute->kW and minutes where same-meter uploads disagree.

    Identical duplicate uploads agree; materially different overlapping blocks
    are a source conflict, not a value to average away.
    """
    raw: dict[int, list[float]] = {}
    for start, end, kwh, _ in blocks:
        span_h = (end - start) / 60.0
        if span_h <= 0:
            continue
        kw = kwh / span_h
        for minute in range(start, end):
            raw.setdefault(minute, []).append(kw)
    profile: dict[int, float] = {}
    conflicts: list[int] = []
    for minute, values in raw.items():
        profile[minute] = sum(values) / len(values)
        if len(values) > 1 and (max(values) - min(values)) > 1e-6:
            conflicts.append(minute)
    return profile, conflicts


def _auth_for_interval(auths: list[dict], service_id: str, start: int, end: int):
    """Pick the effective signed authorisation for an interval."""
    candidates = [a for a in auths if a["service_id"] in (service_id, None)]
    for auth in candidates:
        minute = auth["minute"] if auth["minute"] is not None else auth["start_minute"]
        window_start = auth["start_minute"]
        window_end = auth["end_minute"]
        relevant = True
        if window_start is not None and window_end is not None:
            relevant = window_start < end and window_end > start
        elif minute is not None:
            relevant = start <= minute <= end
        auth["_relevant"] = relevant
    relevant_all = [a for a in candidates if a.get("_relevant", True)]
    signed = [a for a in relevant_all if a.get("signed")]
    switch = [a for a in signed if a["kind"] == "switch_authorization"]
    continuation = [a for a in signed if a["kind"] == "continuation_authorization"]

    conflict = None
    if switch and continuation:
        conflict = "同一区间同时存在相互冲突的签认换表授权与主表继续计费回执"
    elif len(switch) >= 2:
        owners = {str(a["raw"].get("overlap_billing_meter_id")
                      or a["raw"].get("overlap_owner")) for a in switch}
        boundaries = {a["raw"].get("boundary_minute", a["minute"]) for a in switch}
        if len(owners) > 1 or len(boundaries) > 1:
            conflict = "存在两份计费原件/边界不一致的签认换表授权"

    if switch:
        return switch[-1], "switch", conflict
    if continuation:
        return continuation[-1], "continuation", conflict
    # Unsigned / unrecognised authorisation still informs why we are pending.
    if relevant_all:
        chosen = relevant_all[-1]
        kind = ("switch" if chosen["kind"] == "switch_authorization"
                else "continuation" if chosen["kind"] == "continuation_authorization"
                else "unknown")
        return chosen, kind, conflict
    return None, None, None


def _auth_plan(auth: dict | None, auth_type: str | None, norm: dict,
               service_id: str, start: int, end: int):
    """Resolve meter identities, boundary and overlap owner from an auth."""
    sp = norm["service_points"].get(service_id, {})
    raw = auth["raw"] if auth else {}
    main_id = (auth and auth["meter_id"]) or sp.get("main_meter_id")
    backup_id = None
    if auth:
        backup_id = raw.get("backup_meter_id") or raw.get("new_meter_id") \
            or raw.get("replacement_meter") or raw.get("replacement_meter_id")
        main_id = raw.get("main_meter_id") or raw.get("old_meter_id") or main_id
    backup_id = backup_id or sp.get("backup_meter_id")

    boundary = None
    overlap_owner = None
    substitution_window = None
    seal_reading = None
    start_reading = None
    if auth:
        boundary = parse_minute_like(raw.get("boundary_minute", raw.get("switch_minute", auth["minute"])))
        overlap_owner = raw.get("overlap_billing_meter_id") or raw.get("overlap_owner") \
            or raw.get("billing_meter_in_overlap")
        seal = raw.get("seal") or raw.get("seal_reading") or {}
        start_read = raw.get("start_reading") or raw.get("start") or raw.get("initial_reading") or {}
        if isinstance(seal, dict):
            seal_reading = as_float(seal.get("reading_kwh", seal.get("kwh", seal.get("reading"))))
            seal_minute = parse_minute_like(seal.get("minute", seal.get("at_minute")))
            if boundary is None:
                boundary = seal_minute
        else:
            seal_reading = as_float(seal)
        if isinstance(start_read, dict):
            start_reading = as_float(start_read.get("reading_kwh", start_read.get("kwh", start_read.get("reading"))))
            start_minute = parse_minute_like(start_read.get("minute"))
            if boundary is None:
                boundary = start_minute
        else:
            start_reading = as_float(start_read)
        ws = auth["start_minute"]
        we = auth["end_minute"]
        substitution_window = [ws, we] if ws is not None and we is not None else None
    return {
        "main_id": str(main_id) if main_id else None,
        "backup_id": str(backup_id) if backup_id else None,
        "boundary": boundary,
        "overlap_owner": str(overlap_owner) if overlap_owner else None,
        "substitution_window": substitution_window,
        "seal_reading": seal_reading,
        "start_reading": start_reading,
        "signed": bool(auth and auth.get("signed")),
        "authorization_id": (auth and auth["record_id"]) or raw.get("authorization_id") or raw.get("id"),
        "auth_type": auth_type,
    }


def parse_minute_like(value) -> int | None:
    from .power_capture import parse_minute
    return parse_minute(value)


# ---------------------------------------------------------------------------
# Interval adjudication
# ---------------------------------------------------------------------------

def _assess_interval(norm: dict, service_id: str, start: int,
                     interval_minutes: int = INTERVAL_MINUTES) -> Interval:
    end = start + interval_minutes
    records = _records_for(norm, service_id)
    reasons: list[str] = []
    excluded: list[dict] = []
    evidence_ids: list[str] = []

    samples = [r for r in records if r["kind"] == "instantaneous_sample"
               and r["minute"] is not None
               and (start <= r["minute"] < end or r["minute"] == end)]
    statuses = [r for r in records if r["kind"] == "meter_status"
                and r["minute"] is not None and start <= r["minute"] <= end]
    receipts = [r for r in records if r["kind"] == "interval_receipt"]
    auths = [r for r in records if r["kind"] in
             {"switch_authorization", "continuation_authorization"}]

    interval_receipts = []
    for receipt in receipts:
        rs = receipt["start_minute"]
        re_ = receipt["end_minute"]
        if rs is None:
            continue
        span = (re_ - rs) if re_ is not None else receipt["minutes"]
        # Demand is settled per complete quarter-hour.  A coarser (e.g. 60-min)
        # receipt cannot certify each 15-minute demand value.
        if span is not None and int(span) != interval_minutes:
            continue
        if rs == start and (re_ is None or re_ == end or span is None):
            interval_receipts.append(receipt)

    auth, auth_type, auth_conflict = _auth_for_interval(auths, service_id, start, end)
    plan = _auth_plan(auth, auth_type, norm, service_id, start, end)

    # Context: meter drop / backup running / overlap.
    offline_main_minute = None
    backup_running_minute = None
    main_id = plan["main_id"]
    backup_id = plan["backup_id"]
    for st in statuses:
        canonical = _state_canonical(st["state"])
        if canonical == "offline" and (main_id is None or st["meter_id"] in (main_id, None)):
            offline_main_minute = st["minute"]
            if main_id is None and st["meter_id"]:
                main_id = st["meter_id"]
        if canonical == "running" and st["meter_id"] and st["meter_id"] != main_id:
            backup_running_minute = st["minute"]
            if backup_id is None:
                backup_id = st["meter_id"]

    overlap_window = None
    if backup_running_minute is not None and offline_main_minute is not None \
            and backup_running_minute < offline_main_minute:
        overlap_window = [backup_running_minute, offline_main_minute]

    base = Interval(
        service_id=service_id,
        start_minute=start,
        end_minute=end,
        minute=start,
        start=start,
        end=end,
        interval_start=start,
        interval_end=end,
        minutes=interval_minutes,
        interval_minutes=interval_minutes,
        status=PENDING,
        settled=False,
        complete=False,
        single_billing_meter=None,
        billing_meter_id=None,
        meter_id=None,
        kwh=None,
        energy_kwh=None,
        demand_kw=None,
        avg_kw=None,
        average_kw=None,
        kw=None,
        peak_kw=None,
        source=None,
        source_kind=None,
        priority=None,
        source_priority=None,
        source_ids=[],
        evidence_ids=[],
        authorization_id=plan["authorization_id"],
        authorization_type=plan["auth_type"],
        signed_authorization=plan["signed"],
        calibration_id=None,
        calibration_valid=None,
        substitution_window=plan["substitution_window"],
        overlap_window=overlap_window,
        overlap_billing_meter=plan["overlap_owner"],
        boundary_minute=plan["boundary"],
        segments=[],
        excluded=excluded,
        samples=[{"record_id": s["record_id"], "meter_id": s["meter_id"],
                  "minute": s["minute"], "kw": s["kw"]} for s in samples],
        reasons=reasons,
    )

    if auth_conflict:
        base["status"] = PENDING
        base["authorization_conflict"] = True
        reasons.append(auth_conflict + "，合同计费原件无法唯一确定，状态待核")
        return base

    # ---- Route 1: signed complete interval receipt ------------------------
    for receipt in sorted(interval_receipts, key=lambda r: (not r["signed"],)):
        billing_meter = receipt["meter_id"] or plan["backup_id"] or plan["main_id"]
        valid, cal_id, until = _meter_calibration(norm, billing_meter, records, norm.get("as_of"))
        cal_flag = receipt["calibration_valid_flag"]
        if cal_flag is not None:
            valid = cal_flag
        if valid is False:
            reasons.append(f"回执 {receipt['record_id']} 的计费表 {billing_meter} 校准已失效，不能结算")
            base.update(status=REJECTED, settled=False,
                        calibration_id=cal_id or receipt["calibration_id"],
                        calibration_valid=False)
            continue

        meter_info = norm["meters"].get(billing_meter, {})
        is_backup_meter = meter_info.get("role") == "backup" or (
            plan["backup_id"] and billing_meter == plan["backup_id"]
            and billing_meter != plan["main_id"])
        main_failure_evidenced = offline_main_minute is not None or any(
            _state_canonical(s["state"]) == "offline"
            and (plan["main_id"] is None or s["meter_id"] in (plan["main_id"], None))
            for s in records if s["kind"] == "meter_status")
        if is_backup_meter:
            if not (plan["auth_type"] == "switch" and plan["signed"]) and not main_failure_evidenced:
                reasons.append(f"回执指定备用表 {billing_meter} 计费，但无合同主表失效证据/换表授权，备用来源不能替代")
                continue
            if valid is not True:
                reasons.append(f"备用表 {billing_meter} 缺少有效校准证明，不能替代主表结算")
                continue

        demand = receipt["demand_kw"]
        kwh = receipt["kwh"]
        if demand is None and kwh is not None:
            demand = kwh / (interval_minutes / 60.0)
        if demand is None and receipt["peak_kw"] is not None:
            # An authoritative receipt may quote the interval maximum demand.
            demand = receipt["peak_kw"]
        if demand is None:
            continue
        if kwh is None:
            kwh = demand * interval_minutes / 60.0
        # Contractual identity must be resolved when meters conflict.
        identity_unresolved = (
            auth is None
            and (overlap_window is not None or len({s["meter_id"] for s in samples if s["meter_id"]}) > 1)
            and not receipt["signed"]
        )
        if identity_unresolved:
            reasons.append("区间回执缺少双方签认且主备表重叠，计费原件身份待核")
            continue
        joint = bool(receipt["signed"])
        source_kind = "joint_interval_receipt" if joint else "provider_interval_receipt"
        priority = (SOURCE_PRIORITY["joint_interval_receipt"] if joint
                    else SOURCE_PRIORITY["billing_original_pulses"])
        base.update(
            status=SETTLED, settled=True, complete=True, single_billing_meter=True,
            billing_meter_id=billing_meter, meter_id=billing_meter,
            kwh=round(kwh, 6), energy_kwh=round(kwh, 6),
            demand_kw=round(demand, 6), avg_kw=round(demand, 6),
            average_kw=round(demand, 6), kw=round(demand, 6), peak_kw=round(demand, 6),
            source=source_kind, source_kind=source_kind,
            priority=priority, source_priority=priority,
            source_ids=[receipt["record_id"]], evidence_ids=[receipt["record_id"]],
            calibration_id=cal_id or receipt["calibration_id"], calibration_valid=valid,
        )
        if auth is not None:
            base["authorization_id"] = plan["authorization_id"]
        if plan["substitution_window"]:
            base["substitution_window"] = plan["substitution_window"]
        elif plan["auth_type"] == "switch" and plan["boundary"] is not None:
            base["substitution_window"] = [plan["boundary"], end]
        reasons.append("完整区间回执，单一计费表，需量按区间平均核定")
        return base

    # A hard calibration rejection on an interval receipt is final unless a
    # later receipt already settled (successful settlement returns above).
    if base["status"] == REJECTED:
        return base

    # ---- Route 2: integrate pulses / energy blocks ------------------------
    meter_ids = {m for m in (main_id, backup_id) if m}
    meter_ids |= {r["meter_id"] for r in records if r["kind"] == "energy" and r["meter_id"]}
    profiles: dict[str, dict[int, float]] = {}
    blocks_by_meter: dict[str, list] = {}
    meter_conflict_minutes: list[int] = []
    for meter_id in meter_ids:
        blocks = _energy_blocks(records, meter_id)
        if blocks:
            blocks_by_meter[meter_id] = blocks
            profile, conflicts = _profile_and_conflicts(blocks)
            profiles[meter_id] = profile
            if conflicts:
                meter_conflict_minutes.extend(conflicts)

    # Conflicting same-meter uploads must not be averaged into a fabricated mean.
    if meter_conflict_minutes:
        base["status"] = PENDING
        base["source_conflict"] = True
        reasons.append("同一计费表存在数值不一致的重复/重叠上传，来源冲突待核，不得平均或重复计算")
        return base

    conflict_context = (
        overlap_window is not None
        or (offline_main_minute is not None and backup_running_minute is not None)
        or len(profiles) > 1
        or auth is not None
    )

    # Without a signed authorisation in a switch / overlap / multi-meter
    # context the contractual billing original cannot be chosen from data alone.
    if conflict_context and not (auth and auth.get("signed")):
        base["status"] = PENDING
        if auth is not None and not auth.get("signed"):
            reasons.append("换表/继续计费文件未经双方签认，状态待核")
        else:
            reasons.append("主备表存在掉线/重叠或两段表计能量且缺少双方签认的换表或继续计费授权，"
                           "重叠区间合同计费原件不能凭上传先后或读数高低确定")
        if samples:
            reasons.append("离线瞬时采样仅留痕，不替代完整 15 分钟能量积分")
        return base

    # Determine per-minute billing meter.
    def owner_at(minute: int) -> str | None:
        if auth_type == "continuation" and plan["main_id"]:
            return plan["main_id"]
        if auth_type == "switch":
            boundary_minute = plan["boundary"]
            ov = plan["overlap_owner"]
            if ov and overlap_window and overlap_window[0] <= minute < overlap_window[1]:
                return ov
            if boundary_minute is not None:
                return plan["main_id"] if minute < boundary_minute else (plan["backup_id"] or plan["main_id"])
            # No explicit boundary: prefer contract main before backup run start.
            if backup_running_minute is not None and minute >= backup_running_minute:
                return plan["backup_id"] or plan["main_id"]
            return plan["main_id"]
        # No conflict and no auth: the sole metering source owns the interval.
        covered = [mid for mid, profile in profiles.items() if minute in profile]
        return covered[0] if len(covered) == 1 else (covered[0] if covered else None)

    counted_kwh = 0.0
    covered_minutes: list[int] = []
    segments: dict[str, dict] = {}
    owner_minutes: dict[str, list[int]] = {}
    unresolved_minutes: list[int] = []
    competing_minutes: list[int] = []

    for minute in range(start, end):
        available = {mid: profile[minute] for mid, profile in profiles.items() if minute in profile}
        if len(available) > 1 and conflict_context and not (auth and auth.get("signed")):
            competing_minutes.append(minute)
        owner = owner_at(minute)
        if owner is None or owner not in available:
            # Fall back to any sole available source when no contractual owner set.
            if len(available) == 1:
                owner = next(iter(available))
            else:
                unresolved_minutes.append(minute)
                continue
        # Record losing overlap source as excluded (never double count).
        for other_id, kw_value in available.items():
            if other_id != owner:
                excluded.append({"minute": minute, "meter_id": other_id, "kw": kw_value,
                                 "reason": "overlap-not-billing-or-duplicate-upload"})
        kw_value = available.get(owner)
        if kw_value is None:
            unresolved_minutes.append(minute)
            continue
        counted_kwh += kw_value / 60.0
        covered_minutes.append(minute)
        seg = segments.setdefault(owner, {"meter_id": owner, "minutes": 0, "kwh": 0.0})
        seg["minutes"] += 1
        seg["kwh"] += kw_value / 60.0
        owner_minutes.setdefault(owner, []).append(minute)

    if competing_minutes and not (auth and auth.get("signed")):
        base["status"] = PENDING
        reasons.append(f"第 {competing_minutes[0]}-{competing_minutes[-1]} 分钟主备重叠且无签认授权，待核")
        return base

    # Calibration gate for the meters actually counted.
    owner_ids = list(segments)
    cal_results = {mid: _meter_calibration(norm, mid, records, norm.get("as_of")) for mid in owner_ids}
    for mid, (valid, cal_id, until) in cal_results.items():
        if plan["auth_type"] == "switch" and mid == plan["backup_id"]:
            main_failure = offline_main_minute is not None or any(
                _state_canonical(s["state"]) == "offline" for s in statuses if s["meter_id"] == plan["main_id"])
            if not main_failure:
                reasons.append(f"备用表 {mid} 替代缺少合同指定主表失效证据")
                base["status"] = PENDING
                return base
            if valid is False:
                reasons.append(f"备用表 {mid} 校准有效期至 {until}，替代无效，区间不可结算")
                base.update(status=MISSING, complete=False,
                            calibration_id=cal_id, calibration_valid=False)
                return base
            if valid is None:
                reasons.append(f"备用表 {mid} 缺少有效校准证明，不能替代结算")
                base["status"] = PENDING
                base.update(calibration_id=cal_id, calibration_valid=None)
                return base

    # Any other billing meter (notably the contract main) with a known-expired
    # calibration invalidates settlement too.
    for mid, (valid, cal_id, until) in cal_results.items():
        if valid is False:
            reasons.append(f"计费表 {mid} 校准有效期至 {until or '过期'}，校准无效，区间不可结算")
            base.update(status=REJECTED, complete=False,
                        calibration_id=cal_id, calibration_valid=False)
            return base

    complete = len(covered_minutes) == interval_minutes and not unresolved_minutes
    single_meter = len(owner_ids) == 1
    demand = counted_kwh / (interval_minutes / 60.0) if covered_minutes else None

    # Book the minutes the contract backup meter actually served as substitute.
    if plan["backup_id"] and plan["backup_id"] in owner_minutes:
        backup_minutes = owner_minutes[plan["backup_id"]]
        base["substitution_window"] = [min(backup_minutes), max(backup_minutes) + 1]
    elif plan["auth_type"] == "switch" and plan["boundary"] is not None:
        base["substitution_window"] = [plan["boundary"], end]

    base["segments"] = [
        {**seg, "kwh": round(seg["kwh"], 6),
         "start_minute": start if i == 0 else None}
        for i, seg in enumerate(segments.values())
    ]
    base["excluded"] = excluded
    base["evidence_ids"] = sorted({sid for blocks in blocks_by_meter.values() for _, _, _, sid in blocks if sid})

    if not complete:
        missing_minutes = sorted(set(range(start, end)) - set(covered_minutes))
        base.update(
            status=MISSING, settled=False, complete=False,
            kwh=round(counted_kwh, 6) if counted_kwh else None,
            energy_kwh=round(counted_kwh, 6) if counted_kwh else None,
            covered_minutes=covered_minutes,
            missing_minutes=missing_minutes,
        )
        if samples and not profiles:
            reasons.append("仅有离线瞬时采样，缺少完整 15 分钟能量积分，列为不可结算区间")
        else:
            reasons.append("15 分钟区间能量覆盖不完整，缺测分钟 "
                           f"{missing_minutes[0]}-{missing_minutes[-1]}，列为不可结算区间")
        return base

    billing_meter = owner_ids[0] if single_meter else (plan["backup_id"] or "STITCHED")
    priority = SOURCE_PRIORITY["billing_original_pulses"] if single_meter else SOURCE_PRIORITY["boundary_stitched"]
    if not single_meter:
        priority = SOURCE_PRIORITY["boundary_stitched"]
        reasons.append("按封表/启表签认边界拼接两段能量，重叠电量只计一次")
    else:
        reasons.append("单一计费表完整脉冲积分，需量=区间电量/0.25h")
    valid, cal_id, until = cal_results.get(billing_meter, (None, None, None)) if single_meter else (None, None, None)
    base.update(
        status=SETTLED, settled=True, complete=True, single_billing_meter=single_meter,
        billing_meter_id=billing_meter, meter_id=billing_meter,
        kwh=round(counted_kwh, 6), energy_kwh=round(counted_kwh, 6),
        demand_kw=round(demand, 6), avg_kw=round(demand, 6),
        average_kw=round(demand, 6), kw=round(demand, 6), peak_kw=round(demand, 6),
        source="integrated_pulses", source_kind="integrated_pulses",
        priority=priority, source_priority=priority,
        source_ids=base["evidence_ids"],
        calibration_id=cal_id, calibration_valid=valid,
    )
    return base


def _merge_ranges(intervals: list[Interval], wanted_status: Status) -> list[dict]:
    rows = [iv for iv in intervals if iv["status"] == wanted_status]
    rows.sort(key=lambda iv: iv["start_minute"])
    ranges: list[dict] = []
    for iv in rows:
        if ranges and ranges[-1]["end_minute"] == iv["start_minute"] \
                and ranges[-1]["status"] == iv["status"]:
            ranges[-1]["end_minute"] = iv["end_minute"]
            ranges[-1]["minutes"] += iv["minutes"]
            ranges[-1]["interval_starts"].append(iv["start_minute"])
        else:
            ranges.append({
                "service_id": iv["service_id"],
                "start_minute": iv["start_minute"],
                "end_minute": iv["end_minute"],
                "minutes": iv["minutes"],
                "status": iv["status"],
                "reason": iv["reasons"][-1] if iv["reasons"] else None,
                "interval_starts": [iv["start_minute"]],
            })
    return ranges


def adjudicate(source: Any = None, **options) -> dict:
    """Run the full traceable interval adjudication for every service point."""
    if isinstance(source, dict) and "services" in source and "intervals" in source \
            and "records" not in source:
        return source  # idempotent: an existing adjudication result
    if isinstance(source, dict) and "records" in source and "service_points" in source:
        norm = source  # an already-normalised bundle
    else:
        norm = normalise_bundle(_coerce_source(source, options))
    interval_minutes = (norm.get("period") or {}).get("interval_minutes") or INTERVAL_MINUTES

    service_ids = set(norm["service_points"])
    for record in norm["records"]:
        if record["service_id"]:
            service_ids.add(record["service_id"])

    services: dict[str, dict] = {}
    all_intervals: dict[str, list[Interval]] = {}

    for service_id in sorted(service_ids):
        records = _records_for(norm, service_id)
        starts: set[int] = set()
        period = norm.get("period") or {}
        p_start, p_end = period.get("start_minute"), period.get("end_minute")
        if p_start is not None and p_end is not None:
            starts.update(range(p_start, p_end, interval_minutes))

        def add_start(candidate):
            if candidate is None:
                return
            aligned = (int(candidate) // interval_minutes) * interval_minutes
            if p_start is not None and aligned < p_start:
                return
            if p_end is not None and aligned >= p_end:
                return
            starts.add(aligned)

        for record in records:
            minute = record["minute"]
            if record["kind"] == "instantaneous_sample" and minute is not None:
                add_start(minute)
            if record["start_minute"] is not None:
                add_start(record["start_minute"])
            if record["kind"] == "interval_receipt" and record["start_minute"] is not None:
                add_start(record["start_minute"])
        if p_start is None and len(starts) >= 2:
            # No declared period: still expose unmeasured quarter-hours lying
            # between the earliest and latest evidenced interval starts.
            lo, hi = min(starts), max(starts)
            starts.update(range(lo, hi + 1, interval_minutes))
        intervals_row = [
            _assess_interval(norm, service_id, start, interval_minutes)
            for start in sorted(starts)
        ]
        all_intervals[service_id] = intervals_row
        missing_ranges = _merge_ranges(intervals_row, MISSING)
        pending_ranges = _merge_ranges(intervals_row, PENDING)
        unsettleable = sorted(
            [*missing_ranges, *pending_ranges], key=lambda r: r["start_minute"])
        settled_peaks = [iv["demand_kw"] for iv in intervals_row
                         if iv["status"] == SETTLED and iv["demand_kw"] is not None]
        peak = max(settled_peaks) if settled_peaks else None
        if any(iv["status"] == PENDING for iv in intervals_row):
            overall = PENDING
        elif any(iv["status"] == MISSING for iv in intervals_row):
            overall = MISSING
        elif any(iv["status"] == REJECTED for iv in intervals_row):
            overall = REJECTED
        else:
            overall = SETTLED
        services[service_id] = {
            "service_id": service_id,
            "status": overall,
            "peak_kw": round(peak, 6) if peak is not None else None,
            "intervals": intervals_row,
            "missing_intervals": [iv for iv in intervals_row if iv["status"] == MISSING],
            "pending_intervals": [iv for iv in intervals_row if iv["status"] == PENDING],
            "missing_ranges": missing_ranges,
            "pending_ranges": pending_ranges,
            "unsettleable_ranges": unsettleable,
        }

    missing_all = [r for svc in services.values() for r in svc["missing_ranges"]]
    pending_all = [r for svc in services.values() for r in svc["pending_ranges"]]
    result = {
        "normalised": True,
        "threshold_kw": norm.get("threshold_kw", 100.0),
        "as_of": norm.get("as_of"),
        "period": norm.get("period"),
        "services": services,
        "intervals": all_intervals,
        "missing_ranges": missing_all,
        "pending_ranges": pending_all,
        "unsettleable_ranges": sorted([*missing_all, *pending_all],
                                      key=lambda r: (r["service_id"], r["start_minute"])),
        "source_priority": SOURCE_PRIORITY,
    }
    return result


def _coerce_source(source: Any, options: dict) -> Any:
    """Merge positional + keyword evidence into one fixture-like bundle."""
    if not options:
        return source if source is not None else {}
    bundle: dict[str, Any] = {}
    if isinstance(source, dict):
        bundle.update(source)
    elif isinstance(source, list):
        bundle.setdefault("events", source)
    elif source is not None:
        bundle["source"] = source
    for key, value in options.items():
        if value is None:
            continue
        if key in {"events", "records", "readings"} and isinstance(value, list):
            bundle.setdefault("events", []).extend(value) if isinstance(bundle.get("events"), list) else bundle.update(events=value)
        else:
            bundle[key] = value
    return bundle


def intervals(events: Any = None, **options) -> Any:
    """Group raw events by meter (legacy) or adjudicate 15-minute intervals.

    Bare ``{meter_id, minute, kw}`` seed records keep the historical
    ``{meter_id: [events]}`` result exactly.  Typed / evidenced payloads
    return an :class:`IntervalMap` keyed by billing service point.
    """
    if events is None and not options:
        return {}
    if _looks_legacy(events) and not options:
        return _legacy_intervals(events)
    if isinstance(events, IntervalMap):
        return events
    result = adjudicate(events, **options)
    mapping = {sid: svc["intervals"] for sid, svc in result["services"].items()}
    return IntervalMap(mapping, adjudication=result)


def integrated_demand_kw(energy_kwh: float, minutes: int = INTERVAL_MINUTES) -> float | None:
    """Energy integration: demand = kwh / duration hours."""
    value = as_float(energy_kwh)
    if value is None or not minutes:
        return None
    return round(value / (minutes / 60.0), 6)


# Descriptive aliases for the adjudication entry point.
def resolve_intervals(source: Any = None, **options) -> IntervalMap:
    return intervals(source, **options)


def build_intervals(source: Any = None, **options) -> IntervalMap:
    return intervals(source, **options)


def settle_intervals(source: Any = None, **options) -> dict:
    return adjudicate(source, **options)


def integrate_interval(norm_or_source: Any, service_id: str, start_minute: int,
                       interval_minutes: int = INTERVAL_MINUTES) -> Interval:
    if isinstance(norm_or_source, dict) and norm_or_source.get("normalised") is True:
        norm = norm_or_source
    else:
        norm = normalise_bundle(norm_or_source)
    return _assess_interval(norm, service_id, start_minute, interval_minutes)
