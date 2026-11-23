import tempfile
import unittest
from pathlib import Path

import grainrelease
from grainrelease.store import connect, import_file, report, audit

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'

class Workflow(unittest.TestCase):
    def setUp(self):
        self.db=connect(':memory:')
        for kind,name in [('lot','lot-ok.json'),('fumigate','fumigate-ok.json'),('release','release-ok.json')]:
            import_file(self.db,kind,FIX/name)

    def test_current_module_and_normal_release(self):
        self.assertEqual(Path(grainrelease.__file__).resolve().parents[1],ROOT)
        states={x['entity_id']:x['state'] for x in report(self.db)['entities'] if x['subsystem']=='通风检测与出库'}
        self.assertEqual(states,{'rice-8':'released','wheat-7':'released'})

    def test_late_pest_reopens_isolation(self):
        import_file(self.db,'lot',FIX/'lot-bad.json')
        self.assertEqual(report(self.db)['decisions']['release:wheat-7:late-pest']['result'],'hold')
        states={x['entity_id']:x['state'] for x in report(self.db)['entities'] if x['subsystem']=='通风检测与出库'}
        self.assertEqual(states['wheat-7'],'isolated')

    def test_broken_seal_invalidates_old_release(self):
        import_file(self.db,'fumigate',FIX/'fumigate-bad.json')
        self.assertEqual(report(self.db)['decisions']['fumigate:rice-8:seal']['result'],'hold')
        self.assertEqual(report(self.db)['decisions']['release:rice-8:ventilation']['result'],'hold')
        import_file(self.db,'release',FIX/'release-bad.json')
        self.assertEqual(report(self.db)['decisions']['release:rice-8:retest']['result'],'hold')
        self.assertTrue(audit(self.db)['ok'])

    def test_duplicate_and_atomic_failure(self):
        self.assertTrue(import_file(self.db,'lot',FIX/'lot-ok.json')['idempotent'])
        before=report(self.db)
        with tempfile.TemporaryDirectory() as td:
            bad=Path(td)/'bad.json';bad.write_text('[{"kind":"lot","id":"L-1","version":9}]',encoding='utf-8')
            with self.assertRaises(ValueError):import_file(self.db,'lot',bad)
        self.assertEqual(before,report(self.db))

if __name__=='__main__':unittest.main()
