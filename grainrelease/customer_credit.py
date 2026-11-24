"""Collect customer credit-limit records."""


def limits(events):
    result={}
    for event in events:
        result[event['customer_id']]=event['limit']
    return result
