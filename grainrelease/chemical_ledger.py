"""Collect offline fumigant container location records."""


def containers(events):
    result={}
    for event in events:
        result[event['container_id']]=event
    return result
