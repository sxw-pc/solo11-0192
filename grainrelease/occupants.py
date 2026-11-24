"""Badge-scan presence intervals for the fumigation exclusion zone.

A gate scan proves that a *credential* moved.  It does not, by itself, prove
which person carried it.  This module therefore keeps the full interval
history per badge instead of collapsing the ledger to the last entry/exit
state.  A credential that leaves and later re-enters starts a new interval; an
earlier muster signature or an earlier "all clear" can never cover that new
interval.

Person-level attribution (loans, accompanied entry, visitor identity, video
evidence) is deliberately kept out of this module and lives in
:mod:`grainrelease.person_capture`.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone


def parse_time(value):
    """Parse an ISO timestamp, or a bare ``HH:MM[:SS]`` clock time.

    Bare clock times are anchored to a fixed neutral date so that comparisons
    within a single fixture remain meaningful.  Callers should normally use a
    full ISO timestamp.
    """
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
    if parsed is not None:
        # Mixed offsets and bare local timestamps must still compare.  Keep
        # the raw string for display and normalise only for ordering.
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    try:
        clock = time.fromisoformat(text)
    except ValueError:
        return None
    return datetime.combine(date(1970, 1, 1), clock)


def _at(event):
    return (event.get('at') or event.get('event_at')
            or event.get('timestamp') or event.get('scan_at'))


def event_time(event):
    return parse_time(_at(event))


def _kind(event):
    kind = str(event.get('kind', '')).lower()
    if kind in {'entry', 'in', 'enter', 'checkin', 'check-in', 'access'}:
        return 'entry'
    if kind in {'exit', 'out', 'leave', 'checkout', 'check-out', 'egress'}:
        return 'exit'
    return kind


def _order_key(index, event):
    """Order by the time observed on site, never by upload/receipt order.

    Offline gate controllers upload in batches.  Feeding a batch in upload
    order would let a late-uploaded 10:07 re-entry appear "before" a 10:05
    exit.  The event timestamp is the only defensible ordering key.

    At an identical timestamp exits sort before entries.  If the two scans
    were actually the other way round, that choice leaves a new interval open
    (search continues) instead of silently swallowing a re-entry.
    """
    observed = event_time(event)
    ident = str(event.get('event_id') or event.get('id') or '')
    rank = 0 if _kind(event) == 'exit' else 1
    if observed is not None:
        return (0, observed, rank, ident, index)
    raw = _at(event)
    if raw:
        return (1, str(raw), rank, ident, index)
    return (2, '', rank, ident, index)


def event_id(event, index):
    return str(event.get('event_id') or event.get('id') or f'gate-{index}')


@dataclass
class Interval:
    """One continuous presence interval for one badge."""

    badge_id: str
    entry_event: str
    start: str | None = None
    end: str | None = None
    exit_event: str | None = None
    device: str | None = None
    entry_device: str | None = None
    exit_device: str | None = None
    entry_received_at: str | None = None
    exit_received_at: str | None = None

    @property
    def open(self):
        return self.end is None

    def to_dict(self):
        return {
            'badge_id': self.badge_id,
            'start': self.start,
            'end': self.end,
            'entry_event': self.entry_event,
            'exit_event': self.exit_event,
            'device': self.device,
            'entry_device': self.entry_device,
            'exit_device': self.exit_device,
            'entry_received_at': self.entry_received_at,
            'exit_received_at': self.exit_received_at,
            'open': self.open,
        }


@dataclass
class Presence:
    """All badge intervals plus convenient current-state properties."""

    badge_id: str
    intervals: list = field(default_factory=list)
    orphan_exits: list = field(default_factory=list)
    repeated_scans: list = field(default_factory=list)
    duplicate_events: list = field(default_factory=list)

    @property
    def current(self):
        open_intervals = [x for x in self.intervals if x.open]
        return open_intervals[-1] if open_intervals else None

    @property
    def inside(self):
        return self.current is not None

    @property
    def reentered(self):
        return len(self.intervals) > 1

    def __bool__(self):
        return self.inside

    def __eq__(self, other):
        # Keep ``inside(events)['W1'] == True`` usable while exposing the
        # richer interval history.
        if isinstance(other, bool):
            return self.inside is other
        if isinstance(other, Presence):
            return (self.badge_id, [x.to_dict() for x in self.intervals]) == (
                other.badge_id, [x.to_dict() for x in other.intervals])
        if isinstance(other, dict):
            return self.to_dict() == other
        return NotImplemented

    __hash__ = None

    def __repr__(self):
        return (f"Presence(badge_id={self.badge_id!r}, inside={self.inside!r}, "
                f"intervals={len(self.intervals)})")

    def to_dict(self):
        return {
            'badge_id': self.badge_id,
            'inside': self.inside,
            'reentered': self.reentered,
            'intervals': [x.to_dict() for x in self.intervals],
            'orphan_exits': list(self.orphan_exits),
            'repeated_scans': list(self.repeated_scans),
            'duplicate_events': list(self.duplicate_events),
        }


def intervalize(events):
    """Return ``{badge_id: Presence}`` built from all scans in time order."""
    presences = {}
    open_by_badge = {}
    seen_events = set()
    ordered = sorted(enumerate(events or []), key=lambda pair: _order_key(*pair))
    for index, event in ordered:
        badge = event.get('badge_id') or event.get('badge')
        if not badge:
            continue
        badge = str(badge)
        presence = presences.setdefault(badge, Presence(badge))
        ident = event_id(event, index)
        if ident in seen_events:
            presence.duplicate_events.append(ident)
            continue
        seen_events.add(ident)
        kind = _kind(event)
        if kind == 'entry':
            if open_by_badge.get(badge):
                # A second entry without an interleaved exit is a badge
                # re-scan, not a second physical person.  It must not erase
                # the first interval nor manufacture a re-entry.
                presence.repeated_scans.append(ident)
                continue
            interval = Interval(
                badge_id=badge,
                entry_event=ident,
                start=_at(event),
                device=event.get('device'),
                entry_device=event.get('device'),
                entry_received_at=event.get('received_at'),
            )
            presence.intervals.append(interval)
            open_by_badge[badge] = interval
        elif kind == 'exit':
            interval = open_by_badge.pop(badge, None)
            if interval is None:
                presence.orphan_exits.append(ident)
                continue
            interval.end = _at(event)
            interval.exit_event = ident
            interval.exit_device = event.get('device')
            interval.exit_received_at = event.get('received_at')
    return presences


def inside(events, alarm_at=None):
    """Return per-badge presence with the complete interval history.

    Values are :class:`Presence` objects.  They are truthy exactly when the
    badge has an open interval, so historical boolean use still works.
    ``alarm_at`` is accepted for callers that want the interval view next to an
    alarm cut; intervalization itself is independent of the alarm.
    """
    if isinstance(events, dict):
        events = events.get('gates') or events.get('gate_events') or []
    return intervalize(events)


def intervals_for(events, badge_id):
    return inside(events).get(str(badge_id), Presence(str(badge_id))).intervals


def post_alarm_intervals(events, alarm_at):
    """Intervals whose *entry* happened strictly after the alarm cut."""
    cut = parse_time(alarm_at)
    out = []
    for presence in inside(events).values():
        for interval in presence.intervals:
            start = parse_time(interval.start)
            if cut is None or (start is not None and start > cut):
                out.append(interval)
    return out
