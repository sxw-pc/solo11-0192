"""Capture offline carrier reports: vehicle positions, driver shifts, and
highway restriction announcements.

Carriers file their reports offline; every notice and permit is kept in a
typed evidence ledger so later routing decisions can cite what they relied
on.  A segment is only treated as open when the evidence says so.
"""


def _keep_latest(slot, report, stamps):
    carrier = report['carrier_id']
    stamp = max([report.get(key) or '' for key in stamps] or [''])
    if carrier not in slot or stamp >= slot[carrier][0]:
        slot[carrier] = (stamp, report)


def capture(reports):
    """Split offline reports into a typed evidence ledger.

    Legacy ``{"carrier_id", "depot"}`` records count as position reports.
    Notices and permits are kept as an ordered ledger; positions and shifts
    keep only the latest report per carrier.
    """
    positions = {}
    shifts = {}
    ledger = {'positions': {}, 'shifts': {}, 'notices': [], 'permits': []}
    for report in reports:
        kind = report.get('kind') or 'position'
        if kind == 'position':
            _keep_latest(positions, report, ('as_of', 'reported_at'))
        elif kind == 'shift':
            _keep_latest(shifts, report, ('reported_at', 'as_of'))
        elif kind == 'notice':
            ledger['notices'].append(dict(report))
        elif kind == 'permit':
            ledger['permits'].append(dict(report))
        else:
            raise ValueError(f'unknown report kind: {kind!r}')
    ledger['positions'] = {cid: rec for cid, (stamp, rec) in positions.items()}
    ledger['shifts'] = {cid: rec for cid, (stamp, rec) in shifts.items()}
    ledger['notices'].sort(key=lambda n: (n.get('issued_at') or '', n.get('id') or ''))
    ledger['permits'].sort(key=lambda p: (p.get('issued_at') or '', p.get('id') or ''))
    return ledger


def _permit_for(ledger, notice, carrier_id, when, depart_after):
    """A permit exempts only when the authority that closed the road issued
    it before departure and it is already effective at ``when``; anything
    else must not be assumed valid."""
    for permit in ledger['permits']:
        if permit.get('carrier_id') != carrier_id:
            continue
        if permit.get('segment') != notice.get('segment'):
            continue
        if permit.get('issued_by') != notice.get('issuer'):
            continue
        issued = permit.get('issued_at')
        if not issued or not depart_after or issued > depart_after:
            continue
        start, end = permit.get('effective_from'), permit.get('effective_until')
        if not start or not end or not start <= when < end:
            continue
        return permit
    return None


def segment_state(ledger, frm, to, axle_class, carrier_id, when, depart_after):
    """Judge whether segment ``frm->to`` is passable at ``when``.

    Closures are matched by segment, axle class, and the announcement's
    effective period; a valid permit exempts the named carrier.  The result
    cites the notice/permit ids it relied on, and a closed segment reports
    the earliest reopening time so routers may wait instead of detouring.
    """
    key = f'{frm}->{to}'
    blocking = []
    for notice in ledger['notices']:
        if (notice.get('segment') or '') != key:
            continue
        if notice.get('axle_class', 'all') not in ('all', axle_class):
            continue
        start, end = notice.get('effective_from'), notice.get('effective_until')
        if not start or not end or start <= when < end:
            blocking.append(notice)
    if not blocking:
        return {'open': True, 'evidence': [], 'permits': [], 'reopen_at': None}
    permits = []
    for notice in blocking:
        permit = _permit_for(ledger, notice, carrier_id, when, depart_after)
        if permit is None:
            break
        permits.append(permit)
    else:
        evidence = [n['id'] for n in blocking] + [p['id'] for p in permits]
        return {'open': True, 'evidence': evidence,
                'permits': [p['id'] for p in permits], 'reopen_at': None}
    ends = [n.get('effective_until') for n in blocking]
    reopen = max(ends) if all(ends) else None
    return {'open': False, 'evidence': [n['id'] for n in blocking],
            'permits': [], 'reopen_at': reopen}
