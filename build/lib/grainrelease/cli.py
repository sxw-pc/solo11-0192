import argparse
import json
from pathlib import Path
from .store import connect, import_file, report, audit

IMPORTS={'lot-import':'lot','fumigate-import':'fumigate','release-import':'release'}
STAGES=[('lot','lot-ok.json'),('fumigate','fumigate-ok.json'),('release','release-ok.json'),('lot','lot-bad.json'),('fumigate','fumigate-bad.json'),('release','release-bad.json')]

def main(argv=None):
    p=argparse.ArgumentParser(prog='grainrelease')
    p.add_argument('command',choices=[*IMPORTS,'demo','report','audit'])
    p.add_argument('--db',default='state.sqlite')
    p.add_argument('--file')
    a=p.parse_args(argv)
    db=connect(a.db)
    if a.command in IMPORTS:
        if not a.file:p.error('--file required')
        value=import_file(db,IMPORTS[a.command],a.file)
    elif a.command=='demo':
        root=Path(__file__).resolve().parents[1]/'fixtures';stages=[]
        for kind,name in STAGES:
            imported=import_file(db,kind,root/name)
            stages.append({'fixture':name,**imported,'decisions':len(report(db)['decisions'])})
        value={'stages':stages,'report':report(db),'audit':audit(db),'imported_module':str(Path(__file__).resolve())}
    elif a.command=='report':value=report(db)
    else:value=audit(db)
    print(json.dumps(value,ensure_ascii=False,indent=2))
    return 0 if a.command!='audit' or value['ok'] else 2
