"""Attribute badge intervals to real people, never merge two people into one.

A badge is a credential, not a person.  When a card is lent, or when someone is
escorted in on another person's badge, the same scan can belong to either of
two real people.  This module keeps both people on the roster and only assigns
an interval to one of them when there is person-level evidence:

* a custody/loan record covering the interval, signed by at least two distinct
  witnesses;
* identity evidence covering the interval that includes both video footage
  and the person's own confirmation (``confirmed_by`` themselves).

Anything less (for example a verbal account that W1 borrowed V1's card) leaves
the interval *unresolved*: every candidate stays liable for it and no one is
quietly dropped from the search.
"""
from dataclasses import dataclass, field

from . import occupants


STRONG_IDENTITY_KINDS = frozenset({'video', 'self-confirmation'})
CUSTODY_KINDS = frozenset({'loan', 'custody', 'badge-transfer', 'borrowed'})
ACCOMPANIMENT_KINDS = frozenset(
    {'escort', 'accompaniment', 'accompanied', 'guest', 'visitor-escort'})


def _time(value):
    return occupants.parse_time(value)


def _overlap(interval, begin, until):
    """Does a custody/claim window overlap a badge presence interval?"""
    start = _time(interval.start)
    end = _time(interval.end)  # None means still open
    lo = _time(begin)
    hi = _time(until)
    if start is None:
        return True
    if lo is not None and end is not None and end <= lo:
        return False
    if hi is not None and start >= hi:
        return False
    return True


def _in_interval(record, interval):
    at = _time(record.get('at') or record.get('event_at'))
    if at is not None:
        start = _time(interval.start)
        end = _time(interval.end)
        return ((start is None or at >= start)
                and (end is None or at <= end))
    marker = record.get('interval_entry') or record.get('entry_event')
    if marker:
        return marker == interval.entry_event
    return True


def _claim_covers(claim, interval):
    """The loan/custody window must contain the whole presence interval.

    A loan that is still open-ended cannot cover a re-entry whose exit has not
    been recorded; an expired loan cannot cover presence that continued after
    the card was due back.
    """
    if not claim.until:
        return not interval.open
    until = _time(claim.until)
    if until is None:
        return False
    if interval.open:
        return False
    end = _time(interval.end)
    return end is not None and end <= until


def _unique(seq):
    out = []
    for item in seq:
        if item not in out:
            out.append(item)
    return out


@dataclass
class Claim:
    """A custody claim.  Only two-witness, verified claims can attribute."""

    id: str
    badge_id: str
    person_id: str
    kind: str = 'loan'
    begin: str | None = None
    until: str | None = None
    witnesses: tuple = ()
    verified: bool = False

    @property
    def two_witness(self):
        return self.verified and len(set(self.witnesses)) >= 2


@dataclass
class Person:
    id: str
    name: str
    role: str = 'worker'
    badges: tuple = ()
    visitor: bool = False


def _claim(record, index):
    witnesses = record.get('witnesses')
    if witnesses is None:
        witnesses = [record.get('witness')] if record.get('witness') else []
    kind = str(record.get('kind', 'loan')).lower()
    explicit = record.get('verified')
    verified = (explicit is True) or (
        explicit is not False and kind in CUSTODY_KINDS)
    return Claim(
        id=str(record.get('id') or f'claim-{index}'),
        badge_id=str(record.get('badge_id') or ''),
        person_id=str(record.get('person_id') or ''),
        kind=kind,
        begin=record.get('from') or record.get('begin') or record.get('start'),
        until=record.get('until') or record.get('to') or record.get('end'),
        witnesses=tuple(str(w) for w in witnesses if w),
        verified=verified,
    )


def _strong_evidence_by_person(badge_id, interval, evidence, people):
    kinds = {}
    record_ids = {}
    for index, record in enumerate(evidence or []):
        if str(record.get('badge_id') or '') != badge_id:
            continue
        person_id = str(record.get('person_id') or '')
        if not person_id or person_id not in people:
            continue
        if not _in_interval(record, interval):
            continue
        kind = str(record.get('kind', '')).lower()
        if kind == 'video':
            kinds.setdefault(person_id, set()).add('video')
        elif kind == 'self-confirmation':
            confirmed_by = str(record.get('confirmed_by') or '')
            person = people[person_id]
            if confirmed_by in {person.id, person.name}:
                kinds.setdefault(person_id, set()).add('self-confirmation')
        record_ids.setdefault(person_id, []).append(
            str(record.get('id') or f'evidence-{index}'))
    strong = {pid for pid, got in kinds.items() if STRONG_IDENTITY_KINDS <= got}
    return strong, record_ids


def resolve(gates, people, claims=(), custody=(), identity_evidence=(),
            statements=(), muster=(), accompaniment=(), alarm_at=None):
    """Resolve gate scans into per-person presence without merging people.

    ``people`` entries look like::

        {"id": "p-w1", "name": "W1", "role": "worker-rescuer",
         "badges": ["W1"], "visitor": false}

    ``custody`` records have ``badge_id``, ``person_id``, ``from``/``until``,
    ``witnesses`` and ``verified``.  ``identity_evidence`` records have
    ``badge_id``, ``person_id``, ``at`` and ``kind`` (``video`` or
    ``self-confirmation`` with ``confirmed_by``).  ``statements`` are notes
    such as a verbal account: they can widen the candidate set but can never
    attribute an interval on their own.  ``muster`` records also name real
    people, so a visitor who appears only on the muster sheet still joins the
    roster instead of being treated as an anonymous badge.

    ``accompaniment`` records describe an escorted person who walked in on
    someone else's credential (for example a temporary visitor with no badge of
    their own).  The interval is *shared*: both the badge holder and the
    escorted person are physically inside, and each keeps their own presence
    liability.  Sharing is not merging.

    The result contains ``intervals`` (each with ``owner`` and ``candidates``),
    ``people`` views and ``unresolved`` interval ids.  An unresolved interval
    is never silently assigned and never merges its candidates.
    """
    roster = {}
    for index, record in enumerate(people or []):
        pid = str(record.get('id') or record.get('person_id') or '')
        if not pid:
            continue
        roster[pid] = Person(
            id=pid,
            name=str(record.get('name') or pid),
            role=str(record.get('role') or 'worker'),
            badges=tuple(str(b) for b in (record.get('badges') or [])),
            visitor=bool(record.get('visitor', False)),
        )

    parsed_claims = []
    parsed_accompaniment = []
    for index, record in enumerate(list(claims or []) + list(custody or [])):
        parsed = _claim(record, index)
        if not (parsed.badge_id and parsed.person_id):
            continue
        if parsed.kind in ACCOMPANIMENT_KINDS:
            parsed_accompaniment.append(parsed)
        else:
            parsed_claims.append(parsed)
    for index, record in enumerate(accompaniment or []):
        parsed = _claim({**record, 'kind': record.get('kind') or 'escort'},
                        index + len(parsed_claims))
        if parsed.badge_id and parsed.person_id:
            parsed_accompaniment.append(parsed)

    parsed_statements = []
    for index, record in enumerate(statements or []):
        badge_id = str(record.get('badge_id') or '')
        person_id = str(record.get('person_id') or '')
        if badge_id and person_id:
            parsed_statements.append({
                'id': str(record.get('id') or f'statement-{index}'),
                'badge_id': badge_id,
                'person_id': person_id,
                'at': record.get('at') or record.get('event_at'),
                'interval_entry': record.get('interval_entry')
                or record.get('entry_event'),
                'text': str(record.get('text') or ''),
            })

    # Anyone named by evidence is a real person on the roster, even when the
    # roster entry is missing or temporary.  Never drop a person because the
    # people file was incomplete.
    if isinstance(muster, dict):
        muster = muster.get('records') or muster.get('muster') or []
    muster_records = [r for r in (muster or []) if isinstance(r, dict)]

    referenced = [
        (c.person_id, c.badge_id, None, None) for c in parsed_claims]
    # An escorted visitor does not hold the credential they walked in on;
    # listing it as their badge would make them a second "holder" and hide
    # the real holder.  Co-presence is carried by the accompaniment record.
    referenced += [
        (c.person_id, None, None, None) for c in parsed_accompaniment]
    for record in identity_evidence or []:
        referenced.append((
            str(record.get('person_id') or ''),
            str(record.get('badge_id') or ''),
            record.get('role'), record.get('visitor')))
    for item in parsed_statements:
        referenced.append(
            (item['person_id'], item['badge_id'], None, None))
    for record in muster_records:
        referenced.append((
            str(record.get('person_id') or record.get('person') or ''),
            str(record.get('badge_id') or record.get('badge') or ''),
            record.get('role'), record.get('visitor')))
    for person_id, badge_id, role, visitor in referenced:
        if not person_id:
            continue
        if person_id not in roster:
            roster[person_id] = Person(
                id=person_id, name=person_id,
                role=str(role or 'referenced'),
                badges=(badge_id,) if badge_id else (),
                visitor=bool(visitor))
        elif badge_id and badge_id not in roster[person_id].badges:
            # A muster/evidence record is the only source that ties a person
            # to this badge; record it so the interval has a real candidate.
            if not any(badge_id in p.badges and p.id != person_id
                       for p in roster.values()):
                person = roster[person_id]
                roster[person_id] = Person(
                    id=person.id, name=person.name, role=person.role,
                    badges=person.badges + (badge_id,),
                    visitor=person.visitor)

    presences = occupants.inside(gates)
    held_badges = {b for person in roster.values() for b in person.badges}
    claimed_badges = {c.badge_id for c in parsed_claims}
    for badge_id in presences:
        if badge_id not in held_badges and badge_id not in claimed_badges:
            synthetic_id = f'badge:{badge_id}'
            roster[synthetic_id] = Person(
                id=synthetic_id, name=badge_id, role='badge-holder',
                badges=(badge_id,))

    badge_holders = {}
    for person in roster.values():
        for badge in person.badges:
            badge_holders.setdefault(badge, []).append(person.id)
    intervals_out = []
    per_person = {
        pid: {'intervals': [], 'possible': []} for pid in roster
    }

    for badge_id in sorted(presences):
        presence = presences[badge_id]
        for interval in presence.intervals:
            holders = list(badge_holders.get(badge_id, []))
            relevant = [c for c in parsed_claims
                        if c.badge_id == badge_id
                        and _overlap(interval, c.begin, c.until)]
            claim_persons = [c.person_id for c in relevant
                             if c.person_id in roster]
            relevant_statements = [
                s for s in parsed_statements
                if s['badge_id'] == badge_id
                and _in_interval(s, interval)
                and s['person_id'] in roster
            ]
            statement_persons = [s['person_id'] for s in relevant_statements]

            relevant_accompaniment = [
                c for c in parsed_accompaniment
                if c.badge_id == badge_id
                and _overlap(interval, c.begin, c.until)
                and c.person_id in roster]
            co_present = _unique(
                c.person_id for c in relevant_accompaniment)

            candidates = _unique(
                holders + claim_persons + statement_persons + co_present)

            verified_claims = [
                c for c in relevant
                if c.two_witness and c.person_id in roster
                and _claim_covers(c, interval)]
            expired = (any(c.two_witness and c.until for c in relevant)
                       and not verified_claims)
            verified_owners = {c.person_id for c in verified_claims}
            strong_owners, evidence_ids = _strong_evidence_by_person(
                badge_id, interval, identity_evidence, roster)

            unverified_claims = [
                c for c in relevant if not c.two_witness]
            sole_holder = holders[0] if len(holders) == 1 else None
            contested = bool(
                unverified_claims
                or [p for p in statement_persons if p != sole_holder])
            conflict = (
                len(verified_owners) > 1
                or len(strong_owners) > 1
                or bool(verified_owners and strong_owners
                        and verified_owners != strong_owners)
                or expired)

            owner = None
            basis = 'unresolved'
            if not conflict:
                if len(verified_owners) == 1:
                    owner = next(iter(verified_owners))
                    basis = 'two-witness-custody'
                elif len(strong_owners) == 1:
                    owner = next(iter(strong_owners))
                    basis = 'video-and-self-confirmation'
                elif sole_holder and not contested:
                    owner = sole_holder
                    basis = 'own-badge'
            if owner is None:
                if expired:
                    basis = 'custody-window-expired'
                else:
                    basis = 'conflict' if conflict else 'unresolved'

            if conflict:
                co_present = []
            item = {
                **interval.to_dict(),
                'candidates': candidates,
                'owner': owner,
                'co_owners': list(co_present),
                'basis': basis,
                'conflict': conflict,
                'claim_ids': [c.id for c in relevant],
                'statement_ids': [s['id'] for s in relevant_statements],
                'evidence_ids': _unique(
                    [i for ids in evidence_ids.values() for i in ids]),
                'accompaniment_ids': [c.id for c in relevant_accompaniment],
            }
            intervals_out.append(item)
            if owner:
                per_person[owner]['intervals'].append(item)
                for person_id in co_present:
                    if person_id != owner:
                        per_person[person_id]['intervals'].append(item)
            else:
                for person_id in candidates:
                    per_person[person_id]['possible'].append(item)

    person_views = {}
    for pid, person in roster.items():
        owned = per_person[pid]['intervals']
        possible = per_person[pid]['possible']
        owned_open = any(x['open'] for x in owned)
        possible_open = any(x['open'] for x in possible)
        if owned_open:
            status = 'inside'
        elif possible_open:
            status = 'unknown'
        else:
            status = 'outside'
        person_views[pid] = {
            'id': pid,
            'name': person.name,
            'role': person.role,
            'visitor': person.visitor,
            'badges': list(person.badges),
            'intervals': owned,
            'possible_intervals': possible,
            'inside': owned_open,
            'possible_inside': possible_open,
            'status': status,
        }

    unresolved = [x for x in intervals_out if x['owner'] is None]
    return {
        'people': person_views,
        'intervals': intervals_out,
        'unresolved': unresolved,
        'badge_holders': {k: list(v) for k, v in badge_holders.items()},
        'badge_ledger': {k: v.to_dict() for k, v in presences.items()},
        'claims': [
            {
                'id': c.id, 'badge_id': c.badge_id, 'person_id': c.person_id,
                'kind': c.kind, 'from': c.begin, 'until': c.until,
                'witnesses': list(c.witnesses), 'verified': c.verified,
                'two_witness': c.two_witness,
            }
            for c in parsed_claims
        ],
        'accompaniment': [
            {
                'id': c.id, 'badge_id': c.badge_id, 'person_id': c.person_id,
                'kind': c.kind, 'from': c.begin, 'until': c.until,
                'witnesses': list(c.witnesses), 'verified': c.verified,
            }
            for c in parsed_accompaniment
        ],
        'statements': list(parsed_statements),
    }


def resolve_scenario(scenario):
    """Resolve a whole fixture-shaped scenario in one call."""
    return resolve(
        gates=scenario.get('gates') or scenario.get('gate_events') or [],
        people=scenario.get('people') or [],
        claims=scenario.get('claims') or [],
        custody=scenario.get('custody') or [],
        identity_evidence=scenario.get('identity_evidence') or [],
        statements=scenario.get('statements') or [],
        muster=scenario.get('muster') or scenario.get('musters') or [],
        accompaniment=scenario.get('accompaniment')
        or scenario.get('escorts') or [],
        alarm_at=(scenario.get('alarm') or {}).get('at')
        or scenario.get('alarm_at'),
    )
