"""仓位预约与银行担保占用的可恢复同一业务承诺。

柜台先把带幂等令牌的承诺意图持久化，再请银行占用担保函额度；本地与银行
之间无法跨一次断电原子提交，因此断电重启后按令牌核对银行端真实占用，
补齐本地回执或具名撤回承诺，形成可审计的客户通知。预约状态词汇：
effective 已生效 / pending 待核 / revoked 已撤销；promised 是柜台已承诺、
银行回执未落库的过渡态。主营结果仅是客户能否拿到下季仓位，本模块不裁定
粮批放行，也不报送出库运单或计算电费。
"""
import json
import sqlite3
import threading
from datetime import datetime

SCHEMA = """
CREATE TABLE IF NOT EXISTS credit_limits (customer_id TEXT PRIMARY KEY, cap INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS intents (
    token TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL,
    letter_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    counter TEXT NOT NULL,
    state TEXT NOT NULL,
    receipt TEXT
);
CREATE TABLE IF NOT EXISTS notices (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

BANK_SCHEMA = """
CREATE TABLE IF NOT EXISTS letters (
    letter_id TEXT PRIMARY KEY,
    customer_id TEXT NOT NULL,
    beneficiary TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    balance INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS occupations (
    token TEXT PRIMARY KEY,
    booking_id TEXT NOT NULL UNIQUE,
    letter_id TEXT NOT NULL,
    amount INTEGER NOT NULL,
    state TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL,
    booking_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""

LETTER_FIELDS = ('customer_id', 'beneficiary', 'valid_from', 'valid_until', 'balance', 'status')

REASONS = {
    'unknown-letter': '担保函不存在',
    'customer-mismatch': '担保函指定客户不符',
    'beneficiary-mismatch': '担保函受益粮库不符',
    'letter-not-effective': '担保函未生效或已撤销',
    'letter-expired': '担保函不在有效期内',
    'insufficient-balance': '担保函可用余额不足',
    'occupation-released': '银行担保占用已释放',
    'token-booking-mismatch': '占用令牌与预约号绑定不符',
    'token-released': '占用令牌已释放',
}


class BankOfflineError(Exception):
    """银行链路断开：承诺保持 promised，待恢复后按令牌核对。"""


def token_for(booking_id):
    """占用令牌由预约号派生，断网重试沿用同一令牌，与预约号一一绑定。"""
    return 'tok:' + booking_id


def _now():
    return datetime.now().isoformat(timespec='seconds')


def _connect(path):
    db = sqlite3.connect(path, check_same_thread=False)
    db.row_factory = sqlite3.Row
    return db


class BankLedger:
    """银行端担保台账：担保函要件校验与占用令牌的一一绑定在此原子完成。"""

    def __init__(self, db):
        db.row_factory = sqlite3.Row
        self.db = db
        self.db.executescript(BANK_SCHEMA)
        self.online = True  # 模拟柜台到银行的链路；置 False 即断网
        self._lock = threading.Lock()

    @classmethod
    def connect(cls, path=':memory:'):
        return cls(_connect(path))

    def _link(self):
        if not self.online:
            raise BankOfflineError('银行链路断开')

    def _available(self, letter_id):
        row = self.db.execute('SELECT balance FROM letters WHERE letter_id=?', (letter_id,)).fetchone()
        if row is None:
            return 0
        used = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM occupations WHERE letter_id=? AND state='occupied'",
            (letter_id,)).fetchone()[0]
        return row[0] - used

    def _journal(self, token, booking_id, action, result, detail):
        self.db.execute('INSERT INTO journal (token,booking_id,action,result,detail) VALUES (?,?,?,?,?)',
                        (token, booking_id, action, result, detail))

    def submit_letter(self, letter):
        """登记担保函（含未生效草稿）；同号不同内容视为冲突。"""
        with self._lock:
            row = self.db.execute('SELECT * FROM letters WHERE letter_id=?', (letter['letter_id'],)).fetchone()
            if row:
                if any(row[k] != letter[k] for k in LETTER_FIELDS):
                    raise ValueError('same letter id with different content')
                return False
            self.db.execute('INSERT INTO letters VALUES (?,?,?,?,?,?,?)',
                            (letter['letter_id'], *(letter[k] for k in LETTER_FIELDS)))
            self.db.commit()
            return True

    def set_letter_status(self, letter_id, status):
        """银行端生效或撤销担保函；撤销函使其全部占用立即失效，不再充当资金。"""
        with self._lock:
            if self.db.execute('SELECT 1 FROM letters WHERE letter_id=?', (letter_id,)).fetchone() is None:
                raise KeyError(letter_id)
            self.db.execute('UPDATE letters SET status=? WHERE letter_id=?', (status, letter_id))
            if status == 'revoked':
                for occ in self.db.execute(
                        "SELECT token, booking_id FROM occupations WHERE letter_id=? AND state='occupied'",
                        (letter_id,)).fetchall():
                    self.db.execute("UPDATE occupations SET state='released' WHERE token=?", (occ['token'],))
                    self._journal(occ['token'], occ['booking_id'], 'release', 'released',
                                  f'担保函{letter_id}被银行撤销')
            self.db.commit()

    def letter(self, letter_id):
        self._link()
        row = self.db.execute('SELECT * FROM letters WHERE letter_id=?', (letter_id,)).fetchone()
        return dict(row) if row else None

    def letters_for(self, customer_id, beneficiary, now):
        """指定客户、受益粮库且在有效期内的生效担保函（替代确认候选）。"""
        self._link()
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM letters WHERE customer_id=? AND beneficiary=? AND status='effective'"
            " AND valid_from<=? AND valid_until>=? ORDER BY letter_id",
            (customer_id, beneficiary, now, now))]

    def available(self, letter_id):
        self._link()
        return self._available(letter_id)

    def _validate(self, letter, customer_id, beneficiary, amount, now):
        if letter is None:
            return 'unknown-letter'
        if letter['customer_id'] != customer_id:
            return 'customer-mismatch'
        if letter['beneficiary'] != beneficiary:
            return 'beneficiary-mismatch'
        if letter['status'] != 'effective':
            return 'letter-not-effective'
        if not letter['valid_from'] <= now <= letter['valid_until']:
            return 'letter-expired'
        if amount > self._available(letter['letter_id']):
            return 'insufficient-balance'
        return None

    def occupy(self, token, booking_id, customer_id, letter_id, beneficiary, amount, now):
        """占用担保额度；同一令牌重试返回原回执，绝不多扣担保。"""
        self._link()
        with self._lock:
            row = self.db.execute('SELECT * FROM occupations WHERE token=?', (token,)).fetchone()
            if row and row['booking_id'] != booking_id:
                self._journal(token, booking_id, 'occupy', 'rejected', 'token-booking-mismatch')
                self.db.commit()
                return {'token': token, 'booking_id': booking_id, 'result': 'rejected',
                        'reason': 'token-booking-mismatch'}
            if row and row['state'] == 'occupied':
                self._journal(token, booking_id, 'occupy', 'duplicate', '幂等重试返回原回执')
                self.db.commit()
                return {'token': token, 'booking_id': booking_id, 'result': 'occupied',
                        'letter_id': row['letter_id'], 'amount': row['amount'], 'idempotent': True}
            if row and row['state'] == 'released':
                self._journal(token, booking_id, 'occupy', 'rejected', 'token-released')
                self.db.commit()
                return {'token': token, 'booking_id': booking_id, 'result': 'rejected',
                        'reason': 'token-released'}
            letter = self.db.execute('SELECT * FROM letters WHERE letter_id=?', (letter_id,)).fetchone()
            reason = self._validate(letter, customer_id, beneficiary, amount, now)
            if reason:
                self._journal(token, booking_id, 'occupy', 'rejected', reason)
                self.db.commit()
                receipt = {'token': token, 'booking_id': booking_id, 'result': 'rejected',
                           'reason': reason, 'letter_id': letter_id}
                if reason == 'insufficient-balance':
                    receipt['available'] = self._available(letter_id)
                return receipt
            self.db.execute(
                "INSERT INTO occupations (token,booking_id,letter_id,amount,state) VALUES (?,?,?,?,'occupied')",
                (token, booking_id, letter_id, amount))
            self._journal(token, booking_id, 'occupy', 'occupied', f'担保函{letter_id}占用{amount}')
            self.db.commit()
            return {'token': token, 'booking_id': booking_id, 'result': 'occupied',
                    'letter_id': letter_id, 'amount': amount}

    def reassign(self, token, letter_id, customer_id, beneficiary, now):
        """把失效担保函上的占用改挂到替代担保函，令牌与预约号保持不变。"""
        self._link()
        with self._lock:
            occ = self.db.execute('SELECT * FROM occupations WHERE token=?', (token,)).fetchone()
            if occ is None:
                raise KeyError(token)
            letter = self.db.execute('SELECT * FROM letters WHERE letter_id=?', (letter_id,)).fetchone()
            reason = self._validate(letter, customer_id, beneficiary, occ['amount'], now)
            if reason:
                self._journal(token, occ['booking_id'], 'reassign', 'rejected', reason)
                self.db.commit()
                return {'token': token, 'booking_id': occ['booking_id'], 'result': 'rejected',
                        'reason': reason}
            old = occ['letter_id']
            self.db.execute("UPDATE occupations SET letter_id=?, state='occupied' WHERE token=?",
                            (letter_id, token))
            self._journal(token, occ['booking_id'], 'reassign', 'occupied',
                          f'担保函{old}改挂{letter_id}')
            self.db.commit()
            return {'token': token, 'booking_id': occ['booking_id'], 'result': 'occupied',
                    'letter_id': letter_id, 'amount': occ['amount']}

    def release(self, token):
        """释放占用（预约撤销后归还担保额度），幂等。"""
        self._link()
        with self._lock:
            occ = self.db.execute('SELECT * FROM occupations WHERE token=?', (token,)).fetchone()
            if occ and occ['state'] == 'occupied':
                self.db.execute("UPDATE occupations SET state='released' WHERE token=?", (token,))
                self._journal(token, occ['booking_id'], 'release', 'released', '预约撤销归还额度')
                self.db.commit()

    def proof(self, token):
        """按令牌出具占用证明，供断电恢复时核对哪一笔真实占用。"""
        self._link()
        row = self.db.execute('SELECT * FROM occupations WHERE token=?', (token,)).fetchone()
        return dict(row) if row else None

    def journal(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM journal ORDER BY seq')]


class CreditCapture:
    """粮库柜台预约台账：承诺意图先落库，银行回执后生效，断电可恢复。"""

    def __init__(self, db, bank, beneficiary):
        db.row_factory = sqlite3.Row
        self.db = db
        self.bank = bank
        self.beneficiary = beneficiary
        self.db.executescript(SCHEMA)
        self._lock = threading.Lock()

    @classmethod
    def connect(cls, path, bank, beneficiary):
        return cls(_connect(path), bank, beneficiary)

    def close(self):
        self.db.close()

    def load_credit(self, events):
        """载入客户限额，同一 customer_id 以最后一条记录为准。"""
        with self._lock:
            for event in events:
                self.db.execute('INSERT OR REPLACE INTO credit_limits VALUES (?,?)',
                                (event['customer_id'], event['limit']))
            self.db.commit()

    def available(self, customer_id):
        """同一不可循环授信额度的剩余量：限额扣除所有仍有效预约占用。"""
        row = self.db.execute('SELECT cap FROM credit_limits WHERE customer_id=?', (customer_id,)).fetchone()
        if row is None:
            return 0
        used = self.db.execute(
            "SELECT COALESCE(SUM(amount),0) FROM intents WHERE customer_id=?"
            " AND state IN ('promised','effective')", (customer_id,)).fetchone()[0]
        return row[0] - used

    def occupations(self):
        """仍有效的预约占用，供 customer_credit.limits 扣减。"""
        return [dict(r) for r in self.db.execute(
            "SELECT booking_id, customer_id, amount, state FROM intents"
            " WHERE state IN ('promised','effective') ORDER BY booking_id")]

    def status(self):
        """每笔预约当前状态：effective 已生效 / pending 待核 / revoked 已撤销。"""
        return {r['booking_id']: r['state']
                for r in self.db.execute('SELECT booking_id, state FROM intents ORDER BY booking_id')}

    def notices(self, booking_id=None):
        """客户通知日志回读：只增不删，断电重启后仍可逐笔追溯。"""
        sql = 'SELECT seq, booking_id, kind, detail FROM notices'
        args = ()
        if booking_id is not None:
            sql += ' WHERE booking_id=?'
            args = (booking_id,)
        return [dict(r) for r in self.db.execute(sql + ' ORDER BY seq', args)]

    def _notice(self, booking_id, kind, detail):
        self.db.execute('INSERT INTO notices (booking_id,kind,detail) VALUES (?,?,?)',
                        (booking_id, kind, detail))

    @staticmethod
    def _promise_detail(record):
        return (f"柜台{record['counter']}已承诺客户{record['customer_id']}"
                f"预约{record['booking_id']}下季仓位（{record['amount']}），待银行担保回执")

    def book(self, booking_id, customer_id, letter_id, amount, counter, now=None):
        """登记预约：意图与柜台承诺先落库，再请银行占用；断网重试幂等。"""
        if amount <= 0:
            raise ValueError('amount must be positive')
        record = {'booking_id': booking_id, 'customer_id': customer_id, 'letter_id': letter_id,
                  'amount': amount, 'counter': counter}
        with self._lock:
            row = self.db.execute('SELECT * FROM intents WHERE booking_id=?', (booking_id,)).fetchone()
            if row is None:
                remaining = self.available(customer_id)
                state = 'promised' if amount <= remaining else 'pending'
                self.db.execute(
                    'INSERT INTO intents (token,booking_id,customer_id,letter_id,amount,counter,state,receipt)'
                    ' VALUES (?,?,?,?,?,?,?,NULL)',
                    (token_for(booking_id), booking_id, customer_id, letter_id, amount, counter, state))
                if state == 'promised':
                    self._notice(booking_id, 'promise', self._promise_detail(record))
                else:
                    self._notice(booking_id, 'register',
                                 f"柜台{counter}登记预约{booking_id}（客户{customer_id}，{amount}）转入待核："
                                 f"可用额度{remaining}不足")
                self.db.commit()
                row = self.db.execute('SELECT * FROM intents WHERE booking_id=?', (booking_id,)).fetchone()
            intent = dict(row)
        if intent['state'] == 'promised':
            self._settle(intent, now)
        return self.status()[booking_id]

    def import_intent(self, record):
        """合并另一离线柜台同步来的承诺意图（含其柜台承诺），幂等。"""
        with self._lock:
            if self.db.execute('SELECT 1 FROM intents WHERE booking_id=?',
                               (record['booking_id'],)).fetchone():
                return False
            self.db.execute(
                'INSERT INTO intents (token,booking_id,customer_id,letter_id,amount,counter,state,receipt)'
                ' VALUES (?,?,?,?,?,?,?,NULL)',
                (token_for(record['booking_id']), record['booking_id'], record['customer_id'],
                 record['letter_id'], record['amount'], record['counter'], 'promised'))
            self._notice(record['booking_id'], 'promise', self._promise_detail(record))
            self.db.commit()
            return True

    def _settle(self, intent, now):
        """把 promised 意图与银行核对：占用在则生效，被拒则撤回承诺。"""
        token = intent['token']
        try:
            proof = self.bank.proof(token)
            if proof and proof['booking_id'] != intent['booking_id']:
                raise ValueError('占用令牌与预约号绑定不符')
            if proof and proof['state'] == 'occupied':
                receipt = {'token': token, 'booking_id': intent['booking_id'], 'result': 'occupied',
                           'letter_id': proof['letter_id'], 'amount': proof['amount']}
            elif proof and proof['state'] == 'released':
                receipt = {'result': 'rejected', 'reason': 'occupation-released',
                           'letter_id': proof['letter_id']}
            else:
                receipt = self.bank.occupy(token, intent['booking_id'], intent['customer_id'],
                                           intent['letter_id'], self.beneficiary, intent['amount'],
                                           now or _now())
        except BankOfflineError:
            return  # 断网：承诺保持 promised，恢复后按令牌重试
        if receipt['result'] == 'occupied':
            with self._lock:
                if self.db.execute('SELECT state FROM intents WHERE token=?', (token,)).fetchone()[0] != 'promised':
                    return
                self.db.execute("UPDATE intents SET state='effective', receipt=? WHERE token=?",
                                (json.dumps(receipt, ensure_ascii=False, sort_keys=True), token))
                self._notice(intent['booking_id'], 'confirm',
                             f"银行回执确认：预约{intent['booking_id']}生效，担保函{receipt['letter_id']}"
                             f"为客户{intent['customer_id']}占用{receipt['amount']}")
                self.db.commit()
        else:
            self._withdraw(intent, receipt)

    def _withdraw(self, intent, receipt):
        """银行拒付：预约转入待核，并具名撤回柜台先前承诺。"""
        reason = REASONS.get(receipt.get('reason'), '银行拒付')
        detail = f"担保函{receipt['letter_id']}{reason}" if receipt.get('letter_id') else reason
        if receipt.get('reason') == 'insufficient-balance' and 'available' in receipt:
            detail += f"（剩余{receipt['available']}）"
        with self._lock:
            if self.db.execute('SELECT state FROM intents WHERE token=?',
                               (intent['token'],)).fetchone()[0] != 'promised':
                return
            self.db.execute("UPDATE intents SET state='pending' WHERE token=?", (intent['token'],))
            self._notice(intent['booking_id'], 'revoke-promise',
                         f"撤回柜台对预约{intent['booking_id']}（客户{intent['customer_id']}，"
                         f"{intent['amount']}）的先前承诺：{detail}")
            self.db.commit()

    def recover(self, now=None):
        """断电重启：按令牌核对银行真实占用，补齐回执或撤回承诺。"""
        promised = [dict(r) for r in self.db.execute(
            "SELECT * FROM intents WHERE state='promised' ORDER BY rowid")]
        for intent in promised:
            self._settle(intent, now)
        self.reconcile(now)
        return self.status()

    def reconcile(self, now=None):
        """复核已生效预约的担保仍然有效；失效则替代确认或具名撤销。"""
        now = now or _now()
        effective = [dict(r) for r in self.db.execute(
            "SELECT * FROM intents WHERE state='effective' ORDER BY rowid")]
        for intent in effective:
            try:
                proof = self.bank.proof(intent['token'])
                letter = self.bank.letter(intent['letter_id'])
            except BankOfflineError:
                continue
            funded = (proof is not None and proof['state'] == 'occupied'
                      and letter is not None and letter['status'] == 'effective'
                      and letter['valid_from'] <= now <= letter['valid_until'])
            if funded:
                continue
            why = self._funding_loss_reason(proof, letter, intent['letter_id'])
            replacement = self._replacement(intent, now)
            if replacement:
                receipt = self.bank.reassign(intent['token'], replacement['letter_id'],
                                             intent['customer_id'], self.beneficiary, now)
                if receipt['result'] != 'occupied':
                    continue  # 银行拒绝改挂，保持现状待下次复核
                with self._lock:
                    self.db.execute('UPDATE intents SET letter_id=?, receipt=? WHERE token=?',
                                    (replacement['letter_id'],
                                     json.dumps(receipt, ensure_ascii=False, sort_keys=True),
                                     intent['token']))
                    self._notice(intent['booking_id'], 'replace',
                                 f"替代确认：预约{intent['booking_id']}（客户{intent['customer_id']}，"
                                 f"{intent['amount']}）因{why}，银行担保改具{replacement['letter_id']}，"
                                 f"预约维持生效")
                    self.db.commit()
            else:
                try:
                    self.bank.release(intent['token'])
                except BankOfflineError:
                    pass
                with self._lock:
                    self.db.execute("UPDATE intents SET state='revoked' WHERE token=?",
                                    (intent['token'],))
                    self._notice(intent['booking_id'], 'revoke',
                                 f"具名撤销函：预约{intent['booking_id']}（客户{intent['customer_id']}，"
                                 f"{intent['amount']}）因{why}且无替代担保，予以撤销")
                    self.db.commit()
        return self.status()

    @staticmethod
    def _funding_loss_reason(proof, letter, letter_id):
        if letter is None:
            return f'担保函{letter_id}不存在'
        if letter['status'] == 'revoked':
            return f'担保函{letter_id}已被银行撤销'
        if letter['status'] != 'effective':
            return f'担保函{letter_id}未生效'
        if proof is None or proof['state'] != 'occupied':
            return f'担保函{letter_id}的银行占用已释放'
        return f'担保函{letter_id}不在有效期内'

    def _replacement(self, intent, now):
        try:
            for cand in self.bank.letters_for(intent['customer_id'], self.beneficiary, now):
                if cand['letter_id'] == intent['letter_id']:
                    continue
                if self.bank.available(cand['letter_id']) >= intent['amount']:
                    return cand
        except BankOfflineError:
            pass
        return None
