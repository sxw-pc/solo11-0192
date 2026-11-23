"""Permit dose, operator, seal clock, and adjacent bay boundaries."""
def latest(events):
    out={}
    for e in events:
        key=e['lot_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def decide(events,lots):
    out={};current=latest(events)
    for lot,e in current.items():
        source=lots.get(lot)
        target=e['volume_m3']*e['permitted_g_per_m3']
        dose=e['dose_g']>=target and e['dose_g']<=target*1.1
        qualified=e['operator_license_from']<=e['applied_at']<=e['operator_license_until']
        boundary=not any(x['lot_id']!=lot and x['adjacent_group']==e['adjacent_group'] and x['applied_at']<e['seal_until'] and x['seal_at']<e['seal_until'] and (not x['isolation_witness'] or not e['isolation_witness']) for x in current.values())
        breach=bool(e.get('breach_at'))
        clock=e.get('resealed_at') if breach else e['seal_at']
        sealed=bool(clock and clock<=e['seal_until'])
        complete=bool(source and source['identity'] and not source['wet'] and dose and qualified and boundary and sealed)
        out[lot]={'event':e,'dose':dose,'qualified':qualified,'boundary':boundary,'breach':breach,'clock':clock,'complete':complete,'status':'treated' if complete else 'hold'}
    return out
