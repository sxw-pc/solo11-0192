"""Provisional nearest carrier for a delivery."""
from .carriers import carriers


def assign(events, travel):
    located=carriers(events)
    return {stop:min(located,key=lambda carrier:travel[located[carrier]][stop]) for stop in travel['stops']}
