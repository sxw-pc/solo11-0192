"""Read offline warehouse power-meter intervals."""


def intervals(events):
    result = {}
    for event in events:
        result.setdefault(event['meter_id'], []).append(event)
    return result
