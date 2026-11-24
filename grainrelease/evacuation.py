"""Dynamic search roster: can the person search end?

A badge scan only proves that a credential moved.  The evacuation decision is
made on *real people*, on presence intervals cut at the alarm, and on muster
verification that matches the alarm round.  Two hard rules drive everything:

* an earlier muster signature never covers a later re-entry; the re-entry is a
  new interval that needs its own exit and its own re-verification;
* two people are never merged into one because they shared a badge.  An
  ambiguous interval keeps every candidate liable, so the search stays open
  until person-level evidence resolves it.

Published clear notices are append-only.  When new evidence contradicts one,
this module emits a named revocation and, only once the roster is genuinely
complete again, a new clear notice.  Existing notices are never rewritten.
"""
from copy import deepcopy

from . import occupants, person_capture


def _at(value):
    return occupants.parse_time(value)


def _record_at(record):
    return _at(record.get('at') or record.get('event_at')
               or record.get('timestamp') or record.get('scan_at'))


def _max_raw(values):
    """Max timestamp, returned as the original string (offset preserved)."""
    best = None
    for value in values:
        parsed = _at(value)
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, value)
    return best[1] if best else None


def _min_raw(values):
    best = None
    for value in values:
        parsed = _at(value)
        if parsed is None:
            continue
        if best is None or parsed < best[0]:
            best = (parsed, value)
    return best[1] if best else None


def _alarm_of(scenario):
    alarm = scenario.get('alarm') or {}
    at = alarm.get('at') or alarm.get('alarm_at') or scenario.get('alarm_at')
    round_id = (alarm.get('round') or alarm.get('round_id')
                or alarm.get('id') or scenario.get('alarm_round'))
    return {
        'at': at,
        'round': None if round_id is None else str(round_id),
        'id': str(alarm.get('id') or 'alarm'),
        'reason': alarm.get('reason') or 'fumigation alarm',
    }


def _people_by_badge(resolution):
    out = {}
    for pid, person in resolution['people'].items():
        for badge in person['badges']:
            out.setdefault(badge, []).append(pid)
    return out


def _resolve_person(ref, resolution, by_badge):
    if ref is None:
        return None, 'missing-identity'
    ref = str(ref)
    if ref in resolution['people']:
        return ref, None
    holders = by_badge.get(ref, [])
    if len(holders) == 1:
        return holders[0], None
    if len(holders) > 1:
        return None, 'ambiguous-badge'
    return None, 'unknown-person'


def _normalize_muster(muster, resolution, by_badge):
    """Return accepted/rejected muster verifications.

    A record is initially accepted when its person can be resolved.  Alarm
    round, witness and time checks happen later, in ``_validate_muster``.
    """
    if isinstance(muster, dict):
        muster = muster.get('records') or muster.get('muster') or []
    accepted, rejected = [], []
    for index, raw in enumerate(muster or []):
        if isinstance(raw, str):
            record = {'id': f'muster-{index}', 'badge_id': raw}
        elif isinstance(raw, dict):
            record = dict(raw)
        else:
            continue
        record.setdefault('id', f'muster-{index}')
        ref = (record.get('person_id') or record.get('person')
               or record.get('badge_id') or record.get('badge'))
        person_id, problem = _resolve_person(ref, resolution, by_badge)
        record['_person_id'] = person_id
        record['_problem'] = problem
        (rejected if problem else accepted).append(record)
    return accepted, rejected


def _validate_muster(record, person, alarm, alarm_time):
    """Check person / witness / alarm round / time for one muster record.

    Returns a list of problems; empty means the verification is usable.  The
    person must be named explicitly (a badge alone cannot say *who* came to
    the muster point), a witness other than the person must sign, and the
    record must carry the alarm round it belongs to and post-date the alarm.
    """
    reasons = []
    name = person['name']
    pid = person['id']
    if alarm_time is not None:
        if not (record.get('person_id') or record.get('person')):
            reasons.append('muster-lacks-person')
        at = _record_at(record)
        if at is None:
            reasons.append('missing-muster-time')
        elif at < alarm_time:
            reasons.append('muster-before-alarm')
        raw_round = (record.get('round') or record.get('alarm_round')
                     or record.get('round_id'))
        if alarm['round'] is not None:
            if raw_round is None:
                reasons.append('missing-alarm-round')
            elif str(raw_round) != alarm['round']:
                reasons.append('alarm-round-mismatch')
    witness = (record.get('witness') or record.get('witness_id')
               or record.get('witnessed_by'))
    if not witness:
        reasons.append('missing-witness')
    elif str(witness) in {pid, name}:
        reasons.append('self-witness')
    return reasons


def _relevant_intervals(person, alarm_time):
    """Intervals that matter after the alarm cut.

    An interval closed entirely before the alarm is history; it cannot be a
    safety concern for this round.  Anything still open, or ending after the
    cut, needs a matching muster.
    """
    intervals = person['intervals']
    if person['possible_intervals']:
        intervals = [*intervals, *person['possible_intervals']]
    if alarm_time is None:
        return [x for x in intervals if x['open']]
    out = []
    for item in intervals:
        start = _at(item.get('start'))
        end = _at(item.get('end'))
        if item['open'] or (end is not None and end > alarm_time) \
                or (start is not None and start > alarm_time):
            out.append(item)
    return out


def _coverage(person, alarm_time, records):
    """Earliest valid muster that covers every post-alarm interval.

    A muster covers an interval only when the interval is closed and its exit
    time is at or before the muster time.  The same muster cannot cover a later
    re-entry: that interval comes after the muster and fails the check.
    """
    relevant = _relevant_intervals(person, alarm_time)
    if not relevant:
        return True, None, [], 'no post-alarm presence'

    best = None
    for record in records:
        moment = _record_at(record)
        if moment is None:
            continue
        covers_all = True
        for item in relevant:
            start = _at(item.get('start'))
            end = _at(item.get('end'))
            if item['open'] or end is None or end > moment:
                covers_all = False
                break
            if start is not None and start > moment:
                covers_all = False
                break
        if covers_all and (best is None or moment < best[0]):
            best = (moment, record)

    if best is not None:
        return True, best[1], relevant, 'exited and re-verified at muster'
    if any(item['open'] for item in relevant):
        return False, None, relevant, 'still inside or no exit scan'
    return False, None, relevant, 'no matching muster after latest exit'


def evaluate(scenario):
    """Evaluate a full evacuation scenario into a search-roster decision.

    The scenario accepts ``alarm``, ``gates``, ``muster``, ``people``,
    ``claims``/``custody``, ``identity_evidence``, ``statements`` and
    ``announcements``.  The result is a plain dict; ``complete`` tells whether
    the person search can end.
    """
    scenario = scenario or {}
    alarm = _alarm_of(scenario)
    alarm_time = _at(alarm['at'])
    resolution = person_capture.resolve_scenario(scenario)
    by_badge = _people_by_badge(resolution)

    candidate_records, rejected = _normalize_muster(
        scenario.get('muster') or scenario.get('musters') or [],
        resolution, by_badge)

    accepted_by_person = {}
    for record in candidate_records:
        pid = record['_person_id']
        problems = _validate_muster(
            record, resolution['people'][pid], alarm, alarm_time)
        if problems:
            rejected.append({**record, '_problem': problems[0],
                             '_reasons': problems})
            continue
        accepted_by_person.setdefault(pid, []).append(record)

    people_out = {}
    unaccounted = []
    unknown = []
    for pid, person in resolution['people'].items():
        records = sorted(
            accepted_by_person.get(pid, []),
            key=lambda r: _record_at(r) or _at('1970-01-01'))
        covered, record, relevant, reason = _coverage(
            person, alarm_time, records)
        safe = covered
        unresolved_relevant = [x for x in relevant if x.get('owner') is None]
        if unresolved_relevant:
            # An unresolved badge interval might be this person's.  Even a
            # valid muster for their own badge cannot drop them from the
            # search, and neither can a valid muster for the other candidate.
            safe = False
            reason = ('unresolved badge interval may be this person; '
                      'keep both candidates in the search')
        elif person['possible_inside']:
            safe = False
            reason = ('unresolved badge interval may be this person; '
                      'keep both candidates in the search')
        if not safe:
            unaccounted.append(pid)
            if person['status'] == 'unknown':
                unknown.append(pid)
        people_out[pid] = {
            'id': pid,
            'name': person['name'],
            'role': person['role'],
            'visitor': person['visitor'],
            'badges': person['badges'],
            'inside': person['inside'],
            'possible_inside': person['possible_inside'],
            'status': person['status'],
            'accounted': safe,
            'reason': reason,
            'relevant_intervals': relevant,
            'muster': record,
            'intervals': person['intervals'],
            'possible_intervals': person['possible_intervals'],
        }

    complete = not unaccounted
    announcements = _announcements(
        scenario, alarm, resolution, people_out, complete, unaccounted,
        accepted_by_person)

    return {
        'complete': complete,
        'can_end_search': complete,
        'status': 'clear' if complete else 'searching',
        'round': alarm['round'],
        'alarm': alarm,
        'people': people_out,
        'unaccounted': unaccounted,
        'people_at_risk': list(unaccounted),
        'unknown': unknown,
        'unresolved_intervals': resolution['unresolved'],
        'muster': {
            'accepted': [
                {k: v for k, v in record.items() if not k.startswith('_')}
                for pid in sorted(accepted_by_person)
                for record in accepted_by_person[pid]
            ],
            'rejected': [
                {k: v for k, v in record.items() if k != '_person_id'}
                for record in rejected
            ],
        },
        'badge_ledger': resolution['badge_ledger'],
        'announcements': announcements,
        'published_announcements': deepcopy(
            scenario.get('announcements') or []),
    }


def _kind_of(record):
    return str(record.get('kind') or record.get('type') or '').lower()


def _is_clear(record):
    return _kind_of(record) in {
        'clear', 'cleared', 'all-clear', 'all_clear', 'release'}


def _is_revoke(record):
    return _kind_of(record) in {
        'revoke', 'revocation', 'withdraw', 'cancel', 'void'}


def _active_clear(published):
    """Last clear not followed by a revoke targeting it."""
    active = None
    for record in published:
        if _is_clear(record):
            active = record
        elif _is_revoke(record):
            target = record.get('revokes') or record.get('replaces')
            if (target is None or active is None
                    or str(target) == str(active.get('id'))):
                active = None
    return active


def _post_clear_evidence(resolution, after):
    """Re-entries and later custody evidence after a published clear.

    Only *entries* (new presence intervals) and later custody claims can
    contradict an all-clear.  An exit that merely lands after the notice does
    not by itself reopen the search.
    """
    times = []
    affected = []
    for item in resolution['intervals']:
        start = _at(item.get('start'))
        if start is not None and (after is None or start > after):
            times.append(item.get('start'))
            if item.get('owner'):
                affected.append(item['owner'])
            else:
                affected.extend(item.get('candidates') or [])
    for claim in resolution.get('claims', []):
        moment = _at(claim.get('from'))
        if moment is not None and (after is None or moment > after):
            times.append(claim.get('from'))
            if claim.get('person_id'):
                affected.append(claim['person_id'])
    return times, list(dict.fromkeys(affected))


def _announcements(scenario, alarm, resolution, people, complete, unaccounted,
                   accepted_by_person):
    """Build an append-only notice ledger; never edit a published notice."""
    published = [deepcopy(x) for x in (scenario.get('announcements') or [])]
    ledger = [deepcopy(x) for x in published]
    active = _active_clear(published)

    if active is not None:
        active_at = _record_at(active)
        times, affected = _post_clear_evidence(resolution, active_at)
        if times or not complete:
            affected = [p for p in affected if p in people] or list(unaccounted)
            affected = list(dict.fromkeys(affected))
            when = _min_raw(times) if times else _max_raw(
                [item.get(key)
                 for item in resolution['intervals']
                 for key in ('start', 'end')])
            if when is None:
                when = active.get('at') or alarm['at']
            if active_at is not None and (_at(when) or active_at) < active_at:
                # The contradiction is the roster being incomplete, not a
                # later fact; the notice is withdrawn as of its own time.
                when = active.get('at')
            ledger.append({
                'id': f"revoke:{active.get('id')}",
                'kind': 'revoke',
                'at': when,
                'round': alarm['round'],
                'revokes': active.get('id'),
                'names': [people[p]['name'] for p in affected],
                'reason': ('re-entry after clear requires a fresh exit and '
                           'round-matched muster verification'),
            })
            active = None

    if complete and active is None:
        muster_times = [
            record.get('at') for records in accepted_by_person.values()
            for record in records]
        latest = _max_raw([
            *[item.get('end') for item in resolution['intervals']
              if item.get('end')],
            *[item.get('start') for item in resolution['intervals']
              if item.get('start')],
            *muster_times,
            *[record.get('at') for record in published if record.get('at')],
        ])
        if latest is None:
            latest = alarm['at']
        names = [people[p]['name'] for p in sorted(people)
                 if people[p]['accounted']]
        ledger.append({
            'id': f"clear:{alarm['round']}:{len(ledger) + 1}",
            'kind': 'clear',
            'at': latest,
            'round': alarm['round'],
            'names': names,
            'replaces': ledger[-1].get('id') if ledger else None,
            'reason': ('all real people accounted with round-matched '
                       'muster verification'),
        })
    return ledger


def complete(gate_events, muster=None, **scenario):
    """Compatibility entry point.

    * ``complete(gates, ["W1", "V1"])`` keeps the historical badge-muster
      check used by ``fixtures/evac-ok.json`` (no alarm metadata, no witness
      requirements).
    * ``complete(gates, muster_records, alarm=..., people=..., ...)`` runs the
      full person-level evaluation.  A dict scenario may also be passed as the
      second argument, with gates supplied separately.
    """
    if isinstance(muster, dict) and not scenario:
        data = dict(muster)
        data.setdefault('gates', gate_events)
        return evaluate(data)['complete']
    simple_badges = (isinstance(muster, dict)
                     and 'badges' in muster)
    if not scenario and (simple_badges or all(
            isinstance(x, str) for x in (muster or []))):
        present = occupants.inside(gate_events)
        if simple_badges:
            badges = set(muster.get('badges') or [])
        else:
            badges = set(muster or [])
        for badge, presence in present.items():
            if not presence.inside:
                continue
            if badge not in badges:
                return False
            if presence.reentered:
                # A credential that left and came back is a new interval.
                # A flat old badge list carries no timestamp or round, so it
                # cannot verify that later presence.
                return False
        return True
    data = dict(scenario)
    data['gates'] = gate_events
    data['muster'] = muster or []
    return evaluate(data)['complete']


def evaluate_fixture(data):
    return evaluate(data)
