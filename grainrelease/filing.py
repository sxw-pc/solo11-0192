"""Read regulator acknowledgements for a shipment filing.

闭合不再以“见到同 manifest_id 的回执”为准，而是同时要求：

* 回执通过监管端 HMAC 验签（网关包头确认没有监管签名，不计入）；
* 回执的内容根与当前运单内容根一致，且页版本、页摘要与当前原件逐页相符；
* 当前原件的每一页（1/2/3）都至少有一张满足上述条件的回执，或存在一张
  覆盖全页的同根汇总回执。

因此 v1 三页齐全而当前原件已出 v2 时，状态必须回到 pending——编号凑齐
但根不同的回执不能签成同版。
"""
from .filing_capture import verify_receipt_signature
from .manifest import page_digest, prepare


def _page_receipt_matches(page, ack):
    return (
        ack.get('content_root') == page.get('content_root')
        and ack.get('number') == page['number']
        and ack.get('page_version') == page['version']
        and ack.get('digest') == page_digest(page)
    )


def inspect_status(document, acknowledgements, regulator_secret=None):
    """Return detailed closure state including received root and per-page versions."""
    from .filing_capture import DEFAULT_REGULATOR_SECRET
    if regulator_secret is None:
        regulator_secret = DEFAULT_REGULATOR_SECRET
    prepared = prepare(document)
    root = prepared['content_root']
    pages = sorted(prepared['pages'], key=lambda p: p['number'])

    seen_receipt_ids = set()
    valid_page_acks = []
    summary_acks = []
    rejected = []
    for ack in acknowledgements:
        receipt_id = ack.get('receipt_id')
        if not receipt_id:
            rejected.append({'ack': ack, 'reason': '缺少回执编号'})
            continue
        if receipt_id in seen_receipt_ids:
            # ID 去重：同一回执重复到达只计一次
            continue
        try:
            verify_receipt_signature(ack, regulator_secret)
        except ValueError as exc:
            rejected.append({'receipt_id': receipt_id, 'reason': str(exc)})
            continue
        seen_receipt_ids.add(receipt_id)
        if ack.get('manifest_id') != prepared['manifest_id']:
            rejected.append({'receipt_id': receipt_id, 'reason': 'manifest_id 不匹配'})
            continue
        if ack.get('kind') == 'summary':
            if ack.get('content_root') != root:
                rejected.append({'receipt_id': receipt_id, 'reason': '汇总回执内容根与当前运单不同版'})
                continue
            summary_acks.append(ack)
            continue
        page = next((p for p in pages if p['number'] == ack.get('number')), None)
        if page is None or not _page_receipt_matches(page, ack):
            rejected.append({'receipt_id': receipt_id, 'reason': '回执页不属于当前内容根或版本不符（混版）'})
            continue
        valid_page_acks.append(ack)

    counts = {page['number']: 0 for page in pages}
    received_versions = {}
    for ack in valid_page_acks:
        counts[ack['number']] += 1
        received_versions[ack['number']] = ack['page_version']

    page_results = []
    for page in pages:
        page_results.append({
            'number': page['number'],
            'kind': page['kind'],
            'required_version': page['version'],
            'received_version': received_versions.get(page['number']),
            'receipt_count': counts[page['number']],
            'received': counts[page['number']] > 0,
        })

    expected_numbers = [page['number'] for page in pages]
    summary_covers = any(
        sorted(p['number'] for p in ack['pages']) == expected_numbers
        and all(
            p.get('page_version') == next(q['version'] for q in pages if q['number'] == p['number'])
            and p.get('digest') == page_digest(next(q for q in pages if q['number'] == p['number']))
            for p in ack['pages']
        )
        for ack in summary_acks
    )
    missing = [page['number'] for page in page_results if not page['received']]
    accepted = summary_covers or not missing
    return {
        'manifest_id': prepared['manifest_id'],
        'status': 'accepted' if accepted else 'pending',
        'content_root': root,
        'pages': page_results,
        'received_numbers': sorted(n for n in expected_numbers if counts[n] > 0),
        'missing_numbers': missing,
        'summary_receipt_ids': [ack['receipt_id'] for ack in summary_acks],
        'rejected': rejected,
    }


def status(document, acknowledgements, regulator_secret=None):
    """Backward-compatible closure verdict for the prepared→status workflow."""
    return inspect_status(document, acknowledgements, regulator_secret)['status']
