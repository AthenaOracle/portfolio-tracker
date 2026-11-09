#!/usr/bin/env python3
"""Conservative Base external capital-flow audit, driven by successful wallet snapshots.

Auto-accepts only direct plain native or ERC20 wallet-to-wallet transfers.
Wallet-initiated pool interactions, internal claims and swaps are NOT new capital.
Anything that could be an outside transfer or a protocol interaction is flagged
for review. No wallet addresses, keys, or raw transaction hashes are published.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from urllib.request import Request, urlopen
import hashlib
import hmac
import json
import math

PRICE_ROOT = 'https://coins.llama.fi/prices/historical/'
MAX_PAGES = 4
PAGE_SIZE = 200
MAX_TRANSACTIONS = 120
MAX_REVIEW = 250

class AuditError(ValueError): pass


def _lower(value):
    return value.lower() if isinstance(value, str) else ''


def parse_utc(value):
    dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if dt.tzinfo is None: raise AuditError('Transfer timestamp missing UTC offset')
    return dt.astimezone(timezone.utc)


def _finite(value):
    try: result = float(value)
    except (TypeError, ValueError, OverflowError): return None
    return result if math.isfinite(result) else None


def _reference(key, transfer):
    message = f'{transfer.get("hash", "")}:{transfer.get("uniqueId", "")}:{transfer.get("category", "")}'
    return hmac.new(key.encode(), message.encode(), hashlib.sha256).hexdigest()[:20]


def _collect(rpc, owner, start_block, end_block, direction):
    opts = {
        'fromBlock':hex(start_block),'toBlock':hex(end_block),
        direction+'Address':owner,'category':['erc20','external'],
        'withMetadata':True,'excludeZeroValue':True,
        'maxCount':hex(PAGE_SIZE),'order':'asc'
    }
    output = []
    for page in range(MAX_PAGES):
        response = rpc.call('alchemy_getAssetTransfers',[opts])
        if not isinstance(response, dict) or not isinstance(response.get('transfers'),list):
            raise AuditError('Invalid Alchemy Transfers response')
        output.extend(response['transfers'])
        if len(output) > PAGE_SIZE * MAX_PAGES:
            raise AuditError('Transfer audit exceeded per-run result cap')
        continuation = response.get('pageKey')
        if not continuation: return output
        opts['pageKey'] = continuation
    raise AuditError('Too many transfer pages: increase scan cap or scan smaller windows; no capital data advanced')


def historical_price(coin, dt, cache):
    timestamp = int(dt.timestamp())
    # Price updates are not at a precise block; cache identical token/day/time
    key = f'{coin.lower()}:{timestamp}'
    if key in cache: return cache[key]
    url = PRICE_ROOT + f'{timestamp}/' + quote(coin, safe=':')
    req = Request(url, headers={'User-Agent':'PortfolioTracker/14.0', 'Accept':'application/json'})
    with urlopen(req, timeout=14) as response:
        obj = json.load(response)
    entry = (obj.get('coins') or {}).get(coin) or (obj.get('coins') or {}).get(coin.lower())
    price = _finite(entry.get('price')) if isinstance(entry,dict) else None
    if price is None or price <= 0: raise AuditError('Historical price unavailable for transfer token')
    # DefiLlama historical timestamps can be close, but must not be days off.
    observed = entry.get('timestamp')
    if observed is not None and abs(int(observed) - timestamp) > 86400:
        raise AuditError('Transfer price quote is over 24 hours from event')
    cache[key] = price
    return price


def _transfer_asset(row, known):
    kind = _lower(row.get('category'))
    if kind == 'external':
        return 'coingecko:ethereum', 'ETH'
    if kind == 'erc20':
        contract = _lower((row.get('rawContract') or {}).get('address'))
        if contract in known: return 'base:' + contract, known[contract]
        return None, str(row.get('asset') or 'unknown')[:30]
    return None, 'unknown'


def _raw_amount(row):
    raw = row.get('rawContract') or {}
    raw_value = raw.get('value')
    decimals = raw.get('decimal')
    if raw_value is not None and decimals is not None:
        try:
            # rawContract.decimal is generally an integer or hex string.
            scale = int(str(decimals),16) if str(decimals).startswith('0x') else int(str(decimals))
            if not 0 <= scale <= 36: raise AuditError('Bad transfer token decimals')
            amount = Decimal(int(str(raw_value),16)) / (Decimal(10) ** scale)
            if amount >= 0: return amount
        except (ValueError, InvalidOperation, TypeError): pass
    val = row.get('value')
    try:
        amount = Decimal(str(val))
        if amount.is_finite() and amount >= 0: return amount
    except (InvalidOperation, ValueError, TypeError): pass
    raise AuditError('Transfer amount could not be measured')


def _known_assets(rows, pools, extra_known=None):
    result = {}
    for r in rows:
        if r.get('type') != 'token' or r.get('usd') is None: continue
        identifier = r.get('id','')
        if isinstance(identifier, str) and identifier.startswith('base:0x'):
            result[identifier.split(':',1)[1].lower()] = r.get('symbol','TOKEN')
    for p in pools:
        for t in p.get('tokens', []):
            if isinstance(t.get('contract'),str): result[t['contract'].lower()] = t.get('symbol','TOKEN')
    # Only include separately verified LP tokens, not arbitrary airdrops.
    for contract, symbol in (extra_known or {}).items():
        if _address_ok(str(contract)) and isinstance(symbol, str) and symbol:
            result[contract.lower()] = symbol
    return result


def _review(ref, transfer, reason, direction, amount, symbol, at, quote_usd=None):
    return {'ref':ref,'at':at,'direction':direction,'asset':symbol,
            'quantity':str(amount),'usd':round(quote_usd,6) if quote_usd is not None else None,
            'reason':reason[:110]}


# Canonical ERC-20 Transfer(address,address,uint256) event. Require the actual
# transaction receipt to verify BOTH directions of an owner-initiated swap.
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


def _wei_or_zero(raw):
    try:
        number = int(str(raw), 16) if str(raw).startswith('0x') else int(str(raw or 0))
        return number if number >= 0 else None
    except (ValueError, TypeError):
        return None


def _receipt_confirms_internal_swap(rpc, owner, tx, txhash, events, known, price_fn, cache):
    """High-confidence, paired ERC20 swap; never approve one-sided sends.

    Recognition is based on actual token transfer logs and a successful receipt,
    not just a DEX name or the caller-provided event category. Price parity is a
    conservative safety check against disguising a capital withdrawal as a swap.
    When data are missing, the transaction stays pending for user review.
    """
    target = _lower(tx.get('to'))
    input_data = str(tx.get('input') or '0x').lower()
    if (_lower(tx.get('from')) != owner or not target or target in known
        or len(input_data) < 10 or input_data[:10] in ('0xa9059cbb','0x23b872dd')
        or _wei_or_zero(tx.get('value')) != 0):
        return False
    outbound, inbound = [], []
    for event, coin, symbol in events:
        if _lower(event.get('category')) != 'erc20':
            return False  # In particular, no outbound native ETH/side payment.
        token = _lower((event.get('rawContract') or {}).get('address'))
        if token not in known:
            return False
        frm,to = _lower(event.get('from')),_lower(event.get('to'))
        if frm == owner:
            outbound.append((event,coin,symbol,token))
        if to == owner:
            inbound.append((event,coin,symbol,token))
    if not outbound or not inbound:
        return False
    receipt = rpc.call('eth_getTransactionReceipt', [txhash])
    if not isinstance(receipt, dict) or _wei_or_zero(receipt.get('status')) != 1:
        return False
    if _lower(receipt.get('transactionHash')) != txhash:
        return False
    # Inspect all Transfer logs, not just the Alchemy events we already know.
    # An unpriced ERC20 transferred OUT of this wallet must block auto approval.
    owner_topic = '0x' + '0'*24 + owner[2:]
    receipt_out = set()
    receipt_in = set()
    for log in receipt.get('logs',[]):
        topics = log.get('topics') or []
        if len(topics) != 3 or _lower(topics[0]) != TRANSFER_TOPIC:
            continue
        token = _lower(log.get('address'))
        if _lower(topics[1]) == owner_topic:
            if token not in known:
                return False
            receipt_out.add(token)
        if _lower(topics[2]) == owner_topic:
            receipt_in.add(token)
    if any(token not in receipt_out for _,_,_,token in outbound): return False
    if any(token not in receipt_in for _,_,_,token in inbound): return False
    if not receipt_out or not receipt_in: return False
    # A swap is not verified if the owner sent out substantially more USD
    # than it received (possible disguised capital withdrawal/side payment).
    total_out = 0.0
    total_in = 0.0
    try:
        for side, items in (('out',outbound),('in',inbound)):
            for event,coin,_,_ in items:
                when = parse_utc((event.get('metadata') or {}).get('blockTimestamp'))
                value = float(_raw_amount(event)) * price_fn(coin,when,cache)
                if not math.isfinite(value) or value <= 0: return False
                if side == 'out': total_out += value
                else: total_in += value
    except (AuditError, OSError, ValueError, TimeoutError, TypeError, OverflowError):
        return False
    return total_out > 0 and 0.80 <= total_in/total_out <= 1.20


# V21: a routed ERC20 swap can be submitted by an address other than the
# tracked wallet (e.g., through a trusted wallet integration). Such activity
# is NOT automatically a capital deposit if a successful receipt proves an
# exact, balanced two-token exchange affecting the tracked wallet. This is
# deliberately narrower than a general-purpose DeFi transaction interpreter.
RELAYED_QUOTE_MAX_AGE_SECONDS = 30 * 3600
INDICATIVE_TRADE_LIMIT_USD = 25.0


def _snapshot_swap_quote(token, when, price_rows):
    """Use a *fresh* observed wallet quote if a historical quote is unavailable.

    Previous and current snapshot rows are candidates. Keeping the previous
    row matters when a token was completely sold and vanishes from current.
    Never treat stale/zero/unpriced assets as proven monetary equivalence.
    """
    best = None
    for record_at, rows in price_rows or []:
        try:
            seconds = abs((parse_utc(record_at) - when).total_seconds())
        except (ValueError, TypeError, AuditError):
            continue
        if seconds > RELAYED_QUOTE_MAX_AGE_SECONDS or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get('type') != 'token':
                continue
            if _lower(row.get('id')) != 'base:' + token:
                continue
            price = _finite(row.get('price'))
            if price is None or price <= 0 or row.get('usd') is None:
                continue
            source = str(row.get('priceSource') or '')
            if source not in ('chain-market', 'fixed-indicative', 'indicative-cross-chain'):
                continue
            candidate = (seconds, price, source)
            if best is None or seconds < best[0]:
                best = candidate
    return (best[1], best[2]) if best is not None else (None, None)


def _receipt_confirms_relayed_swap(rpc, owner, tx, txhash, events, known,
                                   price_fn, price_cache, price_rows):
    """Strict automatic approval ONLY for an exact two-sided ERC20 trade.

    A third-party sender is acceptable *only* when the successful on-chain
    receipt and Alchemy transfer data agree exactly on BOTH token quantities,
    the assets have a credible value, and no other owner ERC20 movement exists.
    Deposits, one-sided sends, liquidity changes and uncertain prices fail
    closed and stay in the human-review queue.
    """
    if (_lower(tx.get('from')) == owner or
        not _lower(tx.get('to')) or _lower(tx.get('to')) in known or
        _lower(tx.get('to')) == owner or
        len(str(tx.get('input') or '0x')) < 10 or
        str(tx.get('input') or '').lower()[:10] in ('0xa9059cbb', '0x23b872dd') or
        _wei_or_zero(tx.get('value')) != 0):
        return False
    if len(events) != 2:
        return False
    # Expected (contract, indexed sender, indexed recipient, raw token amount).
    expected = []
    directions = []
    for event, coin, _symbol in events:
        if _lower(event.get('category')) != 'erc20':
            return False
        contract = _lower((event.get('rawContract') or {}).get('address'))
        frm, to = _lower(event.get('from')), _lower(event.get('to'))
        if (contract not in known or not _address_ok(frm) or not _address_ok(to)
            or frm == to or (frm == owner) == (to == owner)):
            return False
        raw = (event.get('rawContract') or {}).get('value')
        amount_raw = _wei_or_zero(raw)
        if amount_raw is None or amount_raw <= 0:
            return False
        expected.append((contract, frm, to, amount_raw))
        directions.append('out' if frm == owner else 'in')
    if sorted(directions) != ['in', 'out'] or expected[0][0] == expected[1][0]:
        return False
    receipt = rpc.call('eth_getTransactionReceipt', [txhash])
    if (not isinstance(receipt, dict) or
        _lower(receipt.get('transactionHash')) != txhash or
        _wei_or_zero(receipt.get('status')) != 1):
        return False
    owner_topic = '0x' + '0' * 24 + owner[2:]
    actual = []
    for log in receipt.get('logs') or []:
        topics = [_lower(v) for v in log.get('topics') or []]
        if not topics or topics[0] != TRANSFER_TOPIC:
            continue
        if owner_topic not in topics[1:]:
            continue
        # Reject malformed / atypical Transfer events rather than missing a leg.
        if len(topics) != 3 or len(topics[1]) != 66 or len(topics[2]) != 66:
            return False
        contract = _lower(log.get('address'))
        frm, to = '0x' + topics[1][-40:], '0x' + topics[2][-40:]
        raw = _wei_or_zero(log.get('data'))
        if contract not in known or raw is None or raw <= 0:
            return False
        actual.append((contract, frm, to, raw))
    if sorted(actual) != sorted(expected):
        return False
    # Value parity rules out a one-sided withdrawal disguised by a dust payment.
    values = {}
    for (event, coin, _), direction in zip(events, directions):
        when = parse_utc((event.get('metadata') or {}).get('blockTimestamp'))
        price = None
        try:
            price = _finite(price_fn(coin, when, price_cache))
        except (AuditError, OSError, ValueError, TimeoutError, TypeError, OverflowError):
            pass
        quote_source = 'historical' if price is not None and price > 0 else None
        if not quote_source:
            price, quote_source = _snapshot_swap_quote(coin.split(':', 1)[-1], when, price_rows)
        if price is None or price <= 0:
            return False
        amount = float(_raw_amount(event))
        value_usd = amount * price
        if not math.isfinite(value_usd) or value_usd <= 0:
            return False
        if quote_source in ('fixed-indicative', 'indicative-cross-chain') and value_usd > INDICATIVE_TRADE_LIMIT_USD:
            return False
        values[direction] = value_usd
    return (values.get('out', 0) > 0 and values.get('in', 0) > 0 and
            0.80 <= values['in'] / values['out'] <= 1.20)


def _address_ok(value):
    return isinstance(value, str) and len(value) == 42 and value.startswith('0x') and all(c in '0123456789abcdef' for c in value[2:])


def analyze_transactions(rpc, owner, transfers, key, pools, known, prices, previous_pending, price_fn=historical_price, trusted_sources=None, price_rows=None):
    own = owner.lower()
    trusted_sources = {_lower(x) for x in (trusted_sources or [])}
    pool_addresses = {p['pool'].lower() for p in pools if isinstance(p.get('pool'),str)}
    pool_addresses.update({'0x0000000000000000000000000000000000000000'})
    by_tx = {}
    ids = set()
    for t in transfers:
        if not isinstance(t,dict): raise AuditError('Malformed transfer event')
        uniq = t.get('uniqueId') or (str(t.get('hash'))+':'+str(t.get('category'))+':'+str(t.get('value'))+':'+str(t.get('from'))+':'+str(t.get('to')))
        if uniq in ids: continue
        ids.add(uniq)
        coin, symbol = _transfer_asset(t,known)
        if not coin: continue # Not included in tracked/valued portfolio, e.g. spam tokens.
        frm,to = _lower(t.get('from')), _lower(t.get('to'))
        if frm == own and to == own: continue
        if frm != own and to != own: continue
        txhash = _lower(t.get('hash'))
        if len(txhash)!=66 or not txhash.startswith('0x'): raise AuditError('Missing transfer transaction hash')
        by_tx.setdefault(txhash,[]).append((t,coin,symbol))
    if len(by_tx) > MAX_TRANSACTIONS:
        raise AuditError('Too many wallet transactions in transfer interval; do not silently skip capital flows')
    recorded = []
    pending = [x for x in previous_pending if isinstance(x,dict)]
    pending_refs = {x.get('ref') for x in pending}
    private = {}
    for txhash, events in by_tx.items():
        tx = rpc.call('eth_getTransactionByHash',[txhash])
        if not isinstance(tx,dict): raise AuditError('Transaction origin unavailable')
        origin = _lower(tx.get('from'))
        target = _lower(tx.get('to'))
        input_data = str(tx.get('input') or '0x').lower()
        if not origin: raise AuditError('Transaction sender missing')
        both_directions = any(_lower(e[0].get('to'))==own for e in events) and any(_lower(e[0].get('from'))==own for e in events)
        # Receipts are requested only for plausible two-sided wallet-initiated
        # swaps, never for every outgoing transfer. Unknown cases stay pending.
        verified_swap = False
        if both_directions:
            try:
                if origin == own:
                    verified_swap = _receipt_confirms_internal_swap(
                        rpc, own, tx, txhash, events, known, price_fn, prices)
                else:
                    verified_swap = _receipt_confirms_relayed_swap(
                        rpc, own, tx, txhash, events, known, price_fn, prices, price_rows)
            except (AuditError, OSError, ValueError, TimeoutError, TypeError, OverflowError):
                verified_swap = False
        for t,coin,symbol in events:
            ref = _reference(key,t)
            if ref in pending_refs: continue
            dt = parse_utc((t.get('metadata') or {}).get('blockTimestamp'))
            at = dt.isoformat(timespec='seconds').replace('+00:00','Z')
            frm,to = _lower(t.get('from')),_lower(t.get('to'))
            direction = 'in' if to == own else 'out'
            amount = _raw_amount(t)
            if amount == 0: continue
            kind = _lower(t.get('category'))
            token_contract = _lower((t.get('rawContract') or {}).get('address'))
            # Plain ERC20 transfer() or native ETH send, with a single owner-initiated
            # transfer. Claims and DEX transactions normally target other contracts.
            direct_in = (direction=='in' and origin!=own and frm==origin
                         and ((kind=='erc20' and target==token_contract)
                              or (kind=='external' and target==own and input_data in ('0x',''))))
            direct_out = (direction=='out' and origin==own and frm==own and to not in pool_addresses
                          and ((kind=='erc20' and target==token_contract)
                               or (kind=='external' and target==to and input_data in ('0x',''))))
            # Rewards claimed / pool swaps initiated by this wallet stay within
            # the portfolio. A claim cannot be counted again as new capital.
            clearly_internal = (origin==own and direction=='in') or (origin==own and direction=='out' and to in pool_addresses)
            if not clearly_internal and origin==own and both_directions and not direct_out:
                # Complex swap: might have an external side-payment. Do not guess.
                clearly_internal = False
            if clearly_internal or verified_swap: continue
            if direct_in and frm not in trusted_sources:
                reason = 'Direct incoming transfer from an unverified funding address; may be capital or earnings'
            elif direct_in or direct_out:
                try:
                    price = price_fn(coin,dt,prices)
                    usd = float(amount)*price
                    if not math.isfinite(usd) or usd < 0: raise AuditError('Invalid historical USD value')
                    recorded.append({'at':at,'date':at[:10],'usd':round(usd if direct_in else -usd,6),
                                     'kind':'deposit' if direct_in else 'withdrawal','ref':ref})
                    continue
                except (AuditError,OSError,ValueError,TimeoutError):
                    reason = 'Direct transfer; historical USD quote unavailable'
            else:
                reason = 'Contract or complex transfer; classify as capital or internal only after review'
            if len(pending)>=MAX_REVIEW: raise AuditError('Transfer review list exceeds safety limit')
            candidate = _review(ref,t,reason,direction,amount,symbol,at)
            pending.append(candidate)
            pending_refs.add(ref)
            private[ref] = {'hash':txhash,'from':t.get('from'),'to':t.get('to'),
                            'contract':token_contract,'reason':reason,'at':at}
    return recorded,pending,private


def update_audit(rpc, owner, key, previous, previous_snapshot, current, pools, private_file=None, price_fn=historical_price, trusted_sources=None, extra_known=None):
    """Return audit; no blockchain calls are made when it is first initialized.

    If the user has not yet established coverage, start *at this snapshot*.
    Earlier portfolio returns cannot be reconstructed reliably.
    """
    head_raw = rpc.call('eth_blockNumber',[])
    head = int(head_raw,16)
    current['blockNumber'] = head
    if not isinstance(previous,dict) or not previous.get('baselineAt'):
        return {'schema':1,'status':'active','baselineAt':current['at'],
                'baselineUsd':current['totalUsd'],'scannedThroughBlock':head,
                'flows':[],'pending':[], 'lastScanAt':current['at'],
                'message':'Automatic cash-flow tracking started from this snapshot; older profit history is not reconstructed.'}
    old_head = int(previous.get('scannedThroughBlock',-1))
    if old_head<0 or old_head>head: raise AuditError('Invalid stored transfer audit block; refusing inaccurate P&L')
    if current['at'] <= previous['baselineAt']:
        raise AuditError('Snapshot time is not after performance baseline')
    record = dict(previous)
    old_pending = previous.get('pending',[])
    if old_head == head:
        record['status'] = 'review-needed' if old_pending else 'active'
        record['lastScanAt'] = current['at']
        return record
    incoming = _collect(rpc,owner,old_head+1,head,'to')
    outgoing = _collect(rpc,owner,old_head+1,head,'from')
    rows = list(previous_snapshot.get('rows',[])) + list(current.get('rows',[]))
    known = _known_assets(rows,pools,extra_known)
    # Preserve prices in one update; transfer quotes do not contact Alchemy.
    new_flows, pending, private = analyze_transactions(
        rpc,owner,incoming+outgoing,key,pools,known,{},old_pending,
        price_fn=price_fn, trusted_sources=trusted_sources,
        price_rows=[(previous_snapshot.get('at'), previous_snapshot.get('rows', [])),
                    (current.get('at'), current.get('rows', []))])
    # A replay may revisit previously credited transfers; never count twice.
    earlier_flows = list(previous.get('flows', []))
    credited = {f.get('ref') for f in earlier_flows if isinstance(f, dict)}
    record['flows'] = earlier_flows + [f for f in new_flows if f.get('ref') not in credited]
    record['pending'] = pending
    record['scannedThroughBlock'] = head
    record['status'] = 'review-needed' if pending else 'active'
    record['lastScanAt'] = current['at']
    record['message'] = f'{len(new_flows)} direct external transfer(s) identified, {len(pending)} transaction(s) need review.'
    if private_file is not None and private:
        old = {}
        path = Path(private_file)
        if path.exists():
            try:
                old = json.loads(path.read_text(encoding='utf8'))
            except (OSError,ValueError): old = {}
        old.update(private)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(old,indent=2)+'\n',encoding='utf8')
        temp.replace(path)
    return record
