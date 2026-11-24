"""Provisional billed peak demand of warehouse circuits."""
from .power_meter import intervals


def peak_kw(events):
    return {meter: max(row['kw'] for row in rows) for meter, rows in intervals(events).items()}
