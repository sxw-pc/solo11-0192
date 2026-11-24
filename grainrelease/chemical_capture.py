"""Executable custody plans for sealed fumigant containers.

A :class:`Depot` loads a site spec (locations, routes, the incompatibility
rule table, authorised staff, licensed recyclers and the current issue-list
notice) and applies a plan of actions.  Every action is validated against
the full state and every intermediate transit position, so:

* forbidden pairs never share a cabinet or staging table at any moment,
  and unknown-type drums stay isolated;
* moves follow the location graph, name two distinct authorised carriers,
  and every transit hop is checked, not just the final cabinet;
* seal breaking, the two-person signoff and the entrusted recycler's final
  receipt are separate records, and a recycling appointment never removes
  a drum from depot inventory before the receipt is final;
* the notified issue list is append-only: any change is an explicit
  revision naming the affected drums, and external handover is final.
"""
import copy
import json
from pathlib import Path

from .chemical_cabinet import location_ok
from .chemical_ledger import EXTERNAL


class CustodyError(ValueError):
    """A plan action would break a custody or safety rule."""


class Depot:
    def __init__(self, spec):
        self.spec = spec
        self.locations = spec['locations']
        self.edges = {frozenset(edge) for edge in spec['routes']}
        self.forbidden = {frozenset(pair) for pair in spec.get('incompatible', [['X', 'Y']])}
        self.staff = set(spec.get('staff', ()))
        self.recyclers = spec.get('recyclers', {})
        self.state = {row['container_id']: dict(row) for row in spec['containers']}
        self.events = []
        self.notices = {}
        self.current_notice_id = None
        self.bookings = {}
        self.external = set()
        start = spec.get('start')
        for row in spec['containers']:
            self.events.append({'kind': 'position', 'container_id': row['container_id'],
                                'location': row['location'], 'chemical': row.get('chemical'),
                                'kg': row.get('kg', 0), 'seal': row.get('seal'), 'at': start})
        notice = spec.get('notice')
        if notice:
            self._publish(notice['notice_id'], notice['available'], None, (), notice.get('at', start))

    @classmethod
    def from_file(cls, path):
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    # -- queries ---------------------------------------------------------
    def snapshot(self, step='current', at=None):
        """Safety state and legal issue list at one moment."""
        locations = {}
        for name, spec in self.locations.items():
            if spec.get('kind') == 'external':
                continue
            occupants = sorted(cid for cid, row in self.state.items() if row['location'] == name)
            kg = sum(self.state[cid].get('kg', 0) for cid in occupants)
            locations[name] = {
                'containers': occupants,
                'kg': kg,
                'safe': kg <= spec.get('capacity_kg', 0)
                        and location_ok([self.state[cid].get('chemical') for cid in occupants],
                                        self.forbidden),
            }
        notice = self.notices.get(self.current_notice_id)
        return {'step': step, 'at': at,
                'locations': locations,
                'safe': {name: row['safe'] for name, row in locations.items()},
                'inventory': sorted(cid for cid in self.state if cid not in self.external),
                'available': dict(notice['available']) if notice else {},
                'pending_revision': self.pending_revision()}

    def pending_revision(self):
        """Drums whose notice entry no longer matches reality.

        A late handover that moved a listed drum, or a final recycler
        receipt, shows up here until a revision names the affected drums.
        """
        notice = self.notices.get(self.current_notice_id)
        if not notice:
            return []
        return sorted(cid for cid, location in notice['available'].items()
                      if cid in self.external
                      or cid not in self.state
                      or self.state[cid]['location'] != location)

    def notice(self, notice_id):
        """Read back any published notice; history is never overwritten."""
        if notice_id not in self.notices:
            raise CustodyError(f'no notice {notice_id}')
        return copy.deepcopy(self.notices[notice_id])

    # -- plan execution --------------------------------------------------
    def run(self, plan):
        snapshots = []
        for action in plan:
            snapshots.extend(self.apply(action))
        return snapshots

    def apply(self, action):
        """Apply one action atomically; the depot is unchanged on failure."""
        trial = copy.deepcopy(self)
        snapshots = trial._apply(dict(action))
        self.__dict__.update(trial.__dict__)
        return snapshots

    def _apply(self, action):
        op = action.get('op')
        if op == 'move':
            return self._move(action['container'], action['to'], action.get('via', ()),
                              action.get('by', ()), action.get('at'))
        if op == 'break_seal':
            return self._break_seal(action['container'], action.get('by'), action.get('at'))
        if op == 'signoff':
            return self._signoff(action['container'], action.get('by', ()), action.get('at'))
        if op == 'book':
            return self._book(action['container'], action['recycler'], action.get('at'))
        if op == 'receive':
            return self._receive(action['container'], action['recycler'],
                                 action.get('at'), action.get('seal'))
        if op == 'revise_notice':
            self._publish(action['notice_id'], action['available'], action.get('revises'),
                          action.get('affected', ()), action.get('at'))
            return [self.snapshot(f"notice {action['notice_id']}", action.get('at'))]
        raise CustodyError(f'unknown action {op!r}')

    # -- guards ------------------------------------------------------------
    def _in_depot(self, container):
        if container in self.external:
            raise CustodyError(f'{container} already crossed the external boundary; handover is final')
        row = self.state.get(container)
        if row is None:
            raise CustodyError(f'unknown container {container}')
        return row

    def _carriers(self, by, what):
        by = list(by)
        if len(by) != 2 or len(set(by)) != 2:
            raise CustodyError(f'{what} needs two distinct carriers, got {by}')
        if not set(by) <= self.staff:
            raise CustodyError(f'{what} carriers must be authorised staff, got {by}')
        return by

    def _licensed(self, recycler, at):
        licence = self.recyclers.get(recycler)
        if licence is None:
            raise CustodyError(f'{recycler} is not an entrusted recycler')
        until = licence.get('license_until')
        if until is not None and at is not None and at > until:
            raise CustodyError(f'{recycler} licence expired at {until}, cannot act at {at}')

    def _check_location(self, name):
        spec = self.locations[name]
        if spec.get('kind') == 'external':
            return
        occupants = [row for row in self.state.values() if row['location'] == name]
        kg = sum(row.get('kg', 0) for row in occupants)
        if kg > spec.get('capacity_kg', 0):
            raise CustodyError(f'{name} holds {kg}kg over its {spec.get("capacity_kg", 0)}kg limit')
        if not location_ok([row.get('chemical') for row in occupants], self.forbidden):
            raise CustodyError(f'{name} would hold an incompatible pair or an unisolated unknown drum')

    # -- actions -----------------------------------------------------------
    def _move(self, container, to, via, by, at):
        row = self._in_depot(container)
        carriers = self._carriers(by, 'moves')
        path = [row['location'], *via, to]
        for name in path[1:]:
            spec = self.locations.get(name)
            if spec is None:
                raise CustodyError(f'unknown location {name}')
            if spec.get('kind') == 'external':
                raise CustodyError('only a licensed recycler receipt crosses the external boundary')
        for a, b in zip(path, path[1:]):
            if frozenset((a, b)) not in self.edges:
                raise CustodyError(f'no route from {a} to {b}')
        snapshots = []
        for hop in path[1:]:
            origin = row['location']
            row['location'] = hop
            self._check_location(hop)
            self.events.append({'kind': 'handover', 'container_id': container,
                                'from': origin, 'to': hop, 'by': carriers, 'at': at})
            self.events.append({'kind': 'position', 'container_id': container, 'location': hop,
                                'chemical': row.get('chemical'), 'kg': row.get('kg', 0),
                                'seal': row.get('seal'), 'handover': carriers, 'at': at})
            snapshots.append(self.snapshot(f'move {container} {origin}->{hop}', at))
        return snapshots

    def _break_seal(self, container, by, at):
        row = self._in_depot(container)
        if by not in self.staff:
            raise CustodyError('seal breaking needs an authorised recorder')
        if row.get('seal_status') == 'broken':
            raise CustodyError(f'{container} seal is already broken')
        row['seal_status'] = 'broken'
        self.events.append({'kind': 'seal_break', 'container_id': container,
                            'seal': row.get('seal'), 'by': by, 'at': at})
        return [self.snapshot(f'break seal {container}', at)]

    def _signoff(self, container, by, at):
        self._in_depot(container)
        carriers = self._carriers(by, 'signoff')
        self.events.append({'kind': 'signoff', 'container_id': container, 'by': carriers, 'at': at})
        return [self.snapshot(f'signoff {container}', at)]

    def _book(self, container, recycler, at):
        self._in_depot(container)
        self._licensed(recycler, at)
        record = {'kind': 'booking', 'container_id': container, 'recycler': recycler, 'at': at}
        self.bookings[container] = record
        self.events.append(record)
        return [self.snapshot(f'book {container} with {recycler}', at)]

    def _receive(self, container, recycler, at, seal):
        row = self._in_depot(container)
        self._licensed(recycler, at)
        if not seal:
            raise CustodyError('return shipment needs a recorded transport seal')
        booking = self.bookings.get(container)
        if booking is None or booking['recycler'] != recycler:
            raise CustodyError(f'no recycling appointment for {container} with {recycler}')
        history = [e for e in self.events if e.get('container_id') == container]
        booked = history.index(booking)
        broken = [i for i, e in enumerate(history) if e['kind'] == 'seal_break' and i > booked]
        signed = [i for i, e in enumerate(history) if e['kind'] == 'signoff' and i > booked]
        if not broken:
            raise CustodyError('seal breaking must be recorded before final receipt')
        if not signed or min(signed) < max(broken):
            raise CustodyError('two-person signoff must be recorded after the seal break')
        gates = [n for edge in self.edges if row['location'] in edge for n in edge
                 if n != row['location'] and self.locations[n].get('kind') == 'external']
        if not gates:
            raise CustodyError(f"recycler cannot receive at {row['location']}; no external boundary")
        row['location'] = EXTERNAL
        row['seal'] = seal
        self.external.add(container)
        self.events.append({'kind': 'receipt', 'container_id': container, 'recycler': recycler,
                            'seal': seal, 'at': at})
        self.events.append({'kind': 'position', 'container_id': container, 'location': EXTERNAL,
                            'chemical': row.get('chemical'), 'kg': row.get('kg', 0),
                            'seal': seal, 'at': at})
        return [self.snapshot(f'recycler {recycler} received {container}', at)]

    def _publish(self, notice_id, ids, revises, affected, at):
        if notice_id in self.notices:
            raise CustodyError(f'notice {notice_id} already published; notices are append-only')
        if revises != self.current_notice_id:
            raise CustodyError(f'notice {notice_id} must revise the current notice '
                               f'{self.current_notice_id}')
        mapping = {}
        for cid in ids:
            if cid in self.external or cid not in self.state:
                raise CustodyError(f'{cid} is no longer depot inventory and cannot be issued')
            mapping[cid] = self.state[cid]['location']
        affected = sorted(affected)
        if self.current_notice_id is None:
            if affected:
                raise CustodyError('the first notice affects no predecessor entries')
        else:
            old = self.notices[self.current_notice_id]['available']
            delta = sorted(cid for cid in set(old) | set(mapping) if old.get(cid) != mapping.get(cid))
            if affected != delta:
                raise CustodyError(f'revision must name the affected drums {delta}, got {affected}')
        record = {'kind': 'notice', 'notice_id': notice_id, 'available': mapping,
                  'revises': revises, 'affected': affected, 'at': at}
        self.notices[notice_id] = record
        self.events.append(record)
        self.current_notice_id = notice_id
