import json
import unittest
from pathlib import Path
from grainrelease.margin import required

class HedgeSeedTest(unittest.TestCase):
    def test_net_lots(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'hedge-ok.json').read_text())
        self.assertEqual(required(fixture['fills'],fixture['per_lot']),{'WHEAT-JUL':2000})

if __name__=='__main__': unittest.main()
