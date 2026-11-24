"""Provisional chemical cabinet capacity verdict."""
from .chemical_ledger import containers


def safe(events, capacities):
    totals={}
    for row in containers(events).values():
        totals[row['cabinet']]=totals.get(row['cabinet'],0)+row['kg']
    return {cabinet:total<=capacities[cabinet] for cabinet,total in totals.items()}
