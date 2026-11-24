"""Custody ledger projections for sealed fumigant containers.

The capture log mixes position records (a drum at a cabinet or staging
table) with custody records (seal breaks, two-person signoffs, recycler
bookings and final receipts) and issue-list notices.  These helpers project
the log without mutating it; enforcement lives in chemical_capture.
"""

#: Location marker for drums that crossed the external handover boundary.
EXTERNAL = 'EXTERNAL'

#: Custody record kinds, kept separate so a seal break, a two-person
#: signoff and the entrusted recycler's final receipt never collapse
#: into one another.
CUSTODY_KINDS = ('handover', 'seal_break', 'signoff', 'booking', 'receipt')


def _location(record):
    return record.get('location', record.get('cabinet'))


def containers(events):
    """Last recorded position per container_id.

    Only position-bearing records count, so a later custody record never
    erases a drum's location, and a correction (e.g. Y1 re-recorded in
    cabinet B after a completed two-person handover) replaces the stale
    cabinet instead of coexisting with it.
    """
    result = {}
    for event in events:
        if event.get('container_id') and _location(event) is not None:
            result[event['container_id']] = event
    return result


def custody(events):
    """Chronological custody records per container_id."""
    log = {}
    for event in events:
        if event.get('kind') in CUSTODY_KINDS and event.get('container_id'):
            log.setdefault(event['container_id'], []).append(event)
    return log


def inventory(events):
    """Position records for drums still in depot custody."""
    return {cid: row for cid, row in containers(events).items()
            if _location(row) != EXTERNAL}


def notices(events):
    """Every published issue-list notice by id; history is append-only."""
    return {event['notice_id']: event for event in events if event.get('kind') == 'notice'}


def current_notice(events):
    notice = None
    for event in events:
        if event.get('kind') == 'notice':
            notice = event
    return notice


def available(events):
    """The currently notified issue list as {container_id: location}."""
    notice = current_notice(events)
    return dict(notice['available']) if notice else {}
