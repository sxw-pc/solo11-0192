"""Lot identity, moisture, and pest baseline with late samples."""
def latest(events):
    out={}
    for e in events:
        key=e['lot_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def decide(events):
    out={};current=latest(events)
    for lot,e in current.items():
        identity=not any(x['lot_id']!=lot and x['bay_id']==e['bay_id'] and x['stored_from']<e['stored_until'] and x['stored_until']>e['stored_from'] for x in current.values())
        source_ok=all(s['lot_id']==lot and s['witness'] for s in e['pest_samples'])
        peak=max((s['insects_per_kg'] for s in e['pest_samples']),default=0)
        wet=e['moisture_pct']>e['maximum_moisture_pct']
        isolated=peak>e['pest_limit'] or not identity or not source_ok
        out[lot]={'event':e,'identity':identity,'source_ok':source_ok,'peak':peak,'wet':wet,'isolated':isolated,'status':'transfer-required' if wet else 'isolated' if isolated else 'observe'}
    return out
