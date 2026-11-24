"""Collect offline grain hedge position fills.

The legacy entry point :func:`positions` keeps working on the original simple
``{"contract", "lots"}`` events and returns the same flat
``{contract: signed_net_lots}`` mapping.  When reports carry account,
clearing-round, trade-id or correction-chain fields the result becomes an
account/contract aware map that also compares equal to flat, tuple-keyed and
nested views.  See :mod:`grainrelease.hedge_capture` for the full engine.
"""
from .hedge_capture import (
    HedgeCapture,
    PositionMap,
    build_positions,
    capture,
    effective_fills,
)


def positions(events=None, confirmed_only=False, **kwargs):
    """Return net signed lots.

    The first argument may be a report list or a fixture dict.  Pass
    ``confirmed_only=True`` to keep only clearing-house confirmed fills.
    Additional keyword arguments are forwarded to :class:`HedgeCapture`.
    """
    if isinstance(events, HedgeCapture):
        cap = events
    else:
        cap = HedgeCapture(events, **kwargs)
    return cap.positions(confirmed_only=confirmed_only)


def positions_by_account(events=None, *args, **kwargs):
    return HedgeCapture(events, *args, **kwargs).positions()._nested_view()


def position_rows(events=None, *args, **kwargs):
    return HedgeCapture(events, *args, **kwargs).position_rows()


def confirmed_positions(events=None, *args, **kwargs):
    return HedgeCapture(events, *args, **kwargs).positions(confirmed_only=True)


# Descriptive aliases used by some callers/tests.
net_positions = positions
collect_positions = positions
aggregate_positions = positions


__all__ = [
    'positions',
    'positions_by_account',
    'position_rows',
    'confirmed_positions',
    'net_positions',
    'collect_positions',
    'aggregate_positions',
    'build_positions',
    'effective_fills',
    'capture',
    'HedgeCapture',
    'PositionMap',
]
