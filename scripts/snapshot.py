#!/usr/bin/env python3
"""Read-only multi-chain portfolio tracker. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from decimal import Decimal, localcontext
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
ZERO = Decimal(0)
Q96 = Decimal(2 ** 96)
ADDRESS = re.compile(r'^0x[0-9a-fA-F]{40}$')
CHAINS = {
    'ethereum': {'rpc': 'eth-mainnet', 'llama': 'ethereum', 'native': 'ETH', 'native_price': 'coingecko:ethereum', 'explorer': 'https://etherscan.io', 'factory':'0x1f98431c8ad98523631ae4a59f267346ea31f984', 'manager': '0xc36442b4a4522e871399cd717abdd847ab11fe88'},
    'base': {'rpc': 'base-mainnet', 'llama': 'base', 'native': 'ETH', 'native_price': 'coingecko:ethereum', 'explorer': 'https://basescan.org', 'factory':'0x33128a8fc17869897dce68ed026d694621f6fdd', 'manager':'0x03a520b32c04bf3beef7beb72e919cf822ed34f1'},
    'arbitrum': {'rpc': 'arb-mainnet', 'llama': 'arbitrum', 'native': 'ETH', 'native_price': 'coingecko:ethereum', 'explorer': 'https://arbiscan.io', 'factory':'0x1f98431c8ad98523631ae4a59f267346ea31f984', 'manager':'0xc36442b4a4522e871399cd717abdd847ab11fe88'},
    'polygon': {'rpc': 'polygon-mainnet', 'llama': 'polygon', 'native': 'POL', 'native_price': 'coingecko:polygon-ecosystem-token', 'explorer': 'https://polygonscan.com', 'factory':'0x1f98431c8ad98523631ae4a59f267346ea31f984', 'manager':'0xc36442b4a4522e871399cd717abdd847ab11fe88'},
    'bnb': {'rpc': 'bnb-mainnet', 'llama': 'bsc', 'native': 'BNB', 'native_price': 'coingecko:binancecoin', 'explorer': 'https://bscscan.com', 'factory':'0xdb1d10011ad0ff90774d0c6bb92e5c5c8b4461f7', 'manager':'0x7b8a01b39d58278b5de7e48c8449c9f4f5170613'},
}
# SOLID on Base has very little liquidity. Show its actual wallet quantity using
# one stable, *approximate* price instead of relying on an erratic market quote.
# This reference is the previously displayed $0.003123 per SOLID.
# Only the exact Base contract gets this treatment.
BASE_SOLID = '0x777cf5ba9c291a1a8f57ff14836f6f9dc5c0f9dd'
SOLID_INDICATIVE_USD = Decimal('0.003123')
TRUSTED_TOKEN_META = {('base', BASE_SOLID): {'decimals': 18, 'symbol': 'SOLID', 'name': 'Solidly'}}


def resolve_token_price(chain, contract, prices):
    if chain == 'base' and contract.lower() == BASE_SOLID:
        return SOLID_INDICATIVE_USD, 'fixed-indicative'
    key = f'{CHAINS[chain]["llama"]}:{contract}'.lower()
    primary = prices.get(key)
    if primary is not None:
        return primary, 'chain-market'
    return None, 'unavailable'


# Ethereum ABI function selectors; no web3/ethers dependency required.
BALANCE_OF = '70a08231'
OWNER_INDEX = '2f745c59'
POSITIONS = '99fbab88'
GET_POOL = '1698ee82'
SLOT0 = '3850c7bd'
_ALCHEMY_CALLS = 0
_ALCHEMY_LOCK = Lock()

class SnapshotError(RuntimeError):
    pass


def load_env(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        if k.strip() in ('ALCHEMY_KEY', 'WALLET_ADDRESS'):
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def api_json(url, payload=None, timeout=25):
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=body, headers={'User-Agent': 'PortfolioTracker/2.0', 'Accept': 'application/json', **({'Content-Type': 'application/json'} if body else {})})
    last = None
    for attempt in range(3):
        try:
            if '.g.alchemy.com/v2/' in url:
                global _ALCHEMY_CALLS
                with _ALCHEMY_LOCK:
                    _ALCHEMY_CALLS += 1
            with urlopen(req, timeout=timeout) as response:
                result = json.load(response)
            return result
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as e:
            last = e
            if isinstance(e, HTTPError) and e.code not in (429, 500, 502, 503, 504):
                break
            if attempt < 2:
                time.sleep(0.6 * (attempt + 1))
    raise SnapshotError(f'HTTP/API request failed ({type(last).__name__}, status {last.code if isinstance(last, HTTPError) else "network/timeout"}); check connection, provider or limits')


class RPC:
    def __init__(self, chain, key):
        self.chain = chain
        self.url = f'https://{CHAINS[chain]["rpc"]}.g.alchemy.com/v2/{key}'

    def call(self, method, params):
        result = api_json(self.url, {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
        if 'error' in result:
            # Never embed request URL or API key in exceptions.
            err = result['error']
            raise SnapshotError(f'{self.chain}: {method}: RPC error {err.get("code", "?")}: {str(err.get("message", "unknown"))[:120]}')
        if 'result' not in result:
            raise SnapshotError(f'{self.chain}: {method} did not return result')
        return result['result']

    def eth_call(self, contract, data):
        raw = self.call('eth_call', [{'to': contract, 'data': '0x' + data}, 'latest'])
        if not isinstance(raw, str) or not raw.startswith('0x') or len(raw) < 66:
            raise SnapshotError(f'{self.chain}: invalid contract response')
        return raw[2:]


def abi_uint(number):
    return f'{number:064x}'


def abi_address(address):
    return '0' * 24 + address[2:].lower()


def abi_words(blob):
    if len(blob) % 64:
        raise SnapshotError('Malformed ABI response')
    return [int(blob[i:i+64], 16) for i in range(0, len(blob), 64)]


def extract_address(word):
    return '0x' + format(word & ((1 << 160) - 1), '040x')


def signed(value, bits=24):
    v = value & ((1 << bits) - 1)
    return v - (1 << bits) if v & (1 << (bits-1)) else v


def token_balances(rpc, owner):
    balances = []
    page_key = None
    seen = set()
    for _ in range(40):
        opts = {'maxCount': 100}
        if page_key:
            opts['pageKey'] = page_key
        result = rpc.call('alchemy_getTokenBalances', [owner, 'erc20', opts])
        if not isinstance(result, dict) or not isinstance(result.get('tokenBalances'), list):
            raise SnapshotError(f'{rpc.chain}: invalid token balance response')
        for row in result['tokenBalances']:
            if row.get('error'):
                raise SnapshotError(f'{rpc.chain}: token balance error')
            raw, contract = row.get('tokenBalance'), row.get('contractAddress')
            if not contract or not ADDRESS.fullmatch(contract) or not raw or not isinstance(raw, str) or not raw.startswith('0x'):
                raise SnapshotError(f'{rpc.chain}: malformed token balance row')
            quantity = int(raw, 16)
            if quantity > 0:
                balances.append({'chain': rpc.chain, 'address': contract.lower(), 'raw': quantity})
        next_key = result.get('pageKey')
        if not next_key:
            return balances
        if next_key in seen:
            raise SnapshotError(f'{rpc.chain}: token pagination cycle')
        seen.add(next_key)
        page_key = next_key
    raise SnapshotError(f'{rpc.chain}: too many token balance pages; refusing partial snapshot')


def get_metadata(rpc, addr):
    meta = rpc.call('alchemy_getTokenMetadata', [addr])
    if not isinstance(meta, dict):
        raise SnapshotError(f'{rpc.chain}: invalid token metadata')
    decimals = meta.get('decimals')
    if not isinstance(decimals, int) or not (0 <= decimals <= 36):
        return None   # Unknown/spam token; do not fabricate decimals.
    symbol = str(meta.get('symbol') or 'TOKEN')[:24]
    name = str(meta.get('name') or symbol)[:80]
    return {'decimals': decimals, 'symbol': symbol, 'name': name}


def discover_v3(rpc, owner, max_positions):
    cfg = CHAINS[rpc.chain]
    manager = cfg['manager']
    owned = abi_words(rpc.eth_call(manager, BALANCE_OF + abi_address(owner)))[0]
    if owned > max_positions:
        raise SnapshotError(f'{rpc.chain}: {owned} Uniswap V3 NFTs exceed configured safety cap {max_positions}')
    found = []
    for i in range(owned):
        nft_id = abi_words(rpc.eth_call(manager, OWNER_INDEX + abi_address(owner) + abi_uint(i)))[0]
        words = abi_words(rpc.eth_call(manager, POSITIONS + abi_uint(nft_id)))
        if len(words) < 12:
            raise SnapshotError(f'{rpc.chain}: incomplete Uniswap V3 position response')
        t0, t1 = extract_address(words[2]), extract_address(words[3])
        fee, low, high, liquidity, owed0, owed1 = words[4], signed(words[5]), signed(words[6]), words[7], words[10], words[11]
        if not (-887272 <= low < high <= 887272):
            raise SnapshotError(f'{rpc.chain}: invalid V3 tick range')
        if liquidity == 0 and owed0 == 0 and owed1 == 0:
            continue
        pool = extract_address(abi_words(rpc.eth_call(cfg['factory'], GET_POOL + abi_address(t0) + abi_address(t1) + abi_uint(fee)))[0])
        if int(pool, 16) == 0:
            raise SnapshotError(f'{rpc.chain}: V3 pool not found for NFT {nft_id}')
        spot = abi_words(rpc.eth_call(pool, SLOT0))
        if not spot or spot[0] <= 0:
            raise SnapshotError(f'{rpc.chain}: invalid V3 pool price for NFT {nft_id}')
        found.append({'chain': rpc.chain, 'id': str(nft_id), 'token0': t0, 'token1': t1, 'fee': fee, 'tickLower': low, 'tickUpper': high, 'liquidity': liquidity, 'owed0': owed0, 'owed1': owed1, 'sqrtPriceX96': spot[0], 'pool': pool, 'manager': manager})
    return found


def tick_sqrt(tick):
    with localcontext() as ctx:
        ctx.prec = 82
        return Decimal('1.0001') ** (Decimal(tick) / 2) * Q96


def v3_amounts(liquidity, tick_lower, tick_upper, sqrt_price):
    """Underlying raw token amounts; owed tokens are accounted for separately."""
    with localcontext() as ctx:
        ctx.prec = 82
        p = Decimal(sqrt_price)
        a, b = tick_sqrt(tick_lower), tick_sqrt(tick_upper)
        if p <= a:
            raw0 = Decimal(liquidity) * Q96 * (b-a) / (a*b)
            raw1 = ZERO
        elif p >= b:
            raw0 = ZERO
            raw1 = Decimal(liquidity) * (b-a) / Q96
        else:
            raw0 = Decimal(liquidity) * Q96 * (b-p) / (p*b)
            raw1 = Decimal(liquidity) * (p-a) / Q96
        return max(ZERO, raw0), max(ZERO, raw1)


# Solidly V3 keeps liquidity under (owner, lower tick, upper tick), not wallet LP tokens.
# Tick discovery uses Alchemy's indexed outgoing wallet transfers, then transaction
# receipts. Discovery is best effort: positions opened by another sending address
# may not be discoverable from wallet-initiated transactions alone.
SOLIDLY_TOKEN0 = '0dfe1681'
SOLIDLY_TOKEN1 = 'd21220a7'
SOLIDLY_POOL_POSITIONS = '514ea4bf'  # positions(bytes32); computed/checked in tests


def solidly_position_key(rpc, owner, low, high):
    if not (-887272 <= low < high <= 887272):
        raise SnapshotError('Solidly position has an invalid tick range')
    packed = owner[2:].lower() + format(low & 0xffffff, '06x') + format(high & 0xffffff, '06x')
    response = rpc.call('web3_sha3', ['0x' + packed])
    if not isinstance(response, str) or not re.fullmatch(r'0x[0-9a-fA-F]{64}', response):
        raise SnapshotError('Solidly position key hash was unavailable')
    return response[2:]


def solidly_tx_ranges(rpc, owner, pool_addresses, existing, fingerprint, max_transactions=24):
    """Discover pool tick pairs from *this wallet's* outgoing transaction receipts.

    Never interpret an empty result as proof of an empty LP. Keep the known tick
    pairs in data.json so subsequent daily runs scan only new transactions.
    """
    state = existing.get('solidlyDiscovery') if isinstance(existing.get('solidlyDiscovery'), dict) else {}
    if state.get('fingerprint') != fingerprint:
        state = {}
    saved = state.get('ranges') if isinstance(state.get('ranges'), dict) else {}
    ranges = {}
    for pool in pool_addresses:
        valid = []
        for pair in saved.get(pool, []):
            if isinstance(pair, list) and len(pair) == 2 and all(type(x) == int for x in pair) and -887272 <= pair[0] < pair[1] <= 887272:
                valid.append(tuple(pair))
        ranges[pool] = set(valid)
    prior_scanned = state.get('scannedToBlock')
    if not isinstance(prior_scanned, int) or prior_scanned < 0:
        prior_scanned = -1
    head = int(rpc.call('eth_blockNumber', []), 16)
    if prior_scanned > head:
        raise SnapshotError('Solidly discovery cache block height is ahead of chain')
    if head == prior_scanned:
        return ranges, state, bool(state.get('partial', False))
    # Verified event selectors are constant; avoid three RPC keccak calls each day.
    topics = {
        '0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde', # Mint
        '0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c', # Burn
        '0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0', # Collect
    }
    hashes = []
    cursor = None
    truncated = False
    # Exact upper bound gives stable pagination while transactions are happening.
    # The free API can reject/limit long histories; stop without claiming full coverage.
    for page in range(1):
        query = {'fromBlock': hex(prior_scanned + 1), 'toBlock': hex(head),
                 'fromAddress': owner, 'category': ['external'],
                 'excludeZeroValue': False, 'maxCount': hex(max_transactions), 'order': 'desc'}
        if cursor:
            query['pageKey'] = cursor
        result = rpc.call('alchemy_getAssetTransfers', [query])
        if not isinstance(result, dict) or not isinstance(result.get('transfers'), list):
            raise SnapshotError('Solidly wallet transaction lookup returned incomplete data')
        for tx in result['transfers']:
            txhash = tx.get('hash') if isinstance(tx, dict) else None
            if isinstance(txhash, str) and re.fullmatch(r'0x[0-9a-fA-F]{64}', txhash):
                hashes.append(txhash)
        cursor = result.get('pageKey')
        if not cursor:
            break
    if cursor:
        truncated = True
    hashes = list(dict.fromkeys(hashes))
    expected_owner = '0x' + '0'*24 + owner[2:].lower()

    def read_receipt(hash_):
        return rpc.call('eth_getTransactionReceipt', [hash_])

    # Bounded concurrency; no scans across millions of Base blocks.
    with ThreadPoolExecutor(max_workers=6) as executor:
        for receipt in executor.map(read_receipt, hashes):
            if not isinstance(receipt, dict) or receipt.get('status') not in ('0x1','0x01'):
                continue
            logs = receipt.get('logs')
            if not isinstance(logs, list):
                raise SnapshotError('Solidly discovery receipt is missing its event logs')
            for log in logs:
                if not isinstance(log, dict) or str(log.get('address', '')).lower() not in ranges:
                    continue
                event_topics = log.get('topics')
                if not isinstance(event_topics, list) or len(event_topics) != 4:
                    continue
                if (str(event_topics[0]).lower() not in topics or str(event_topics[1]).lower() != expected_owner):
                    continue
                try:
                    low = signed(int(event_topics[2], 16))
                    high = signed(int(event_topics[3], 16))
                except (ValueError, TypeError):
                    continue
                if -887272 <= low < high <= 887272:
                    ranges[log['address'].lower()].add((low, high))
    next_state = {'fingerprint': fingerprint, 'scannedToBlock': head,
                  'partial': bool(state.get('partial', False) or truncated),
                  'ranges': {pool: [list(pair) for pair in sorted(items)] for pool, items in ranges.items()}}
    return ranges, next_state, bool(next_state['partial'])


def read_solidly_pool(rpc, owner, lp, tick_pairs, meta, prices):
    """Return the sum of real underlying tokens across identified wallet-owned ranges."""
    chain = lp.get('chain', 'base')
    pool = lp['pool'].lower()
    t0 = extract_address(abi_words(rpc.eth_call(pool, SOLIDLY_TOKEN0))[0]).lower()
    t1 = extract_address(abi_words(rpc.eth_call(pool, SOLIDLY_TOKEN1))[0]).lower()
    configs = {t['contract'].lower(): t for t in lp['tokens']}
    if set(configs) != {t0, t1}:
        raise SnapshotError(f'Solidly LP {lp["name"]}: on-chain tokens do not match config.json')
    sqrt_price = abi_words(rpc.eth_call(pool, SLOT0))[0]
    if not (0 < sqrt_price < (1 << 160)):
        raise SnapshotError(f'Solidly LP {lp["name"]}: invalid pool price')
    raw = {t0: ZERO, t1: ZERO}
    live_ranges = 0
    for low, high in sorted(tick_pairs):
        key = solidly_position_key(rpc, owner, low, high)
        words = abi_words(rpc.eth_call(pool, SOLIDLY_POOL_POSITIONS + key))
        if len(words) < 3:
            raise SnapshotError(f'Solidly LP {lp["name"]}: incomplete position contract response')
        liquidity, owed0, owed1 = words[:3]
        if liquidity == 0 and owed0 == 0 and owed1 == 0:
            continue
        if liquidity >= (1 << 128) or owed0 >= (1 << 128) or owed1 >= (1 << 128):
            raise SnapshotError(f'Solidly LP {lp["name"]}: invalid liquidity/owed balances')
        a, b = v3_amounts(liquidity, low, high, sqrt_price)
        raw[t0] += a + owed0
        raw[t1] += b + owed1
        live_ranges += 1
    value = ZERO
    components = []
    # Preserve the token order supplied in config, not on-chain token0 order.
    for token in lp['tokens']:
        addr = token['contract'].lower()
        info = meta.get((chain, addr))
        price = prices.get(f'{CHAINS[chain]["llama"]}:{addr}'.lower())
        if not info or price is None:
            raise SnapshotError(f'Solidly LP {lp["name"]}: missing underlying token price or decimals')
        amount = raw[addr] / (10 ** info['decimals'])
        worth = amount * price
        value += worth
        components.append({'symbol': token['symbol'], 'amount': str(amount), 'usd': usd(worth)})
    return components, usd(value), live_ranges


def fetch_prices(keys):
    prices = {}
    keys = sorted(set(keys))
    # Keep URL lengths short; coins.llama.fi supports comma-delimited identifiers.
    for i in range(0, len(keys), 40):
        batch = keys[i:i+40]
        res = api_json('https://coins.llama.fi/prices/current/' + ','.join(batch))
        if not isinstance(res, dict) or not isinstance(res.get('coins'), dict):
            raise SnapshotError('DefiLlama price service returned unexpected JSON')
        for k, v in res['coins'].items():
            p = v.get('price') if isinstance(v, dict) else None
            # Do not treat weak/stale oracle marks as reliable wallet value.
            if isinstance(v, dict):
                conf = v.get('confidence')
                timestamp = v.get('timestamp')
                try:
                    if conf is not None and float(conf) < 0.7:
                        continue
                    if timestamp is not None and int(timestamp) > 0 and time.time() - int(timestamp) > 7 * 86400:
                        continue
                except (ValueError, TypeError):
                    continue
            try:
                d = Decimal(str(p))
                if d.is_finite() and d > 0:
                    prices[k.lower()] = d
            except (ValueError, TypeError, ArithmeticError):
                continue
    return prices


def usd(x):
    return float(Decimal(x).quantize(Decimal('0.000001')))


def tvl_counter(protocols):
    try:
        if protocols:
            total = ZERO
            for slug in protocols:
                value = api_json('https://api.llama.fi/tvl/' + slug)
                total += Decimal(str(value))
            return usd(total), None
        series = api_json('https://api.llama.fi/v2/historicalChainTvl')
        if not isinstance(series, list) or not series:
            raise SnapshotError('TVL history empty')
        latest = max(series, key=lambda o: int(o.get('date', 0)))
        return usd(latest['tvl']), None
    except Exception:
        return None, 'DeFi TVL counter could not update (wallet snapshot is unaffected).'


def validate_config(config):
    chains = config.get('chains')
    if not isinstance(chains, list) or not chains or len(set(chains)) != len(chains) or any(c not in CHAINS for c in chains):
        raise SnapshotError('config.json: chains must be a nonempty list of supported unique chains')
    if not isinstance(config.get('manual', []), list):
        raise SnapshotError('config.json: manual must be an array')
    for entry in config.get('manual', []):
        if not isinstance(entry, dict) or not entry.get('name'):
            raise SnapshotError('config.json: manual item missing name')
        try:
            value = Decimal(str(entry['usd']))
            if not value.is_finite() or value < 0:
                raise ValueError()
        except (KeyError, ValueError, ArithmeticError):
            raise SnapshotError('config.json: each manual USD value must be nonnegative')
    if not isinstance(config.get('tvlProtocols', []), list) or any(not re.fullmatch(r'[a-zA-Z0-9-]+', str(s)) for s in config.get('tvlProtocols', [])):
        raise SnapshotError('config.json: tvlProtocols must contain only protocol slugs')

    # Manual quantities for unsupported direct-to-pool Solidly V3 positions.
    pools = config.get('solidlyLps', [])
    if not isinstance(pools, list) or len(pools) > 20:
        raise SnapshotError('config.json: solidlyLps must be an array of at most 20 pools')
    seen_pools = set()
    for pos in pools:
        if not isinstance(pos, dict) or not pos.get('name') or not ADDRESS.fullmatch(str(pos.get('pool', ''))):
            raise SnapshotError('config.json: invalid Solidly LP name/pool address')
        pool_id = (pos.get('chain', 'base'), pos['pool'].lower())
        if pool_id in seen_pools or pool_id[0] not in config['chains']:
            raise SnapshotError('config.json: duplicate Solidly LP or chain not enabled')
        seen_pools.add(pool_id)
        if not isinstance(pos.get('auto', False), bool):
            raise SnapshotError('config.json: Solidly auto must be true or false')
        # Optional user-confirmed tick pairs are discovery hints, never manual USD
        # values. Each is still queried from the actual pool at every snapshot.
        known = pos.get('knownRanges', [])
        if not isinstance(known, list) or len(known) > 100:
            raise SnapshotError('config.json: Solidly knownRanges must be a list of up to 100 tick pairs')
        for pair in known:
            if (not isinstance(pair, list) or len(pair) != 2
                    or any(type(t) is not int for t in pair)
                    or not (-887272 <= pair[0] < pair[1] <= 887272)):
                raise SnapshotError('config.json: invalid Solidly knownRanges tick pair')
        parts = pos.get('tokens')
        if not isinstance(parts, list) or len(parts) != 2:
            raise SnapshotError('config.json: each Solidly LP must have two tokens')
        for part in parts:
            if not isinstance(part, dict) or not ADDRESS.fullmatch(str(part.get('contract', ''))) or not part.get('symbol'):
                raise SnapshotError('config.json: invalid Solidly token contract or symbol')
            if part.get('amount') is None:  # Unknown quantity: never interpret as zero.
                continue
            try:
                value = Decimal(str(part['amount']))
                if not value.is_finite() or value < 0:
                    raise ValueError()
            except (ValueError, ArithmeticError):
                raise SnapshotError('config.json: Solidly token amounts must be nonnegative numbers, or null')
    live_since = config.get('liveSince', '')
    if live_since:
        try:
            dt = datetime.fromisoformat(str(live_since).replace('Z', '+00:00'))
            if dt.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            raise SnapshotError('config.json: liveSince must be an ISO 8601 timestamp with Z/UTC offset, or blank')


def build_snapshot(config, address, key, existing=None):
    existing = existing or {}
    univ3_cfg = config.get('uniswapV3', {})
    if not isinstance(univ3_cfg, dict):
        raise SnapshotError('uniswapV3 config must be an object')
    max_v3 = int(univ3_cfg.get('maxPositionsPerChain', 40))
    if not 0 <= max_v3 <= 200:
        raise SnapshotError('maxPositionsPerChain must be 0..200')
    rpc_by_chain = {chain: RPC(chain, key) for chain in config['chains']}
    balances = []
    v3 = []
    native = []
    warnings = []
    # Fail on ANY chain outage: no partial portfolio snapshots.
    for chain, rpc in rpc_by_chain.items():
        print(f'Checking {chain}...', flush=True)
        raw = rpc.call('eth_getBalance', [address, 'latest'])
        if not isinstance(raw, str) or not raw.startswith('0x'):
            raise SnapshotError(f'{chain}: invalid native balance')
        n = int(raw, 16)
        if n:
            native.append((chain, n))
        balances.extend(token_balances(rpc, address))
        if univ3_cfg.get('enabled', True):
            v3.extend(discover_v3(rpc, address, max_v3))

    # Collect unique metadata reads, including LP underlying tokens.
    meta_keys = set((item['chain'], item['address']) for item in balances)
    for pos in v3:
        meta_keys.add((pos['chain'], pos['token0']))
        meta_keys.add((pos['chain'], pos['token1']))
    for lp in config.get('solidlyLps', []):
        if lp.get('auto', False):
            for token in lp['tokens']:
                meta_keys.add((lp.get('chain', 'base'), token['contract'].lower()))
    meta = {}
    cache = existing.get('tokenMetadata') if isinstance(existing.get('tokenMetadata'), dict) else {}
    for pair in meta_keys:
        cache_record = cache.get(':'.join(pair))
        if (isinstance(cache_record, dict)
            and type(cache_record.get('decimals')) == int
            and 0 <= cache_record['decimals'] <= 36
            and isinstance(cache_record.get('symbol'), str)
            and isinstance(cache_record.get('name'), str)):
            meta[pair] = cache_record
        elif (isinstance(cache_record, dict) and isinstance(cache_record.get('_missingUntil'), str)
              and cache_record['_missingUntil'] > datetime.now(timezone.utc).strftime('%Y-%m-%d')):
            meta[pair] = None  # Retry unrecognized/spam metadata after seven days.
    # Trusted official contract metadata needs no repeated Alchemy request.
    for pair in meta_keys - set(meta):
        if pair in TRUSTED_TOKEN_META:
            meta[pair] = dict(TRUSTED_TOKEN_META[pair])
    needed = sorted(meta_keys - set(meta))
    if meta:
        print(f'Using cached metadata for {len(meta)} token(s); fetching {len(needed)} new token(s).', flush=True)
    def fetch_metadata(pair):
        chain, addr = pair
        return pair, get_metadata(rpc_by_chain[chain], addr)
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(fetch_metadata, item) for item in needed]
        for future in as_completed(futures):
            pair, data = future.result()
            meta[pair] = data

    # Discover current LP ranges internally. No ticks/ranges are displayed to users.
    # On a first snapshot, this scans a bounded set of wallet-sent transactions;
    # a result marked partial is never presented as a proven complete portfolio.
    auto_lps = [lp for lp in config.get('solidlyLps', []) if lp.get('auto', False)
                and any(t.get('amount') is None for t in lp['tokens'])]
    solidly_ranges = {}
    discovery = existing.get('solidlyDiscovery') if isinstance(existing.get('solidlyDiscovery'), dict) else {}
    discovery_partial = False
    discovery_error = None
    if auto_lps:
        if any(lp.get('chain', 'base') != 'base' for lp in auto_lps):
            raise SnapshotError('Solidly automatic position lookup currently supports Base only')
        fingerprint = hmac.new(key.encode('utf8'), address.lower().encode('ascii'), hashlib.sha256).hexdigest()[:24]
        try:
            solidly_ranges, discovery, discovery_partial = solidly_tx_ranges(
                rpc_by_chain['base'], address, {lp['pool'].lower() for lp in auto_lps},
                existing, fingerprint)
        except SnapshotError as exc:
            discovery_error = str(exc)
            # Preserve the last trustworthy value instead of recording a false drop.
            if any(r.get('protocol') == 'Solidly V3' and r.get('usd') is not None
                   for r in existing.get('last', {}).get('rows', []) if isinstance(r, dict)):
                raise SnapshotError('Solidly position discovery failed; previous snapshot preserved') from exc
            warnings.append(f'Solidly automatic lookup unavailable: {discovery_error}. LP values remain unknown.')
    # Known, previously verified Nami position references are valued together
    # with wallet/Solidly at the SAME snapshot time, never from stale preview JSON.
    import nami_preview as nami
    from nami_integration import verified_nami_rows, coverage_bridges, mark_coverage_gaps
    nami_refs = nami.load_position_refs()
    if nami_refs and 'base' not in rpc_by_chain:
        raise SnapshotError('Saved Nami NFTs require Base in the selected chains')
    price_keys = [CHAINS[chain]['native_price'] for chain, _ in native]
    if nami_refs:
        price_keys.extend(['base:' + token for token in sorted(nami.EXPECTED)])
    price_keys.extend(f'{CHAINS[b["chain"]]["llama"]}:{b["address"]}' for b in balances if meta.get((b['chain'], b['address'])))
    for pos in v3:
        for t in ('token0', 'token1'):
            price_keys.append(f'{CHAINS[pos["chain"]]["llama"]}:{pos[t]}')
    for lp in config.get('solidlyLps', []):
        # Fetch USD prices for the two underlying tokens, never price the pool's TVL as your holding.
        if lp.get('auto', False) or all(token.get('amount') is not None for token in lp['tokens']):
            price_keys.extend(f'{CHAINS[lp.get("chain", "base")]["llama"]}:{token["contract"].lower()}' for token in lp['tokens'])
    prices = fetch_prices(price_keys) if price_keys else {}
    rows = []
    missing = []
    unpriced_keys = set()

    for chain, raw in native:
        conf = CHAINS[chain]
        key_ = conf['native_price'].lower()
        price = prices.get(key_)
        if not price:
            raise SnapshotError(f'{chain}: missing price for native token {conf["native"]}')
        amount = Decimal(raw) / 10**18
        rows.append({'id': f'{chain}:native', 'chain': chain, 'type': 'token', 'symbol': conf['native'], 'name': conf['native'] + ' (native)', 'amount': str(amount), 'price': usd(price), 'usd': usd(amount*price), 'url': None})

    for item in balances:
        chain, contract, raw = item['chain'], item['address'], item['raw']
        m = meta.get((chain, contract))
        if not m:
            missing.append(f'{chain}: token with unknown decimals {contract[:10]}…')
            unpriced_keys.add(f'{chain}:{contract}')
            continue
        amount = Decimal(raw) / (10 ** m['decimals'])
        p, price_source = resolve_token_price(chain, contract, prices)
        ident = f'{chain}:{contract}'
        if p is None:
            missing.append(f'{chain}: unpriced {m["symbol"]} ({contract[:10]}…)')
            unpriced_keys.add(ident)
            continue
        rows.append({'id': ident, 'chain': chain, 'type': 'token', 'symbol': m['symbol'], 'name': m['name'], 'amount': str(amount), 'price': usd(p), 'usd': usd(amount * p), 'priceSource': price_source, 'url': f'{CHAINS[chain]["explorer"]}/token/{contract}'})

    for pos in v3:
        chain = pos['chain']
        raw0, raw1 = v3_amounts(pos['liquidity'], pos['tickLower'], pos['tickUpper'], pos['sqrtPriceX96'])
        units = []
        value = ZERO
        for contract, raw, owed in ((pos['token0'], raw0, pos['owed0']), (pos['token1'], raw1, pos['owed1'])):
            m = meta.get((chain, contract))
            if not m:
                raise SnapshotError(f'{chain}: V3 position {pos["id"]} token metadata unavailable; refusing inaccurate portfolio')
            p = prices.get(f'{CHAINS[chain]["llama"]}:{contract}'.lower())
            if p is None:
                raise SnapshotError(f'{chain}: V3 position {pos["id"]} underlying token price unavailable; refusing inaccurate portfolio')
            amount = (raw + Decimal(owed)) / (10 ** m['decimals'])
            value += amount * p
            units.append({'symbol': m['symbol'], 'amount': str(amount), 'usd': usd(amount * p)})
        sqrtp = pos['sqrtPriceX96']
        range_status = 'below' if Decimal(sqrtp) <= tick_sqrt(pos['tickLower']) else ('above' if Decimal(sqrtp) >= tick_sqrt(pos['tickUpper']) else 'in')
        rows.append({'id': f'{chain}:univ3:{pos["id"]}', 'chain': chain, 'type': 'lp', 'protocol': 'Uniswap V3', 'name': f'{units[0]["symbol"]}/{units[1]["symbol"]}', 'symbol': 'LP', 'usd': usd(value), 'nftId': pos['id'], 'feeTier': pos['fee']/10000, 'rangeStatus': range_status, 'underlying': units, 'url': f'{CHAINS[chain]["explorer"]}/nft/{pos["manager"]}/{pos["id"]}'})

    for lp in config.get('solidlyLps', []):
        chain = lp.get('chain', 'base')
        ident = f'{chain}:solidly:{lp["pool"].lower()}'
        pool_address = lp['pool'].lower()
        manually_known = all(token.get('amount') is not None for token in lp['tokens'])
        # Always query user-confirmed ranges even when the bounded transaction
        # scan missed the original mint. Retain automatic discovery of new ranges.
        hinted_ranges = {tuple(pair) for pair in lp.get('knownRanges', [])}
        auto_ranges = set(solidly_ranges.get(pool_address, set())) | hinted_ranges
        components = None
        value = None
        source = 'unknown'
        live_ranges = 0
        if lp.get('auto', False) and auto_ranges:
            components, value, live_ranges = read_solidly_pool(
                rpc_by_chain[chain], address, lp, auto_ranges, meta, prices)
            source = 'on-chain best effort'
            warnings.append(f'Solidly LP {lp["name"]}: positions detected from a bounded wallet transaction scan; earlier or externally created positions may be missed.')
        elif manually_known:
            components = []
            value_dec = ZERO
            for token in lp['tokens']:
                amount = Decimal(str(token['amount']))
                price = prices.get(f'{CHAINS[chain]["llama"]}:{token["contract"].lower()}')
                if price is None:
                    raise SnapshotError(f'{chain}: Solidly LP {lp["name"]}: price unavailable')
                value_dec += amount * price
                components.append({'symbol': token['symbol'], 'amount': str(amount), 'usd': usd(amount * price)})
            value = usd(value_dec)
            source = 'manual quantities'
            warnings.append(f'Solidly LP {lp["name"]}: quantities entered in config.json are not live.')
        else:
            components = [{'symbol': token['symbol'], 'amount': None, 'usd': None} for token in lp['tokens']]
            warnings.append(f'Solidly LP {lp["name"]} could not be identified from wallet-sent transactions. Value unknown; confirm the LP owner address or provide discovery data.')
        rows.append({'id': ident, 'chain': chain, 'type': 'lp', 'protocol': 'Solidly V3',
                     'name': str(lp['name'])[:80], 'symbol': 'LP', 'usd': value,
                     'underlying': components, 'pending': value is None,
                     'rangesChecked': len(auto_ranges) if lp.get('auto', False) else None,
                     'activeRanges': live_ranges if source == 'on-chain best effort' else None,
                     'source': source, 'url': f'https://basescan.org/address/{lp["pool"]}'})

    nami_rows = verified_nami_rows(rpc_by_chain['base'], address, prices, nami_refs) if nami_refs else []
    rows.extend(nami_rows)
    previously_nami = {r.get('id') for r in existing.get('last', {}).get('rows', [])
                       if isinstance(r, dict) and r.get('protocol') == 'Nami' and r.get('usd') is not None}
    if previously_nami - {r['id'] for r in nami_rows}:
        raise SnapshotError('Previously valued Nami NFT no longer verifies; preserving last snapshot')

    # The historic safeguard prevents price coverage losses from fabricating a drawdown.
    old_ids = set(existing.get('last', {}).get('pricedIds', []))
    current_ids = {r['id'] for r in rows if r['type'] == 'token' and r.get('usd') is not None}
    overlapping_unpriced = sorted(unpriced_keys & old_ids)
    if overlapping_unpriced:
        raise SnapshotError(f'{len(overlapping_unpriced)} previously priced token(s) lost pricing or metadata; refusing false dip: {", ".join(overlapping_unpriced[:3])}')
    # A previously valued LP becoming unconfigured must not create an artificial drop.
    previously_valued_lps = {row.get('id') for row in existing.get('last', {}).get('rows', []) if isinstance(row, dict) and row.get('type') == 'lp' and row.get('usd') is not None and str(row.get('id', '')).startswith('base:solidly:')}
    current_solidly_values = {r['id'] for r in rows if r.get('type') == 'lp' and r.get('usd') is not None and str(r.get('id','')).startswith('base:solidly:')}
    missing_lps = sorted(previously_valued_lps - current_solidly_values)
    if missing_lps:
        raise SnapshotError('Previously valued Solidly LP now lacks quantities. Refusing a false portfolio dip.')

    for i, item in enumerate(config.get('manual', [])):
        value = Decimal(str(item['usd']))
        if value:
            rows.append({'id': f'manual:{i}', 'chain': str(item.get('chain', 'other')).lower(), 'type': 'manual', 'name': str(item['name'])[:90], 'symbol': 'Manual', 'usd': usd(value), 'note': str(item.get('note', 'User-supplied valuation'))[:180]})
    if missing:
        warnings.append(f'{len(missing)} ERC-20 token balance(s) lack prices or metadata and are excluded. Check these before trusting the total.')
    if univ3_cfg.get('enabled', True):
        warnings.append('Uniswap V3 values include liquidity and recorded owed tokens, but exclude fees accrued since the last position update. Staked/transferred NFTs are not found.')
    warnings.append('Daily value changes include deposits, withdrawals and transfers; they are not investment returns.')
    tvl, tvl_warning = tvl_counter(config.get('tvlProtocols', [])) if config.get('showTvl', False) else (None, None)
    if tvl_warning:
        warnings.append(tvl_warning)
    rows.sort(key=lambda r: (-(r['usd'] if r['usd'] is not None else -1), r['chain'], r['name']))
    total = usd(sum(Decimal(str(r['usd'])) for r in rows if r['usd'] is not None))
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
    started = config.get('liveSince') or existing.get('startedAt') or ((existing.get('history') or [{}])[0].get('date', '') + 'T00:00:00Z' if existing.get('history') else now)
    current = {'at': now, 'date': now[:10], 'totalUsd': total, 'tvlUsd': tvl, 'counts': {'tokens': sum(r['type']=='token' for r in rows), 'lps': sum(r['type']=='lp' for r in rows), 'manual': sum(r['type']=='manual' for r in rows), 'unpriced': len(missing)}, 'pricedIds': sorted(current_ids), 'warnings': warnings, 'missing': missing[:80], 'rows': rows}
    history = [x for x in existing.get('history', []) if isinstance(x, dict) and x.get('date') != current['date']]
    history.append({'date': current['date'], 'totalUsd': total, 'tvlUsd': tvl})
    history.sort(key=lambda x: x['date'])
    limit = int(config.get('historyDays', 365))
    if limit < 2 or limit > 3650:
        raise SnapshotError('historyDays must be between 2 and 3650')
    # V22: retain the complete dated portfolio history, including the original
    # baseline. historyDays remains validated for configuration compatibility,
    # but is no longer used to erase older audited performance observations.
    # This intentionally allows data.json to grow as more days are recorded.
    output = {'schema': 2, 'startedAt': started, 'last': current, 'history': history}
    # V14: unified investment P&L audits only cash that actually enters or
    # leaves the wallet. Claims, LP changes, staking yield and token prices are
    # already included in the combined wallet+LP USD valuation.
    if 'base' in rpc_by_chain and config.get('capitalTracking', {}).get('enabled', True):
        if str(ROOT / 'scripts') not in sys.path:
            sys.path.insert(0, str(ROOT / 'scripts'))
        from capital import update_audit
        capital_sources = []
        source_text = os.environ.get('PORTFOLIO_CAPITAL_SOURCES_JSON','')
        if not source_text and (ROOT/'capital-sources.json').exists():
            source_text = (ROOT/'capital-sources.json').read_text(encoding='utf8')
        if source_text:
            try:
                source_data = json.loads(source_text)
                capital_sources = source_data.get('addresses', []) if isinstance(source_data,dict) else source_data
                if not isinstance(capital_sources,list) or any(not isinstance(a,str) or not ADDRESS.fullmatch(a) for a in capital_sources):
                    raise ValueError('Bad capital funding address allowlist')
            except (ValueError, TypeError):
                capital_sources = []
                print('Capital sources file invalid; all incoming direct transfers will require review.', flush=True)
        try:
            audit = update_audit(rpc_by_chain['base'], address, key,
                existing.get('capitalAudit'), existing.get('last', {}), current,
                [p for p in config.get('solidlyLps', []) if p.get('chain','base') == 'base'],
                ROOT / 'capital-review-private.json',
                trusted_sources=capital_sources)
        except Exception as exc:
            old_audit = existing.get('capitalAudit')
            if isinstance(old_audit, dict) and old_audit.get('baselineAt'):
                audit = dict(old_audit)
                audit['status'] = 'scan-failed'
                audit['message'] = 'Capital transfer audit could not complete; investment P&L is hidden until the next successful scan.'
            else:
                audit = {'schema': 1, 'status': 'uninitialized', 'message':
                         'Capital audit could not initialize; retry the next wallet snapshot.'}
            print('Capital flow audit unavailable (' + type(exc).__name__ + '); portfolio balance remains available, P&L withheld.', flush=True)
        # A Nami position discovered after an older snapshot may have existed
        # then but not been counted. Register a ONE-TIME non-capital adjustment,
        # with its source receipt proving it predated the last recorded value.
        # This never edits the original audited baseline or historical values.
        if nami_rows:
            try:
                bridges = coverage_bridges(existing, nami_rows, nami_refs,
                                           rpc_by_chain['base'], address, current['at'])
            except Exception as exc:
                raise SnapshotError('Nami coverage verification failed; previous snapshot preserved') from exc
            existing_bridges = list(audit.get('coverageAdjustments') or [])
            used_ids = {row.get('id') for row in existing_bridges if isinstance(row, dict)}
            audit['coverageAdjustments'] = existing_bridges + [b for b in bridges if b['id'] not in used_ids]
        output['capitalAudit'] = audit
        observed = existing.get('observations', []) if isinstance(existing.get('observations'),list) else []
        if audit.get('baselineAt') and audit.get('baselineAt') == current['at']:
            observed = []  # Start fresh; older snapshots lack verified flow coverage.
        # Preserve earlier snapshots but mark ones missing an NFT as incomplete.
        # Otherwise a previously omitted LP would look like a loss and recovery.
        if audit.get('coverageAdjustments'):
            try:
                mark_coverage_gaps(observed, audit['coverageAdjustments'], audit.get('baselineAt'))
            except Exception as exc:
                raise SnapshotError('Nami historical coverage cannot be verified; previous snapshot preserved') from exc
        observed = [x for x in observed if isinstance(x, dict) and x.get('at') != current['at']]
        lp_complete = all(row.get('usd') is not None for row in rows if row.get('type') == 'lp')
        observed.append({'at':current['at'], 'totalUsd':total,
                         'blockNumber':current.get('blockNumber'), 'complete':lp_complete})
        observed.sort(key=lambda x: x['at'])
        # V22: never drop the audited baseline/earlier observations after 4,000
        # manual or scheduled refreshes. Preserve every observed timestamp.
        output['observations'] = observed
    if auto_lps and discovery:
        output['solidlyDiscovery'] = discovery
    # Store public token metadata only; never store Alchemy keys or the wallet address.
    metadata_cache = {k:v for k,v in cache.items() if isinstance(v, dict)}
    metadata_cache.update({':'.join(pair): (value if value is not None else {'_missingUntil': (datetime.now(timezone.utc) + timedelta(days=7)).strftime('%Y-%m-%d')}) for pair,value in meta.items()})
    output['tokenMetadata'] = dict(sorted(metadata_cache.items())[:500])
    return output


def main():
    parser = argparse.ArgumentParser(description='Read-only wallet portfolio snapshot')
    parser.add_argument('--config', default=str(ROOT / 'config.json'))
    parser.add_argument('--output', default=str(ROOT / 'data.json'))
    parser.add_argument('--dry-run', action='store_true', help='Validate data without writing')
    parser.add_argument('--force', action='store_true', help='Manually refresh now, ignoring the 24-hour limit')
    args = parser.parse_args()
    load_env(ROOT / '.env')
    key = os.environ.get('ALCHEMY_KEY', '').strip()
    address = os.environ.get('WALLET_ADDRESS', '').strip()
    if not key:
        raise SnapshotError('Missing ALCHEMY_KEY. Run setup.bat locally or add a GitHub Actions secret.')
    if not ADDRESS.fullmatch(address):
        raise SnapshotError('Missing or invalid WALLET_ADDRESS. Use public 0x address (never a seed phrase).')
    config_path = Path(args.config)
    config = json.loads(config_path.read_text(encoding='utf8'))
    validate_config(config)
    out_path = Path(args.output)
    existing = json.loads(out_path.read_text(encoding='utf8')) if out_path.exists() else {}
    if not args.force and not args.dry_run:
        previous = existing.get('last', {}).get('at') if isinstance(existing.get('last'), dict) else None
        if isinstance(previous, str):
            try:
                last_update = datetime.fromisoformat(previous.replace('Z', '+00:00'))
                age = (datetime.now(timezone.utc) - last_update).total_seconds()
                if 0 <= age < 86400:
                    hours = (86400 - age) / 3600
                    print(f'No blockchain requests: last snapshot is under 24 hours old. Next automatic refresh in about {hours:.1f} hours.')
                    return
            except ValueError:
                pass
    result = build_snapshot(config, address, key, existing)
    if args.dry_run:
        print('DRY RUN: snapshot validated, no data.json written')
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_name(out_path.name + '.tmp')
        tmp.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf8')
        tmp.replace(out_path)  # Atomic replace only after all chains and pricing succeed.
    priced_rows = sum(row.get('usd') is not None for row in result['last']['rows'])
    print(f'Snapshot OK: ${result["last"]["totalUsd"]:,.2f} tracked; {priced_rows} priced rows; {result["last"]["counts"]["unpriced"]} unpriced token balances')
    if isinstance(result.get('capitalAudit'), dict):
        audit=result['capitalAudit']
        print(f'Capital audit: {audit.get("status","unknown")}; {len(audit.get("flows",[]))} external transfer(s) recorded; {len(audit.get("pending",[]))} pending review.')
    print(f'Alchemy HTTP requests this update (including retries): {_ALCHEMY_CALLS}')


if __name__ == '__main__':
    try:
        main()
    except (SnapshotError, json.JSONDecodeError, OSError, ValueError) as e:
        print('SNAPSHOT FAILED: ' + str(e), file=sys.stderr)
        sys.exit(1)
