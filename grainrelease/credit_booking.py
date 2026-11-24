"""Provisional credit booking decision."""
from .customer_credit import limits


def accepted(credit_events, bookings):
    caps=limits(credit_events)
    return {row['booking_id']:row['amount']<=caps[row['customer_id']] for row in bookings}
