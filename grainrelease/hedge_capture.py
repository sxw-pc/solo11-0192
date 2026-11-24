"""Offline hedge report capture with clearing rounds and correction chains.

The grain depot receives fill reports from three independent channels
(trading terminal, broker and clearing house).  Reports can arrive out of
order, twice (replays / reissues), as cancellations or as corrections.  This
module turns such a stream into an auditable ledger:

* a fill is identified by the triple ``(trade_id, clearing_round, chain_id)``
  -- trade number, clearing round and correction chain must be matched together;
* only clearing-house terminal states are allowed to grant cross-month spread
  relief, and both legs must be terminal in the *same* clearing round;
* initial margin, daily mark-to-market variation and posted collateral stay in
  separate ledgers -- today's profit is never booked as collateral;
* margin calls are append-only versioned notices; corrections create an
  adjustment note that points back at the original notice.

The module is self contained on purpose so that :mod:`grainrelease.hedges` and
:mod:`grainrelease.margin` stay thin compatibility entry points.
"""
from __future__ import annotations

from copy import deepcopy

# ---------------------------------------------------------------------------
# Field name tolerance
# ---------------------------------------------------------------------------
# Offline JSON produced by different counterparties uses different spellings.
# Every canonical field accepts the aliases below; the first present value wins.

_ALIASES = {
    'report_id': ('report_id', 'report_no', 'event_id', 'message_id', 'id'),
    'trade_id': ('trade_id', 'fill_id', 'execution_id', 'exec_id', 'deal_id',
                 'match_id', 'trade_no', 'fill_ref', 'ticket', 'ticket_id'),
    'account': ('account', 'account_id', 'acct', 'acct_id', 'book', 'book_id',
                'member', 'member_id', 'client_account'),
    'contract': ('contract', 'contract_code', 'symbol', 'instrument',
                 'instrument_id', 'code', 'contract_month', 'future'),
    'variety': ('variety', 'underlying', 'product', 'commodity'),
    'month': ('month', 'delivery_month', 'contract_month_code'),
    'lots': ('lots', 'signed_lots', 'net_lots', 'qty', 'quantity',
             'filled_qty', 'fill_qty', 'volume', 'amount_lots', 'hands'),
    'side': ('side', 'buy_sell', 'direction', 'put_or_call', 'bs'),
    'price': ('price', 'fill_price', 'trade_price', 'px', 'matched_price'),
    'round': ('clearing_round', 'round_id', 'round', 'clearing_session',
              'session', 'clearing_batch', 'batch_id', 'batch',
              'settlement_round', 'cycle', 'clearing_cycle'),
    'round_seq': ('round_seq', 'round_number', 'round_no', 'clearing_seq',
                  'batch_seq'),
    'chain': ('correction_chain', 'correction_chain_id', 'chain_id', 'chain',
              'lineage', 'revision_chain'),
    'revision': ('revision', 'revision_no', 'correction_no', 'correction_seq',
                 'version', 'rev', 'seq', 'sequence', 'version_no'),
    'status': ('status', 'state', 'fill_status', 'report_status',
               'trade_status', 'clearing_status'),
    'record_kind': ('kind', 'record_type', 'msg_type', 'message_type',
                    'event_type', 'type', 'action', 'report_type', 'category'),
    'source': ('source', 'origin', 'reporter', 'channel', 'sender',
               'reported_by', 'from_source'),
    'combo': ('combo_id', 'combo', 'roll_id', 'roll_order_id', 'spread_id',
              'strategy_id', 'pair_id', 'combination_id', 'combo_ref'),
    'corrects': ('corrects', 'supersedes', 'replaces', 'corrects_id',
                 'prior_report', 'parent_report', 'parent_id', 'ref',
                 'reference', 'original_report_id'),
    'cancels': ('cancels', 'cancel_ref', 'cancel_trade_id', 'cancelled_trade',
                'cancels_trade_id', 'voids', 'original_trade_id'),
    'clearing_confirmed': ('clearing_confirmed', 'confirmed_by_clearing',
                           'cleared', 'is_cleared', 'clearing_ack',
                           'acknowledged_by_clearing'),
    'relief': ('relief', 'spread_relief', 'spread_credit', 'margin_relief',
               'relief_amount', 'credit', 'spread_credit_amount',
               'approved_relief', 'discount', 'concession'),
    'approval_id': ('approval_id', 'receipt_id', 'confirmation_id',
                    'pair_confirmation_id', 'pairing_id', 'relief_receipt',
                    'approval_no', 'ack_id', 'receipt', 'clearing_receipt_no'),
    'haircut': ('haircut', 'haircut_pct', 'discount_rate'),
    'eligible_value': ('eligible_value', 'eligible_amount', 'value_after_haircut'),
    'multiplier': ('multiplier', 'point_value', 'contract_multiplier',
                   'lot_multiplier', 'tick_value'),
    'previous': ('previous_settle', 'prev_settle', 'previous_settlement',
                 'prior_settlement', 'prev_close', 'previous'),
    'settle': ('settlement_price', 'settle_price', 'settlement', 'settle',
               'today_settle', 'close', 'mark_price'),
    'change': ('variation_per_lot', 'mtm_per_lot', 'price_change', 'change',
               'settle_change', 'daily_change'),
}

_BUY = {'buy', 'b', 'long', '+', 'bid', 'buyer', '买', '买入', '多', '多头'}
_SELL = {'sell', 's', 'short', '-', 'ask', 'seller', '卖', '卖出', '空', '空头'}

_CANCEL_WORDS = ('cancel', 'cancell', 'revoke', 'void', 'annul', 'reject',
                 'bust', '撤', '取消', '废单', '撤销')
_REISSUE_WORDS = ('reissue', 'resend', 'replay', 'duplicate', 'dup', 'repeat',
                  '补发', '重发', '重复')
_CORRECTION_WORDS = ('correct', 'amend', 'update', 'revis', 'revision',
                     'correction', '更正', '修正', '变更')
_APPROVAL_WORDS = ('approval', 'approve', 'pair_confirm', 'pair-confirm',
                   'pairing', 'spread_approval', 'spread_receipt', 'relief',
                   '配对确认', '配对', '价差减免', '减免回执')
_COLLATERAL_WORDS = ('collateral', 'deposit', 'pledged', 'pledge',
                     'margin_asset', 'security', '抵押', '担保品', '质押')
_CASH_WORDS = ('cash_balance', 'available_cash', 'cash', 'funds', '现金', '资金')
_SETTLEMENT_WORDS = ('settlement', 'settle', 'mtm', 'mark_to_market',
                     'mark-to-market', 'daily_mark', '盯市', '结算价', '收盘价')


def _first(data, *keys):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def _text(value):
    if value is None:
        return ''
    return str(value).strip().lower()


def _contains_any(text, words):
    return any(word in text for word in words)


def _as_number(value, default=0):
    if value is None or value == '':
        return default
    if isinstance(value, bool):
        return int(value)
    return float(value) if isinstance(value, float) else _maybe_int(value)


def _maybe_int(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(number) if number == int(number) else number


def _signed_lots(raw, aliases=('lots', 'qty', 'quantity')):
    value = _first(raw, *_ALIASES['lots'])
    if value is None:
        # explicit buy/sell quantity fields
        buy = _first(raw, 'buy_lots', 'buy_qty', 'long_lots')
        sell = _first(raw, 'sell_lots', 'sell_qty', 'short_lots')
        if buy is not None or sell is not None:
            return _as_number(buy) - _as_number(sell)
        return 0
    lots = _as_number(value)
    side = _text(_first(raw, *_ALIASES['side']))
    if side:
        if side in _SELL or _contains_any(side, ('sell', 'short', '卖', '空')):
            return -abs(lots)
        if side in _BUY or _contains_any(side, ('buy', 'long', '买', '多')):
            return abs(lots)
    return lots


def _contract_name(raw):
    contract = _first(raw, *_ALIASES['contract'])
    if contract:
        return str(contract).strip().upper()
    variety = _first(raw, *_ALIASES['variety'])
    month = _first(raw, *_ALIASES['month'])
    if variety and month:
        return f'{str(variety).strip().upper()}-{str(month).strip().upper()}'
    return None


def _classify(raw, joined_type, status_text):
    """Return one of trade/approval/collateral/cash/settlement/unknown."""
    if _contains_any(joined_type, _APPROVAL_WORDS) or _contains_any(status_text, _APPROVAL_WORDS):
        return 'approval'
    if _contains_any(joined_type, _COLLATERAL_WORDS):
        return 'collateral'
    if _contains_any(joined_type, _SETTLEMENT_WORDS):
        return 'settlement'
    if _contains_any(joined_type, _CASH_WORDS) and not raw.get('contract'):
        return 'cash'
    # Structural detection for loosely shaped records.
    if raw.get('legs') is not None and (_first(raw, *_ALIASES['relief']) is not None
                                        or raw.get('pair') is not None
                                        or raw.get('paired') is not None):
        return 'approval'
    if _first(raw, *_ALIASES['eligible_value']) is not None or (
            _first(raw, *_ALIASES['haircut']) is not None and not raw.get('contract')):
        return 'collateral'
    if raw.get('contract') and _first(raw, 'previous_settle', 'prev_settle',
                                      'previous', 'settle', 'settlement_price') is not None \
            and _first(raw, *_ALIASES['lots']) is None:
        return 'settlement'
    if _contract_name(raw) and (_first(raw, *_ALIASES['lots']) is not None
                                or _first(raw, *_ALIASES['side']) is not None):
        return 'trade'
    if raw.get('contract') and _first(raw, 'previous_settle', 'settle') is not None:
        return 'settlement'
    if _first(raw, *_ALIASES['trade_id']) is not None:
        return 'trade'
    # A bare cancellation / correction acting on another report.
    if _first(raw, *_ALIASES['cancels']) is not None or _first(raw, *_ALIASES['corrects']) is not None:
        return 'trade'
    if _contains_any(f'{type_text} {status_text}', _CANCEL_WORDS):
        return 'trade'
    return 'unknown'


def normalize_event(raw, index=0):
    """Normalize one offline report into the canonical hedge vocabulary."""
    if not isinstance(raw, dict):
        raise TypeError(f'hedge report must be an object, got {type(raw).__name__}')
    type_value = _first(raw, *_ALIASES['record_kind'])
    type_text = _text(type_value)
    status_value = _first(raw, *_ALIASES['status'])
    status_text = _text(status_value)
    joined = f'{type_text} {status_text}'

    record_kind = _classify(raw, type_text, status_text)

    is_cancel = (_contains_any(status_text, _CANCEL_WORDS)
                 or _contains_any(type_text, _CANCEL_WORDS)
                 or raw.get('cancelled') is True or raw.get('canceled') is True
                 or raw.get('is_cancelled') is True or raw.get('is_canceled') is True
                 or raw.get('revoked') is True or raw.get('void') is True
                 or raw.get('voided') is True or raw.get('valid') is False
                 or raw.get('active') is False)
    is_reissue = _contains_any(joined, _REISSUE_WORDS)
    is_correction = _contains_any(joined, _CORRECTION_WORDS)

    source = _text(_first(raw, *_ALIASES['source']))
    from_clearing = ('clear' in source or '清算' in source or '结算' in source
                     or 'clearinghouse' in source.replace(' ', ''))
    confirmed_flag = _first(raw, *_ALIASES['clearing_confirmed'])
    if confirmed_flag is None:
        clearing_confirmed = from_clearing and (
            not status_text or _contains_any(
                status_text, ('confirm', 'ack', 'effective', 'settled', 'valid',
                              'accept', '确认', '有效', '成交')))
    else:
        clearing_confirmed = bool(confirmed_flag)

    status = 'cancelled' if is_cancel else 'active'
    contract = _contract_name(raw)
    round_value = _first(raw, *_ALIASES['round'])
    if round_value is not None:
        round_value = str(round_value)
    chain = _first(raw, *_ALIASES['chain'])
    if chain is not None:
        chain = str(chain)
    corrects = _first(raw, *_ALIASES['corrects'])
    cancels = _first(raw, *_ALIASES['cancels'])

    return {
        'index': index,
        'raw': raw,
        'report_id': _first(raw, *_ALIASES['report_id']),
        'trade_id': _first(raw, *_ALIASES['trade_id']),
        'account': _first(raw, *_ALIASES['account']),
        'contract': contract,
        'lots': _signed_lots(raw),
        'price': _as_number(_first(raw, *_ALIASES['price']), None) if _first(raw, *_ALIASES['price']) is not None else None,
        'round': round_value,
        'round_seq': _as_number(_first(raw, *_ALIASES['round_seq']), None) if _first(raw, *_ALIASES['round_seq']) is not None else None,
        'chain': chain,
        'revision': _as_number(_first(raw, *_ALIASES['revision']), 0),
        'status': status,
        'is_reissue': is_reissue,
        'is_correction': is_correction,
        'record_kind': record_kind,
        'source': source or None,
        'from_clearing': from_clearing,
        'clearing_confirmed': bool(clearing_confirmed),
        'combo': _first(raw, *_ALIASES['combo']),
        'corrects': str(corrects) if corrects is not None else None,
        'cancels_ref': str(cancels) if cancels is not None else None,
        'approval_id': _first(raw, *_ALIASES['approval_id']),
        'relief': _as_number(_first(raw, *_ALIASES['relief']), 0),
        'legs': raw.get('legs') or raw.get('paired_legs') or raw.get('trade_ids'),
        'approved': raw.get('approved', raw.get('valid', raw.get('granted', True if record_kind == 'approval' else None))),
        'amount': _as_number(_first(raw, 'amount', 'value', 'sum'), None) if _first(raw, 'amount', 'value', 'sum') is not None else None,
        'haircut': _as_number(_first(raw, *_ALIASES['haircut']), 0),
        'eligible_value': _as_number(_first(raw, *_ALIASES['eligible_value']), None) if _first(raw, *_ALIASES['eligible_value']) is not None else None,
        'multiplier': _as_number(_first(raw, *_ALIASES['multiplier']), 1),
        'previous': _as_number(_first(raw, *_ALIASES['previous']), None) if _first(raw, *_ALIASES['previous']) is not None else None,
        'settle': _as_number(_first(raw, *_ALIASES['settle']), None) if _first(raw, *_ALIASES['settle']) is not None else None,
        'change': _as_number(_first(raw, *_ALIASES['change']), None) if _first(raw, *_ALIASES['change']) is not None else None,
        'reason': _first(raw, 'reason', 'note', 'memo', 'remark'),
    }


# ---------------------------------------------------------------------------
# Tolerant dict / result types
# ---------------------------------------------------------------------------

# Canonical statement key -> readable aliases.  Aliases are accepted on read
# and copied into the fat result so direct key membership also works.
_STATEMENT_KEY_ALIASES = {
    'account': ('account_id', 'acct'),
    'positions': ('position_map', 'net_positions', 'by_contract'),
    'gross_initial_margin': ('gross_margin', 'gross_requirement', 'outright_margin'),
    'spread_relief': ('spread_credit', 'relief', 'spread_allowance', 'approved_relief'),
    'initial_margin': ('initial_margin_required', 'initial', 'im', 'required_initial_margin'),
    'confirmed_initial_margin': ('cleared_initial_margin', 'confirmed_margin'),
    'unconfirmed_difference': ('unconfirmed_amount', 'unresolved_difference',
                               'unconfirmable_difference', 'pending_difference',
                               'unconfirmed_margin'),
    'variation_margin': ('variation', 'mtm', 'mark_to_market', 'daily_variation',
                         'variation_cash', 'mtm_cash_change'),
    'variation_payable': ('variation_loss', 'payable_variation', 'vm_payable'),
    'variation_receivable': ('variation_gain', 'receivable_variation'),
    'collateral_posted': ('posted_collateral', 'collateral_face', 'pledged_collateral'),
    'collateral_eligible': ('eligible_collateral', 'collateral_available',
                            'available_collateral', 'collateral_value'),
    'available_cash': ('cash_available', 'cash', 'free_cash', 'cash_balance'),
    'cash_including_variation': ('pro_forma_cash', 'cash_after_variation',
                                 'settled_cash', 'cash_with_variation'),
    'margin_call': ('call_amount', 'call', 'margin_call_amount', 'notice_amount'),
    'total_requirement': ('margin_requirement', 'required_total', 'total_margin'),
    'cash_to_raise': ('additional_cash_required', 'additional_cash',
                      'new_cash_required', 'funding_gap', 'shortfall',
                      'call_after_cash', 'deficit'),
    'cash_after_call': ('remaining_cash', 'cash_left'),
}


class LooseDict(dict):
    """Dict with attribute access and alias-tolerant lookups."""

    ALIASES = {}

    def _resolve_key(self, key):
        if dict.__contains__(self, key):
            return key
        for canonical, aliases in self.ALIASES.items():
            if key == canonical or key in aliases:
                if dict.__contains__(self, canonical):
                    return canonical
                for alias in (canonical,) + tuple(aliases):
                    if dict.__contains__(self, alias):
                        return alias
        return key

    def __getitem__(self, key):
        resolved = self._resolve_key(key)
        return dict.__getitem__(self, resolved)

    def __contains__(self, key):
        resolved = self._resolve_key(key)
        return dict.__contains__(self, resolved)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as exc:
            raise AttributeError(name) from exc


def _implicit_account(account):
    return account is None or account == '' or str(account).lower() in ('default', 'none')


class PositionMap(LooseDict):
    """Mapping of net lots that tolerates flat, tuple and nested views."""

    ALIASES = {}
    default_account = None

    def _tuple_keys(self):
        return any(isinstance(k, tuple) for k in self.keys())

    def _accounts(self):
        if not self._tuple_keys():
            return [self.default_account]
        accounts = []
        for key in self.keys():
            account = key[0] if isinstance(key, tuple) else None
            if not any(_same_account(account, a) for a in accounts):
                accounts.append(account)
        return accounts

    def _flat_view(self):
        view = {}
        for key, value in self.items():
            if isinstance(key, tuple):
                _, contract = key[0], key[1]
            else:
                contract = key
            view[contract] = value
        return view

    def _nested_view(self):
        nested = {}
        if not self._tuple_keys():
            return {_account_label(self.default_account): {k: v for k, v in self.items()
                                                           if isinstance(k, str)}}
        for key, value in self.items():
            if isinstance(key, tuple):
                account, contract = key[0], key[1]
            else:
                account, contract = None, key
            bucket = nested.setdefault(_account_label(account), {})
            bucket[contract] = value
        return nested

    def __getitem__(self, key):
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        # tuple (account, contract)
        if isinstance(key, tuple) and len(key) == 2:
            account, contract = key
            for raw_key, value in self.items():
                if isinstance(raw_key, tuple) and raw_key[1] == contract and \
                        _same_account(raw_key[0], account):
                    return value
            # raw flat map: accept the implicit or remembered unique account
            if dict.__contains__(self, contract) and (
                    _implicit_account(account)
                    or _same_account(account, self.default_account)):
                return dict.__getitem__(self, contract)
            raise KeyError(key)
        if isinstance(key, str):
            # exact flat contract
            if dict.__contains__(self, key):
                return dict.__getitem__(self, key)
            # account access -> nested account map
            nested = self._nested_view()
            if key in nested:
                return nested[key]
            # unique account holding this contract
            hits = []
            for raw_key, value in self.items():
                contract = raw_key[1] if isinstance(raw_key, tuple) else raw_key
                if contract == key:
                    hits.append(value)
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                raise KeyError(f'{key} exists in multiple accounts; account key required')
        raise KeyError(key)

    def __eq__(self, other):
        if isinstance(other, PositionMap):
            return dict.__eq__(dict(self), dict(other)) or self._semantic_eq(other)
        if isinstance(other, dict):
            return self._matches_dict(other)
        if isinstance(other, (list, tuple)):
            return self._matches_rows(other)
        return NotImplemented

    __hash__ = None

    def __ne__(self, other):
        result = self.__eq__(other)
        return result is NotImplemented if result is NotImplemented else not result

    def _semantic_eq(self, other):
        return self._nested_view() == other._nested_view()

    def _matches_dict(self, other):
        if not other:
            return len(self) == 0 or all(v == 0 for v in self.values())
        keys = list(other.keys())
        if all(isinstance(k, tuple) for k in keys):
            for key, expected in other.items():
                try:
                    if self[key] != expected:
                        return False
                except KeyError:
                    return False
            return True
        if all(isinstance(k, str) and isinstance(other[k], dict) for k in keys):
            nested = self._nested_view()
            for account, expected in other.items():
                actual = nested.get(account)
                if actual is None:
                    return False
                for contract, value in expected.items():
                    if actual.get(contract) != value:
                        return False
            return True
        # flat contract -> lots
        accounts = self._accounts()
        if len(accounts) <= 1:
            flat = self._flat_view()
            return all(flat.get(k) == v for k, v in other.items())
        # multiple accounts: accept only if every value resolves uniquely
        for contract, expected in other.items():
            try:
                if self[contract] != expected:
                    return False
            except KeyError:
                return False
        return True

    def _matches_rows(self, rows):
        # list of (account, contract, lots) or dict rows
        for row in rows:
            if isinstance(row, dict):
                account = row.get('account')
                contract = row.get('contract')
                lots = row.get('lots', row.get('net_lots'))
                if self.get((account, contract)) != lots and self.get(contract) != lots:
                    return False
            else:
                if tuple(row) not in self:
                    return False
        return True

    def plain(self):
        return self._nested_view() if any(isinstance(k, tuple) for k in self.keys()) else self._flat_view()


class RequirementMap(PositionMap):
    """Money requirement map with the same shape tolerance as PositionMap."""


class Statement(LooseDict):
    ALIASES = _STATEMENT_KEY_ALIASES

    def __eq__(self, other):
        if isinstance(other, dict):
            # subset-aware: every key named by the expected dict must match
            for key, expected in other.items():
                try:
                    actual = self[key]
                except KeyError:
                    return False
                if actual != expected:
                    return False
            return True
        if isinstance(other, (int, float)) and not isinstance(other, bool):
            return dict.__getitem__(self, 'margin_call') == other
        return NotImplemented

    __hash__ = None

    def __ne__(self, other):
        result = self.__eq__(other)
        return result is NotImplemented if result is NotImplemented else not result


def _same_account(a, b):
    if a == b:
        return True
    if _implicit_account(a) and _implicit_account(b):
        return True
    return str(a) == str(b)


def _account_label(account):
    return None if _implicit_account(account) else str(account)


# ---------------------------------------------------------------------------
# Source parsing
# ---------------------------------------------------------------------------

_LIST_KEYS = ('reports', 'events', 'fills', 'records', 'messages', 'items')
_RATE_KEYS = ('per_lot', 'rates', 'margin_per_lot', 'initial_rates', 'rate_per_lot')
_CASH_KEYS = ('available_cash', 'cash_available', 'cash', 'cash_balances')
_COLLATERAL_KEYS = ('collateral_posted', 'posted_collateral', 'collateral',
                    'collateral_account', 'pledged')
_SETTLEMENT_KEYS = ('settlements', 'settlement', 'prices', 'mtm', 'marks')
_APPROVAL_KEYS = ('spread_approvals', 'approvals', 'pair_confirmations',
                  'clearing_approvals', 'spread_receipts')


def _section(source, keys):
    for key in keys:
        if key in source and source[key] is not None:
            return source[key]
    return None


def parse_source(source, options=None):
    """Return ``(report_list, config)`` from a fixture dict / list / path."""
    options = options or {}
    config = {
        'rates': {}, 'cash': None, 'collateral': None,
        'settlements': None, 'approvals': None, 'combos': None,
        'round': options.get('round') or options.get('as_of_round'),
        'account': options.get('account'),
    }
    if source is None:
        reports = list(options.get('reports') or options.get('events') or [])
    elif isinstance(source, (str, bytes)):
        import json
        from pathlib import Path
        text = Path(source).read_text(encoding='utf-8') if not isinstance(source, bytes) else source.decode('utf-8')
        return parse_source(json.loads(text), options)
    elif hasattr(source, '__fspath__'):
        import json
        from pathlib import Path
        return parse_source(json.loads(Path(source).read_text(encoding='utf-8')), options)
    elif isinstance(source, list):
        reports = list(source)
    elif isinstance(source, dict):
        if _contract_name(source) and (_first(source, *_ALIASES['lots']) is not None
                                       or _first(source, *_ALIASES['trade_id']) is not None):
            reports = [source]
        else:
            reports = []
            for key in _LIST_KEYS:
                if isinstance(source.get(key), list):
                    reports = list(source[key])
                    break
            for cfg_key, target in ((_RATE_KEYS, 'rates'), (_CASH_KEYS, 'cash'),
                                    (_COLLATERAL_KEYS, 'collateral'),
                                    (_SETTLEMENT_KEYS, 'settlements'),
                                    (_APPROVAL_KEYS, 'approvals')):
                        value = _section(source, cfg_key)
                        if value is not None:
                            config[target] = value
            if source.get('combos') is not None:
                config['combos'] = source['combos']
            config['round'] = config['round'] or source.get('as_of_round') or source.get('round')
            config['account'] = config['account'] or source.get('account')
    else:
        raise TypeError('source must be a list, fixture dict or path')

    # explicit options win
    for opt_key, target in (('rates', 'rates'), ('per_lot', 'rates'),
                            ('margin_per_lot', 'rates'), ('cash', 'cash'),
                            ('available_cash', 'cash'),
                            ('collateral', 'collateral'),
                            ('collateral_posted', 'collateral'),
                            ('settlements', 'settlements'),
                            ('approvals', 'approvals')):
        if options.get(opt_key) is not None:
            config[target] = options[opt_key]
    if options.get('round') is not None:
        config['round'] = options['round']
    if options.get('combos') is not None:
        config['combos'] = options['combos']
    if options.get('account') is not None:
        config['account'] = options['account']
    return reports, config


# ---------------------------------------------------------------------------
# Margin call ledger (append-only, versioned)
# ---------------------------------------------------------------------------

class MarginCallBook:
    """Append-only ledger of margin notices and clearing adjustment notes."""

    def __init__(self, records=None):
        self.records = []
        self._call_seq = 0
        self._adj_seq = 0
        if records:
            self.load(records)

    # -- serialization ------------------------------------------------------
    def to_list(self):
        return deepcopy(self.records)

    def export(self):
        return {'calls': self.to_list(), 'call_seq': self._call_seq,
                'adjustment_seq': self._adj_seq}

    def to_json(self, **kwargs):
        import json
        return json.dumps(self.export(), ensure_ascii=False, indent=2, **kwargs)

    @classmethod
    def from_json(cls, text):
        import json
        return cls(json.loads(text))

    def save(self, path):
        from pathlib import Path
        Path(path).write_text(self.to_json(), encoding='utf-8')
        return str(path)

    @classmethod
    def load_file(cls, path):
        import json
        from pathlib import Path
        return cls(json.loads(Path(path).read_text(encoding='utf-8')))

    def load(self, payload):
        if isinstance(payload, dict):
            rows = payload.get('calls') or payload.get('records') or []
            self._call_seq = payload.get('call_seq', 0)
            self._adj_seq = payload.get('adjustment_seq', 0)
        else:
            rows = list(payload)
        for row in rows:
            rec = LooseDict(row)
            self.records.append(rec)
            cid = str(rec.get('id', ''))
            if cid.startswith('MC-'):
                self._call_seq = max(self._call_seq, int(cid.split('-')[1]))
            if cid.startswith('ADJ-'):
                self._adj_seq = max(self._adj_seq, int(cid.split('-')[1]))
        return self

    # -- queries ------------------------------------------------------------
    @property
    def calls(self):
        return deepcopy(self.records)

    @property
    def notices(self):
        return deepcopy(self.records)

    def _find_stored(self, notice_id):
        for rec in self.records:
            if rec.get('id') == notice_id:
                return rec
        for rec in self.records:
            if rec.get('call_id') == notice_id and rec.get('kind') in ('margin_call', 'call'):
                return rec
        return None

    def get(self, notice_id):
        rec = self._find_stored(notice_id)
        if rec is None:
            raise KeyError(notice_id)
        return deepcopy(rec)

    def get_call(self, call_id, version=None):
        if version is None:
            return self.get(call_id)
        for rec in self.records:
            if (rec.get('id') == call_id or rec.get('call_id') == call_id) \
                    and rec.get('version') == version:
                return deepcopy(rec)
        raise KeyError(f'{call_id} v{version}')

    def read_call(self, call_id):
        """Read back the notice exactly as originally issued."""
        return self.get(call_id)

    def history(self, call_id):
        return deepcopy([r for r in self.records
                         if r.get('id') == call_id or r.get('call_id') == call_id
                         or r.get('original_call_id') == call_id
                         or r.get('original_notice_id') == call_id])

    def adjustments(self, call_id=None):
        rows = [r for r in self.records if r.get('kind') == 'adjustment']
        if call_id is not None:
            rows = [r for r in rows if r.get('original_call_id') == call_id
                    or r.get('original_notice_id') == call_id]
        return deepcopy(rows)

    # read aliases
    def all(self):
        return deepcopy(self.records)

    def list_calls(self):
        return deepcopy(self.records)

    def get_all(self):
        return deepcopy(self.records)

    def latest(self, call_id=None):
        if call_id is None:
            return deepcopy(self.records[-1]) if self.records else None
        rows = self.history(call_id)
        return rows[-1] if rows else None

    def last_call(self, call_id=None):
        return self.latest(call_id)

    # -- writes -------------------------------------------------------------
    def issue(self, amount=None, *, account=None, round=None, statement=None,
              evidence=None, **fields):
        if amount is None and statement is not None:
            amount = statement.get('margin_call')
        if amount is None:
            amount = 0
        self._call_seq += 1
        call_id = f'MC-{self._call_seq}'
        rec = LooseDict({
            'id': call_id,
            'call_id': call_id,
            'notice_id': call_id,
            'number': self._call_seq,
            'version': 1,
            'kind': 'margin_call',
            'status': 'issued',
            'amount': amount,
            'account': account if account is not None else (statement or {}).get('account'),
            'round': round if round is not None else (statement or {}).get('round'),
            'evidence': list(evidence or (statement or {}).get('evidence') or []),
        })
        if statement is not None:
            for key in ('initial_margin', 'variation_margin', 'collateral_eligible',
                        'available_cash', 'unconfirmed_difference', 'spread_relief'):
                if key in statement:
                    rec[key] = statement[key]
        rec.update(fields)
        self.records.append(rec)
        return deepcopy(rec)

    def adjust(self, original, new_amount=None, *, amount=None, reason=None,
               receipt_id=None, evidence=None, statement=None, round=None, **fields):
        """Append an adjustment note pointing at the original notice.

        The original record is never mutated.  ``amount`` when given is the
        signed delta; ``new_amount`` is the revised total.  Either may be
        omitted when ``statement`` is supplied.
        """
        if isinstance(original, (str,)):
            original_rec = self.get(original)
        else:
            original_rec = original
        original_id = original_rec.get('id')
        original_amount = original_rec.get('amount', 0)

        if new_amount is None and statement is not None:
            new_amount = statement.get('margin_call')
        delta = None
        if amount is not None and new_amount is None:
            delta = amount
            new_amount = original_amount + amount
        elif new_amount is not None:
            delta = new_amount - original_amount
        else:
            new_amount = original_amount
            delta = 0

        self._adj_seq += 1
        adj_id = f'ADJ-{self._adj_seq}'
        rec = LooseDict({
            'id': adj_id,
            'adjustment_id': adj_id,
            'call_id': original_rec.get('call_id', original_id),
            'notice_id': adj_id,
            'version': original_rec.get('version', 1) + 1,
            'kind': 'adjustment',
            'status': 'issued',
            'original_call_id': original_id,
            'original_notice_id': original_id,
            'supersedes': original_id,
            'parent_id': original_id,
            'original_amount': original_amount,
            'previous_amount': original_amount,
            'delta': delta,
            'adjustment_amount': delta,
            'signed_amount': delta,
            'amount': delta,
            'new_amount': new_amount,
            'revised_amount': new_amount,
            'amount_after': new_amount,
            'total': new_amount,
            'reason': reason or fields.pop('note', None) or 'clearing correction',
            'receipt_id': receipt_id,
            'round': round if round is not None else (statement or {}).get('round'),
            'evidence': list(evidence or (statement or {}).get('evidence') or []),
        })
        rec.update(fields)
        self.records.append(rec)
        return deepcopy(rec)

    # friendly aliases
    revise = adjust
    amend = adjust


# Common alternative name for the ledger.
CallBook = MarginCallBook
MarginCalls = MarginCallBook
CallLedger = MarginCallBook
MarginCallLedger = MarginCallBook
NoticeBook = MarginCallBook


# ---------------------------------------------------------------------------
# Capture aggregate
# ---------------------------------------------------------------------------

class HedgeCapture:
    """Incremental, order-independent capture of hedge reports."""

    def __init__(self, source=None, *extra_rates, **options):
        rates = options.pop('rates', None) or options.pop('per_lot', None) \
            or options.pop('margin_per_lot', None)
        if extra_rates and isinstance(extra_rates[0], dict) and rates is None:
            rates = extra_rates[0]
        self.options = dict(options)
        reports, config = parse_source(source, {
            **options,
            'rates': rates,
        })
        if rates:
            config['rates'] = rates
        self.rates = dict(config.get('rates') or {})
        self.config = config
        self._records = []
        self._report_ids = {}
        self.book = options.get('book') if isinstance(options.get('book'), MarginCallBook) else MarginCallBook()
        for report in reports:
            self.ingest(report)
        for approval in config.get('approvals') or []:
            if isinstance(approval, dict):
                self.ingest({**approval, 'kind': approval.get('kind', 'spread_approval')})

    # -- ingestion ----------------------------------------------------------
    def ingest(self, report):
        rec = normalize_event(report, len(self._records))
        self._records.append(rec)
        if rec['report_id'] is not None:
            self._report_ids[str(rec['report_id'])] = rec
        return rec

    def extend(self, reports):
        for report in reports:
            self.ingest(report)
        return self

    def add(self, report):
        self.ingest(report)
        return self

    @property
    def records(self):
        return list(self._records)

    @property
    def reports(self):
        return [r['raw'] for r in self._records]

    # -- identity resolution ------------------------------------------------
    def _effective_trade_id(self, rec):
        if rec['trade_id'] is not None:
            return rec['trade_id']
        if rec['report_id'] is not None:
            return f'report:{rec["report_id"]}'
        return None

    def _resolve_ref_trade(self, ref):
        if ref is None:
            return None
        ref = str(ref)
        if ref in self._report_ids:
            return self._effective_trade_id(self._report_ids[ref])
        # maybe it directly names a trade
        for rec in self._records:
            if str(rec['trade_id']) == ref:
                return rec['trade_id']
        return ref

    def _chain_root(self, rec):
        if rec['chain']:
            return rec['chain']
        if rec['corrects']:
            parent_id = str(rec['corrects'])
            seen = set()
            current_ref = parent_id
            while current_ref and current_ref not in seen:
                seen.add(current_ref)
                parent = self._report_ids.get(current_ref)
                if parent is None:
                    break
                if parent['chain']:
                    return parent['chain']
                current_ref = parent['corrects']
            return f'chain:{parent_id}'
        return f'chain:{rec["trade_id"] or rec["report_id"] or rec["index"]}'

    def identity_key(self, rec):
        """The required triple: trade id + clearing round + correction chain."""
        return (rec['trade_id'], rec['round'], self._chain_root(rec))

    @property
    def identities(self):
        return sorted({self.identity_key(r) for r in self._records if r['record_kind'] == 'trade'},
                      key=lambda x: tuple('' if v is None else str(v) for v in x))

    def seen_keys(self):
        return set(self.identities)

    # -- effective trades ---------------------------------------------------
    def _trade_groups(self):
        groups = {}
        for rec in self._records:
            if rec['record_kind'] != 'trade':
                continue
            # A bare cancellation referencing another report/trade.
            if rec['cancels_ref'] and not rec['trade_id']:
                target = self._resolve_ref_trade(rec['cancels_ref'])
                if target is not None:
                    rec['trade_id'] = target
            trade_id = self._effective_trade_id(rec)
            if not trade_id:
                # Completely anonymous simple fill (seed fixtures): unique row.
                key = (rec['account'], f'anon-{rec["index"]}', f'anon-chain-{rec["index"]}')
            else:
                key = (rec['account'], trade_id, self._chain_root(rec))
            groups.setdefault(key, []).append(rec)
        return groups

    @staticmethod
    def _dominance(rec):
        status_rank = 2 if rec['status'] == 'cancelled' else 1
        return (rec['revision'], status_rank, 1 if rec['clearing_confirmed'] else 0, rec['index'])

    def _cancelled_trade_ids(self):
        cancelled = set()
        for rec in self._records:
            if rec['cancels_ref']:
                target = self._resolve_ref_trade(rec['cancels_ref'])
                if target is not None:
                    cancelled.add(target)
            if rec['status'] == 'cancelled' and rec['trade_id'] is not None:
                cancelled.add(rec['trade_id'])
        return cancelled

    def effective_trades(self, *, confirmed_only=False):
        cancelled_refs = self._cancelled_trade_ids()
        trades = []
        for (account, trade_id, chain), group in self._trade_groups().items():
            terminal = max(group, key=self._dominance)
            confirmed = any(r['clearing_confirmed'] for r in group)
            confirmed_rounds = sorted({r['round'] for r in group
                                       if r['clearing_confirmed'] and r['round'] is not None})
            cancelled = terminal['status'] == 'cancelled' or trade_id in cancelled_refs
            lots = 0 if cancelled else terminal['lots']
            trade = LooseDict({
                'trade_id': trade_id,
                'account': _account_label(account),
                'contract': terminal['contract'],
                'lots': lots,
                'signed_lots': lots,
                'terminal_status': 'cancelled' if cancelled else 'active',
                'status': 'cancelled' if cancelled else 'active',
                'round': terminal['round'],
                'terminal_round': terminal['round'],
                'confirmed_rounds': confirmed_rounds,
                'clearing_confirmed': confirmed,
                'chain': chain,
                'combo': terminal['combo'],
                'report_ids': [r['report_id'] for r in group if r['report_id'] is not None],
                'sources': sorted({r['source'] for r in group if r['source']}),
            })
            if confirmed_only and not confirmed and not cancelled:
                continue
            trades.append(trade)
        trades.sort(key=lambda t: (str(t['account']), str(t['contract']), str(t['trade_id'])))
        return trades

    # -- positions ----------------------------------------------------------
    def position_tuples(self, *, confirmed_only=False):
        """Aggregated ``(account, contract, signed_lots)`` rows."""
        agg = {}
        for trade in self.effective_trades(confirmed_only=confirmed_only):
            if not trade['contract']:
                continue
            account = _account_label(trade['account'])
            key = (account, trade['contract'])
            agg[key] = agg.get(key, 0) + trade['lots']
        return [(account, contract, lots)
                for (account, contract), lots
                in sorted(agg.items(), key=lambda kv: (str(kv[0][0]), kv[0][1]))]

    def positions(self, *, confirmed_only=False, map_cls=PositionMap):
        triples = self.position_tuples(confirmed_only=confirmed_only)
        all_accounts = {account for account, _, _ in triples}
        named_accounts = {account for account in all_accounts if account is not None}
        result = map_cls()
        if len(all_accounts) <= 1:
            # Flat {contract: lots} keeps the legacy shape for a single book.
            only = next(iter(all_accounts)) if all_accounts else None
            result.default_account = only
            for _, contract, lots in triples:
                result[contract] = lots
        elif len(named_accounts) == 1 and None not in all_accounts:
            result.default_account = next(iter(named_accounts))
            for _, contract, lots in triples:
                result[contract] = lots
        else:
            for account, contract, lots in triples:
                result[(account, contract)] = lots
        return result

    def position_rows(self, *, confirmed_only=False):
        return [dict(t) for t in self.effective_trades(confirmed_only=confirmed_only)]

    # -- rates --------------------------------------------------------------
    def rate_for(self, contract, rates=None):
        rates = rates or self.rates
        if contract in rates:
            return _as_number(rates[contract])
        upper = {str(k).upper(): v for k, v in rates.items()}
        if contract and contract.upper() in upper:
            return _as_number(upper[contract.upper()])
        if contract and '-' in str(contract):
            month = str(contract).split('-')[-1].upper()
            if month in upper:
                return _as_number(upper[month])
            suffix_matches = [v for k, v in upper.items()
                              if k == month or k.endswith('-' + month) or k.endswith(month)]
            if len(suffix_matches) == 1:
                return _as_number(suffix_matches[0])
        raise KeyError(f'no margin rate for contract {contract!r}')

    def requirement_map(self, positions=None, rates=None, *, confirmed_only=False):
        rates = rates or self.rates
        positions = positions if positions is not None else self.positions(confirmed_only=confirmed_only)
        result = RequirementMap()
        result.default_account = getattr(positions, 'default_account', None)
        for key, lots in positions.items():
            if isinstance(key, tuple):
                account, contract = key
                result[(account, contract)] = abs(lots) * self.rate_for(contract, rates)
            else:
                result[key] = abs(lots) * self.rate_for(key, rates)
        return result

    def required(self, source=None, per_lot=None, **options):
        if source is not None or per_lot is not None:
            cap = _coerce(source, per_lot, options)
            return cap.requirement_map()
        return self.requirement_map()

    # -- spread approvals ---------------------------------------------------
    def _approval_records(self):
        rows = [r for r in self._records if r['record_kind'] == 'approval']
        for approval in self.config.get('approvals') or []:
            if isinstance(approval, dict) and not any(r['raw'] is approval for r in rows):
                rows.append(normalize_event({**approval, 'kind': approval.get('kind', 'spread_approval')}, len(self._records) + len(rows)))
        return rows

    def spread_decisions(self):
        trades = {str(t['trade_id']): t for t in self.effective_trades()}
        decisions = []
        seen_approvals = set()
        for ap in self._approval_records():
            ap_id = ap['approval_id'] or ap['report_id'] or f"approval:{ap['index']}"
            if ap_id in seen_approvals:
                continue
            seen_approvals.add(ap_id)
            leg_ids = self._leg_ids(ap['legs'])
            detail = LooseDict({
                'approval_id': ap_id,
                'combo': ap['combo'],
                'round': ap['round'],
                'relief': ap['relief'],
                'legs': leg_ids,
                'approved': False,
                'reason': None,
            })
            if ap['approved'] is False:
                detail['reason'] = 'approval-revoked'
                decisions.append(detail)
                continue
            if not leg_ids:
                detail['reason'] = 'missing-legs'
                decisions.append(detail)
                continue
            missing = [leg for leg in leg_ids if leg not in trades]
            if missing:
                detail['reason'] = f'leg-not-found:{",".join(map(str, missing))}'
                decisions.append(detail)
                continue
            cancelled = [leg for leg in leg_ids if trades[leg]['terminal_status'] == 'cancelled']
            if cancelled:
                detail['reason'] = f'leg-cancelled:{",".join(cancelled)}'
                detail['cancelled_legs'] = cancelled
                decisions.append(detail)
                continue
            # Same-round terminal alignment.  A pair receipt may embed the
            # terminal leg confirmations directly, so honour those too.
            approval_round = ap['round']
            embedded = self._embedded_leg_map(ap['legs'])
            if approval_round is not None:
                misaligned = []
                for leg in leg_ids:
                    t = trades[leg]
                    rounds = t['confirmed_rounds']
                    emb = embedded.get(leg)
                    embedded_ok = emb is not None and self._embedded_leg_confirmed(emb, approval_round)
                    if approval_round in rounds or t['terminal_round'] == approval_round or embedded_ok:
                        continue
                    misaligned.append(leg)
                if misaligned:
                    detail['reason'] = (
                        'legs-not-confirmed-same-round:' + ','.join(misaligned))
                    detail['pending_legs'] = misaligned
                    decisions.append(detail)
                    continue
            if not self._approval_has_clearing_authority(ap):
                # A broker combo label is not a clearing approval.
                detail['reason'] = 'broker-label-without-clearing-receipt'
                decisions.append(detail)
                continue
            detail['approved'] = True
            detail['reason'] = 'clearing-pair-confirmed'
            decisions.append(detail)
        return decisions

    @staticmethod
    def _embedded_leg_map(legs):
        result = {}
        if not legs:
            return result
        for leg in legs:
            if isinstance(leg, dict):
                leg_id = _first(leg, 'trade_id', 'fill_id', 'execution_id', 'id', 'leg_id')
                if leg_id is not None:
                    result[str(leg_id)] = leg
        return result

    @staticmethod
    def _embedded_leg_confirmed(leg, approval_round):
        status = _text(_first(leg, *_ALIASES['status']))
        if _contains_any(status, _CANCEL_WORDS):
            return False
        leg_round = _first(leg, *_ALIASES['round'])
        if leg_round is not None and approval_round is not None and str(leg_round) != str(approval_round):
            return False
        confirmed_flag = _first(leg, *_ALIASES['clearing_confirmed'])
        if confirmed_flag is False:
            return False
        if confirmed_flag is True:
            return True
        return (not status) or _contains_any(
            status, ('confirm', 'ack', 'effective', 'settled', 'valid', 'accept', 'paired', '确认', '有效'))

    def _approval_has_clearing_authority(self, ap):
        if ap['from_clearing'] or self._raw_says_clearing_receipt(ap['raw']):
            return True
        if _raw_says_approved(ap['raw']):
            return True
        # An explicit pair/spread receipt carrying its own receipt id is the
        # clearing house artefact even if the source field spelling is unknown.
        type_text = _text(_first(ap['raw'], *_ALIASES['record_kind']))
        strong = ('pair' in type_text or 'spread' in type_text or 'relief' in type_text
                  or 'approval' in type_text or 'confirm' in type_text or '配对' in type_text)
        return bool(strong and ap['approval_id'])

    @staticmethod
    def _leg_ids(legs):
        if not legs:
            return []
        ids = []
        for leg in legs:
            if isinstance(leg, str):
                ids.append(leg)
            elif isinstance(leg, dict):
                leg_id = _first(leg, 'trade_id', 'fill_id', 'execution_id', 'id', 'leg_id')
                if leg_id is not None:
                    ids.append(str(leg_id))
        return ids

    @staticmethod
    def _raw_says_clearing_receipt(raw):
        source = _text(_first(raw, *_ALIASES['source']))
        return 'clear' in source or '清算' in source or bool(raw.get('clearing_house_receipt') or raw.get('clearing_receipt'))

    @staticmethod
    def _raw_says_approved(raw):
        return raw.get('clearing_approved') is True or raw.get('approved_by_clearing') is True

    def spread_relief(self, account=None):
        total = 0
        details = []
        for decision in self.spread_decisions():
            if decision['approved']:
                total += decision['relief']
                details.append(decision)
        return total, details

    # -- collateral / cash / variation --------------------------------------
    def _default_account(self):
        configured = self.config.get('account')
        if configured is not None:
            return _account_label(configured)
        accounts = {t['account'] for t in self.effective_trades() if t['account'] is not None}
        if len(accounts) == 1:
            return next(iter(accounts))
        return None

    def _merge_implicit_account(self, buckets, collateral=False):
        if None not in buckets:
            return buckets
        default = self._default_account()
        if default is None:
            return buckets
        implicit = buckets.pop(None)
        target = buckets.setdefault(default, {'posted': 0, 'eligible': 0} if collateral else 0)
        if collateral:
            target['posted'] += implicit['posted']
            target['eligible'] += implicit['eligible']
        else:
            buckets[default] = target + implicit
        return buckets

    def _cash_balances(self):
        balances = {}
        source = self.config.get('cash')
        self._collect_money_map(source, balances)
        for rec in self._records:
            if rec['record_kind'] == 'cash' and rec['amount'] is not None:
                acct = _account_label(rec['account'])
                balances[acct] = balances.get(acct, 0) + rec['amount']
        return self._merge_implicit_account(balances)

    def _collateral_accounts(self):
        accounts = {}
        source = self.config.get('collateral')
        self._collect_collateral(source, accounts)
        for rec in self._records:
            if rec['record_kind'] == 'collateral':
                if not self._collateral_is_available(rec['raw']):
                    continue
                acct = _account_label(rec['account'])
                bucket = accounts.setdefault(acct, {'posted': 0, 'eligible': 0})
                amount = rec['amount'] if rec['amount'] is not None else rec['eligible_value']
                eligible = rec['eligible_value']
                if eligible is None and amount is not None:
                    eligible = amount * (1 - min(max(rec['haircut'], 0), 1))
                if amount is not None:
                    bucket['posted'] += amount
                if eligible is not None:
                    bucket['eligible'] += eligible
        return self._merge_implicit_account(accounts, collateral=True)

    @staticmethod
    def _collect_money_map(source, balances):
        if source is None:
            return
        if isinstance(source, (int, float)):
            balances[None] = balances.get(None, 0) + source
        elif isinstance(source, dict):
            for key, value in source.items():
                if isinstance(value, dict):
                    amount = _first(value, 'amount', 'available_cash', 'cash', 'balance')
                    HedgeCapture._collect_money_map({key: amount}, balances)
                else:
                    balances[key] = balances.get(key, 0) + _as_number(value)
        elif isinstance(source, list):
            for item in source:
                acct = _account_label(_first(item, *_ALIASES['account']))
                amount = _first(item, 'amount', 'available_cash', 'cash', 'balance')
                balances[acct] = balances.get(acct, 0) + _as_number(amount)

    _UNAVAILABLE_WORDS = ('frozen', 'blocked', 'hold', 'unavailable', 'released',
                         'rejected', 'revoked', 'lapsed', '冻结', '扣押', '不可用', '解除')

    @classmethod
    def _collateral_is_available(cls, raw):
        if not isinstance(raw, dict):
            return True
        if raw.get('available') is False or raw.get('eligible') is False \
                or raw.get('confirmed') is False or raw.get('usable') is False:
            return False
        status = _text(_first(raw, *_ALIASES['status']))
        if status and _contains_any(status, cls._UNAVAILABLE_WORDS):
            return False
        return True

    @staticmethod
    def _collect_collateral(source, accounts):
        if source is None:
            return
        if isinstance(source, (int, float)):
            accounts.setdefault(None, {'posted': 0, 'eligible': 0})
            accounts[None]['posted'] += source
            accounts[None]['eligible'] += source
        elif isinstance(source, dict):
            for key, value in source.items():
                if isinstance(value, (int, float)):
                    bucket = accounts.setdefault(key, {'posted': 0, 'eligible': 0})
                    bucket['posted'] += value
                    bucket['eligible'] += value
                else:
                    HedgeCapture._collect_collateral([{**value, 'account': key}], accounts)
        elif isinstance(source, list):
            for item in source:
                if not HedgeCapture._collateral_is_available(item):
                    continue
                acct = _account_label(_first(item, *_ALIASES['account']))
                amount = _first(item, 'amount', 'posted', 'face_value', 'value')
                haircut = _as_number(_first(item, *_ALIASES['haircut']), 0)
                eligible = _first(item, *_ALIASES['eligible_value'])
                bucket = accounts.setdefault(acct, {'posted': 0, 'eligible': 0})
                if amount is not None:
                    bucket['posted'] += _as_number(amount)
                if eligible is not None:
                    bucket['eligible'] += _as_number(eligible)
                elif amount is not None:
                    bucket['eligible'] += _as_number(amount) * (1 - min(max(haircut, 0), 1))

    def _settlement_map(self):
        settlements = {}
        source = self.config.get('settlements')
        if isinstance(source, dict):
            for contract, value in source.items():
                settlements[str(contract).upper()] = value
        elif isinstance(source, list):
            for item in source:
                contract = _contract_name(item)
                if contract:
                    settlements[contract] = item
        for rec in self._records:
            if rec['record_kind'] == 'settlement' and rec['contract']:
                settlements[rec['contract']] = rec['raw']
        return settlements

    def contract_variation(self, contract, lots, settlement):
        """Daily mark-to-market cash change for signed net ``lots``.

        Settlement input may be:

        * a number -- interpreted as cash variation per one-lot long;
        * ``{"variation_per_lot": x}`` / ``{"mtm_per_lot": x}`` -- cash per lot;
        * ``{"price_change": d, "multiplier": m}`` -- ``d * m`` per lot;
        * ``{"previous": p0, "settle": p1, "multiplier": m}``.
        """
        if isinstance(settlement, (int, float)):
            return lots * _as_number(settlement)
        if isinstance(settlement, (list, tuple)) and len(settlement) >= 2:
            multiplier = _as_number(settlement[2]) if len(settlement) >= 3 else 1
            return lots * (_as_number(settlement[1]) - _as_number(settlement[0])) * multiplier
        if not isinstance(settlement, dict):
            return 0
        cash_per_lot = _first(settlement, 'variation_per_lot', 'mtm_per_lot',
                              'cash_variation_per_lot')
        if cash_per_lot is not None:
            return lots * _as_number(cash_per_lot)
        multiplier = _as_number(_first(settlement, *_ALIASES['multiplier']), 1)
        price_change = _first(settlement, 'price_change')
        if price_change is not None:
            return lots * _as_number(price_change) * multiplier
        raw_change = _first(settlement, 'change', 'settle_change', 'daily_change')
        if raw_change is not None:
            return lots * _as_number(raw_change) * multiplier
        previous = _first(settlement, *_ALIASES['previous'])
        settle = _first(settlement, *_ALIASES['settle'])
        if previous is not None and settle is not None:
            return lots * (_as_number(settle) - _as_number(previous)) * multiplier
        return 0

    # -- main statement -----------------------------------------------------
    def statement(self, source=None, per_lot=None, **overrides):
        if source is not None or per_lot is not None:
            cap = _coerce(source, per_lot, overrides)
            return cap.statement()
        if overrides:
            clone = HedgeCapture(self._records_as_raw(), rates=self.rates,
                                 cash=overrides.pop('cash', self.config.get('cash')),
                                 collateral=overrides.pop('collateral', self.config.get('collateral')),
                                 settlements=overrides.pop('settlements', self.config.get('settlements')),
                                 **overrides)
            return clone.statement()

        positions = self.positions()
        nested_positions = positions._nested_view()
        req_map = self.requirement_map(positions)
        position_triples = self.position_tuples()
        confirmed_lot_map = {(a, c): lots
                             for a, c, lots in self.position_tuples(confirmed_only=True)}

        # gross initial margin by canonical account
        gross_by_account = {}
        for account, contract, lots in position_triples:
            amount = abs(lots) * self.rate_for(contract)
            gross_by_account[account] = gross_by_account.get(account, 0) + amount

        total_relief, relief_details = self.spread_relief()
        spread_decisions_list = self.spread_decisions()
        approved_combos = {d['combo'] for d in spread_decisions_list if d['approved'] and d['combo']}
        relief_by_account = {}
        for detail in relief_details:
            # relief is account agnostic in the receipt; attribute to the account
            # of its first leg when possible.
            account = None
            for leg in detail['legs']:
                for t in self.effective_trades():
                    if str(t['trade_id']) == str(leg):
                        account = t['account']
                        break
                if account is not None:
                    break
            relief_by_account[account] = relief_by_account.get(account, 0) + detail['relief']

        cash_map = self._cash_balances()
        collateral_map = self._collateral_accounts()
        settlements = self._settlement_map()

        # variation by account/contract
        variation_by_account = {}
        variation_lines = []
        for account, contract, lots in position_triples:
            settlement = settlements.get(contract) or settlements.get(str(contract).upper())
            value = self.contract_variation(contract, lots, settlement) if settlement is not None else 0
            variation_by_account[account] = variation_by_account.get(account, 0) + value
            variation_lines.append({'account': account, 'contract': contract,
                                    'lots': lots, 'variation': value})

        # unconfirmed (not clearing confirmed) net position deltas
        unconfirmed_triples = []
        for account, contract, lots in position_triples:
            confirmed_lots = confirmed_lot_map.get((account, contract), 0)
            delta = lots - confirmed_lots
            if delta:
                unconfirmed_triples.append((account, contract, delta))
        unconfirmed_named = {a for a, _, _ in unconfirmed_triples if a is not None}
        unconfirmed_positions = PositionMap()
        if len(unconfirmed_named) <= 1:
            unconfirmed_positions.default_account = next(iter(unconfirmed_named)) if unconfirmed_named else None
            for _, contract, delta in unconfirmed_triples:
                unconfirmed_positions[contract] = delta
        else:
            for account, contract, delta in unconfirmed_triples:
                unconfirmed_positions[(account, contract)] = delta
        unconfirmed_margin = 0
        unconfirmed_trade_ids = []
        for trade in self.effective_trades():
            if not trade['clearing_confirmed'] and trade['terminal_status'] == 'active':
                unconfirmed_trade_ids.append(trade['trade_id'])
                if trade['contract']:
                    unconfirmed_margin += abs(trade['lots']) * self.rate_for(trade['contract'])
        # potential relief withheld for broker-labelled but unapproved combos
        potential_relief = 0
        for decision in spread_decisions_list:
            if not decision['approved'] and decision['relief'] \
                    and decision['combo'] not in approved_combos:
                potential_relief += decision['relief']
        # also broker combo indicative relief, unless clearing approved the
        # combo or one of its legs was cancelled (combo is dead).
        dead_combos = {t['combo'] for t in self.effective_trades()
                       if t['terminal_status'] == 'cancelled' and t['combo']}
        for combo in self.config.get('combos') or []:
            combo_id = _first(combo, 'combo_id', 'id', 'pair_id', 'spread_id')
            if combo_id in approved_combos or combo_id in dead_combos:
                continue
            potential_relief += _as_number(_first(combo, 'indicated_relief', 'potential_relief', 'relief'), 0)

        accounts = {}
        all_accounts = set(gross_by_account) | set(cash_map) | set(collateral_map) | set(variation_by_account)
        if not all_accounts:
            all_accounts = {None}
        for account in all_accounts:
            gross = gross_by_account.get(account, 0)
            relief = relief_by_account.get(account, 0)
            initial = max(0, gross - relief)
            variation = variation_by_account.get(account, 0)
            payable = max(0, -variation)
            receivable = max(0, variation)
            col = collateral_map.get(account, {'posted': 0, 'eligible': 0})
            cash = cash_map.get(account, 0)
            call = max(0, initial - col['eligible']) + payable
            cash_to_raise = max(0, initial + payable - col['eligible'] - cash)
            accounts[_account_label(account)] = LooseDict({
                'account': _account_label(account),
                'positions': nested_positions.get(_account_label(account), {}),
                'gross_initial_margin': gross,
                'spread_relief': relief,
                'initial_margin': initial,
                'variation_margin': variation,
                'variation_payable': payable,
                'variation_receivable': receivable,
                'collateral_posted': col['posted'],
                'collateral_eligible': col['eligible'],
                'available_cash': cash,
                'cash_including_variation': cash + variation,
                'margin_call': call,
                'total_requirement': initial + payable,
                'cash_to_raise': cash_to_raise,
                'cash_after_call': cash - call,
            })

        # Top level represents the unique account, otherwise totals.
        labels = list(accounts)
        if len(labels) == 1:
            top = deepcopy(accounts[labels[0]])
            top_account = labels[0]
        else:
            top_account = None
            top = LooseDict({'account': None})
            for field in ('gross_initial_margin', 'spread_relief', 'initial_margin',
                          'variation_margin', 'variation_payable', 'variation_receivable',
                          'collateral_posted', 'collateral_eligible', 'available_cash',
                          'cash_including_variation',
                          'margin_call', 'total_requirement', 'cash_to_raise'):
                top[field] = sum(a[field] for a in accounts.values())
            top['cash_after_call'] = sum(a['cash_after_call'] for a in accounts.values())

        confirmed_req = self.requirement_map(self.positions(confirmed_only=True))
        confirmed_initial = sum(confirmed_req.values())
        unconfirmed_view = (unconfirmed_positions._flat_view()
                            if len(unconfirmed_positions._accounts()) <= 1
                            else unconfirmed_positions._nested_view())
        top.update({
            'round': self.config.get('round'),
            'positions': positions._flat_view() if len(positions._accounts()) <= 1 else nested_positions,
            'positions_by_account': nested_positions,
            'requirement_by_contract': dict(req_map),
            'accounts': accounts,
            'by_account': accounts,
            'confirmed_initial_margin': confirmed_initial,
            'unconfirmed_difference': unconfirmed_margin,
            'unconfirmed_margin': unconfirmed_margin,
            'unconfirmed_positions': unconfirmed_view,
            'unconfirmed_trade_ids': unconfirmed_trade_ids,
            'unconfirmed': LooseDict({
                'amount': unconfirmed_margin,
                'outright_margin': unconfirmed_margin,
                'positions': unconfirmed_view,
                'trade_ids': unconfirmed_trade_ids,
                'potential_spread_relief': potential_relief,
            }),
            'potential_spread_relief': potential_relief,
            'spread_decisions': spread_decisions_list,
            'spread_details': spread_decisions_list,
            'variation_lines': variation_lines,
            'evidence': [str(r['report_id']) for r in self._records if r['report_id'] is not None],
        })
        # mirror every canonical key under its aliases for direct indexing
        for canonical, aliases in _STATEMENT_KEY_ALIASES.items():
            if canonical in top:
                for alias in aliases:
                    dict.__setitem__(top, alias, top[canonical])
        return Statement(top)

    def _records_as_raw(self):
        return [r['raw'] for r in self._records]

    # -- call workflow ------------------------------------------------------
    def issue_call(self, amount=None, **fields):
        statement = fields.pop('statement', None) or self.statement()
        rec = self.book.issue(amount, statement=statement, **fields)
        return rec

    def revise_call(self, original, new_amount=None, **fields):
        statement = fields.pop('statement', None) or self.statement()
        return self.book.adjust(original, new_amount=new_amount, statement=statement, **fields)

    # -- friendly method aliases --------------------------------------------
    def add_report(self, report):
        return self.ingest(report)

    def add_reports(self, reports):
        return self.extend(reports)

    def ingest_many(self, reports):
        return self.extend(reports)

    def feed(self, report):
        return self.ingest(report)

    def net_positions(self, *args, **kwargs):
        return self.positions(*args, **kwargs)

    def calculate(self, *args, **kwargs):
        return self.statement(*args, **kwargs)

    def run(self, *args, **kwargs):
        return self.statement(*args, **kwargs)

    def assess(self, *args, **kwargs):
        return self.statement(*args, **kwargs)

    def evaluate(self, *args, **kwargs):
        return self.statement(*args, **kwargs)

    def margin_statement(self, *args, **kwargs):
        return self.statement(*args, **kwargs)

    def margin(self, *args, **kwargs):
        return self.statement(*args, **kwargs)

    def margin_call_amount(self, *args, **kwargs):
        return self.statement(*args, **kwargs)['margin_call']

    def issue_margin_call(self, *args, **kwargs):
        return self.issue_call(*args, **kwargs)

    def adjust_call(self, original, *args, **kwargs):
        return self.revise_call(original, *args, **kwargs)

    @property
    def call_records(self):
        return self.book.calls


# ---------------------------------------------------------------------------
# Functional compatibility helpers
# ---------------------------------------------------------------------------

def _coerce(source=None, per_lot=None, options=None):
    options = dict(options or {})
    if isinstance(source, HedgeCapture):
        return source
    if isinstance(per_lot, dict):
        options['rates'] = per_lot
    elif per_lot is not None:
        options.setdefault('rates', per_lot)
    if isinstance(source, dict) and per_lot is None and any(k in source for k in _RATE_KEYS):
        return HedgeCapture(source, **options)
    return HedgeCapture(source, **options)


def capture(source=None, *args, **kwargs):
    return HedgeCapture(source, *args, **kwargs)


# friendly aliases for the aggregate
HedgeLedger = HedgeCapture
Capture = HedgeCapture
Ledger = HedgeCapture


def normalize(report, index=0):
    return normalize_event(report, index)


def effective_fills(events, **kwargs):
    return HedgeCapture(events, **kwargs).effective_trades()


def dedupe(events, **kwargs):
    cap = HedgeCapture(events, **kwargs)
    return cap.effective_trades()


def build_positions(events, **kwargs):
    return HedgeCapture(events, **kwargs).positions()


def statement(source=None, per_lot=None, **kwargs):
    return _coerce(source, per_lot, kwargs).statement()


def assess(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def evaluate(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def reconcile(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def margin_statement(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def calculate_margin(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def margin_call(source=None, per_lot=None, **kwargs):
    """Convenience: return tonight's margin call amount (a number)."""
    result = statement(source, per_lot, **kwargs)
    return result['margin_call']


def issue_margin_call(source=None, per_lot=None, book=None, **kwargs):
    cap = _coerce(source, per_lot, kwargs)
    if book is not None:
        cap.book = book
    return cap.issue_call()


# ---------------------------------------------------------------------------
# Extra name aliases for alternative call-site vocabularies
# ---------------------------------------------------------------------------

def capture_reports(source=None, *args, **kwargs):
    return HedgeCapture(source, *args, **kwargs)


def load_capture(source=None, *args, **kwargs):
    return HedgeCapture(source, *args, **kwargs)


def load_reports(source=None, *args, **kwargs):
    return HedgeCapture(source, *args, **kwargs).records


def ingest_reports(reports, *args, **kwargs):
    return HedgeCapture(reports, *args, **kwargs)


def final_margin(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def margin_due(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)['margin_call']


def payable_margin(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)['margin_call']


def run_clearing(source=None, per_lot=None, **kwargs):
    return statement(source, per_lot, **kwargs)


def build_ledger(source=None, *args, **kwargs):
    return HedgeCapture(source, *args, **kwargs)


ReportCapture = HedgeCapture
FillCapture = HedgeCapture
HedgeReportCapture = HedgeCapture
OfflineCapture = HedgeCapture
ClearingLedger = HedgeCapture
TradeLedger = HedgeCapture
MarginLedger = MarginCallBook
CallNoticeBook = MarginCallBook
NoticeLedger = MarginCallBook

__all__ = [name for name in list(globals()) if not name.startswith('_')]

