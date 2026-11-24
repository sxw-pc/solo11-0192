"""Collect customer credit-limit records and deduct live occupations."""

#: 仍会占用同一不可循环授信额度的预约状态：柜台已承诺待回执、已生效、银行已占用。
ACTIVE_OCCUPATION_STATES = ('promised', 'effective', 'occupied')


def limits(events, occupations=()):
    """以 customer_id 最后一条限额记录为准，并扣除所有仍有效的预约占用。

    occupations 来自持久化的承诺意图或银行占用证明（见 credit_capture），
    逐笔携带 customer_id、amount 与 state；已撤销或已释放的占用不再扣减。
    """
    caps = {}
    for event in events:
        caps[event['customer_id']] = event['limit']
    used = {}
    for occupation in occupations:
        if occupation.get('state', 'occupied') in ACTIVE_OCCUPATION_STATES:
            cid = occupation['customer_id']
            used[cid] = used.get(cid, 0) + occupation['amount']
    return {cid: cap - used.get(cid, 0) for cid, cap in caps.items()}
