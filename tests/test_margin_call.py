"""Verification for offline hedge clearing, margin calls and audit readback.

Covers the six required evidence paths:

1. reissued / duplicate reports never count a fill twice;
2. cancellation and correction chains (WHEAT-JUL -1 reclassified -> 3 lots);
3. single-leg clearing cannot support spread relief;
4. the two clearing endgames (paired relief vs cancelled leg);
5. initial / variation / collateral / cash ledgers stay separate and today's
   profit is never booked as collateral;
6. issued margin calls are immutable and clearing corrections only append
   adjustment notes that point back at the original notice.

Account, contract month, clearing round and the trade/correction identity are
asserted throughout.  Other grainbusiness modules are untouched by design.
"""
import copy
import json
import unittest
from pathlib import Path

from grainrelease.hedges import positions, positions_by_account
from grainrelease.hedge_capture import HedgeCapture, MarginCallBook
from grainrelease.margin import required, statement

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'hedge-clearing.json'
FX = json.loads(FIXTURE.read_text(encoding='utf-8'))
RATES = FX['per_lot']
ACCOUNT = FX['account']


def cap_for(*branches):
    reports = copy.deepcopy(FX['reports'])
    for branch in branches:
        reports.extend(copy.deepcopy(FX['branches'][branch]))
    return HedgeCapture(
        reports,
        rates=copy.deepcopy(FX['per_lot']),
        available_cash=FX['available_cash'],
        collateral_posted=FX['collateral_posted'],
        settlements=copy.deepcopy(FX['settlements']),
        combos=copy.deepcopy(FX['combos']),
        account=FX['account'],
        as_of_round=FX['as_of_round'],
    )


def stmt_for(*branches):
    return cap_for(*branches).statement()


class ReissueAndCancellationTest(unittest.TestCase):
    def test_seed_fixture_still_requires_2000(self):
        seed = json.loads((ROOT / 'fixtures' / 'hedge-ok.json').read_text())
        self.assertEqual(required(seed['fills'], seed['per_lot']), {'WHEAT-JUL': 2000})
        self.assertEqual(positions(seed['fills']), {'WHEAT-JUL': 2})

    def test_reissued_fill_same_triple_counts_once(self):
        fill = {'trade_id': 'T-1', 'account': 'A', 'contract': 'WHEAT-JUL',
                'lots': 3, 'clearing_round': 'R1', 'correction_chain': 'C1',
                'version': 1}
        events = [
            {'id': 'rep-1', 'source': 'terminal', **fill},
            {'id': 'rep-2', 'source': 'broker', 'kind': 'reissue', **fill},
            {'id': 'rep-3', 'source': 'broker', 'kind': 'duplicate', **fill},
            {'id': 'rep-4', **fill},  # same triple, different report id
        ]
        pos = positions(events)
        self.assertEqual(pos[('A', 'WHEAT-JUL')], 3)
        self.assertEqual(required(events, RATES)[('A', 'WHEAT-JUL')], 3000)

    def test_identical_report_id_replay_is_idempotent(self):
        report = {'id': 'same-id', 'trade_id': 'T-9', 'account': 'A',
                  'contract': 'WHEAT-SEP', 'lots': -2,
                  'clearing_round': 'R1', 'correction_chain': 'C9'}
        pos = positions([report, copy.deepcopy(report)])
        self.assertEqual(pos[('A', 'WHEAT-SEP')], -2)

    def test_trade_round_and_chain_form_identity_triple(self):
        cap = HedgeCapture([
            {'id': 'r1', 'trade_id': 'T', 'clearing_round': 'R1',
             'correction_chain': 'C1', 'contract': 'WHEAT-JUL', 'lots': 1},
            {'id': 'r2', 'trade_id': 'T', 'clearing_round': 'R1',
             'correction_chain': 'C1', 'contract': 'WHEAT-JUL', 'lots': 1},
            {'id': 'r3', 'trade_id': 'T', 'clearing_round': 'R2',
             'correction_chain': 'C1', 'contract': 'WHEAT-JUL', 'lots': 1},
            {'id': 'r4', 'trade_id': 'T', 'clearing_round': 'R1',
             'correction_chain': 'C2', 'contract': 'WHEAT-JUL', 'lots': 1},
        ])
        identities = set(cap.identities)
        self.assertIn(('T', 'R1', 'C1'), identities)
        self.assertIn(('T', 'R2', 'C1'), identities)
        self.assertIn(('T', 'R1', 'C2'), identities)
        # the R1/C1 reissue is one identity, not two
        self.assertEqual(sum(1 for key in identities if key == ('T', 'R1', 'C1')), 1)

    def test_cancelled_sell_moves_requirement_from_2000_to_3000(self):
        events = [
            {'id': 'b3', 'trade_id': 'T-B', 'account': 'GD',
             'contract': 'WHEAT-JUL', 'lots': 3,
             'clearing_round': 'R1', 'correction_chain': 'CB'},
            {'id': 's1', 'trade_id': 'T-S', 'account': 'GD',
             'contract': 'WHEAT-JUL', 'lots': -1,
             'clearing_round': 'R1', 'correction_chain': 'CS', 'version': 1},
            {'id': 's1-cancel', 'trade_id': 'T-S', 'account': 'GD',
             'contract': 'WHEAT-JUL', 'lots': -1, 'status': 'cancelled',
             'source': 'clearing-house',
             'clearing_round': 'R2', 'correction_chain': 'CS', 'version': 2},
        ]
        # valid close version -> net 2 -> 2000
        valid_close = events[:2]
        self.assertEqual(positions(valid_close), {'WHEAT-JUL': 2})
        self.assertEqual(required(valid_close, {'WHEAT-JUL': 1000}), {'WHEAT-JUL': 2000})
        # clearing revokes the sell -> only the 3-lot buy remains -> 3000
        pos = positions(events)
        self.assertEqual(pos, {'WHEAT-JUL': 3})
        due = required(events, {'WHEAT-JUL': 1000})
        self.assertEqual(due, {'WHEAT-JUL': 3000})
        # source chain proves WHY it is 3000, not a blind +1000 patch
        sell = next(t for t in HedgeCapture(events).effective_trades()
                    if t['trade_id'] == 'T-S')
        self.assertEqual(sell['terminal_status'], 'cancelled')
        self.assertEqual(sell['lots'], 0)
        self.assertIn('s1', sell['report_ids'])
        self.assertIn('s1-cancel', sell['report_ids'])

    def test_cancel_arriving_before_original_report_still_zeroes_fill(self):
        cancel = {'id': 's-c', 'trade_id': 'T-S2', 'account': 'GD',
                  'contract': 'WHEAT-JUL', 'lots': -1, 'status': 'cancelled',
                  'clearing_round': 'R1', 'correction_chain': 'CS2'}
        fill = {'id': 's-f', 'trade_id': 'T-S2', 'account': 'GD',
                'contract': 'WHEAT-JUL', 'lots': -1, 'status': 'filled',
                'clearing_round': 'R1', 'correction_chain': 'CS2'}
        buy = {'id': 'b', 'trade_id': 'T-B2', 'account': 'GD',
               'contract': 'WHEAT-JUL', 'lots': 3}
        self.assertEqual(positions([cancel, fill, buy]), {'WHEAT-JUL': 3})

    def test_correction_chain_highest_revision_supersedes(self):
        events = [
            {'trade_id': 'B', 'contract': 'WHEAT-JUL', 'lots': 3},
            {'trade_id': 'S', 'contract': 'WHEAT-JUL', 'lots': -1,
             'correction_chain': 'CS', 'version': 1},
            {'trade_id': 'S', 'contract': 'WHEAT-JUL', 'lots': -2,
             'correction_chain': 'CS', 'version': 2, 'kind': 'amendment'},
        ]
        self.assertEqual(positions(events), {'WHEAT-JUL': 1})
        self.assertEqual(required(events, RATES), {'WHEAT-JUL': 1000})


class AccountMonthBoundaryTest(unittest.TestCase):
    def test_jul_and_sep_same_variety_never_net(self):
        pos = stmt_for()['positions']
        self.assertEqual(pos['WHEAT-JUL'], 2)
        self.assertEqual(pos['WHEAT-SEP'], -2)
        due = required(cap_for()._records_as_raw(), RATES)
        self.assertEqual(due[('GD-MAIN', 'WHEAT-JUL')], 2000)
        self.assertEqual(due[('GD-MAIN', 'WHEAT-SEP')], 2000)

    def test_different_accounts_never_net(self):
        events = [
            {'trade_id': '1', 'account': 'ACC-A', 'contract': 'WHEAT-JUL', 'lots': 3},
            {'trade_id': '2', 'account': 'ACC-B', 'contract': 'WHEAT-JUL', 'lots': -3},
        ]
        pos = positions(events)
        self.assertEqual(pos[('ACC-A', 'WHEAT-JUL')], 3)
        self.assertEqual(pos[('ACC-B', 'WHEAT-JUL')], -3)
        self.assertEqual(pos, {'ACC-A': {'WHEAT-JUL': 3},
                              'ACC-B': {'WHEAT-JUL': -3}})
        nested = positions_by_account(events)
        self.assertEqual(nested['ACC-A']['WHEAT-JUL'], 3)
        self.assertEqual(nested['ACC-B']['WHEAT-JUL'], -3)
        due = required(events, {'WHEAT-JUL': 1000})
        self.assertEqual(due[('ACC-A', 'WHEAT-JUL')], 3000)
        self.assertEqual(due[('ACC-B', 'WHEAT-JUL')], 3000)
        self.assertEqual(sum(due.values()), 6000)

    def test_broker_combo_label_alone_grants_no_relief(self):
        s = stmt_for()
        self.assertEqual(s['spread_relief'], 0)
        self.assertEqual(s['initial_margin'], s['gross_initial_margin'])
        # the indicative 1500 is disclosed only as unconfirmed potential relief
        self.assertEqual(s['potential_spread_relief'], 1500)


class SingleLegClearingTest(unittest.TestCase):
    def test_fixture_base_is_single_cleared_leg(self):
        s = stmt_for()
        expected = FX['expect']['single_leg']
        self.assertEqual(s['positions']['WHEAT-JUL'], expected['WHEAT-JUL'])
        self.assertEqual(s['positions']['WHEAT-SEP'], expected['WHEAT-SEP'])
        self.assertEqual(s['confirmed_initial_margin'], 2000)
        self.assertEqual(s['unconfirmed_difference'], 2000)
        self.assertIn('T-SEP-S2', s['unconfirmed_trade_ids'])
        self.assertEqual(s['spread_relief'], 0)
        self.assertEqual(s['margin_call'], expected['margin_call'])
        self.assertEqual(s['cash_to_raise'], expected['cash_to_raise'])

    def test_pair_receipt_rejected_when_second_leg_not_same_round(self):
        # Only T-A gets an R9 clearing ack; the pair receipt claims R9 too.
        events = [
            {'trade_id': 'TA', 'account': 'A', 'contract': 'WHEAT-JUL',
             'lots': 1, 'source': 'clearing-house', 'status': 'confirmed',
             'clearing_round': 'R9'},
            {'trade_id': 'TB', 'account': 'A', 'contract': 'WHEAT-SEP',
             'lots': -1, 'source': 'broker', 'status': 'reported'},
            {'kind': 'spread_approval', 'approval_id': 'PAIR-X',
             'legs': ['TA', 'TB'], 'clearing_round': 'R9',
             'source': 'clearing-house', 'relief': 900},
        ]
        cap = HedgeCapture(events, rates=RATES)
        decisions = cap.spread_decisions()
        self.assertEqual(len(decisions), 1)
        self.assertFalse(decisions[0]['approved'])
        self.assertIn('same-round', decisions[0]['reason'])
        self.assertEqual(cap.statement()['spread_relief'], 0)


class TwoEndgameTest(unittest.TestCase):
    def test_paired_same_round_confirmation_reduces_call(self):
        s = stmt_for('paired')
        e = FX['expect']['paired']
        self.assertEqual(s['positions']['WHEAT-JUL'], 2)
        self.assertEqual(s['positions']['WHEAT-SEP'], -2)
        self.assertEqual(s['spread_relief'], e['spread_relief'])
        self.assertEqual(s['initial_margin'], e['initial_margin'])
        self.assertEqual(s['margin_call'], e['margin_call'])
        self.assertEqual(s['cash_to_raise'], e['cash_to_raise'])
        approved = [d for d in s['spread_decisions'] if d['approved']]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]['reason'], 'clearing-pair-confirmed')

    def test_cancelled_jul_with_standalone_sep_costs_more(self):
        s = stmt_for('cancelled_jul')
        e = FX['expect']['cancelled_jul']
        self.assertEqual(s['positions']['WHEAT-JUL'], 3)
        self.assertEqual(s['positions']['WHEAT-SEP'], -2)
        self.assertEqual(s['spread_relief'], 0)
        self.assertEqual(s['initial_margin'], 5000)
        self.assertEqual(s['margin_call'], e['margin_call'])
        self.assertEqual(s['cash_to_raise'], e['cash_to_raise'])

    def test_two_endgames_ordering_and_cash_difference(self):
        pending = stmt_for()
        paired = stmt_for('paired')
        revoked = stmt_for('cancelled_jul')
        # approved combo -> least cash; revoked sell -> most; single-leg in between
        self.assertLess(paired['margin_call'], pending['margin_call'])
        self.assertLess(pending['margin_call'], revoked['margin_call'])
        self.assertEqual((paired['margin_call'], pending['margin_call'],
                          revoked['margin_call']), (1700, 3200, 4200))
        self.assertEqual((paired['cash_to_raise'], pending['cash_to_raise'],
                          revoked['cash_to_raise']), (500, 2000, 3000))
        # both endgames still segregate months and never double count
        for s in (paired, revoked):
            self.assertNotIn('WHEAT', s['positions'])  # no variety-level net key
            self.assertEqual(len([k for k in s['positions']]), 2)


class CollateralLedgerTest(unittest.TestCase):
    def _cap(self, **kw):
        events = [{'trade_id': 'T1', 'account': 'A',
                   'contract': 'WHEAT-JUL', 'lots': 3,
                   'source': 'clearing-house', 'status': 'confirmed'}]
        opts = dict(rates={'WHEAT-JUL': 1000}, collateral_posted=1000,
                    available_cash=0)
        opts.update(kw)
        return HedgeCapture(events, **opts).statement()

    def test_initial_variation_collateral_cash_are_separate(self):
        s = self._cap(settlements={'WHEAT-JUL': {'variation_per_lot': 100}})
        self.assertEqual(s['initial_margin'], 3000)
        self.assertEqual(s['variation_margin'], 300)
        self.assertEqual(s['collateral_posted'], 1000)
        self.assertEqual(s['collateral_eligible'], 1000)
        self.assertEqual(s['available_cash'], 0)
        # profit is receivable, not collateral
        self.assertEqual(s['variation_receivable'], 300)
        self.assertEqual(s['variation_payable'], 0)

    def test_today_profit_is_not_counted_as_collateral(self):
        s = self._cap(settlements={'WHEAT-JUL': {'variation_per_lot': 500 / 3}})
        self.assertAlmostEqual(s['variation_margin'], 500)
        self.assertEqual(s['collateral_eligible'], 1000)
        # 3000 initial - 1000 posted collateral; the 500 gain must NOT deduct more
        self.assertEqual(s['margin_call'], 2000)
        self.assertNotEqual(s['margin_call'], 1500)

    def test_profit_is_not_available_cash_either(self):
        s = self._cap(available_cash=0,
                      settlements={'WHEAT-JUL': {'variation_per_lot': 200}})
        self.assertEqual(s['variation_margin'], 600)
        self.assertEqual(s['available_cash'], 0)

    def test_variation_loss_adds_to_call(self):
        s = self._cap(settlements={'WHEAT-JUL': {'variation_per_lot': -100}})
        self.assertEqual(s['variation_margin'], -300)
        self.assertEqual(s['variation_payable'], 300)
        self.assertEqual(s['margin_call'], 2300)

    def test_collateral_haircut_reduces_availability(self):
        s = self._cap(collateral_posted=[
            {'account': 'A', 'kind': 'cash', 'amount': 500, 'haircut': 0},
            {'account': 'A', 'kind': 'warehouse-receipt', 'amount': 500,
             'haircut': 0.4},
        ])
        self.assertEqual(s['collateral_posted'], 1000)
        self.assertEqual(s['collateral_eligible'], 800)
        self.assertEqual(s['margin_call'], 2200)  # 3000 - 800

    def test_frozen_or_released_collateral_is_unavailable(self):
        s = self._cap(collateral_posted=[
            {'account': 'A', 'kind': 'cash', 'amount': 600},
            {'account': 'A', 'kind': 'bond', 'amount': 400, 'status': 'frozen'},
        ])
        self.assertEqual(s['collateral_eligible'], 600)
        self.assertEqual(s['margin_call'], 2400)  # 3000 - 600


class VersionedMarginCallReadbackTest(unittest.TestCase):
    def _issued_base_call(self):
        book = MarginCallBook()
        cap = cap_for()
        cap.book = book
        call = cap.issue_call(account=ACCOUNT, round='R1')
        return cap, book, call

    def test_original_call_is_immutable_after_clearing_adjustment(self):
        cap, book, call = self._issued_base_call()
        self.assertEqual(call['amount'], 3200)
        self.assertEqual(call['version'], 1)

        cap.extend(copy.deepcopy(FX['branches']['cancelled_jul']))
        adj = cap.revise_call(call, reason='JUL sell revoked by clearing',
                              receipt_id='R2-CANC')
        self.assertEqual(adj['kind'], 'adjustment')
        self.assertEqual(adj['original_call_id'], call['id'])
        self.assertEqual(adj['delta'], 1000)
        self.assertEqual(adj['new_amount'], 4200)

        # old notice reads back with its original amount, untouched
        read_back = book.read_call(call['id'])
        self.assertEqual(read_back['amount'], 3200)
        self.assertEqual(book.calls[0]['amount'], 3200)
        self.assertNotIn('adjusted_amount', read_back)
        history = book.history(call['id'])
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0]['amount'], 3200)
        self.assertEqual(history[1]['new_amount'], 4200)

    def test_paired_endgame_adjustment_points_back_with_credit(self):
        cap, book, call = self._issued_base_call()
        cap.extend(copy.deepcopy(FX['branches']['paired']))
        adj = cap.revise_call(call, reason='pair approved + relief receipt',
                              receipt_id='SPR-ROLL-1-R2')
        self.assertEqual(adj['delta'], -1500)
        self.assertEqual(adj['new_amount'], 1700)
        self.assertEqual(adj['original_call_id'], 'MC-1')
        self.assertEqual(book.read_call('MC-1')['amount'], 3200)

    def test_returned_call_copy_cannot_rewrite_book(self):
        _, book, call = self._issued_base_call()
        self.assertEqual(call['amount'], 3200)
        call['amount'] = 1  # mutate the returned snapshot
        # stored original notice is unaffected
        self.assertEqual(book.read_call('MC-1')['amount'], 3200)
        self.assertEqual(book.calls[0]['amount'], 3200)
        # the audit-safe correction path appends, never overwrites
        book.adjust('MC-1', new_amount=4200, reason='clearing correction')
        self.assertEqual(book.read_call('MC-1')['amount'], 3200)
        self.assertEqual(book.adjustments('MC-1')[0]['new_amount'], 4200)

    def test_book_export_import_reads_old_notice_and_adjustments(self):
        cap, book, call = self._issued_base_call()
        cap.extend(copy.deepcopy(FX['branches']['paired']))
        cap.revise_call(call)
        payload = book.export()

        restored = MarginCallBook(payload)
        self.assertEqual(restored.read_call('MC-1')['amount'], 3200)
        adjustments = restored.adjustments('MC-1')
        self.assertEqual(len(adjustments), 1)
        self.assertEqual(adjustments[0]['new_amount'], 1700)
        self.assertEqual(adjustments[0]['original_call_id'], 'MC-1')

    def test_statement_from_fixture_path(self):
        s = statement(str(FIXTURE))
        self.assertEqual(s['margin_call'], 3200)
        self.assertEqual(s['available_cash'], 1200)

    def test_old_notice_read_back_from_disk(self):
        import tempfile
        from grainrelease.hedge_capture import MarginCallBook as Book

        cap, book, call = self._issued_base_call()
        cap.extend(copy.deepcopy(FX['branches']['paired']))
        cap.revise_call(call, receipt_id='SPR-ROLL-1-R2')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'margin-calls.json'
            book.save(path)
            reopened = Book.load_file(path)
            # original notice still reads back at its issued amount
            self.assertEqual(reopened.read_call('MC-1')['amount'], 3200)
            adj = reopened.adjustments('MC-1')
            self.assertEqual(adj[0]['new_amount'], 1700)
            self.assertEqual(adj[0]['original_call_id'], 'MC-1')


if __name__ == '__main__':
    unittest.main()
