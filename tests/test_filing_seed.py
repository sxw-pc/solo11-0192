import json
import unittest
from pathlib import Path
from grainrelease.filing import status


class FilingSeedTest(unittest.TestCase):
    def test_header_ack_appears_accepted(self):
        fixture = json.loads((Path(__file__).parents[1] / 'fixtures' / 'manifest-ok.json').read_text())
        self.assertEqual(status(fixture['document'], fixture['acknowledgements']), 'accepted')


if __name__ == '__main__':
    unittest.main()
