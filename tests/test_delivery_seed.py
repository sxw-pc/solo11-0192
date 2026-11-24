import json
import unittest
from pathlib import Path
from grainrelease.delivery import assign

class DeliverySeedTest(unittest.TestCase):
    def test_nearest_carrier(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'delivery-ok.json').read_text())
        self.assertEqual(assign(fixture['events'],fixture['travel']),{'D1':'C1'})

if __name__=='__main__': unittest.main()
