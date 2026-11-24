import json
import tempfile
import threading
import unittest
from pathlib import Path

from grainrelease.customer_credit import limits
from grainrelease.credit_booking import accepted
from grainrelease.credit_capture import BankLedger, CreditCapture, token_for

ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / 'fixtures' / 'credit-race.json').read_text(encoding='utf-8'))
NOW = FIX['as_of']
BEN = FIX['beneficiary']
BOOKINGS = {b['booking_id']: b for b in FIX['bookings']}
LETTERS = {l['letter_id']: l for l in FIX['letters']}


def make_bank(letters=None):
    bank = BankLedger.connect()
    for letter in (FIX['letters'] if letters is None else letters):
        bank.submit_letter(letter)
    return bank


def make_capture(bank):
    cap = CreditCapture.connect(':memory:', bank, BEN)
    cap.load_credit(FIX['credit'])
    return cap


def occupied_rows(bank):
    return [dict(r) for r in bank.db.execute("SELECT * FROM occupations WHERE state='occupied'")]


class LimitsAndAcceptedTest(unittest.TestCase):
    """限额扣减与逐笔试算：占用必须来自持久化意图或银行证明。"""

    def test_limits_deduct_live_occupations(self):
        # 银行已为 R1 预占 80，C1 可用额降为 20
        self.assertEqual(limits(FIX['credit'], FIX['occupations']), {'C1': 20})

    def test_limits_keep_last_limit_record(self):
        events = [{'customer_id': 'C1', 'limit': 50}, {'customer_id': 'C1', 'limit': 100}]
        self.assertEqual(limits(events), {'C1': 100})

    def test_settled_occupations_do_not_deduct(self):
        for state in ('released', 'revoked', 'pending'):
            occ = [{'booking_id': 'R9', 'customer_id': 'C1', 'amount': 80, 'state': state}]
            self.assertEqual(limits(FIX['credit'], occ), {'C1': 100}, state)

    def test_accepted_flips_overcommitted_booking_to_pending(self):
        # R1 已被银行预占 80 后，R2=50 必须从 true 改为 pending
        self.assertEqual(accepted(FIX['credit'], [BOOKINGS['R2']], FIX['occupations']),
                         {'R2': 'pending'})

    def test_accepted_without_occupations_reports_both_true(self):
        # 旧行为：两个柜台同时受理时各自看 100，两笔都报 true（竞态缺口）
        self.assertEqual(accepted(FIX['credit'], FIX['bookings']), {'R1': True, 'R2': True})


class ConcurrencyTest(unittest.TestCase):
    """双柜台并发：同一不可循环额度不能被两个柜台各自全额承诺。"""

    def test_two_counters_never_overcommit(self):
        for _ in range(10):
            bank = make_bank()
            cap = make_capture(bank)
            errors = []

            def run(b):
                try:
                    cap.book(**b, now=NOW)
                except Exception as exc:  # noqa: BLE001 - 线程内异常必须显式回收
                    errors.append(exc)

            threads = [threading.Thread(target=run, args=(b,)) for b in FIX['bookings']]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual(errors, [])

            states = cap.status()
            self.assertEqual(sorted(states.values()), ['effective', 'pending'])
            winner = next(bid for bid, s in states.items() if s == 'effective')
            loser = next(bid for bid, s in states.items() if s == 'pending')
            amount = BOOKINGS[winner]['amount']

            # 总额不超 100：本地仍有效占用与银行占用都不超额
            self.assertEqual(cap.available('C1'), 100 - amount)
            self.assertEqual(limits(FIX['credit'], cap.occupations())['C1'], 100 - amount)
            occupied = occupied_rows(bank)
            self.assertEqual(len(occupied), 1)
            self.assertLessEqual(sum(o['amount'] for o in occupied), 100)

            # 令牌与预约号一一绑定
            self.assertEqual(occupied[0]['token'], token_for(winner))
            self.assertEqual(occupied[0]['booking_id'], winner)

            # 只有一笔对客户承诺并生效；另一笔登记待核，未曾承诺
            self.assertEqual([n['kind'] for n in cap.notices(winner)], ['promise', 'confirm'])
            self.assertEqual([n['kind'] for n in cap.notices(loser)], ['register'])
            cap.close()


class OfflineRetryTest(unittest.TestCase):
    """银行断网前后：承诺先落库，重试不多扣担保、不多占仓位。"""

    def test_booking_survives_bank_outage_until_receipt(self):
        bank = make_bank()
        cap = make_capture(bank)
        r1 = BOOKINGS['R1']

        bank.online = False
        self.assertEqual(cap.book(**r1, now=NOW), 'promised')
        self.assertEqual(cap.book(**r1, now=NOW), 'promised')  # 断网重试不多占
        self.assertEqual([n['kind'] for n in cap.notices('R1')], ['promise'])
        self.assertEqual(occupied_rows(bank), [])  # 担保未被多扣
        self.assertEqual(bank.journal(), [])

        cap.recover(NOW)  # 断网中恢复：承诺原地保留，不悄悄消失
        self.assertEqual(cap.status()['R1'], 'promised')
        self.assertEqual([n['kind'] for n in cap.notices('R1')], ['promise'])

        bank.online = True
        cap.recover(NOW)
        self.assertEqual(cap.status()['R1'], 'effective')
        self.assertEqual([n['kind'] for n in cap.notices('R1')], ['promise', 'confirm'])
        self.assertEqual(bank.available('G-1'), 20)
        occupied = occupied_rows(bank)
        self.assertEqual(len(occupied), 1)
        self.assertEqual(occupied[0]['token'], token_for('R1'))

        notices_before = cap.notices()
        cap.recover(NOW)  # 重复恢复幂等：不多扣、不重复通知
        self.assertEqual(cap.notices(), notices_before)
        self.assertEqual(bank.available('G-1'), 20)
        self.assertEqual(cap.book(**r1, now=NOW), 'effective')  # 迟到重试沿用原令牌
        self.assertEqual(bank.available('G-1'), 20)
        cap.close()


class GuaranteeLetterTest(unittest.TestCase):
    """担保函要件：指定客户、有效期、受益粮库、可用余额；草稿与已撤销不算资金。"""

    def book_and_settle(self, bank, letter_id, booking_id='R9', amount=40):
        cap = make_capture(bank)
        state = cap.book(booking_id, 'C1', letter_id, amount, 'desk-1', now=NOW)
        return cap, state

    def test_draft_letter_is_not_funds(self):
        bank = make_bank()
        cap, state = self.book_and_settle(bank, 'G-3')  # G-3 是未生效草稿
        self.assertEqual(state, 'pending')
        self.assertEqual([n['kind'] for n in cap.notices('R9')], ['promise', 'revoke-promise'])
        self.assertIn('未生效', cap.notices('R9')[-1]['detail'])
        self.assertEqual(occupied_rows(bank), [])

    def test_expired_letter_is_not_funds(self):
        bank = make_bank()
        bank.submit_letter(dict(LETTERS['G-1'], letter_id='G-9',
                                valid_until='2026-11-15T00:00:00'))
        cap, state = self.book_and_settle(bank, 'G-9')
        self.assertEqual(state, 'pending')
        self.assertIn('有效期', cap.notices('R9')[-1]['detail'])
        self.assertEqual(occupied_rows(bank), [])

    def test_revoked_letter_is_not_funds(self):
        bank = make_bank()
        bank.submit_letter(dict(LETTERS['G-1'], letter_id='G-7'))
        bank.set_letter_status('G-7', 'revoked')
        cap, state = self.book_and_settle(bank, 'G-7')
        self.assertEqual(state, 'pending')
        self.assertIn('已撤销', cap.notices('R9')[-1]['detail'])
        self.assertEqual(occupied_rows(bank), [])

    def test_beneficiary_must_name_this_depot(self):
        bank = make_bank()
        bank.submit_letter(dict(LETTERS['G-1'], letter_id='G-8', beneficiary='depot-south'))
        cap, state = self.book_and_settle(bank, 'G-8')
        self.assertEqual(state, 'pending')
        self.assertIn('受益粮库', cap.notices('R9')[-1]['detail'])
        self.assertEqual(occupied_rows(bank), [])

    def test_letter_must_name_the_customer(self):
        bank = make_bank()
        bank.submit_letter(dict(LETTERS['G-1'], letter_id='G-6', customer_id='C2'))
        cap, state = self.book_and_settle(bank, 'G-6')
        self.assertEqual(state, 'pending')
        self.assertIn('客户不符', cap.notices('R9')[-1]['detail'])
        self.assertEqual(occupied_rows(bank), [])

    def test_letter_balance_enforced_and_retry_idempotent(self):
        bank = make_bank()
        first = bank.occupy(token_for('RA'), 'RA', 'C1', 'G-1', BEN, 80, NOW)
        self.assertEqual(first['result'], 'occupied')
        again = bank.occupy(token_for('RA'), 'RA', 'C1', 'G-1', BEN, 80, NOW)
        self.assertEqual(again['result'], 'occupied')  # 同一令牌重试返回原回执
        self.assertEqual(bank.available('G-1'), 20)  # 不多扣
        second = bank.occupy(token_for('RB'), 'RB', 'C1', 'G-1', BEN, 50, NOW)
        self.assertEqual(second['result'], 'rejected')
        self.assertEqual(second['reason'], 'insufficient-balance')
        self.assertEqual(second['available'], 20)
        self.assertEqual(bank.available('G-1'), 20)


class RevocationTest(unittest.TestCase):
    """撤销函：已生效预约的资金失效后，必须具名撤销或替代确认。"""

    def test_revoked_letter_triggers_named_revocation(self):
        bank = make_bank([LETTERS['G-1']])  # 无替代担保函
        cap = make_capture(bank)
        self.assertEqual(cap.book(**BOOKINGS['R1'], now=NOW), 'effective')
        bank.set_letter_status('G-1', 'revoked')  # 银行收到撤销函
        cap.reconcile(NOW)
        self.assertEqual(cap.status()['R1'], 'revoked')
        kinds = [n['kind'] for n in cap.notices('R1')]
        self.assertEqual(kinds, ['promise', 'confirm', 'revoke'])
        detail = cap.notices('R1')[-1]['detail']
        self.assertIn('R1', detail)
        self.assertIn('G-1', detail)
        self.assertIn('撤销', detail)
        self.assertEqual(cap.available('C1'), 100)  # 占用解除，额度归还
        self.assertEqual(bank.proof(token_for('R1'))['state'], 'released')
        cap.close()

    def test_replacement_letter_keeps_booking_effective(self):
        bank = make_bank([LETTERS['G-1'], LETTERS['G-2']])
        cap = make_capture(bank)
        self.assertEqual(cap.book(**BOOKINGS['R1'], now=NOW), 'effective')
        bank.set_letter_status('G-1', 'revoked')
        cap.reconcile(NOW)
        self.assertEqual(cap.status()['R1'], 'effective')
        kinds = [n['kind'] for n in cap.notices('R1')]
        self.assertEqual(kinds, ['promise', 'confirm', 'replace'])
        self.assertIn('G-2', cap.notices('R1')[-1]['detail'])
        proof = bank.proof(token_for('R1'))  # 令牌不变，担保改挂 G-2
        self.assertEqual(proof['state'], 'occupied')
        self.assertEqual(proof['letter_id'], 'G-2')
        self.assertEqual(bank.available('G-2'), 20)
        self.assertEqual(cap.available('C1'), 20)
        cap.close()


class RecoveryConflictTest(unittest.TestCase):
    """两种恢复：同一断电快照，银行按令牌证明哪笔真实占用就保哪笔。"""

    def snapshot(self):
        """两个离线柜台均已承诺、银行回执尚未写本地的断电快照。"""
        bank = make_bank()
        cap = make_capture(bank)
        for b in FIX['bookings']:
            self.assertTrue(cap.import_intent(b))
        self.assertFalse(cap.import_intent(FIX['bookings'][0]))  # 同步幂等
        self.assertEqual([n['kind'] for n in cap.notices()], ['promise', 'promise'])
        return bank, cap

    def test_bank_proofs_decide_which_promise_survives(self):
        for name, scenario in FIX['recovery'].items():
            with self.subTest(scenario=name):
                bank, cap = self.snapshot()
                self.assertEqual(cap.status(), {'R1': 'promised', 'R2': 'promised'})

                # 断电前银行端曾收到两笔同一担保函的请求，先到者占用
                order = scenario['occupied'] + [b['booking_id'] for b in FIX['bookings']
                                                if b['booking_id'] not in scenario['occupied']]
                for bid in order:
                    b = BOOKINGS[bid]
                    bank.occupy(token_for(bid), bid, b['customer_id'], b['letter_id'],
                                BEN, b['amount'], NOW)
                attempts = [j for j in bank.journal() if j['action'] == 'occupy']
                self.assertEqual(len(attempts), 2)
                self.assertEqual(cap.status(), {'R1': 'promised', 'R2': 'promised'})  # 回执未写本地

                cap.recover(NOW)
                self.assertEqual(cap.status(), scenario['expect'])

                winner = scenario['occupied'][0]
                loser = 'R2' if winner == 'R1' else 'R1'
                amount = BOOKINGS[winner]['amount']

                # 赢家补齐回执生效；输家待核并具名撤回先前承诺
                self.assertEqual([n['kind'] for n in cap.notices(winner)], ['promise', 'confirm'])
                self.assertEqual([n['kind'] for n in cap.notices(loser)],
                                 ['promise', 'revoke-promise'])
                detail = cap.notices(loser)[-1]['detail']
                self.assertIn(loser, detail)
                self.assertIn('撤回', detail)
                self.assertIn('余额不足', detail)

                # 两路都守：总额不超 100、令牌一一绑定、占用不多扣
                self.assertEqual(cap.available('C1'), 100 - amount)
                self.assertEqual(limits(FIX['credit'], cap.occupations())['C1'], 100 - amount)
                occupied = occupied_rows(bank)
                self.assertEqual(len(occupied), 1)
                self.assertEqual(occupied[0]['token'], token_for(winner))
                self.assertEqual(occupied[0]['booking_id'], winner)
                self.assertLessEqual(sum(o['amount'] for o in occupied), 100)

                notices_before = cap.notices()
                cap.recover(NOW)  # 重复恢复幂等
                self.assertEqual(cap.notices(), notices_before)
                self.assertEqual(cap.status(), scenario['expect'])
                cap.close()


class NotificationReadbackTest(unittest.TestCase):
    """旧通知回读：通知只增不删，迟到回执在重启后补齐，承诺不悄悄消失。"""

    def test_notices_survive_reopen_and_every_promise_is_answered(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as td:
            path = str(Path(td) / 'counter.sqlite')
            cap = CreditCapture.connect(path, bank, BEN)
            cap.load_credit(FIX['credit'])
            for b in FIX['bookings']:
                cap.import_intent(b)
            # 银行已占用 R1、拒绝 R2，但本地断电未写回执
            bank.occupy(token_for('R1'), 'R1', 'C1', 'G-1', BEN, 80, NOW)
            bank.occupy(token_for('R2'), 'R2', 'C1', 'G-1', BEN, 50, NOW)
            old = cap.notices()
            self.assertEqual([n['kind'] for n in old], ['promise', 'promise'])
            cap.close()

            reopened = CreditCapture.connect(path, bank, BEN)
            self.assertEqual(reopened.notices(), old)  # 旧通知回读
            self.assertEqual(reopened.status(), {'R1': 'promised', 'R2': 'promised'})

            reopened.recover(NOW)  # 迟到回执在重启后核对补齐
            self.assertEqual(reopened.status(), {'R1': 'effective', 'R2': 'pending'})
            kinds = [n['kind'] for n in reopened.notices()]
            self.assertEqual(kinds, ['promise', 'promise', 'confirm', 'revoke-promise'])

            # 每条对客户承诺都有下文：生效确认或具名撤回，不悄悄消失
            by_booking = {}
            for n in reopened.notices():
                by_booking.setdefault(n['booking_id'], []).append(n['kind'])
            for bid, seq_kinds in by_booking.items():
                if 'promise' in seq_kinds:
                    later = seq_kinds[seq_kinds.index('promise'):]
                    self.assertTrue({'confirm', 'revoke-promise', 'revoke'} & set(later), bid)
            seqs = [n['seq'] for n in reopened.notices()]
            self.assertEqual(seqs, sorted(seqs))

            reopened.recover(NOW)  # 重启后重复恢复仍幂等
            self.assertEqual([n['kind'] for n in reopened.notices()], kinds)
            reopened.close()


if __name__ == '__main__':
    unittest.main()
