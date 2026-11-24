import copy
import json
import tempfile
import unittest
from pathlib import Path

from grainrelease import filing_capture as fc
from grainrelease.filing import inspect_status, status
from grainrelease.filing_capture import (
    apply_receipt,
    capture_original,
    connect,
    inspect,
    mark_sent,
    open_send_round,
    record_gateway_ack,
    record_summary_receipt,
    reopen_with_partial_proof,
    retry_round,
    verify_receipt_signature,
)
from grainrelease.manifest import page_digest, prepare, sign_document, verify_document

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


class FilingRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.fx = json.loads((FIX / 'manifest-mixed-version.json').read_text(encoding='utf-8'))
        self.v1 = self.fx['documents']['v1']
        self.v2 = self.fx['documents']['v2']
        self.root_v1 = self.fx['content_roots']['v1']
        self.root_v2 = self.fx['content_roots']['v2']
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def _replay_outage(self):
        """Steps 1-4 of the fixture: v1 accepted, v2 outage, mixed late receipts."""
        capture_original(self.db, self.v1)
        step1, step2, step3, step4 = self.fx['steps']
        open_send_round(self.db, 'M1', self.root_v1,
                        page_numbers=step1['round']['page_numbers'],
                        token=step1['round']['token'], round_id=step1['round']['round_id'])
        for ack in step1['gateway_acks']:
            record_gateway_ack(self.db, ack)
        for receipt in step1['receipts']:
            apply_receipt(self.db, receipt)
        # v2 capture supersedes the v1 accepted announcement
        capture_original(self.db, self.v2)
        open_send_round(self.db, 'M1', self.root_v2,
                        page_numbers=step2['round']['page_numbers'],
                        token=step2['round']['token'], round_id=step2['round']['round_id'])
        for item in step2['sent']:
            mark_sent(self.db, step2['round']['round_id'], item['number'], item.get('error'))
        for ack in step2['gateway_acks']:
            record_gateway_ack(self.db, ack)
        # duplicate v2 page-1 receipt is delivered twice (ID dedup must hold)
        for receipt in step3['receipts']:
            apply_receipt(self.db, receipt)
        for receipt in step4['receipts']:
            apply_receipt(self.db, receipt)
        return inspect(self.db, 'M1')

    # ---- signing / prepare boundary ----

    def test_prepare_returns_manifest_id_and_pages_with_root(self):
        prepared = prepare(self.v2)
        self.assertEqual(prepared['manifest_id'], 'M1')
        self.assertEqual([p['number'] for p in prepared['pages']], [1, 2, 3])
        self.assertEqual(prepared['content_root'], self.root_v2)

    def test_page_signature_and_root_binding_verified(self):
        self.assertEqual(verify_document(self.v1), self.root_v1)
        tampered = copy.deepcopy(self.v2)
        tampered['pages'][1]['payload']['load_kg'] = 99999
        with self.assertRaises(ValueError):
            verify_document(tampered)
        mixed = copy.deepcopy(self.v2)
        # splice a v1 page into the v2 document
        mixed['pages'][1] = copy.deepcopy(self.v1['pages'][1])
        with self.assertRaises(ValueError):
            verify_document(mixed)

    def test_bad_page_signature_rejected_at_capture(self):
        bad = copy.deepcopy(self.v2)
        bad['pages'][0]['signature'] = 'deadbeef'
        with self.assertRaises(ValueError):
            capture_original(self.db, bad)
        self.assertEqual(
            self.db.execute('SELECT COUNT(*) FROM captured_roots').fetchone()[0], 0)

    # ---- half packet / mixed pages ----

    def test_half_packet_naive_id_check_vs_root_aware_status(self):
        state = self._replay_outage()
        # the naive rule the old code used would accept on same-ID presence
        mixed_acks = self.fx['mixed_acknowledgements']
        self.assertTrue(any(r['manifest_id'] == 'M1' for r in mixed_acks))
        # root-aware verdict must stay pending: only a v2 page-1 receipt exists
        self.assertEqual(status(self.v2, mixed_acks), 'pending')
        detail = inspect_status(self.v2, mixed_acks)
        self.assertEqual(detail['content_root'], self.root_v2)
        self.assertEqual(detail['received_numbers'], [1])
        self.assertEqual(detail['missing_numbers'], [2, 3])
        self.assertEqual(detail['pages'][1]['required_version'], 2)
        self.assertIsNone(detail['pages'][1]['received_version'])
        # late v1 receipts are rejected as mixed-version rather than counted
        rejected_ids = {item.get('receipt_id') for item in detail['rejected']}
        self.assertIn('R-V1-P2-LATE', rejected_ids)
        self.assertIn('R-V1-P3-LATE', rejected_ids)
        self.assertEqual(state['status'], 'pending')

    def test_prepare_then_status_flips_back_to_pending_after_v2(self):
        # v1 with its three real receipts was accepted ...
        state = self._replay_outage()
        v1_detail = inspect_status(
            self.v1, self.fx['steps'][0]['receipts'] + self.fx['steps'][3]['receipts'])
        self.assertEqual(v1_detail['status'], 'accepted')
        # ... but once page 2 moves to v2, the three same-ID receipts no longer close v2
        self.assertEqual(status(self.v2, self.fx['mixed_acknowledgements']), 'pending')
        v2_root = next(r for r in state['roots'] if r['content_root'] == self.root_v2)
        self.assertEqual(v2_root['missing_numbers'], [2, 3])
        self.assertFalse(v2_root['complete'])

    def test_receipt_signature_and_wrong_root_are_rejected(self):
        capture_original(self.db, self.v2)
        open_send_round(self.db, 'M1', self.root_v2,
                        token=self.fx['tokens']['v2'], round_id='RND-V2-STANDALONE')
        forged = dict(self.fx['steps'][2]['receipts'][0])
        forged = dict(forged)
        forged['regulator_signature'] = '0' * len(forged['regulator_signature'])
        with self.assertRaises(ValueError):
            apply_receipt(self.db, forged)
        v1_receipt = dict(self.fx['steps'][0]['receipts'][1])  # v1 page 2
        with self.assertRaises(ValueError):
            apply_receipt(self.db, v1_receipt)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0], 0)

    def test_receipt_id_dedup_and_per_page_counts(self):
        state = self._replay_outage()
        v2_root = next(r for r in state['roots'] if r['content_root'] == self.root_v2)
        counts = {p['number']: p['receipt_count'] for p in v2_root['pages']}
        self.assertEqual(counts, {1: 1, 2: 0, 3: 0})
        v1_root = next(r for r in state['roots'] if r['content_root'] == self.root_v1)
        v1_counts = {p['number']: p['receipt_count'] for p in v1_root['pages']}
        # late v1 receipts carry new receipt ids and remain traceable on the old root
        self.assertEqual(v1_counts, {1: 1, 2: 2, 3: 2})

    # ---- outage / retry cursor ----

    def test_outage_watermark_and_retry_never_mixes_versions(self):
        self._replay_outage()
        v2_round = next(r for r in inspect(self.db, 'M1')['rounds']
                        if r['round_id'] == 'RND-V2')
        self.assertEqual(v2_round['status'], 'sending')
        self.assertEqual(v2_round['watermark'], 1)
        failed = {item['number']: item for item in v2_round['items']}
        self.assertEqual(failed[2]['sent'], 0)
        self.assertEqual(failed[3]['sent'], 0)
        self.assertIsNotNone(failed[2]['last_error'])
        # retry opens a NEW round/token covering only missing pages, same content root
        new_round = retry_round(self.db, 'M1', self.root_v2,
                                token=self.fx['recovery_b']['retry_token'],
                                round_id=self.fx['recovery_b']['retry_round_id'])
        self.assertEqual(new_round['pages'], [2, 3])
        self.assertNotEqual(new_round['token'], self.fx['tokens']['v2'])
        self.assertEqual(new_round['content_root'], self.root_v2)
        with self.assertRaises(ValueError):
            open_send_round(self.db, 'M1', self.root_v2,
                            token=self.fx['tokens']['v2'])  # token reuse forbidden
        rounds = inspect(self.db, 'M1')['rounds']
        self.assertEqual(len(rounds), 3)
        retry = rounds[-1]
        self.assertEqual([i['page_version'] for i in retry['items']], [2, 1])
        # completing the retry with real v2 receipts closes the same root
        mark_sent(self.db, retry['round_id'], 2)
        mark_sent(self.db, retry['round_id'], 3)
        for page in (self.v2['pages'][1], self.v2['pages'][2]):
            receipt = fc.sign_receipt({
                'receipt_id': f'R-V2-RETRY-P{page["number"]}',
                'manifest_id': 'M1', 'content_root': self.root_v2,
                'token': retry['token'], 'number': page['number'],
                'page_version': page['version'], 'digest': page_digest(page)})
            apply_receipt(self.db, receipt)
        state = inspect(self.db, 'M1')
        self.assertEqual(state['status'], 'accepted')
        self.assertEqual(state['current_content_root'], self.root_v2)

    def test_gateway_ack_does_not_count_as_acceptance(self):
        state = self._replay_outage()
        self.assertTrue(state['gateway_acks'])
        # a gateway header exists for the v2 packet yet no regulator acceptance followed
        gw = next(a for a in state['gateway_acks'] if a['content_root'] == self.root_v2)
        self.assertEqual(gw['number'], 1)
        self.assertEqual(state['status'], 'pending')

    # ---- recovery path A: regulator summary receipt ----

    def test_recovery_path_a_summary_receipt_keeps_v2_accepted(self):
        state = self._replay_outage()
        self.assertEqual(state['status'], 'pending')
        result = record_summary_receipt(self.db, self.fx['recovery_a']['summary_receipt'])
        self.assertTrue(result['inserted'])
        state = inspect(self.db, 'M1')
        self.assertEqual(state['status'], 'accepted')
        self.assertEqual(state['current_content_root'], self.root_v2)
        v2_root = next(r for r in state['roots'] if r['content_root'] == self.root_v2)
        self.assertTrue(v2_root['complete'])
        self.assertEqual(v2_root['missing_numbers'], [])
        # current announcement is the v2 accepted one; v1 accepted remains as history
        states = [(a['content_root'] == self.root_v2, a['state']) for a in state['announcements']]
        self.assertIn((True, 'accepted'), states)
        self.assertTrue(any(a['state'] == 'superseded' for a in state['announcements']))

    def test_summary_receipt_of_wrong_root_is_refused(self):
        self._replay_outage()
        wrong = copy.deepcopy(self.fx['recovery_a']['summary_receipt'])
        wrong['content_root'] = self.root_v1
        with self.assertRaises(ValueError):
            record_summary_receipt(self.db, wrong)

    # ---- recovery path B: partial proof, revoke, new round ----

    def test_recovery_path_b_partial_proof_revokes_and_reopens_pending(self):
        state = self._replay_outage()
        before_ids = {a['announcement_id'] for a in state['announcements']}
        result = reopen_with_partial_proof(self.db, self.fx['recovery_b']['partial_proof'])
        self.assertEqual(result['missing'], [2, 3])
        self.assertEqual(result['arrived'], [1])
        self.assertEqual(result['new_round']['content_root'], self.root_v2)
        self.assertEqual(result['new_round']['pages'], [2, 3])
        state = inspect(self.db, 'M1')
        self.assertEqual(state['status'], 'pending')
        # old accepted announcement is explicitly revoked, current one is pending
        self.assertTrue(any(a['state'] == 'revoked' for a in state['announcements']))
        latest = state['announcements'][-1]
        self.assertEqual(latest['state'], 'pending')
        self.assertEqual(latest['content_root'], self.root_v2)
        self.assertNotIn(latest['announcement_id'], before_ids)
        rounds = state['rounds']
        self.assertEqual(rounds[-1]['page_numbers'], [2, 3])
        # finishing the new round with v2 receipts eventually closes v2
        retry = rounds[-1]
        for page in (self.v2['pages'][1], self.v2['pages'][2]):
            mark_sent(self.db, retry['round_id'], page['number'])
            receipt = fc.sign_receipt({
                'receipt_id': f'R-B-P{page["number"]}',
                'manifest_id': 'M1', 'content_root': self.root_v2,
                'token': retry['token'], 'number': page['number'],
                'page_version': page['version'], 'digest': page_digest(page)})
            apply_receipt(self.db, receipt)
        self.assertEqual(inspect(self.db, 'M1')['status'], 'accepted')

    def test_two_recovery_paths_leave_different_states(self):
        # path A
        db_a = connect(':memory:')
        self.addCleanup(db_a.close)
        capture_original(db_a, self.v1)
        s1 = self.fx['steps'][0]
        open_send_round(db_a, 'M1', self.root_v1, token=s1['round']['token'],
                        round_id=s1['round']['round_id'])
        for receipt in s1['receipts']:
            apply_receipt(db_a, receipt)
        capture_original(db_a, self.v2)
        s2 = self.fx['steps'][1]
        open_send_round(db_a, 'M1', self.root_v2, token=s2['round']['token'],
                        round_id=s2['round']['round_id'])
        record_summary_receipt(db_a, self.fx['recovery_a']['summary_receipt'])
        state_a = inspect(db_a, 'M1')
        # path B on the fully replayed outage (v1 accepted, v2 partial, mixed receipts)
        self._replay_outage()
        reopen_with_partial_proof(self.db, self.fx['recovery_b']['partial_proof'])
        state_b = inspect(self.db, 'M1')
        self.assertEqual(state_a['status'], 'accepted')
        self.assertEqual(state_b['status'], 'pending')
        self.assertTrue(any(a['state'] == 'revoked' for a in state_b['announcements']))
        self.assertFalse(any(a['state'] == 'revoked' for a in state_a['announcements']))

    # ---- announcement scoping and traceability ----

    def test_only_m1_announcements_are_revised(self):
        other = sign_document({'manifest_id': 'M9', 'pages': [
            {'number': 1, 'kind': 'header', 'version': 1, 'payload': {'x': 1}},
            {'number': 2, 'kind': 'load', 'version': 1, 'payload': {'x': 2}},
            {'number': 3, 'kind': 'seal', 'version': 1, 'payload': {'x': 3}},
        ]})
        capture_original(self.db, other)
        other_round = open_send_round(self.db, other['manifest_id'], other['content_root'])
        for page in other['pages']:
            mark_sent(self.db, other_round['round_id'], page['number'])
            apply_receipt(self.db, fc.sign_receipt({
                'receipt_id': f'R-M9-P{page["number"]}',
                'manifest_id': other['manifest_id'], 'content_root': other['content_root'],
                'token': other_round['token'], 'number': page['number'],
                'page_version': page['version'], 'digest': page_digest(page)}))
        m9_before = inspect(self.db, 'M9')
        self.assertEqual(m9_before['status'], 'accepted')
        # exactly one current announcement (the initial pending one is kept as superseded history)
        self.assertEqual(len(m9_before['announcements']), 2)
        self.assertEqual(sum(1 for a in m9_before['announcements'] if a['state'] == 'accepted'), 1)
        # re-capturing the same root is idempotent and adds no announcement
        capture_original(self.db, other)
        self.assertEqual(len(inspect(self.db, 'M9')['announcements']), 2)
        # now drive M1 through the outage/reopen churn
        self._replay_outage()
        reopen_with_partial_proof(self.db, self.fx['recovery_b']['partial_proof'])
        m9 = inspect(self.db, 'M9')
        self.assertEqual(m9['status'], 'accepted')
        self.assertTrue(all(a['manifest_id'] == 'M9' for a in m9['announcements']))
        self.assertEqual(len(m9['announcements']), 2)

    def test_old_version_pages_remain_traceable_and_announcements_read_back(self):
        state = self._replay_outage()
        roots = {r['content_root']: r for r in state['roots']}
        self.assertEqual(set(roots), {self.root_v1, self.root_v2})
        v1_page2 = next(p for p in roots[self.root_v1]['pages'] if p['number'] == 2)
        v2_page2 = next(p for p in roots[self.root_v2]['pages'] if p['number'] == 2)
        self.assertEqual(v1_page2['version'], 1)
        self.assertEqual(v2_page2['version'], 2)
        self.assertNotEqual(v1_page2['digest'], v2_page2['digest'])
        # every announcement (including the superseded v1 accepted one) reads back
        history = state['announcements']
        self.assertTrue(any(
            a['content_root'] == self.root_v1 and a['state'] == 'superseded'
            for a in history))
        reopen_with_partial_proof(self.db, self.fx['recovery_b']['partial_proof'])
        history = inspect(self.db, 'M1')['announcements']
        self.assertIn('revoked', {a['state'] for a in history})
        self.assertIn('pending', {a['state'] for a in history})
        self.assertTrue(all(a.get('supersedes') is None or isinstance(a['supersedes'], str)
                            for a in history))

    # ---- SQLite transaction atomicity ----

    def test_failed_receipt_rolls_back_without_partial_state(self):
        self._replay_outage()
        before = {
            'receipts': self.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],
            'events': self.db.execute('SELECT COUNT(*) FROM capture_events').fetchone()[0],
        }
        bad = dict(self.fx['steps'][0]['receipts'][0])
        bad = dict(bad)
        bad['page_version'] = 99  # digest/version mismatch with captured original
        with self.assertRaises(ValueError):
            apply_receipt(self.db, bad)
        after = {
            'receipts': self.db.execute('SELECT COUNT(*) FROM receipts').fetchone()[0],
            'events': self.db.execute('SELECT COUNT(*) FROM capture_events').fetchone()[0],
        }
        self.assertEqual(before, after)

    def test_persistence_across_database_reopen(self):
        self._replay_outage()
        with tempfile.NamedTemporaryFile(suffix='.sqlite', delete=False) as tmp:
            path = tmp.name
        # dump only data rows; the fresh connect() recreates the schema itself
        backup = '\n'.join(
            line for line in self.db.iterdump()
            if not line.startswith(('BEGIN', 'COMMIT', 'CREATE TABLE'))
        )
        reopened = connect(path)
        self.addCleanup(reopened.close)
        reopened.executescript(backup)
        state = inspect(reopened, 'M1')
        self.assertEqual(state['status'], 'pending')
        self.assertEqual(len(state['rounds']), 2)
        Path(path).unlink()

    def test_regulator_receipt_signatures_are_well_formed(self):
        for receipt in self.fx['steps'][0]['receipts']:
            self.assertTrue(verify_receipt_signature(receipt))
        self.assertTrue(verify_receipt_signature(self.fx['recovery_a']['summary_receipt']))
        self.assertTrue(verify_receipt_signature(self.fx['recovery_b']['partial_proof']))


if __name__ == '__main__':
    unittest.main()
