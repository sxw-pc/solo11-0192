"""Collect offline carrier availability records."""
from .route_capture import capture


def carriers(events):
    """Merge offline reports into per-carrier availability records.

    Legacy ``{"carrier_id", "depot"}`` events still yield a depot record;
    position/shift reports add axle class plus cleaning and rest duties.
    Only carriers with both a position and a driver shift on file are
    ``confirmed`` -- an unconfirmed vehicle must not be promised on-time,
    and an unknown axle class is treated as heavy so restrictions are
    never silently skipped.
    """
    ledger = capture(events)
    roster = {}
    for cid in sorted(set(ledger['positions']) | set(ledger['shifts'])):
        position = ledger['positions'].get(cid) or {}
        shift = ledger['shifts'].get(cid) or {}
        roster[cid] = {
            'carrier_id': cid,
            'depot': position.get('depot'),
            'position_as_of': position.get('as_of'),
            'axle_class': position.get('axle_class') or 'heavy',
            'rest_minutes': shift.get('rest_minutes', 0),
            'cleaning_minutes': shift.get('cleaning_minutes', 0),
            'confirmed': bool(position.get('depot')) and bool(shift),
        }
    return roster
