"""Provisional evacuation roster completion."""
from .occupants import inside


def complete(gate_events, muster_badges):
    present=inside(gate_events)
    return all(badge in muster_badges for badge,is_inside in present.items() if is_inside)
