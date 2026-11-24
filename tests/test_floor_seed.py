import json
import unittest
from pathlib import Path
from grainrelease.floor_load import safe

class FloorSeedTest(unittest.TestCase):
    def test_simple_capacity(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'floor-ok.json').read_text())
        self.assertEqual(safe(fixture['events'],fixture['capacities']),{'B1':True})

if __name__=='__main__': unittest.main()
