import json
import unittest
from pathlib import Path
from grainrelease.chemical_cabinet import safe

class ChemicalSeedTest(unittest.TestCase):
    def test_capacity(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'chemical-ok.json').read_text())
        self.assertEqual(safe(fixture['events'],fixture['capacities']),{'A':True})

if __name__=='__main__': unittest.main()
