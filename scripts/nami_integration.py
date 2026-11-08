#!/usr/bin/env python3
"""Verified Nami NFT valuation and a one-time, provenance-checked coverage bridge.

Never infer capital flows from NFT transfers. Nami must be wallet-owned, its
official manager/factory/pair must match and USD quotes must be available.
A bridge is NOT a deposit; it merely prevents an LP omitted from older
snapshots from creating artificial investment profit when discovered.
"""
from __future__ import annotations
from datetime import datetime, timezone
from decimal import Decimal
import math

import nami_preview as nami


def _utc(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            raise ValueError('Timestamp lacks timezone')
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError, AttributeError) as exc:
        raise nami.NamiError('Invalid timestamp for Nami coverage decision') from exc


def verified_nami_rows(rpc, owner, quotes, refs=None):
    """Always fail closed on partial on-chain/price coverage."""
    refs = nami.load_position_refs() if refs is None else refs
    rows = []
    for ref in refs:
        ref = nami.validated_position_ref(ref)
        if ref['manager'] != nami.NAMI_MANAGER:
            raise nami.NamiError('Non-Nami NFT reference cannot be incorporated into audited total')
        position = nami.read_nft(rpc, owner, ref['manager'], ref['tokenId'])
        if not position['dexAttributionVerified'] or position['protocol'] != 'Nami':
            raise nami.NamiError('Nami NFT contract/pool provenance failed')
        components = []
        total = Decimal('0')
        for contract in (nami.WETH, nami.CBBTC):
            amount = position['amounts'][contract]
            price = quotes.get('base:' + contract)
            if not isinstance(amount, Decimal) or not amount.is_finite() or amount < 0:
                raise nami.NamiError('Invalid Nami LP token quantity')
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise nami.NamiError('Nami LP token price unavailable; preserving previous snapshot')
            subtotal = amount * price
            total += subtotal
            components.append({'symbol': nami.TOKENS[contract][0], 'amount': str(amount),
                               'usd': nami.s.usd(subtotal)})
        if not total.is_finite() or total < 0:
            raise nami.NamiError('Invalid Nami position valuation')
        rows.append({'id': position['id'], 'chain': 'base', 'type': 'lp',
                     'protocol': 'Nami', 'name': 'WETH / cbBTC', 'symbol': 'LP',
                     'usd': nami.s.usd(total), 'underlying': components,
                     'source': 'verified Nami Algebra NFT', 'url': position['url'],
                     'nftId': position['tokenId']})
    return rows


def _source_timestamp(rpc, owner, ref):
    """Only bridge assets proved present *before* the last pre-upgrade reading."""
    txhash = ref.get('sourceTx')
    if not txhash:
        raise nami.NamiError('Nami NFT discovery transaction missing: cannot safely establish coverage bridge')
    receipt = rpc.call('eth_getTransactionReceipt', [txhash])
    if not isinstance(receipt, dict) or receipt.get('status') != '0x1':
        raise nami.NamiError('Nami NFT source transaction receipt unavailable')
    evidence = nami.candidates_from_receipt(receipt, owner, txhash)
    if not any(r['manager'] == ref['manager'] and r['tokenId'] == ref['tokenId'] for r in evidence):
        raise nami.NamiError('Nami receipt does not establish NFT custody at source transaction')
    number = receipt.get('blockNumber')
    if not isinstance(number, str) or not number.startswith('0x'):
        raise nami.NamiError('Nami mint block number unavailable')
    block = rpc.call('eth_getBlockByNumber', [number, False])
    if not isinstance(block, dict) or not isinstance(block.get('timestamp'), str):
        raise nami.NamiError('Nami mint timestamp unavailable')
    try:
        at = datetime.fromtimestamp(int(block['timestamp'], 16), timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise nami.NamiError('Nami mint timestamp malformed') from exc
    return at


def coverage_bridges(existing, rows, refs, rpc, owner, current_at):
    """Determine only *newly discovered, pre-existing* Nami values.

    If the NFT was opened after the previous reading, incoming LP value
    compensates a wallet debit and is NOT an artificial coverage increase.
    """
    prior = (existing.get('last') or {}).get('at')
    if not prior:
        return []
    previously = {(r.get('id')) for r in (existing.get('last', {}).get('rows') or [])
                  if isinstance(r, dict) and r.get('type') == 'lp' and r.get('usd') is not None}
    reference = {(r['manager'], r['tokenId']): r for r in refs}
    result = []
    for row in rows:
        if row['id'] in previously:
            continue
        token_id = str(row['nftId'])
        ref = reference.get((nami.NAMI_MANAGER, token_id))
        if ref is None:
            raise nami.NamiError('Missing verified Nami reference for coverage bridge')
        source_at = _source_timestamp(rpc, owner, ref)
        if source_at <= _utc(prior):
            # The immediately preceding snapshot can be *after* minting yet
            # omit this NFT because an older tracker had no Nami adapter.
            # Never subtract the NFT's value if a complete tracked-wallet
            # observation predates its creation: its original wallet assets
            # were already valued, so converting them into an LP is internal.
            observations = (existing.get('observations') or [])
            complete_before_mint = False
            for observation in observations:
                if not isinstance(observation, dict) or observation.get('complete') is False:
                    continue
                try:
                    observed_at = _utc(observation['at'])
                except (KeyError, TypeError, ValueError, nami.NamiError):
                    continue
                if observed_at < source_at:
                    complete_before_mint = True
                    break
            value = 0.0 if complete_before_mint else float(row['usd'])
            if not math.isfinite(value) or value < 0:
                raise nami.NamiError('Invalid Nami coverage adjustment value')
            result.append({'at': current_at, 'usd': value, 'id': row['id'],
                           'sourceAt': source_at.isoformat(timespec='seconds').replace('+00:00','Z'),
                           'reason': ('Nami minted after last complete observation; gap only, no capital adjustment'
                                      if complete_before_mint else
                                      'previously untracked, verified Nami NFT; not external capital')})
    return result


def mark_coverage_gaps(observations, bridges, baseline_at):
    """Retain pre-upgrade readings but exclude those missing a discovered NFT.

    If the NFT predates the audited baseline, do not pretend historical P&L
    was ever complete: a separate audited migration would be required.
    """
    for bridge in bridges:
        when = bridge.get('sourceAt')
        if not when:
            continue
        if baseline_at and _utc(when) <= _utc(baseline_at):
            raise nami.NamiError('Nami position predates audited baseline; historical coverage cannot be repaired automatically')
        for item in observations:
            if not isinstance(item, dict) or not item.get('at'):
                continue
            stamp = str(item['at'])
            if _utc(when) <= _utc(stamp) < _utc(bridge['at']):
                item['complete'] = False
                item['coverageGap'] = 'Nami NFT not yet counted'
    return observations
