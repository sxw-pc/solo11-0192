import copy
import json
import unittest
from pathlib import Path

from grainrelease import chemical_cabinet, chemical_ledger
from grainrelease.chemical_capture import CustodyError, Depot

ROOT = Path(__file__).resolve().parents[1]
SPEC = json.loads((ROOT / 'fixtures' / 'chemical-transfer.json').read_text(encoding='utf-8'))
CAPACITIES = {name: s['capacity_kg'] for name, s in SPEC['locations'].items()
              if s.get('kind') != 'external'}
CHEMICAL = {c['container_id']: c['chemical'] for c in SPEC['containers']}

# 获仓管批准的处置一：双人先把 X2 从 S 转入相容临时柜 T，再经清空的 S 把 Y1 移到 B。
TRANSFER_PLAN = [
    {'op': 'move', 'container': 'X2', 'to': 'T',
     'by': ['zhang', 'li'], 'at': '2026-11-24T09:00:00'},
    {'op': 'move', 'container': 'Y1', 'to': 'B', 'via': ['S'],
     'by': ['zhang', 'li'], 'at': '2026-11-24T09:20:00'},
    {'op': 'revise_notice', 'notice_id': 'N2', 'revises': 'N1',
     'available': ['X1', 'Y1', 'X2'], 'affected': ['X2', 'Y1'], 'at': '2026-11-24T09:40:00'},
]

# 获仓管批准的处置二：持证回收方在 A 门口直接签收 Y1 并封存退运。
DISPOSAL_PLAN = [
    {'op': 'book', 'container': 'Y1', 'recycler': 'R-7', 'at': '2026-11-24T09:00:00'},
    {'op': 'break_seal', 'container': 'Y1', 'by': 'zhang', 'at': '2026-11-24T09:10:00'},
    {'op': 'signoff', 'container': 'Y1', 'by': ['zhang', 'li'], 'at': '2026-11-24T09:20:00'},
    {'op': 'receive', 'container': 'Y1', 'recycler': 'R-7', 'seal': 'RET-Y1-07',
     'at': '2026-11-24T09:30:00'},
    {'op': 'revise_notice', 'notice_id': 'N2', 'revises': 'N1',
     'available': ['X1', 'X2'], 'affected': ['Y1'], 'at': '2026-11-24T09:45:00'},
]


def fresh_depot():
    return Depot(copy.deepcopy(SPEC))


class ForbiddenPairTest(unittest.TestCase):
    """禁忌对：重量未超柜限不构成相容。"""

    def test_weight_under_limit_does_not_imply_compatible(self):
        events = [
            {'container_id': 'X1', 'cabinet': 'A', 'chemical': 'X', 'kg': 2},
            {'container_id': 'Y1', 'cabinet': 'A', 'chemical': 'Y', 'kg': 2},
        ]
        kg_only_fits = sum(e['kg'] for e in events) <= 5  # the retired kg-only rule
        self.assertTrue(kg_only_fits)  # 4 kg fits cabinet A, yet the pair is forbidden
        self.assertEqual(chemical_cabinet.safe(events, {'A': 5}), {'A': False})
        # enlarging the cabinet limit cannot make the forbidden pair safe either
        self.assertEqual(chemical_cabinet.safe(events, {'A': 500}), {'A': False})

    def test_correction_to_second_cabinet_flips_verdict(self):
        events = [
            {'container_id': 'X1', 'cabinet': 'A', 'chemical': 'X', 'kg': 2},
            {'container_id': 'Y1', 'cabinet': 'A', 'chemical': 'Y', 'kg': 2},
        ]
        self.assertFalse(chemical_cabinet.safe(events, CAPACITIES)['A'])
        # Y1 is corrected to cabinet B with a completed two-person handover
        events.append({'container_id': 'Y1', 'location': 'B', 'chemical': 'Y', 'kg': 2,
                       'handover': ['zhang', 'li'], 'at': '2026-11-24T09:20:00'})
        positions = chemical_ledger.containers(events)
        self.assertEqual(positions['Y1']['location'], 'B')
        self.assertEqual(positions['Y1']['handover'], ['zhang', 'li'])
        verdict = chemical_cabinet.safe(events, CAPACITIES)
        self.assertEqual(verdict, {'A': True, 'B': True})  # A holds only X now

    def test_unknown_type_stays_isolated(self):
        alone = [{'container_id': 'U1', 'cabinet': 'Q', 'chemical': None, 'kg': 1}]
        self.assertEqual(chemical_cabinet.safe(alone, CAPACITIES), {'Q': True})
        shared = alone + [{'container_id': 'X1', 'cabinet': 'Q', 'chemical': 'X', 'kg': 1}]
        self.assertEqual(chemical_cabinet.safe(shared, CAPACITIES), {'Q': False})
        two_unknowns = alone + [{'container_id': 'U2', 'cabinet': 'Q', 'chemical': None, 'kg': 1}]
        self.assertEqual(chemical_cabinet.safe(two_unknowns, CAPACITIES), {'Q': False})


class TransferPlanTest(unittest.TestCase):
    """处置一：移柜，Y1 留在本库可供后续领用。"""

    def test_every_moment_stays_separated(self):
        depot = fresh_depot()
        before = depot.snapshot()
        self.assertFalse(before['safe']['A'])  # X1+Y1 share cabinet A at 4 kg < 5 kg
        self.assertEqual(before['available'], {'X1': 'A', 'Y1': 'A', 'X2': 'S'})

        snaps = depot.run(TRANSFER_PLAN)
        self.assertEqual([s['step'] for s in snaps],
                         ['move X2 S->T', 'move Y1 A->S', 'move Y1 S->B', 'notice N2'])
        # X2 cleared staging table S into the compatible temporary cabinet T
        self.assertEqual(snaps[0]['locations']['S']['containers'], [])
        self.assertEqual(snaps[0]['locations']['T']['containers'], ['X2'])
        self.assertFalse(snaps[0]['safe']['A'])  # the pair still shares A
        # Y1 crosses S alone: no moment of sharing the staging table with X2
        self.assertEqual(snaps[1]['locations']['S']['containers'], ['Y1'])
        self.assertTrue(snaps[1]['safe']['S'])
        self.assertTrue(snaps[1]['safe']['A'])  # A is down to X only
        # final cabinet layout is fully safe
        self.assertTrue(all(snaps[2]['safe'].values()))
        # the transit hops are on record as two-person handovers
        hops = chemical_ledger.custody(depot.events)['Y1']
        self.assertEqual([(h['from'], h['to']) for h in hops], [('A', 'S'), ('S', 'B')])
        self.assertTrue(all(len(h['by']) == 2 for h in hops))
        # Y1 stays in depot inventory and on the issue list at its new cabinet
        final = snaps[-1]
        self.assertEqual(final['available'], {'X1': 'A', 'Y1': 'B', 'X2': 'T'})
        self.assertIn('Y1', final['inventory'])
        self.assertEqual(final['pending_revision'], [])
        self.assertEqual(chemical_ledger.containers(depot.events)['Y1']['location'], 'B')
        # the engine's per-moment view agrees with the ledger/cabinet projection
        projected = chemical_cabinet.safe(depot.events, CAPACITIES)
        for location, ok in projected.items():
            self.assertEqual(ok, snaps[2]['safe'][location], location)

    def test_forbidden_transit_is_rejected_without_side_effects(self):
        depot = fresh_depot()
        events_before = list(depot.events)
        # moving Y1 to B while X2 still sits on S would mix the pair on the table
        with self.assertRaises(CustodyError):
            depot.apply({'op': 'move', 'container': 'Y1', 'to': 'B', 'via': ['S'],
                         'by': ['zhang', 'li'], 'at': '2026-11-24T09:00:00'})
        self.assertEqual(depot.events, events_before)  # nothing was recorded
        self.assertEqual(depot.snapshot()['locations']['S']['containers'], ['X2'])
        self.assertEqual(depot.snapshot()['locations']['A']['containers'], ['X1', 'Y1'])

    def test_capacity_is_checked_separately_from_compatibility(self):
        depot = fresh_depot()
        depot.apply(TRANSFER_PLAN[0])  # X2 -> T, 2 kg in a 3 kg cabinet
        # X1 is compatible with X2, but 2+2 kg exceeds T's 3 kg limit
        with self.assertRaises(CustodyError):
            depot.apply({'op': 'move', 'container': 'X1', 'to': 'T', 'via': ['S'],
                         'by': ['zhang', 'li'], 'at': '2026-11-24T09:10:00'})
        self.assertEqual(depot.snapshot()['locations']['A']['containers'], ['X1', 'Y1'])

    def test_two_person_rule_routes_and_unknown_isolation(self):
        depot = fresh_depot()
        move = {'op': 'move', 'container': 'X2', 'to': 'T', 'at': '2026-11-24T09:00:00'}
        with self.assertRaises(CustodyError):  # a single carrier is not a handover
            depot.apply({**move, 'by': ['zhang']})
        with self.assertRaises(CustodyError):  # the same person twice is not two people
            depot.apply({**move, 'by': ['zhang', 'zhang']})
        with self.assertRaises(CustodyError):  # both carriers must be authorised staff
            depot.apply({**move, 'by': ['zhang', 'mallory']})
        with self.assertRaises(CustodyError):  # no direct route from A to B
            depot.apply({'op': 'move', 'container': 'Y1', 'to': 'B',
                         'by': ['zhang', 'li'], 'at': '2026-11-24T09:00:00'})
        with self.assertRaises(CustodyError):  # unknown-type U1 must stay isolated
            depot.apply({'op': 'move', 'container': 'X1', 'to': 'Q',
                         'by': ['zhang', 'li'], 'at': '2026-11-24T09:00:00'})
        with self.assertRaises(CustodyError):  # moves cannot cross the external boundary
            depot.apply({'op': 'move', 'container': 'Y1', 'to': 'GATE',
                         'by': ['zhang', 'li'], 'at': '2026-11-24T09:00:00'})


class DisposalPlanTest(unittest.TestCase):
    """处置二：回收方签收退运，Y1 从可领用清单移除。"""

    def test_booking_keeps_inventory_and_issue_list(self):
        depot = fresh_depot()
        snap = depot.apply(DISPOSAL_PLAN[0])[0]
        self.assertIn('Y1', snap['inventory'])  # 预约不能让桶提前从库存消失
        self.assertEqual(snap['available'], {'X1': 'A', 'Y1': 'A', 'X2': 'S'})
        self.assertIn('Y1', chemical_ledger.inventory(depot.events))

    def test_separate_custody_records_and_final_handover(self):
        depot = fresh_depot()
        snaps = depot.run(DISPOSAL_PLAN)
        # 封签拆开、双人签收、回收方最终接收是分别的记录
        kinds = [e['kind'] for e in chemical_ledger.custody(depot.events)['Y1']]
        self.assertEqual(kinds, ['booking', 'seal_break', 'signoff', 'receipt'])
        receipt = chemical_ledger.custody(depot.events)['Y1'][-1]
        self.assertEqual(receipt['seal'], 'RET-Y1-07')
        # the external boundary is irreversible
        self.assertNotIn('Y1', snaps[-1]['inventory'])
        self.assertEqual(chemical_ledger.containers(depot.events)['Y1']['location'],
                         chemical_ledger.EXTERNAL)
        for late in [
            {'op': 'move', 'container': 'Y1', 'to': 'B', 'via': ['S'],
             'by': ['zhang', 'li'], 'at': '2026-11-24T10:00:00'},
            {'op': 'break_seal', 'container': 'Y1', 'by': 'zhang', 'at': '2026-11-24T10:00:00'},
            {'op': 'book', 'container': 'Y1', 'recycler': 'R-8', 'at': '2026-11-24T10:00:00'},
            {'op': 'receive', 'container': 'Y1', 'recycler': 'R-7', 'seal': 'X',
             'at': '2026-11-24T10:00:00'},
        ]:
            with self.assertRaises(CustodyError):
                depot.apply(late)
        # A is safe with only X1, and Y1 is off the issue list
        self.assertTrue(snaps[-1]['safe']['A'])
        self.assertEqual(snaps[-1]['available'], {'X1': 'A', 'X2': 'S'})

    def test_receipt_forces_revision_of_the_issue_list(self):
        depot = fresh_depot()
        depot.run(DISPOSAL_PLAN[:4])
        self.assertEqual(depot.snapshot()['pending_revision'], ['Y1'])
        # silently keeping Y1 on the list is rejected: it left the depot
        with self.assertRaises(CustodyError):
            depot.apply({'op': 'revise_notice', 'notice_id': 'N2', 'revises': 'N1',
                         'available': ['X1', 'Y1', 'X2'], 'affected': ['Y1'],
                         'at': '2026-11-24T09:45:00'})
        # dropping Y1 without naming it as affected is a silent overwrite
        with self.assertRaises(CustodyError):
            depot.apply({'op': 'revise_notice', 'notice_id': 'N2', 'revises': 'N1',
                         'available': ['X1', 'X2'], 'affected': [],
                         'at': '2026-11-24T09:45:00'})
        # a revision must revise the current notice, not fork history
        with self.assertRaises(CustodyError):
            depot.apply({'op': 'revise_notice', 'notice_id': 'N2', 'revises': 'N0',
                         'available': ['X1', 'X2'], 'affected': ['Y1'],
                         'at': '2026-11-24T09:45:00'})
        depot.apply(DISPOSAL_PLAN[4])
        self.assertEqual(depot.snapshot()['pending_revision'], [])

    def test_recycler_must_be_licensed_and_follow_the_chain(self):
        depot = fresh_depot()
        with self.assertRaises(CustodyError):  # not an entrusted recycler
            depot.apply({'op': 'book', 'container': 'Y1', 'recycler': 'R-X',
                         'at': '2026-11-24T09:00:00'})
        with self.assertRaises(CustodyError):  # licence already expired
            depot.apply({'op': 'book', 'container': 'Y1', 'recycler': 'R-9',
                         'at': '2026-11-24T09:00:00'})
        with self.assertRaises(CustodyError):  # receipt without an appointment
            depot.apply({'op': 'receive', 'container': 'Y1', 'recycler': 'R-7',
                         'seal': 'RET-1', 'at': '2026-11-24T09:30:00'})
        depot.apply(DISPOSAL_PLAN[0])  # book
        with self.assertRaises(CustodyError):  # seal break not yet recorded
            depot.apply({'op': 'receive', 'container': 'Y1', 'recycler': 'R-7',
                         'seal': 'RET-1', 'at': '2026-11-24T09:30:00'})
        depot.apply(DISPOSAL_PLAN[1])  # break seal
        with self.assertRaises(CustodyError):  # two-person signoff not yet recorded
            depot.apply({'op': 'receive', 'container': 'Y1', 'recycler': 'R-7',
                         'seal': 'RET-1', 'at': '2026-11-24T09:30:00'})
        depot.apply(DISPOSAL_PLAN[2])  # signoff
        with self.assertRaises(CustodyError):  # a different recycler than booked
            depot.apply({'op': 'receive', 'container': 'Y1', 'recycler': 'R-8',
                         'seal': 'RET-1', 'at': '2026-11-24T09:30:00'})
        with self.assertRaises(CustodyError):  # return shipment needs a transport seal
            depot.apply({'op': 'receive', 'container': 'Y1', 'recycler': 'R-7',
                         'at': '2026-11-24T09:30:00'})

    def test_receipt_only_at_external_boundary(self):
        depot = fresh_depot()
        depot.apply({'op': 'move', 'container': 'X2', 'to': 'T',
                     'by': ['zhang', 'li'], 'at': '2026-11-24T09:00:00'})
        depot.apply({'op': 'book', 'container': 'X2', 'recycler': 'R-7',
                     'at': '2026-11-24T09:05:00'})
        depot.apply({'op': 'break_seal', 'container': 'X2', 'by': 'zhang',
                     'at': '2026-11-24T09:10:00'})
        depot.apply({'op': 'signoff', 'container': 'X2', 'by': ['zhang', 'li'],
                     'at': '2026-11-24T09:15:00'})
        with self.assertRaises(CustodyError):  # cabinet T has no recycler gate
            depot.apply({'op': 'receive', 'container': 'X2', 'recycler': 'R-7',
                         'seal': 'RET-X2', 'at': '2026-11-24T09:20:00'})


class NoticeRevisionTest(unittest.TestCase):
    """可领用清单：修订留痕，旧清单可回读。"""

    def test_old_notice_survives_revision(self):
        depot = fresh_depot()
        depot.run(DISPOSAL_PLAN)
        n1 = depot.notice('N1')  # 旧清单回读
        self.assertEqual(n1['available'], {'X1': 'A', 'Y1': 'A', 'X2': 'S'})
        n2 = depot.notice('N2')
        self.assertEqual(n2['available'], {'X1': 'A', 'X2': 'S'})
        self.assertEqual(n2['revises'], 'N1')
        self.assertEqual(n2['affected'], ['Y1'])
        with self.assertRaises(CustodyError):  # a published notice id cannot be reused
            depot.apply({'op': 'revise_notice', 'notice_id': 'N1', 'revises': 'N2',
                         'available': ['X1'], 'affected': ['X2'],
                         'at': '2026-11-24T10:00:00'})
        # the ledger projection reads the same append-only history
        self.assertEqual(chemical_ledger.notices(depot.events)['N1']['available'],
                         n1['available'])
        self.assertEqual(chemical_ledger.available(depot.events), {'X1': 'A', 'X2': 'S'})

    def test_late_handover_revision_names_affected_drums(self):
        depot = fresh_depot()
        depot.run(DISPOSAL_PLAN)  # N2 current: X1@A, X2@S
        # a handover signed at 08:50, before N2 was published, is captured late
        depot.apply({'op': 'move', 'container': 'X2', 'to': 'T',
                     'by': ['li', 'wang'], 'at': '2026-11-24T08:50:00'})
        self.assertEqual(depot.snapshot()['pending_revision'], ['X2'])
        depot.apply({'op': 'revise_notice', 'notice_id': 'N3', 'revises': 'N2',
                     'available': ['X1', 'X2'], 'affected': ['X2'],
                     'at': '2026-11-24T10:05:00'})
        self.assertEqual(depot.snapshot()['available'], {'X1': 'A', 'X2': 'T'})
        self.assertEqual(depot.snapshot()['pending_revision'], [])
        self.assertEqual(depot.notice('N2')['available'], {'X1': 'A', 'X2': 'S'})


class TwoDispositionsTest(unittest.TestCase):
    """同一批桶与路线下的两条获批处置：都全时段安全，可用量不同。"""

    def test_both_paths_safe_but_different_availability(self):
        transfer = fresh_depot()
        t_snaps = transfer.run(TRANSFER_PLAN)
        disposal = fresh_depot()
        d_snaps = disposal.run(DISPOSAL_PLAN)
        # both end fully safe, and the only unsafe moment anywhere is the
        # original X/Y sharing of cabinet A before Y1 leaves it
        for snaps in (t_snaps, d_snaps):
            self.assertTrue(all(snaps[-1]['safe'].values()))
            for snap in snaps:
                unsafe = {loc for loc, ok in snap['safe'].items() if not ok}
                self.assertTrue(unsafe <= {'A'}, snap['step'])
        # outside the pre-existing A violation both plans eliminate, the
        # pair never shares any location (in particular never the staging
        # table), and the unknown drum is never left unisolated
        for snap in t_snaps + d_snaps:
            for name, loc in snap['locations'].items():
                chems = {CHEMICAL[c] for c in loc['containers']}
                if name != 'A':
                    self.assertFalse({'X', 'Y'} <= chems, (snap['step'], name))
                if None in chems:
                    self.assertEqual(len(loc['containers']), 1, snap['step'])
        # 两路给班组不同的可领用量：移柜保留 Y1，退运移除 Y1
        self.assertEqual(set(t_snaps[-1]['available']), {'X1', 'Y1', 'X2'})
        self.assertEqual(set(d_snaps[-1]['available']), {'X1', 'X2'})
        self.assertIn('Y1', chemical_ledger.inventory(transfer.events))
        self.assertNotIn('Y1', chemical_ledger.inventory(disposal.events))


if __name__ == '__main__':
    unittest.main()
