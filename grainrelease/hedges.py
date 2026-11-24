"""Collect offline grain hedge position fills."""


def positions(events):
    result={}
    for event in events:
        result[event['contract']]=result.get(event['contract'],0)+event['lots']
    return result
