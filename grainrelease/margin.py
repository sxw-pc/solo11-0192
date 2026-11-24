"""Provisional hedge margin requirement."""
from .hedges import positions


def required(events, per_lot):
    return {contract:abs(lots)*per_lot[contract] for contract,lots in positions(events).items()}
