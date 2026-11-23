"""Independent residue samples, ventilation interval, and shipment state."""
from datetime import datetime

def latest(events):
    out={}
    for e in events:
        key=e['lot_id']
        if key not in out or (e['version'],e['id'])>(out[key]['version'],out[key]['id']):out[key]=e
    return out

def decide(events,lots,fumes):
    out={}
    for lot,e in latest(events).items():
        source=lots.get(lot);fume=fumes.get(lot)
        clock=fume['clock'] if fume else None
        interval=bool(clock and e['ventilation_started_at']>=fume['event']['seal_until'] and (datetime.fromisoformat(e['sampled_at'])-datetime.fromisoformat(e['ventilation_started_at'])).total_seconds()/3600>=e['minimum_ventilation_hours'])
        samples=e['residue_samples']
        independent=bool(fume and len(samples)>=2 and len({s['witness'] for s in samples})>=2 and all(s['witness']!=fume['event']['operator'] and s['calibrated_until']>=e['sampled_at'] for s in samples))
        residue=bool(samples and all(s['mg_per_kg']<=e['residue_limit_mg_per_kg'] for s in samples))
        pests=bool(source and source['peak']<=source['event']['pest_limit'] and source['source_ok'])
        booking=e['dispatch_at']>=e['sampled_at']
        released=bool(source and source['identity'] and not source['wet'] and fume and fume['complete'] and interval and independent and residue and pests and booking)
        out[lot]={'event':e,'interval':interval,'independent':independent,'residue':residue,'pests':pests,'booking':booking,'released':released,'status':'released' if released else 'isolated'}
    return out
