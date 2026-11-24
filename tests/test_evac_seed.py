import json
import unittest
from pathlib import Path
from grainrelease.evacuation import complete

class EvacSeedTest(unittest.TestCase):
    def test_complete(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'evac-ok.json').read_text())
        self.assertTrue(complete(fixture['gates'],fixture['muster']))

if __name__=='__main__': unittest.main()
