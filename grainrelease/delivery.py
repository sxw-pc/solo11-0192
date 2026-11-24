"""Delivery promises and executable routes over captured road evidence."""
from datetime import datetime, timedelta
from heapq import heappop, heappush

from .carriers import carriers
from .route_capture import capture, segment_state


def _minutes(iso, minutes):
    return (datetime.fromisoformat(iso) + timedelta(minutes=minutes)).isoformat()


def _closed(closures, frm, to, axle_class, depart):
    key = f'{frm}->{to}'
    for closure in closures:
        segment = closure.get('segment') or f"{closure.get('from')}->{closure.get('to')}"
        if segment != key:
            continue
        if closure.get('axle_class', 'all') not in ('all', axle_class):
            continue
        start, end = closure.get('effective_from'), closure.get('effective_until')
        if depart is None or not start or not end or start <= depart < end:
            return True
    return False


def assignments(events, travel):
    """Provisional per-stop pick with departure time and closures honoured."""
    located = carriers(events)
    depart = travel.get('depart_after')
    closures = travel.get('closures') or []
    picks = {}
    for stop in travel['stops']:
        best = None
        for cid, record in located.items():
            depot = record.get('depot')
            if not depot or depot not in travel or stop not in travel[depot]:
                continue
            if _closed(closures, depot, stop, record.get('axle_class'), depart):
                continue
            candidate = {'carrier': cid, 'depot': depot, 'minutes': travel[depot][stop],
                         'depart_at': depart,
                         'arrive_at': _minutes(depart, travel[depot][stop]) if depart else None}
            if best is None or (candidate['minutes'], cid) < (best['minutes'], best['carrier']):
                best = candidate
        if best is not None:
            picks[stop] = best
    return picks


def assign(events, travel):
    """Provisional nearest carrier for a delivery."""
    return {stop: pick['carrier'] for stop, pick in assignments(events, travel).items()}


def build_graph(segments):
    """Directed adjacency; only ``oneway`` segments forbid the reverse leg."""
    graph = {}
    for seg in segments:
        graph.setdefault(seg['from'], []).append(seg)
        if not seg.get('oneway'):
            graph.setdefault(seg['to'], []).append({**seg, 'from': seg['to'], 'to': seg['from']})
    return graph


def route(graph, ledger, origin, stop, axle_class, carrier_id, depart, depart_after):
    """Earliest-arrival path over the live evidence ledger.

    Axle-limited segments are skipped outright; temporarily closed segments
    may be waited out until the announcement lapses.  Returns ``None`` when
    no evidenced path exists -- the caller must not assume arrival anyway.
    """
    best = {origin: depart}
    legs = {}
    queue = [(depart, origin)]
    while queue:
        moment, node = heappop(queue)
        if moment > best[node]:
            continue
        if node == stop:
            break
        for seg in graph.get(node, []):
            if seg.get('axle_limit') and seg['axle_limit'] != axle_class:
                continue
            entry = moment
            state = segment_state(ledger, seg['from'], seg['to'],
                                  axle_class, carrier_id, entry, depart_after)
            while not state['open'] and state['reopen_at'] and state['reopen_at'] > entry:
                entry = state['reopen_at']
                state = segment_state(ledger, seg['from'], seg['to'],
                                      axle_class, carrier_id, entry, depart_after)
            if not state['open']:
                continue
            arrive = _minutes(entry, seg['minutes'])
            if seg['to'] not in best or arrive < best[seg['to']]:
                best[seg['to']] = arrive
                legs[seg['to']] = {'from': node, 'to': seg['to'], 'depart_at': entry,
                                   'arrive_at': arrive, 'minutes': seg['minutes'],
                                   'evidence': state['evidence'], 'permits': state['permits']}
                heappush(queue, (arrive, seg['to']))
    if stop not in best:
        return None
    chain = []
    node = stop
    while node != origin:
        chain.append(legs[node])
        node = legs[node]['from']
    chain.reverse()
    return {'path': [origin] + [leg['to'] for leg in chain], 'legs': chain,
            'depart_at': chain[0]['depart_at'] if chain else depart,
            'arrive_at': best[stop],
            'evidence': sorted({e for leg in chain for e in leg['evidence']}),
            'permits': sorted({p for leg in chain for p in leg['permits']})}


def plan(reports, roads, customers, depart_after):
    """Form window promises and executable chains from offline evidence.

    One vehicle runs one loading-and-delivery chain at a time: unloading,
    cleaning, and driver rest all count into the next departure.  Customers
    are promised in window order; anything that cannot be evidenced is
    reported as missed or unconfirmed instead of assumed on-time.
    """
    ledger = capture(reports)
    fleet = carriers(reports)
    graph = build_graph(roads['segments'])
    confirmed = {cid: rec for cid, rec in fleet.items() if rec['confirmed']}
    state = {cid: {'location': rec['depot'], 'available_at': depart_after}
             for cid, rec in confirmed.items()}
    chains = {cid: [] for cid in confirmed}
    promises = {}
    missed = {}
    unconfirmed_stops = []
    notice_ids = [n['id'] for n in ledger['notices']]
    issued = max([n.get('issued_at') or '' for n in ledger['notices']] or [''])
    for customer in sorted(customers,
                           key=lambda c: (c['window_until'], c['window_from'], c['stop'])):
        stop = customer['stop']
        candidates = []
        for cid, current in state.items():
            depart = max(depart_after, current['available_at'])
            found = route(graph, ledger, current['location'], stop,
                          confirmed[cid]['axle_class'], cid, depart, depart_after)
            if found is None:
                continue
            arrival = found['arrive_at']
            feasible = (arrival <= customer['window_until']
                        and (not customer.get('dock_close') or arrival <= customer['dock_close']))
            candidates.append({'carrier': cid, 'route': found, 'depart_at': depart,
                               'arrive_at': arrival, 'feasible': feasible})
        if not candidates:
            unconfirmed_stops.append(stop)
            continue
        candidates.sort(key=lambda c: (not c['feasible'], c['arrive_at'], c['carrier']))
        chosen = candidates[0]
        if not chosen['feasible']:
            reason = f"最早到货{chosen['arrive_at']}超过交付窗{customer['window_until']}"
            if customer.get('dock_close') and chosen['arrive_at'] > customer['dock_close']:
                reason += f"，卸货月台{customer['dock_close']}已关闭"
            missed[stop] = {'stop': stop, 'carrier': chosen['carrier'],
                            'earliest_arrival': chosen['arrive_at'],
                            'window_until': customer['window_until'],
                            'dock_close': customer.get('dock_close'),
                            'attempt': {'depart_at': chosen['depart_at'],
                                        'path': chosen['route']['path']},
                            'reason': reason,
                            'evidence': sorted(set(notice_ids) | set(chosen['route']['evidence'])),
                            'issued_at': issued or depart_after}
            continue
        cid = chosen['carrier']
        arrival = chosen['arrive_at']
        turnaround = (customer.get('unload_minutes', 0) + confirmed[cid]['cleaning_minutes']
                      + confirmed[cid]['rest_minutes'])
        state[cid] = {'location': stop, 'available_at': _minutes(arrival, turnaround)}
        chains[cid].append({'customer': stop, 'path': chosen['route']['path'],
                            'legs': chosen['route']['legs'],
                            'depart_at': chosen['route']['depart_at'], 'arrive_at': arrival,
                            'evidence': chosen['route']['evidence'],
                            'permits': chosen['route']['permits']})
        promises[stop] = {'promise_id': f'promise-{stop}', 'stop': stop, 'carrier': cid,
                          'route': chosen['route']['path'],
                          'depart_at': chosen['route']['depart_at'], 'arrive_at': arrival,
                          'service_at': max(arrival, customer['window_from']),
                          'window_from': customer['window_from'],
                          'window_until': customer['window_until'],
                          'dock_close': customer.get('dock_close'),
                          'status': 'kept',
                          'evidence': chosen['route']['evidence'],
                          'permits': chosen['route']['permits'],
                          'history': [{'revision': 1, 'carrier': cid, 'arrive_at': arrival,
                                       'note': '初始规划，客户已签收'}]}
    return {'promises': promises, 'missed': missed,
            'unconfirmed_carriers': sorted(set(fleet) - set(confirmed)),
            'unconfirmed_stops': sorted(unconfirmed_stops),
            'chains': {cid: legs for cid, legs in chains.items() if legs}}


def replan(prior, reports, roads, customers, depart_after):
    """Rebuild promises after late announcements.

    A promise the customer already acknowledged is never silently
    overwritten: still-feasible promises keep their id and append history;
    broken promises are preserved and a traceable rebooking notice is
    issued; promises that can no longer be evidenced are marked
    unconfirmed rather than assumed on-time.
    """
    fresh = plan(reports, roads, customers, depart_after)
    promises = {}
    rebookings = list(prior.get('rebookings', []))
    for stop, old in prior.get('promises', {}).items():
        renewed = fresh['promises'].get(stop)
        revision = len(old['history']) + 1
        if renewed is not None and old['status'] == 'kept':
            history = old['history'] + [{'revision': revision,
                                         'carrier': renewed['carrier'],
                                         'arrive_at': renewed['arrive_at'],
                                         'note': '迟到公告后重建，仍在约定窗口内'}]
            promises[stop] = {**renewed, 'promise_id': old['promise_id'], 'history': history}
        elif stop in fresh['missed']:
            miss = fresh['missed'][stop]
            notice = {'notice_id': f'rebook-{stop}-{len(rebookings) + 1}',
                      'promise_id': old['promise_id'], 'stop': stop,
                      'original': {'carrier': old['carrier'], 'arrive_at': old['arrive_at'],
                                   'window_until': old['window_until']},
                      'earliest_arrival': miss['earliest_arrival'],
                      'reason': miss['reason'], 'evidence': miss['evidence'],
                      'issued_at': miss['issued_at']}
            rebookings.append(notice)
            promises[stop] = {**old, 'status': 'rebooked',
                              'history': old['history'] + [{
                                  'revision': revision, 'carrier': old['carrier'],
                                  'arrive_at': old['arrive_at'],
                                  'note': f"迟到公告后无法履约，已发改约通知{notice['notice_id']}"}]}
        else:
            promises[stop] = {**old, 'status': 'unconfirmed',
                              'history': old['history'] + [{
                                  'revision': revision, 'carrier': old['carrier'],
                                  'arrive_at': None,
                                  'note': '车辆或路线缺证，不得假定准时'}]}
    for stop, renewed in fresh['promises'].items():
        if stop not in promises:
            promises[stop] = renewed
    return {'promises': promises, 'rebookings': rebookings, 'chains': fresh['chains'],
            'missed': fresh['missed'],
            'unconfirmed_carriers': fresh['unconfirmed_carriers'],
            'unconfirmed_stops': fresh['unconfirmed_stops']}
