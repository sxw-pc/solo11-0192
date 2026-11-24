"""Collect offline carrier availability records."""


def carriers(events):
    return {event['carrier_id']:event['depot'] for event in events}
