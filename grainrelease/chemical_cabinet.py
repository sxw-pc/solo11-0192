"""Chemical cabinet and staging-table safety verdicts.

A location is safe only when its total weight fits the cabinet limit *and*
its occupants are mutually compatible: the synthesis rule table forbids
pairs such as X/Y from sharing a cabinet or a staging table at any moment,
and drums of unknown type must stay isolated.  Weight under the limit never
by itself makes a forbidden pair safe.
"""
from itertools import combinations

from .chemical_ledger import EXTERNAL, containers

#: Synthesis rule table: chemical pairs that must never share a location.
FORBIDDEN = {frozenset(('X', 'Y'))}


def location_ok(chemicals, forbidden=FORBIDDEN):
    """Check one location's occupants against the rule table."""
    chemicals = list(chemicals)
    if any(chemical is None for chemical in chemicals):
        return len(chemicals) == 1  # unknown-type drums stay isolated
    return all(frozenset(pair) not in forbidden for pair in combinations(chemicals, 2))


def safe(events, capacities, forbidden=FORBIDDEN):
    """Per-location safety verdict from the latest recorded positions."""
    by_location = {}
    for row in containers(events).values():
        location = row.get('location', row.get('cabinet'))
        if location == EXTERNAL:
            continue  # left depot custody; the recycler accounts for it now
        by_location.setdefault(location, []).append(row)
    verdict = {}
    for location, rows in by_location.items():
        kg = sum(row.get('kg', 0) for row in rows)
        limit = capacities.get(location)
        fits = limit is not None and kg <= limit
        verdict[location] = bool(fits and location_ok([row.get('chemical') for row in rows], forbidden))
    return verdict
