"""Project three evidence ledgers into revisioned business decisions."""
from . import lots, fumigation, releases

def business_records(e):
    k=e['kind'];spec={'lot':('粮批虫害确权','lot'),'fumigate':('熏蒸措施履约','treatment'),'release':('通风检测与出库','clearance')}[k]
    return [(spec[0],spec[1],e['lot_id'],e)]

def calculate(events):
    by={k:[e for e in events if e['kind']==k] for k in ('lot','fumigate','release')}
    l=lots.decide(by['lot']);f=fumigation.decide(by['fumigate'],l);r=releases.decide(by['release'],l,f)
    decisions={};states=[]
    def put(name,ok,reason,evidence):decisions[name]={'result':'pass' if ok else 'hold','reason':reason,'evidence':evidence}
    for lot,x in l.items():
        e=x['event'];base=f'lot:{lot}:'
        put(base+'identity',x['identity'],'仓位身份唯一' if x['identity'] else '仓位冲突',[e['id']])
        put(base+'pest',not x['isolated'],'虫样低于隔离阈值' if not x['isolated'] else '虫样升级隔离',[e['id']])
        put(base+'moisture',not x['wet'],'含水率适合熏蒸' if not x['wet'] else '先行转仓干燥',[e['id']])
        states.append(('粮批虫害确权',lot,x['status'],e['version']))
    for lot,x in f.items():
        e=x['event'];base=f'fumigate:{lot}:'
        put(base+'dose',x['dose'],'许可剂量适配体积' if x['dose'] else '剂量不足或超限',[e['id']])
        put(base+'operator',x['qualified'],'施药资质有效' if x['qualified'] else '班组证照不覆盖施药',[e['id']])
        put(base+'seal',not x['breach'],'封仓时钟未中断' if not x['breach'] else '破封重置安全时钟',[e['id']])
        put(base+'boundary',x['boundary'],'并行仓隔离见证有效' if x['boundary'] else '并行仓通道未隔离',[e['id']])
        states.append(('熏蒸措施履约',lot,x['status'],e['version']))
    for lot,x in r.items():
        e=x['event'];base=f'release:{lot}:'
        put(base+'ventilation',x['interval'],'通风间隔达标' if x['interval'] else '通风时长不足',[e['id']])
        put(base+'retest',x['independent'] and x['residue'],'独立复测残留达标' if x['independent'] and x['residue'] else '复测不独立或残留超限',[e['id']])
        put(base+'late-pest',x['pests'],'虫样未撤销资格' if x['pests'] else '迟到虫样撤销出库',[e['id'],l[lot]['event']['id']] if lot in l else [e['id']])
        put(base+'dispatch',x['released'],'可出库交付' if x['released'] else '维持隔离',[e['id']])
        states.append(('通风检测与出库',lot,x['status'],e['version']))
    return decisions,states
