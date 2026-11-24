"""Hedge margin requirements and tonight's margin call.

:func:`required` preserves the original seed contract --
``required(fills, per_lot) == {"WHEAT-JUL": 2000}`` -- while understanding
trade ids, cancellation/correction chains and account/month segregation.

The richer :func:`statement` keeps initial margin, daily variation and posted
collateral in separate ledgers and reports available cash, the margin call and
the unconfirmable difference.  Use :class:`MarginCallBook` for versioned
notices and clearing adjustment notes.
"""
from .hedge_capture import (
    CallBook,
    HedgeCapture,
    MarginCallBook,
    RequirementMap,
    Statement,
    assess,
    build_ledger,
    calculate_margin,
    capture,
    capture_reports,
    evaluate,
    final_margin,
    issue_margin_call,
    load_capture,
    margin_call,
    margin_due,
    margin_statement,
    payable_margin,
    reconcile,
    run_clearing,
    statement,
)
from .hedges import positions


def required(events=None, per_lot=None, **options):
    """Per-contract (or account/contract) initial margin requirement.

    Works with the legacy ``required(fills, {"WHEAT-JUL": 1000})`` calling
    convention and with a full fixture dict as the only argument.
    """
    from .hedge_capture import _coerce
    if isinstance(events, HedgeCapture):
        return events.requirement_map()
    cap = _coerce(events, per_lot, options)
    return cap.requirement_map()


def requirements(events=None, per_lot=None, **options):
    return required(events, per_lot, **options)


def calculate(events=None, per_lot=None, **options):
    return statement(events, per_lot, **options)


def margin_required(events=None, per_lot=None, **options):
    return required(events, per_lot, **options)


def build_margin_call(*args, **kwargs):
    """Return a full margin :class:`Statement` (alias of :func:`statement`)."""
    return statement(*args, **kwargs)


def create_margin_call(*args, **kwargs):
    return statement(*args, **kwargs)


def run_margin(*args, **kwargs):
    return statement(*args, **kwargs)


__all__ = [
    'required',
    'requirements',
    'calculate',
    'margin_required',
    'build_margin_call',
    'create_margin_call',
    'run_margin',
    'statement',
    'margin_statement',
    'final_margin',
    'margin_due',
    'payable_margin',
    'assess',
    'evaluate',
    'reconcile',
    'run_clearing',
    'calculate_margin',
    'margin_call',
    'issue_margin_call',
    'positions',
    'capture',
    'capture_reports',
    'load_capture',
    'build_ledger',
    'HedgeCapture',
    'MarginCallBook',
    'CallBook',
    'RequirementMap',
    'Statement',
]
