"""Provisional bay load safety verdict."""
from .bay_plan import placements


def safe(events, capacities):
    grouped=placements(events)
    return {bay:sum(row['tonnes'] for row in rows)<=capacities[bay] for bay,rows in grouped.items()}
