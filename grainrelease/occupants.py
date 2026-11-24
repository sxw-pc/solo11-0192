"""Collect offline warehouse occupancy badge scans."""


def inside(events):
    result={}
    for event in events:
        result[event['badge_id']]=event['kind']=='entry'
    return result
