"""Offline multi-page original capture, send-job state and regulator receipt merging.

闭合口径（硬约束）：

1. 三页同版 —— 受理以 *内容根* 为准，v1 页与 v2 页根不同，编号凑齐
   1/2/3 也不能签成同一份运单；
2. 网关包头不等于受理 —— ``gateway_acks`` 只记录网关确认收到的包，
   只有监管端带内容根和 HMAC 签名的回执才计入受理；
3. 重试不混版 —— 每个发送轮次绑定一个提交令牌和一个内容根，重试
   开启新轮次、新令牌，仅补同一根下未受理的页，绝不把旧根页拼进新轮次。

所有写操作在单个 SQLite 事务内完成，失败整体回滚；回执按
``receipt_id`` 幂等去重，每页回执单独计数。
"""
import hashlib
import hmac
import json
import sqlite3
import uuid

from .manifest import (
    canonical,
    page_digest,
    verify_document,
)

DEFAULT_REGULATOR_SECRET = 'local-regulator-hmac-key'

SCHEMA = """
CREATE TABLE IF NOT EXISTS captured_roots (
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    version INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    pages_json TEXT NOT NULL,
    PRIMARY KEY (manifest_id, content_root)
);
CREATE TABLE IF NOT EXISTS captured_pages (
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    number INTEGER NOT NULL,
    kind TEXT NOT NULL,
    version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    signature TEXT NOT NULL,
    PRIMARY KEY (manifest_id, content_root, number)
);
CREATE TABLE IF NOT EXISTS send_rounds (
    round_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    round_no INTEGER NOT NULL,
    opened_at TEXT NOT NULL,
    status TEXT NOT NULL,
    watermark INTEGER NOT NULL,
    page_numbers_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS send_items (
    round_id TEXT NOT NULL,
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    number INTEGER NOT NULL,
    page_version INTEGER NOT NULL,
    sent INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (round_id, number)
);
CREATE TABLE IF NOT EXISTS receipts (
    receipt_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    token TEXT NOT NULL,
    round_id TEXT,
    number INTEGER,
    kind TEXT NOT NULL,
    page_version INTEGER,
    pages_json TEXT,
    regulator_signature TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gateway_acks (
    packet_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    token TEXT NOT NULL,
    number INTEGER,
    acknowledged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS announcements (
    announcement_id TEXT PRIMARY KEY,
    manifest_id TEXT NOT NULL,
    content_root TEXT NOT NULL,
    state TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    supersedes TEXT
);
CREATE TABLE IF NOT EXISTS capture_events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    manifest_id TEXT NOT NULL,
    content_root TEXT,
    kind TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA)
    return db


def _sign_receipt(material, secret=DEFAULT_REGULATOR_SECRET):
    return hmac.new(secret.encode('utf-8'), canonical(material), hashlib.sha256).hexdigest()


def receipt_material(entry):
    """Canonical material covered by a regulator receipt signature."""
    if entry.get('kind') in ('summary', 'partial'):
        return {
            'kind': entry['kind'],
            'manifest_id': entry['manifest_id'],
            'content_root': entry['content_root'],
            'token': entry['token'],
            'pages': sorted(entry['pages'], key=lambda p: p['number']),
        }
    return {
        'kind': 'page',
        'manifest_id': entry['manifest_id'],
        'content_root': entry['content_root'],
        'token': entry['token'],
        'number': entry['number'],
        'page_version': entry['page_version'],
        'digest': entry['digest'],
    }


def sign_receipt(entry, secret=DEFAULT_REGULATOR_SECRET):
    """Attach a regulator HMAC signature to a page or summary receipt."""
    out = dict(entry)
    out['regulator_signature'] = _sign_receipt(receipt_material(entry), secret)
    return out


def verify_receipt_signature(entry, secret=DEFAULT_REGULATOR_SECRET):
    signature = entry.get('regulator_signature')
    if not signature:
        raise ValueError('监管回执缺少签名')
    if not hmac.compare_digest(signature, _sign_receipt(receipt_material(entry), secret)):
        raise ValueError('监管回执验签失败：回执内容或来源不可信')
    return True


def _event(db, manifest_id, root, kind, detail):
    db.execute(
        'INSERT INTO capture_events(manifest_id,content_root,kind,detail_json) VALUES (?,?,?,?)',
        (manifest_id, root, kind, json.dumps(detail, ensure_ascii=False, sort_keys=True)),
    )


def _latest_announcement(db, manifest_id):
    return db.execute(
        'SELECT * FROM announcements WHERE manifest_id=? ORDER BY rowid DESC LIMIT 1',
        (manifest_id,),
    ).fetchone()


def _publish_announcement(db, manifest_id, root, state, reason, supersedes=None):
    ann_id = 'ANN-' + uuid.uuid4().hex
    db.execute(
        'INSERT INTO announcements VALUES (?,?,?,?,?,?,?)',
        (ann_id, manifest_id, root, state, reason, _now(), supersedes),
    )
    return ann_id


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _covered_numbers(db, manifest_id, root):
    """Distinct page numbers with a valid receipt for one content root."""
    return {
        row[0]
        for row in db.execute(
            "SELECT DISTINCT number FROM receipts "
            "WHERE manifest_id=? AND content_root=? AND kind='page' AND number IS NOT NULL",
            (manifest_id, root),
        )
    } | {
        number
        for row in db.execute(
            "SELECT pages_json FROM receipts "
            "WHERE manifest_id=? AND content_root=? AND kind='summary'",
            (manifest_id, root),
        )
        for number in [p['number'] for p in json.loads(row[0])]
    }


def _expected_numbers(db, manifest_id, root):
    return [
        row[0]
        for row in db.execute(
            'SELECT number FROM captured_pages WHERE manifest_id=? AND content_root=? ORDER BY number',
            (manifest_id, root),
        )
    ]


def capture_original(db, document, secret=None):
    """Capture a signed multi-page original of one content root.

    幂等：同一内容根重复采录不重复记账；出现更新的根时，同 manifest 的旧
    公告仅被标记 superseded（仍可追溯），并发布新根的 pending 公告。
    """
    if secret is None:
        from .manifest import DEFAULT_DOCUMENT_SECRET
        secret = DEFAULT_DOCUMENT_SECRET
    root = verify_document(document, secret)
    manifest_id = document['manifest_id']
    with db:
        existing = db.execute(
            'SELECT 1 FROM captured_roots WHERE manifest_id=? AND content_root=?',
            (manifest_id, root),
        ).fetchone()
        if existing:
            _event(db, manifest_id, root, 'capture_idempotent', {'content_root': root})
            return {'manifest_id': manifest_id, 'content_root': root, 'inserted': False}
        versions = sorted({p['version'] for p in document['pages']})
        db.execute(
            'INSERT INTO captured_roots VALUES (?,?,?,?,?)',
            (manifest_id, root, max(versions), _now(),
             json.dumps(document['pages'], ensure_ascii=False, sort_keys=True)),
        )
        for page in sorted(document['pages'], key=lambda p: p['number']):
            db.execute(
                'INSERT INTO captured_pages VALUES (?,?,?,?,?,?,?)',
                (manifest_id, root, page['number'], page['kind'], page['version'],
                 page_digest(page), page['signature']),
            )
        previous = _latest_announcement(db, manifest_id)
        if previous is None:
            # 该 manifest 的首个内容根：发布 pending 公告，等待同版三页受理
            ann_id = _publish_announcement(
                db, manifest_id, root, 'pending', '运单原件已离线采录，等待同版三页受理', None)
        elif previous['content_root'] != root:
            # 新版本换根：旧公告留痕并标记 superseded，仅修订本 manifest 的公告
            ann_id = _publish_announcement(
                db, manifest_id, root, 'pending',
                '新版运单原件已离线采录，等待同版三页受理', previous['announcement_id'])
            if previous['state'] == 'accepted':
                db.execute(
                    "UPDATE announcements SET state='superseded' WHERE announcement_id=?",
                    (previous['announcement_id'],),
                )
        else:
            ann_id = previous['announcement_id']
        _event(db, manifest_id, root, 'capture', {'content_root': root, 'announcement_id': ann_id})
    return {'manifest_id': manifest_id, 'content_root': root, 'inserted': True}


def _next_round_no(db, manifest_id):
    row = db.execute(
        'SELECT COALESCE(MAX(round_no),0)+1 FROM send_rounds WHERE manifest_id=?',
        (manifest_id,),
    ).fetchone()
    return row[0]


def open_send_round(db, manifest_id, content_root, page_numbers=None, token=None, round_id=None):
    """Open a regulator submission round bound to one token and one content root."""
    with db:
        root_row = db.execute(
            'SELECT 1 FROM captured_roots WHERE manifest_id=? AND content_root=?',
            (manifest_id, content_root),
        ).fetchone()
        if not root_row:
            raise ValueError('发送轮次必须基于已采录的原件')
        if page_numbers is None:
            page_numbers = _expected_numbers(db, manifest_id, content_root)
        page_numbers = sorted(page_numbers)
        if not page_numbers:
            raise ValueError('发送轮次至少包含一页')
        placeholders = ','.join('?' for _ in page_numbers)
        pages = list(db.execute(
            f'SELECT number,version FROM captured_pages '
            f'WHERE manifest_id=? AND content_root=? AND number IN ({placeholders}) ORDER BY number',
            (manifest_id, content_root, *page_numbers),
        ))
        if [r['number'] for r in pages] != page_numbers:
            raise ValueError('轮次包含未采录的页面')
        token = token or ('TOK-' + uuid.uuid4().hex)
        if db.execute('SELECT 1 FROM send_rounds WHERE token=?', (token,)).fetchone():
            raise ValueError('提交令牌必须唯一，禁止跨轮次复用令牌')
        round_id = round_id or ('RND-' + uuid.uuid4().hex)
        round_no = _next_round_no(db, manifest_id)
        db.execute(
            'INSERT INTO send_rounds(round_id,manifest_id,content_root,token,round_no,'
            'opened_at,status,watermark,page_numbers_json) VALUES (?,?,?,?,?,?,?,?,?)',
            (round_id, manifest_id, content_root, token, round_no,
             _now(), 'open', 0, json.dumps(page_numbers)),
        )
        for row in pages:
            db.execute(
                'INSERT INTO send_items(round_id,manifest_id,content_root,number,page_version) '
                'VALUES (?,?,?,?,?)',
                (round_id, manifest_id, content_root, row['number'], row['version']),
            )
        _event(db, manifest_id, content_root, 'round_open',
               {'round_id': round_id, 'token': token, 'pages': page_numbers})
    return {'round_id': round_id, 'token': token, 'round_no': round_no,
            'manifest_id': manifest_id, 'content_root': content_root, 'pages': page_numbers}


def mark_sent(db, round_id, number, error=None):
    """Advance the local job watermark after a page upload attempt."""
    with db:
        item = db.execute(
            'SELECT * FROM send_items WHERE round_id=? AND number=?',
            (round_id, number),
        ).fetchone()
        if not item:
            raise ValueError('轮次中不存在该页')
        db.execute(
            'UPDATE send_items SET sent=?,attempt=attempt+1,last_error=? WHERE round_id=? AND number=?',
            (0 if error else 1, error, round_id, number),
        )
        round_row = db.execute('SELECT * FROM send_rounds WHERE round_id=?', (round_id,)).fetchone()
        if not error:
            watermark = max(round_row['watermark'], number)
            done = db.execute(
                'SELECT COUNT(*) FROM send_items WHERE round_id=? AND sent=1',
                (round_id,),
            ).fetchone()[0]
            total = db.execute(
                'SELECT COUNT(*) FROM send_items WHERE round_id=?', (round_id,)
            ).fetchone()[0]
            status = 'sent' if done == total else 'sending'
            db.execute(
                'UPDATE send_rounds SET watermark=?,status=? WHERE round_id=?',
                (watermark, status, round_id),
            )
        _event(db, round_row['manifest_id'], round_row['content_root'], 'page_sent',
               {'round_id': round_id, 'number': number, 'error': error})
    return {'round_id': round_id, 'number': number, 'ok': error is None}


def record_gateway_ack(db, packet):
    """Record a gateway packet-header acknowledgement (never counts as acceptance)."""
    packet_id = packet['packet_id']
    with db:
        if db.execute('SELECT 1 FROM gateway_acks WHERE packet_id=?', (packet_id,)).fetchone():
            return {'packet_id': packet_id, 'inserted': False}
        db.execute(
            'INSERT INTO gateway_acks VALUES (?,?,?,?,?,?)',
            (packet_id, packet['manifest_id'], packet['content_root'], packet['token'],
             packet.get('number'), _now()),
        )
        _event(db, packet['manifest_id'], packet['content_root'], 'gateway_ack',
               {'packet_id': packet_id, 'number': packet.get('number')})
    return {'packet_id': packet_id, 'inserted': True}


def _validate_round_token(db, entry):
    # 令牌必须在本地轮次中登记；随后由内容根/页摘要与采录原件的比对
    # 拦截混版。旧根轮次下迟到的合法旧版回执仍然入账并可追溯，但
    # 永远不会满足新根的同版闭合。
    row = db.execute(
        'SELECT round_id,content_root FROM send_rounds '
        'WHERE token=? AND manifest_id=?',
        (entry['token'], entry['manifest_id']),
    ).fetchone()
    if not row:
        raise ValueError('回执令牌与本地发送轮次不匹配，拒绝未知回执')
    return row['round_id']


def _accept_if_complete(db, manifest_id, root):
    expected = _expected_numbers(db, manifest_id, root)
    covered = _covered_numbers(db, manifest_id, root)
    if expected and all(number in covered for number in expected):
        latest = _latest_announcement(db, manifest_id)
        if latest and latest['state'] == 'accepted' and latest['content_root'] == root:
            return latest['announcement_id']
        # 旧版根在换版后才凑齐回执，只能补记旧版受理留痕，不得覆盖当前
        # 新版根的 pending 公告——当前公告始终属于最新采录的内容根。
        if latest and latest['content_root'] != root:
            ann_id = _publish_announcement(
                db, manifest_id, root, 'accepted',
                '旧版内容根回执事后凑齐，补记受理；当前新版运单仍待同版闭合', None)
            _event(db, manifest_id, root, 'accepted_historical',
                   {'announcement_id': ann_id, 'current_root': latest['content_root']})
            return ann_id
        supersedes = latest['announcement_id'] if latest else None
        ann_id = _publish_announcement(
            db, manifest_id, root, 'accepted',
            '同内容根三页监管回执齐全，报送闭合', supersedes
        )
        if latest:
            db.execute(
                "UPDATE announcements SET state='superseded' WHERE announcement_id=?",
                (latest['announcement_id'],),
            )
        db.execute(
            "UPDATE send_rounds SET status='accepted' "
            "WHERE manifest_id=? AND content_root=? AND status!='accepted'",
            (manifest_id, root),
        )
        _event(db, manifest_id, root, 'accepted', {'announcement_id': ann_id})
        return ann_id
    return None


def apply_receipt(db, entry, secret=DEFAULT_REGULATOR_SECRET):
    """Merge one signed regulator page receipt; idempotent by receipt_id.

    验签、ID 去重、令牌-轮次匹配、内容根与页版本一致性全部通过后才记账，
    每页回执独立计数；同根页齐全时才发布 accepted 公告。
    """
    verify_receipt_signature(entry, secret)
    if entry.get('kind') not in (None, 'page'):
        raise ValueError('单页回执请使用 kind=page；汇总/部分证明有专门的恢复入口')
    manifest_id = entry['manifest_id']
    root = entry['content_root']
    with db:
        if db.execute('SELECT 1 FROM receipts WHERE receipt_id=?', (entry['receipt_id'],)).fetchone():
            _event(db, manifest_id, root, 'receipt_duplicate', {'receipt_id': entry['receipt_id']})
            return {'receipt_id': entry['receipt_id'], 'inserted': False}
        round_id = _validate_round_token(db, entry)
        page = db.execute(
            'SELECT * FROM captured_pages WHERE manifest_id=? AND content_root=? AND number=?',
            (manifest_id, root, entry['number']),
        ).fetchone()
        if not page:
            raise ValueError('回执对应页面不在该内容根原件中，疑似混版')
        if page['version'] != entry['page_version'] or page['digest'] != entry['digest']:
            raise ValueError('回执页版本或摘要与采录原件不一致')
        db.execute(
            'INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (entry['receipt_id'], manifest_id, root, entry['token'], round_id,
             entry['number'], 'page', entry['page_version'], None,
             entry['regulator_signature'], _now()),
        )
        _event(db, manifest_id, root, 'receipt_page',
               {'receipt_id': entry['receipt_id'], 'number': entry['number']})
        ann_id = _accept_if_complete(db, manifest_id, root)
    return {'receipt_id': entry['receipt_id'], 'inserted': True, 'announcement_id': ann_id}


def record_summary_receipt(db, entry, secret=DEFAULT_REGULATOR_SECRET):
    """Approved recovery path A: regulator proves a full same-root set was accepted."""
    verify_receipt_signature(entry, secret)
    if entry.get('kind') != 'summary':
        raise ValueError('汇总回执 kind 必须为 summary')
    manifest_id = entry['manifest_id']
    root = entry['content_root']
    pages = sorted(entry['pages'], key=lambda p: p['number'])
    with db:
        if db.execute('SELECT 1 FROM receipts WHERE receipt_id=?', (entry['receipt_id'],)).fetchone():
            return {'receipt_id': entry['receipt_id'], 'inserted': False}
        round_id = _validate_round_token(db, entry)
        expected = _expected_numbers(db, manifest_id, root)
        if [p['number'] for p in pages] != expected:
            raise ValueError('汇总回执覆盖页与采录原件不一致')
        for covered in pages:
            page = db.execute(
                'SELECT * FROM captured_pages WHERE manifest_id=? AND content_root=? AND number=?',
                (manifest_id, root, covered['number']),
            ).fetchone()
            if not page or page['version'] != covered['page_version'] or page['digest'] != covered['digest']:
                raise ValueError('汇总回执中的页版本/摘要与原件不一致')
        db.execute(
            'INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (entry['receipt_id'], manifest_id, root, entry['token'], round_id,
             None, 'summary', None,
             json.dumps(pages, ensure_ascii=False, sort_keys=True),
             entry['regulator_signature'], _now()),
        )
        _event(db, manifest_id, root, 'receipt_summary', {'receipt_id': entry['receipt_id']})
        ann_id = _accept_if_complete(db, manifest_id, root)
    return {'receipt_id': entry['receipt_id'], 'inserted': True, 'announcement_id': ann_id}


def retry_round(db, manifest_id, content_root, token=None, round_id=None):
    """Open a new round that re-sends only pages still missing receipts (same root)."""
    with db:
        if not db.execute(
            'SELECT 1 FROM captured_roots WHERE manifest_id=? AND content_root=?',
            (manifest_id, content_root),
        ).fetchone():
            raise ValueError('重试必须基于已采录的原件')
        expected = _expected_numbers(db, manifest_id, content_root)
        covered = _covered_numbers(db, manifest_id, content_root)
        missing = [number for number in expected if number not in covered]
        if not missing:
            raise ValueError('该内容根页面已全部受理，无需重试')
        result = open_send_round(db, manifest_id, content_root, missing, token=token, round_id=round_id)
        _event(db, manifest_id, content_root, 'retry_round',
               {'round_id': result['round_id'], 'missing': missing})
    return result


def reopen_with_partial_proof(db, proof, secret=DEFAULT_REGULATOR_SECRET):
    """Approved recovery path B: regulator proves only some pages of a root arrived.

    撤销该 manifest 旧的 accepted 公告，保持 pending，并以新令牌开启只补
    未到达页的新发送轮次；旧轮次与旧版已送出页全部保留可追溯。
    """
    verify_receipt_signature(proof, secret)
    if proof.get('kind') != 'partial':
        raise ValueError('部分到达证明 kind 必须为 partial')
    manifest_id = proof['manifest_id']
    root = proof['content_root']
    arrived = sorted(proof['pages'], key=lambda p: p['number'])
    with db:
        if not db.execute(
            'SELECT 1 FROM captured_roots WHERE manifest_id=? AND content_root=?',
            (manifest_id, root),
        ).fetchone():
            raise ValueError('部分到达证明对应的原件未采录')
        expected = _expected_numbers(db, manifest_id, root)
        for item in arrived:
            page = db.execute(
                'SELECT * FROM captured_pages WHERE manifest_id=? AND content_root=? AND number=?',
                (manifest_id, root, item['number']),
            ).fetchone()
            if not page or page['version'] != item['page_version'] or page['digest'] != item['digest']:
                raise ValueError('部分到达证明的页版本/摘要与原件不一致')
        previous = _latest_announcement(db, manifest_id)
        supersedes = previous['announcement_id'] if previous else None
        ann_id = _publish_announcement(
            db, manifest_id, root, 'pending',
            f"监管端证明仅到达页 {','.join(str(p['number']) for p in arrived)}，撤销受理并补齐缺页",
            supersedes,
        )
        if previous and previous['state'] == 'accepted':
            db.execute(
                "UPDATE announcements SET state='revoked' WHERE announcement_id=?",
                (previous['announcement_id'],),
            )
        _event(db, manifest_id, root, 'reopen_partial',
               {'proof_id': proof['receipt_id'], 'arrived': [p['number'] for p in arrived],
                'announcement_id': ann_id})
        missing = [number for number in expected if number not in {p['number'] for p in arrived}]
        result = open_send_round(db, manifest_id, root, missing)
    return {'announcement_id': ann_id, 'new_round': result,
            'arrived': [p['number'] for p in arrived], 'missing': missing}


def inspect(db, manifest_id):
    """Full state view: received content root, per-page versions and receipt counts."""
    roots = [
        dict(r) for r in db.execute(
            'SELECT manifest_id,content_root,version,captured_at FROM captured_roots '
            'WHERE manifest_id=? ORDER BY rowid',
            (manifest_id,),
        )
    ]
    for root_row in roots:
        root = root_row['content_root']
        root_row['pages'] = [
            dict(r) for r in db.execute(
                'SELECT number,kind,version,digest FROM captured_pages '
                'WHERE manifest_id=? AND content_root=? ORDER BY number',
                (manifest_id, root),
            )
        ]
        root_row['receipts'] = [
            dict(r) for r in db.execute(
                'SELECT receipt_id,token,round_id,number,kind,page_version,applied_at '
                'FROM receipts WHERE manifest_id=? AND content_root=? ORDER BY rowid',
                (manifest_id, root),
            )
        ]
        counts = dict(db.execute(
            "SELECT number,COUNT(*) FROM receipts "
            "WHERE manifest_id=? AND content_root=? AND kind='page' GROUP BY number",
            (manifest_id, root),
        ).fetchall())
        for page in root_row['pages']:
            page['receipt_count'] = counts.get(page['number'], 0)
        covered = _covered_numbers(db, manifest_id, root)
        expected = [p['number'] for p in root_row['pages']]
        root_row['received_numbers'] = sorted(covered)
        root_row['missing_numbers'] = [n for n in expected if n not in covered]
        root_row['complete'] = bool(expected) and all(n in covered for n in expected)
    rounds = [
        dict(r) for r in db.execute(
            'SELECT round_id,content_root,token,round_no,status,watermark,page_numbers_json '
            'FROM send_rounds WHERE manifest_id=? ORDER BY round_no',
            (manifest_id,),
        )
    ]
    for round_row in rounds:
        round_row['page_numbers'] = json.loads(round_row.pop('page_numbers_json'))
        round_row['items'] = [
            dict(r) for r in db.execute(
                'SELECT number,page_version,sent,attempt,last_error FROM send_items '
                'WHERE round_id=? ORDER BY number',
                (round_row['round_id'],),
            )
        ]
    gateway = [
        dict(r) for r in db.execute(
            'SELECT packet_id,content_root,token,number,acknowledged_at FROM gateway_acks '
            'WHERE manifest_id=? ORDER BY rowid',
            (manifest_id,),
        )
    ]
    announcements = [
        dict(r) for r in db.execute(
            'SELECT announcement_id,manifest_id,content_root,state,reason,supersedes,created_at '
            'FROM announcements WHERE manifest_id=? ORDER BY rowid',
            (manifest_id,),
        )
    ]
    current_root = roots[-1]['content_root'] if roots else None
    # 当前状态只看属于最新采录内容根的最新公告；旧根事后补记的受理
    # 公告留痕但不改变当前报送状态。
    current_announcements = [a for a in announcements if a['content_root'] == current_root]
    latest = current_announcements[-1] if current_announcements else None
    return {
        'manifest_id': manifest_id,
        'status': latest['state'] if latest else 'unknown',
        'current_content_root': current_root,
        'roots': roots,
        'rounds': rounds,
        'gateway_acks': gateway,
        'announcements': announcements,
    }
