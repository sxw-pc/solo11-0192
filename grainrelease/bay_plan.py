"""Collect warehouse floor placement requests."""


def placements(events):
    result={}
    for event in events:
        result.setdefault(event['bay_id'], []).append(event)
    return result
