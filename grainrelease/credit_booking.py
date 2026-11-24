"""Provisional credit booking decision."""
from .customer_credit import limits


def accepted(credit_events, bookings, occupations=()):
    """逐笔试算：可用额（限额扣除仍有效占用后）足够则接受，否则列入待核。

    仅在本批内比较总额（如 80+50）无法在银行占用成功而本地提交前断电时
    提供恢复依据，因此仍有效占用必须来自持久化的承诺意图或银行占用证明，
    由 occupations 传入（见 credit_capture.CreditCapture.occupations）。
    """
    caps = limits(credit_events, occupations)
    out = {}
    for row in bookings:
        cap = caps.get(row['customer_id'])
        out[row['booking_id']] = True if cap is not None and row['amount'] <= cap else 'pending'
    return out
