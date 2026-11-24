import copy
import json
import unittest
from pathlib import Path

from grainrelease import evacuation, occupants, person_capture

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / 'fixtures'


def load_fixture(name):
    return json.loads((FIX / name).read_text(encoding='utf-8'))


def load_reentry():
    return load_fixture('evac-reentry.json')


def branch(name):
    data = load_reentry()
    spec = data.pop('branches')[name]
    data['gates'] = data['gates'] + copy.deepcopy(spec.get('gates_add', []))
    data['muster'] = data['muster'] + copy.deepcopy(spec.get('muster_add', []))
    for key in ('custody', 'claims', 'identity_evidence', 'statements'):
        if spec.get(key):
            data[key] = data.get(key, []) + copy.deepcopy(spec[key])
    return data


class OccupantIntervalTest(unittest.TestCase):
    def test_static_fixture_still_completes(self):
        fixture = load_fixture('evac-ok.json')
        self.assertTrue(evacuation.complete(fixture['gates'], fixture['muster']))

    def test_reentry_becomes_a_new_open_interval(self):
        data = load_reentry()
        presence = occupants.inside(data['gates'])
        self.assertEqual(len(presence['W1'].intervals), 1)
        self.assertFalse(presence['W1'].inside)
        self.assertEqual(len(presence['V1'].intervals), 2)
        self.assertTrue(presence['V1'].inside)
        self.assertTrue(presence['V1'].reentered)
        self.assertEqual(presence['V1'].current.start,
                         '2026-11-24T10:07:00')

    def test_flat_badge_set_misses_the_reentry(self):
        # This is the mistake the seed made: current badges are {"V1"} and the
        # old list contains V1, so a set difference says all-clear while the
        # person is actually back inside.
        data = load_reentry()
        current = {b for b, p in occupants.inside(data['gates']).items()
                   if p.inside}
        self.assertEqual(current, {'V1'})
        self.assertTrue(current <= {m['badge_id'] for m in data['muster']})
        self.assertFalse(evacuation.complete(
            data['gates'], data['muster'],
            alarm=data['alarm'], people=data['people'],
            statements=data['statements']))

    def test_offline_upload_order_does_not_reorder_presence(self):
        data = load_reentry()
        observed = occupants.inside(data['gates'])
        # Upload batches arrive out of on-site order; received_at must not be
        # used to order presence facts.
        shuffled = sorted(
            data['gates'],
            key=lambda e: e.get('received_at') or e['at'], reverse=True)
        self.assertNotEqual([e['id'] for e in shuffled],
                            [e['id'] for e in data['gates']])
        replayed = occupants.inside(shuffled)
        for badge in observed:
            self.assertEqual(observed[badge].to_dict(),
                             replayed[badge].to_dict())
        self.assertTrue(replayed['V1'].inside)

    def test_duplicate_event_and_repeated_scan_do_not_fake_intervals(self):
        data = load_reentry()
        gates = list(data['gates'])
        gates.append(copy.deepcopy(gates[0]))  # duplicate upload, same id
        gates.append({'id': 'G-DOUBLE', 'badge_id': 'W1', 'kind': 'entry',
                      'at': '2026-11-24T09:51:00'})
        presence = occupants.inside(gates)
        self.assertEqual(len(presence['W1'].intervals), 1)
        self.assertIn('G-DOUBLE', presence['W1'].repeated_scans)
        self.assertIn('G-W1-IN', presence['W1'].duplicate_events)


class PersonCaptureTest(unittest.TestCase):
    def test_ambiguous_reentry_keeps_both_real_people(self):
        data = load_reentry()
        resolved = person_capture.resolve_scenario(data)
        self.assertEqual(len(resolved['unresolved']), 1)
        interval = resolved['unresolved'][0]
        self.assertEqual(interval['badge_id'], 'V1')
        self.assertEqual(sorted(interval['candidates']), ['p-v1', 'p-w1'])
        self.assertIsNone(interval['owner'])
        self.assertTrue(resolved['people']['p-v1']['visitor'])
        self.assertTrue(resolved['people']['p-v1']['possible_inside'])
        self.assertTrue(resolved['people']['p-w1']['possible_inside'])
        # One shared badge must never collapse the two people into one.
        self.assertEqual(len(resolved['people']), 2)

    def test_two_witness_loan_attributes_to_w1(self):
        data = branch('w1_loan')
        resolved = person_capture.resolve_scenario(data)
        self.assertEqual(resolved['unresolved'], [])
        w1_badges = [x['badge_id'] for x in resolved['people']['p-w1']['intervals']]
        self.assertEqual(sorted(w1_badges), ['V1', 'W1'])
        self.assertEqual(resolved['people']['p-v1']['status'], 'outside')

    def test_verbal_statement_alone_cannot_attribute(self):
        data = load_reentry()
        resolved = person_capture.resolve_scenario(data)
        self.assertTrue(resolved['people']['p-w1']['possible_inside'])
        self.assertIn('S-W1-BORROW',
                      resolved['unresolved'][0]['statement_ids'])

    def test_visitor_identity_evidence_attributes_to_v1(self):
        data = branch('v1_reentry')
        resolved = person_capture.resolve_scenario(data)
        self.assertEqual(resolved['unresolved'], [])
        self.assertTrue(resolved['people']['p-v1']['inside'])
        self.assertFalse(resolved['people']['p-w1']['possible_inside'])

    def test_visitor_is_counted_even_when_roster_is_incomplete(self):
        data = load_reentry()
        data['people'] = [p for p in data['people'] if p['id'] != 'p-v1']
        resolved = person_capture.resolve_scenario(data)
        self.assertIn('p-v1', resolved['people'])
        self.assertTrue(resolved['people']['p-v1']['possible_inside'])
        self.assertFalse(evacuation.evaluate(data)['complete'])

    def test_unknown_badge_never_becomes_an_anonymous_free_pass(self):
        data = load_reentry()
        data['people'] = [p for p in data['people'] if p['id'] != 'p-v1']
        data['muster'] = []
        data['statements'] = []
        resolved = person_capture.resolve_scenario(data)
        self.assertIn('badge:V1', resolved['people'])
        self.assertEqual(len(resolved['people']), 2)
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertIn('badge:V1', result['unaccounted'])


class EscortedVisitorTest(unittest.TestCase):
    def scenario(self):
        return {
            'alarm': {'id': 'A', 'at': '2026-11-24T10:00:00', 'round': 1},
            'gates': [
                {'id': 'e1', 'badge_id': 'W1', 'kind': 'entry',
                 'at': '2026-11-24T09:50:00'},
                {'id': 'x1', 'badge_id': 'W1', 'kind': 'exit',
                 'at': '2026-11-24T10:03:00'}],
            'people': [{'id': 'p-w1', 'name': 'W1', 'badges': ['W1']}],
            'accompaniment': [{
                'id': 'A1', 'badge_id': 'W1', 'person_id': 'p-vis',
                'kind': 'escort', 'from': '2026-11-24T09:50:00',
                'until': '2026-11-24T10:03:00', 'witnesses': ['s-a']}],
            'muster': [
                {'id': 'm1', 'person_id': 'p-w1',
                 'at': '2026-11-24T10:05:00', 'witness': 's-a', 'round': 1},
                {'id': 'm2', 'person_id': 'p-vis',
                 'at': '2026-11-24T10:05:00', 'witness': 's-b', 'round': 1}],
        }

    def test_escorted_person_is_a_second_real_person(self):
        data = self.scenario()
        resolved = person_capture.resolve_scenario(data)
        self.assertEqual(len(resolved['people']), 2)
        self.assertFalse(resolved['people']['p-vis']['possible_inside'])
        self.assertTrue(resolved['people']['p-vis']['intervals'])
        self.assertTrue(evacuation.evaluate(data)['complete'])

    def test_escorted_person_needs_their_own_muster(self):
        data = self.scenario()
        data['muster'] = data['muster'][:1]
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertEqual(result['unaccounted'], ['p-vis'])

    def test_badge_interval_stays_open_if_escort_window_expires(self):
        data = self.scenario()
        data['accompaniment'][0]['until'] = '2026-11-24T10:01:00'
        data['gates'].append({'id': 'e2', 'badge_id': 'W1', 'kind': 'entry',
                              'at': '2026-11-24T10:07:00'})
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        # The expired escort window does not extend to the later re-entry, so
        # only the badge holder is liable for that new open interval.
        self.assertEqual(result['unaccounted'], ['p-w1'])
        self.assertFalse(result['people']['p-vis']['inside'])


class EvacuationRosterTest(unittest.TestCase):
    def test_fixture_branches_are_not_applied_until_evidence_is_added(self):
        data = load_reentry()
        self.assertIn('branches', data)
        self.assertFalse(evacuation.evaluate(data)['complete'])

    def test_unresolved_reentry_stops_the_search_and_revokes_the_clear(self):
        data = load_reentry()
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertFalse(result['can_end_search'])
        self.assertEqual(sorted(result['unaccounted']), ['p-v1', 'p-w1'])
        self.assertEqual(result['round'], '1')
        revokes = [a for a in result['announcements'] if a['kind'] == 'revoke']
        self.assertEqual(len(revokes), 1)
        self.assertEqual(revokes[0]['revokes'], 'ANN-1')
        self.assertEqual(sorted(revokes[0]['names']), ['V1', 'W1'])
        only_clear = [a for a in result['announcements'] if a['kind'] == 'clear']
        self.assertEqual([a['id'] for a in only_clear], ['ANN-1'])
        self.assertEqual(result['published_announcements'],
                         load_reentry()['announcements'])

    def test_two_witness_loan_branch_allows_a_new_clear(self):
        data = branch('w1_loan')
        result = evacuation.evaluate(data)
        self.assertTrue(result['complete'])
        self.assertEqual(result['unaccounted'], [])
        self.assertTrue(result['people']['p-w1']['accounted'])
        self.assertTrue(result['people']['p-v1']['accounted'])
        kinds = [a['kind'] for a in result['announcements']]
        self.assertEqual(kinds, ['clear', 'revoke', 'clear'])
        old, revoke, fresh = result['announcements']
        self.assertEqual(old, load_reentry()['announcements'][0])
        self.assertEqual(revoke['revokes'], 'ANN-1')
        self.assertEqual(revoke['names'], ['W1'])
        self.assertNotEqual(fresh['id'], 'ANN-1')
        self.assertEqual(fresh['replaces'], revoke['id'])
        self.assertEqual(sorted(fresh['names']), ['V1', 'W1'])

    def test_visitor_self_return_branch_keeps_searching_v1(self):
        data = branch('v1_reentry')
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertEqual(result['unaccounted'], ['p-v1'])
        self.assertTrue(result['people']['p-v1']['inside'])
        self.assertTrue(result['people']['p-v1']['visitor'])
        self.assertFalse(result['people']['p-v1']['accounted'])
        self.assertTrue(result['people']['p-w1']['accounted'])
        self.assertEqual([a['kind'] for a in result['announcements']],
                         ['clear', 'revoke'])
        self.assertEqual(result['announcements'][-1]['names'], ['V1'])
        self.assertEqual(result['announcements'][-1]['revokes'], 'ANN-1')

    def test_reentry_needs_its_own_exit_and_matching_muster(self):
        data = branch('w1_loan')
        without_exit = copy.deepcopy(data)
        without_exit['gates'] = [g for g in without_exit['gates']
                                 if g['id'] != 'G-V1-OUT2']
        result = evacuation.evaluate(without_exit)
        self.assertFalse(result['complete'])
        self.assertIn('p-w1', result['unaccounted'])
        self.assertIn('p-v1', result['unaccounted'])

        without_remuster = copy.deepcopy(data)
        without_remuster['muster'] = [m for m in without_remuster['muster']
                                      if m['id'] != 'M-W1-1010']
        result = evacuation.evaluate(without_remuster)
        self.assertFalse(result['complete'])
        self.assertIn('p-w1', result['unaccounted'])

        self.assertTrue(evacuation.evaluate(data)['complete'])

    def test_muster_must_match_person_witness_and_round(self):
        data = branch('w1_loan')
        # Self-witness
        bad = copy.deepcopy(data)
        for record in bad['muster']:
            if record['id'] == 'M-W1-1010':
                record['witness'] = 'p-w1'
        self.assertFalse(evacuation.evaluate(bad)['complete'])
        # Missing alarm round
        bad = copy.deepcopy(data)
        for record in bad['muster']:
            if record['id'] == 'M-W1-1010':
                record.pop('round')
        rejected = evacuation.evaluate(bad)['muster']['rejected']
        self.assertEqual(rejected[0]['_problem'], 'missing-alarm-round')
        self.assertFalse(evacuation.evaluate(bad)['complete'])
        # Wrong round
        bad = copy.deepcopy(data)
        for record in bad['muster']:
            if record['id'] == 'M-W1-1010':
                record['round'] = 2
        self.assertFalse(evacuation.evaluate(bad)['complete'])
        # Muster before the alarm
        bad = copy.deepcopy(data)
        for record in bad['muster']:
            if record['id'] == 'M-W1-1010':
                record['at'] = '2026-11-24T09:59:00'
        self.assertFalse(evacuation.evaluate(bad)['complete'])
        # A badge-only record cannot say who reached the muster point
        bad = copy.deepcopy(data)
        for record in bad['muster']:
            if record['id'] == 'M-W1-1010':
                record.pop('person_id')
        rejected = evacuation.evaluate(bad)['muster']['rejected']
        self.assertIn('muster-lacks-person',
                      [r['_problem'] for r in rejected])
        self.assertFalse(evacuation.evaluate(bad)['complete'])

    def test_alarm_cut_ignores_presence_that_ended_before_the_alarm(self):
        data = load_reentry()
        data.pop('announcements')
        data['gates'] = [g for g in data['gates']
                         if g['id'] != 'G-V1-RE']
        data['alarm']['at'] = '2026-11-24T10:20:00'
        # Every interval closed before this cut, so nobody is post-alarm
        # present and the search can end without a fresh muster.
        result = evacuation.evaluate(data)
        self.assertTrue(result['complete'])
        self.assertEqual([a['kind'] for a in result['announcements']],
                         ['clear'])

    def test_late_upload_after_a_clear_still_revokes_it(self):
        data = load_reentry()
        data['gates'].append({
            'id': 'G-LATE', 'badge_id': 'W1', 'kind': 'entry',
            'at': '2026-11-24T10:07:30',
            'received_at': '2026-11-24T10:45:00'})
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertTrue(result['badge_ledger']['W1']['inside'])
        revoke = result['announcements'][-1]
        self.assertEqual(revoke['kind'], 'revoke')
        self.assertEqual(revoke['revokes'], 'ANN-1')
        # The notice is revoked at the earliest contradicting fact (10:07);
        # the late-uploaded 10:07:30 W1 entry is still on the ledger and in
        # the named revocation.
        self.assertEqual(revoke['at'], '2026-11-24T10:07:00')
        self.assertIn('W1', revoke['names'])

    def test_conflicting_person_evidence_keeps_the_search_open(self):
        data = branch('w1_loan')
        data['identity_evidence'] = copy.deepcopy(
            load_reentry()['branches']['v1_reentry']['identity_evidence'])
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertEqual(sorted(result['unaccounted']), ['p-v1', 'p-w1'])
        self.assertEqual([a['kind'] for a in result['announcements']],
                         ['clear', 'revoke'])

    def test_offset_timestamps_order_correctly(self):
        data = load_reentry()
        data['alarm']['at'] = '2026-11-24T10:00:00+08:00'
        for gate in data['gates']:
            gate['at'] = gate['at'] + '+08:00'
        for record in data['muster']:
            record['at'] = record['at'] + '+08:00'
        for record in data['announcements']:
            record['at'] = record['at'] + '+08:00'
        result = evacuation.evaluate(data)
        self.assertFalse(result['complete'])
        self.assertEqual(result['announcements'][-1]['kind'], 'revoke')
        self.assertEqual(result['announcements'][-1]['at'],
                         '2026-11-24T10:07:00+08:00')
        # The same instant expressed in UTC must order identically.
        utc_bytes = json.dumps(data).replace('10:', '02:').replace(
            '+08:00', '+00:00').encode()
        utc = json.loads(utc_bytes)
        same = evacuation.evaluate(utc)
        self.assertEqual(same['complete'], result['complete'])
        self.assertEqual(
            [x['badge_id'] for x in same['unresolved_intervals']],
            [x['badge_id'] for x in result['unresolved_intervals']])
        self.assertEqual(same['announcements'][-1]['at'],
                         '2026-11-24T02:07:00+00:00')

    def test_published_notices_are_append_only_and_replay_is_idempotent(self):
        data = branch('w1_loan')
        first = evacuation.evaluate(data)
        replay = copy.deepcopy(data)
        replay['announcements'] = copy.deepcopy(first['announcements'])
        second = evacuation.evaluate(replay)
        self.assertEqual(first['announcements'], second['announcements'])
        self.assertEqual(second['published_announcements'],
                         first['announcements'])

    def test_visitor_replay_does_not_drop_the_revocation(self):
        data = branch('v1_reentry')
        first = evacuation.evaluate(data)
        replay = copy.deepcopy(data)
        replay['announcements'] = copy.deepcopy(first['announcements'])
        second = evacuation.evaluate(replay)
        self.assertEqual(first['announcements'], second['announcements'])
        self.assertFalse(second['complete'])
        self.assertEqual(second['announcements'][-1]['revokes'], 'ANN-1')


class BusinessBoundaryTest(unittest.TestCase):
    def test_evacuation_decision_does_not_touch_release_records(self):
        from grainrelease import rules
        events = []
        for name in ('lot-ok.json', 'fumigate-ok.json', 'release-ok.json'):
            events.extend(load_fixture(name))
        before = rules.calculate(copy.deepcopy(events))
        evacuation.evaluate(load_reentry())
        evacuation.evaluate(branch('w1_loan'))
        after = rules.calculate(copy.deepcopy(events))
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
