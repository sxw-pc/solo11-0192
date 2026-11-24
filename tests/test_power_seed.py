import json
import unittest
from pathlib import Path
from grainrelease.demand_bill import peak_kw

class PowerSeedTest(unittest.TestCase):
    def test_peak(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'power-ok.json').read_text())
        self.assertEqual(peak_kw(fixture['events']),{'E1':120})

if __name__=='__main__': unittest.main()
