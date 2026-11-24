import json
import unittest
from pathlib import Path

from grainrelease.filing import inspect_status, status
from grainrelease.manifest import sign_document

FIX = Path(__file__).parents[1] / 'fixtures'


class FilingSeedTest(unittest.TestCase):
    def test_half_packet_stays_pending(self):
        # M1 有三页但只有页 1 的同 ID 回执：旧实现误报 accepted，
        # 闭合口径要求三页同根同版回执齐全，故半包必须保持 pending。
        fixture = json.loads((FIX / 'manifest-ok.json').read_text())
        self.assertEqual(status(fixture['document'], fixture['acknowledgements']), 'pending')
        detail = inspect_status(fixture['document'], fixture['acknowledgements'])
        self.assertEqual(detail['received_numbers'], [1])
        self.assertEqual(detail['missing_numbers'], [2, 3])
        self.assertEqual(detail['pages'][0]['receipt_count'], 1)

    def test_complete_same_root_packet_accepts(self):
        fixture = json.loads((FIX / 'manifest-ok.json').read_text())
        document = fixture['document']
        acks = list(fixture['acknowledgements'])
        from grainrelease.filing_capture import sign_receipt
        from grainrelease.manifest import page_digest
        token = fixture['acknowledgements'][0]['token']
        for page in document['pages']:
            if page['number'] == 1:
                continue
            acks.append(sign_receipt({
                'receipt_id': f'R-SEED-P{page["number"]}',
                'manifest_id': document['manifest_id'],
                'content_root': document['content_root'],
                'token': token,
                'number': page['number'],
                'page_version': page['version'],
                'digest': page_digest(page),
            }))
        self.assertEqual(status(document, acks), 'accepted')
        self.assertEqual(inspect_status(document, acks)['missing_numbers'], [])

    def test_gateway_headers_without_regulator_signature_are_rejected(self):
        fixture = json.loads((FIX / 'manifest-ok.json').read_text())
        header = dict(fixture['gateway_acks'][0])
        header['receipt_id'] = 'GW-FAKE'
        detail = inspect_status(fixture['document'], [header])
        self.assertEqual(detail['status'], 'pending')
        self.assertTrue(any('签名' in item['reason'] for item in detail['rejected']))


if __name__ == '__main__':
    unittest.main()
