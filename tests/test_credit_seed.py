import json
import unittest
from pathlib import Path
from grainrelease.credit_booking import accepted

class CreditSeedTest(unittest.TestCase):
    def test_provisional_booking(self):
        fixture=json.loads((Path(__file__).parents[1]/'fixtures'/'credit-ok.json').read_text())
        self.assertEqual(accepted(fixture['credit'],fixture['bookings']),{'R1':True})

if __name__=='__main__': unittest.main()
