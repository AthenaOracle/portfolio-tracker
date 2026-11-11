#!/usr/bin/env python3
"""Optional *read-only* Base Algebra WETH/cbBTC LP valuation.

A transaction that the user identifies as their Nami mint can provide the NFT
manager and token ID. The position is used only when on-chain ownerOf, Algebra
positions, factory/pool correspondence and token addresses all match. Nothing
here changes data.json, the audited P&L, or the capital transfer ledger.

Standard library only. API key and wallet address are loaded from local .env;
neither is written to the preview or printed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode

import snapshot as s

ROOT = Path(__file__).resolve().parents[1]
POSITIONS_PATH = ROOT / 'nami-positions.json'
PREVIEW_PATH = ROOT / 'nami-preview.json'
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
OWNER_OF = '6352211e'
FACTORY = 'c45a0155'
POOL_BY_PAIR = 'd9a641e1'
V3_POOL_BY_FEE = '1698ee82'  # getPool(address,address,uint24)
V3_FEE = 'ddca3f43'  # fee() on a Uniswap-v3-style pool
V3_SLOT0 = '3850c7bd'  # slot0()
GLOBAL_STATE = 'e76c01e4'
TOKEN0 = '0dfe1681'
TOKEN1 = 'd21220a7'
WETH = '0x4200000000000000000000000000000000000006'
CBBTC = '0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf'
EXPECTED = {WETH, CBBTC}
# Nami's official Base Algebra contracts; this allows an evidence-backed
# Nami-specific ABI interpretation without loosening other NFT checks.
NAMI_MANAGER = '0xd0f883ea4e7596705522d3ab5589d0c582de99be'
NAMI_FACTORY = '0xf4502a98a15e82d66d28d2381e2f3a407ea45b83'
TOKENS = {
    WETH: ('WETH', 18),
    CBBTC: ('cbBTC', 8),
}
HASH = re.compile(r'^0x[0-9a-fA-F]{64}$')
MAX_POSITIONS = 12


class NamiError(ValueError):
    pass


def validated_position_ref(item):
    if not isinstance(item, dict) or not s.ADDRESS.fullmatch(str(item.get('manager',''))):
        raise NamiError('Invalid Algebra position manager address')
    ident = item.get('tokenId')
    if type(ident) not in (int, str) or not str(ident).isdigit() or not (0 < int(ident) < 2**256):
        raise NamiError('Invalid Algebra NFT token ID')
    tx = item.get('sourceTx')
    if tx is not None and (not isinstance(tx,str) or not HASH.fullmatch(tx)):
        raise NamiError('Invalid supporting transaction hash')
    return {'manager': item['manager'].lower(), 'tokenId':str(int(ident)),
            **({'sourceTx':tx.lower()} if tx else {})}


def load_position_refs(path=POSITIONS_PATH):
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding='utf8'))
    if not isinstance(payload, dict) or payload.get('schema') != 1 or not isinstance(payload.get('positions'), list):
        raise NamiError('Invalid nami-positions.json; previous preview preserved')
    if len(payload['positions']) > MAX_POSITIONS:
        raise NamiError('Too many Nami NFTs; check configuration')
    refs = [validated_position_ref(p) for p in payload['positions']]
    if len({(p['manager'],p['tokenId']) for p in refs}) != len(refs):
        raise NamiError('Duplicate Nami position references')
    return refs


def atomic_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(obj,indent=2,ensure_ascii=False) + '\n',encoding='utf8')
    tmp.replace(path)


def address_arg(word):
    return s.abi_address(word)


def _contract_address(words, context):
    if len(words) != 1 or words[0] >= 2**160 or words[0] == 0:
        raise NamiError(f'{context} returned an invalid address')
    return s.extract_address(words[0]).lower()


def read_nft(rpc, wallet, manager, token_id):
    """Verify custody, pair and live pool for supported Algebra or V3-style NFTs.

    Nami documents Algebra Integral, but the receipt can also contain unrelated
    ERC721s. A V3-style NFT is verified as a real WETH/cbBTC position only,
    and is NOT asserted to be issued by Nami without external provenance.
    """
    owner_words = s.abi_words(rpc.eth_call(manager, OWNER_OF + s.abi_uint(int(token_id))))
    if len(owner_words) != 1 or s.extract_address(owner_words[0]).lower() != wallet.lower():
        raise NamiError('Position NFT not currently owned by the configured wallet; skipped')
    words = s.abi_words(rpc.eth_call(manager, s.POSITIONS + s.abi_uint(int(token_id))))
    if len(words) not in (11, 12):
        raise NamiError(f'Unsupported position NFT response ({len(words)} ABI fields); run --diagnose-tx to identify it')
    # Both known layouts use the same first four fields. Only accept actual ABI
    # addresses, not arbitrary 256-bit integers truncated to 160 bits.
    if words[2] >= 2**160 or words[3] >= 2**160:
        raise NamiError('Position token addresses are malformed')
    t0,t1 = s.extract_address(words[2]).lower(), s.extract_address(words[3]).lower()
    if {t0,t1} != EXPECTED or t0 == t1:
        raise NamiError('NFT is not the configured WETH/cbBTC pair')
    is_official_nami = manager.lower() == NAMI_MANAGER
    if len(words) == 11:
        interface='algebra-11'; fee = None
        lower_idx, upper_idx, liquidity_idx, owed0_idx, owed1_idx = 4,5,6,9,10
    elif is_official_nami:
        # The live Nami manager returned 12 ABI words for NFT 307. Its fifth
        # word is NOT proven to be a Uniswap V3 fixed-fee tier. Safely accept
        # this extended layout *only* for Nami's documented manager and factory,
        # with independently checked token pair, ticks, liquidity, pool and price.
        # Do not infer the optional fifth word's meaning or count it as fees.
        interface='nami-algebra-12'; fee = None
        lower_idx, upper_idx, liquidity_idx, owed0_idx, owed1_idx = 5,6,7,10,11
    else:
        # Standard Uniswap V3 periphery ABI: nonce, operator, token0, token1,
        # fee, tickLower, tickUpper, liquidity, feeGrowth0, feeGrowth1, owed0, owed1.
        interface='v3-12'; fee = words[4]
        lower_idx, upper_idx, liquidity_idx, owed0_idx, owed1_idx = 5,6,7,10,11
        if not (0 < fee < 1_000_000):
            raise NamiError('V3-style NFT has an invalid fee tier')
    low, high, liquidity = s.signed(words[lower_idx]), s.signed(words[upper_idx]),words[liquidity_idx]
    owed0,owed1 = words[owed0_idx],words[owed1_idx]
    if not (-887272 <= low < high <= 887272) or liquidity >= 2**128 or owed0 >= 2**128 or owed1 >= 2**128:
        raise NamiError('Malformed liquidity position')
    factory_words = s.abi_words(rpc.eth_call(manager,FACTORY))
    factory = _contract_address(factory_words, 'Position manager factory')
    if is_official_nami and factory != NAMI_FACTORY:
        raise NamiError('Official Nami position manager returned an unexpected factory')
    if interface in ('algebra-11','nami-algebra-12'):
        lookup = POOL_BY_PAIR + address_arg(t0) + address_arg(t1)
    else:
        lookup = V3_POOL_BY_FEE + address_arg(t0) + address_arg(t1) + s.abi_uint(fee)
    pool = _contract_address(s.abi_words(rpc.eth_call(factory,lookup)), 'Liquidity pool lookup')
    if _contract_address(s.abi_words(rpc.eth_call(pool,TOKEN0)), 'Pool token0') != t0 or \
       _contract_address(s.abi_words(rpc.eth_call(pool,TOKEN1)), 'Pool token1') != t1:
        raise NamiError('On-chain pool tokens do not match NFT position')
    if interface in ('algebra-11','nami-algebra-12'):
        global_words = s.abi_words(rpc.eth_call(pool,GLOBAL_STATE))
        if len(global_words) < 2:
            raise NamiError('Algebra globalState response unavailable')
    else:
        pool_fee = s.abi_words(rpc.eth_call(pool,V3_FEE))
        if len(pool_fee) != 1 or pool_fee[0] != fee:
            raise NamiError('V3-style pool fee does not match position')
        global_words = s.abi_words(rpc.eth_call(pool,V3_SLOT0))
        if len(global_words) < 2:
            raise NamiError('V3-style slot0 response unavailable')
    if not 0 < global_words[0] < 2**160:
        raise NamiError('Liquidity pool price unavailable')
    # Owed fees last accounted for by the position manager only. Unaccrued
    # fees are intentionally excluded so reported values are conservative.
    raw0,raw1 = s.v3_amounts(liquidity,low,high,global_words[0])
    amounts = {t0:(raw0 + Decimal(owed0))/Decimal(10**TOKENS[t0][1]),
               t1:(raw1 + Decimal(owed1))/Decimal(10**TOKENS[t1][1])}
    return {'id':f'base:cl-nft:{manager}:{token_id}',
            'chain':'base','protocol':('Nami' if is_official_nami else 'Concentrated liquidity NFT (attribution unverified)'),
            'type':'lp','name':'WETH / cbBTC',
            'interface':interface,'dexAttributionVerified':bool(is_official_nami and factory == NAMI_FACTORY),
            'manager':manager,'tokenId':str(token_id),'pool':pool,
            'amounts':amounts,
            'url':f'https://basescan.org/nft/{manager}/{token_id}'}


def diagnose_tx(rpc, wallet, txhash):
    """Show only public NFT references, detected ABI field counts and custody.

    No raw RPC response, wallet address or credential is printed or written.
    """
    receipt=rpc.call('eth_getTransactionReceipt',[txhash])
    candidates=candidates_from_receipt(receipt,wallet,txhash)
    if not candidates:
        print('No ERC721 transfer to the configured wallet in the transaction.')
        return 0
    for ref in candidates[:12]:
        manager, token_id = ref['manager'],ref['tokenId']
        print(f'NFT {token_id}: contract {manager} · https://basescan.org/nft/{manager}/{token_id}')
        try:
            owner_words=s.abi_words(rpc.eth_call(manager,OWNER_OF+s.abi_uint(int(token_id))))
            matches=len(owner_words)==1 and s.extract_address(owner_words[0]).lower()==wallet.lower()
            print(f'  Owned by configured wallet: {matches}')
            if not matches: continue
            words=s.abi_words(rpc.eth_call(manager,s.POSITIONS+s.abi_uint(int(token_id))))
            print(f'  positions(uint256): {len(words)} ABI fields')
            if len(words) in (11,12):
                print(f'  Candidate pair: {s.extract_address(words[2])} / {s.extract_address(words[3])}')
                label = 'Algebra 11-field' if len(words)==11 else ('Nami extended 12-field' if manager.lower()==NAMI_MANAGER else 'V3-style 12-field')
                print(f'  Format: {label}')
                if len(words)==12 and manager.lower()==NAMI_MANAGER:
                    print('  Extended field #5 (not assumed to be a fee tier): '+str(words[4]))
            else:
                print('  Not a known 11/12-field position layout; do not classify without ABI review.')
            try:
                record=read_nft(rpc,wallet,manager,token_id)
                print('  On-chain valuation verification: PASSED ('+record['interface']+')')
            except (NamiError,s.SnapshotError,IndexError,ValueError) as exc:
                print('  On-chain valuation verification: '+str(exc)[:180])
        except (NamiError,s.SnapshotError,IndexError,TypeError,ValueError) as exc:
            print('  Diagnosis unavailable: '+str(exc)[:180])
    print('Diagnostic only: no files changed, no transfer approvals, no capital/P&L changes.')
    return 0


def candidates_from_receipt(receipt, wallet, txhash):
    """ERC721 Transfer with 4 indexed fields TO the wallet; no guess from ERC20 logs."""
    if not isinstance(receipt, dict) or receipt.get('status') not in ('0x1','0x01'):
        raise NamiError('Nami candidate transaction is not confirmed successful')
    result = []
    for event in receipt.get('logs',[]):
        if not isinstance(event,dict): continue
        topics=event.get('topics')
        if not isinstance(topics,list) or len(topics)!=4 or str(topics[0]).lower()!=TRANSFER_TOPIC:
            continue
        manager=event.get('address','')
        if not isinstance(manager,str) or not s.ADDRESS.fullmatch(manager): continue
        try:
            to=s.extract_address(int(topics[2],16)).lower()
            token_id=int(topics[3],16)
        except (TypeError,ValueError): continue
        if to == wallet.lower() and token_id>0:
            result.append({'manager':manager.lower(),'tokenId':str(token_id),'sourceTx':txhash.lower()})
    return list({(r['manager'],r['tokenId']):r for r in result}.values())


def collect_verified(rpc, wallet, references, strict=True):
    found=[]; errors=[]
    for ref in references:
        try:
            record=read_nft(rpc,wallet,ref['manager'],ref['tokenId'])
            record['sourceTx']=ref.get('sourceTx')
            found.append(record)
        except (s.SnapshotError,NamiError,IndexError,TypeError,ValueError) as exc:
            errors.append(f'NFT {ref["tokenId"]}: {str(exc)[:110]}')
    if errors and strict:
        raise NamiError('; '.join(errors[:3])+'; previous preview preserved')
    return found,errors


def build_preview(found, at=None, price_fetcher=None):
    if not found:
        raise NamiError('No verified WETH/cbBTC Algebra NFTs; previous preview preserved')
    price_fetcher = price_fetcher or s.fetch_prices
    quotes=price_fetcher(['base:'+t for t in sorted(EXPECTED)])
    prices={t:quotes.get('base:'+t) for t in EXPECTED}
    if any(p is None or not isinstance(p,Decimal) or not p.is_finite() or p<=0 for p in prices.values()):
        raise NamiError('Missing WETH/cbBTC market price; previous preview preserved')
    positions=[]; total=Decimal(0)
    for pos in found:
        value=Decimal(0); components=[]
        for token in [WETH,CBBTC]:
            amount=pos['amounts'][token]
            if not amount.is_finite() or amount<0: raise NamiError('Invalid underlying token amount')
            usd=amount*prices[token]; value+=usd
            components.append({'symbol':TOKENS[token][0], 'amount':str(amount),'usd':s.usd(usd)})
        total+=value
        positions.append({'id':pos['id'],'name':pos['name'],'protocol':pos['protocol'],
                          'type':'lp','chain':'base','usd':s.usd(value),
                          'interface':pos['interface'],'dexAttributionVerified':pos['dexAttributionVerified'],
                          'underlying':components,'url':pos['url'],'tokenId':pos['tokenId']})
    return {'schema':1,'provider':'Nami on-chain (Alchemy)','chain':'base',
            'at':at or datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z'),
            'status':'verified-position','totalUsd':s.usd(total), 'positionCount':len(positions),
            'positions':positions,'includedInAuditedTotal':False,
            'note':'Verified NFT custody/pair/pool; Nami attribution only for official manager and factory. NOT in audited total/P&L or market-close comparison. Unaccrued fees excluded.'}


def scan_wallet_nfts(rpc, key, wallet, max_checked=18):
    """Read-only current Nami holdings; contract-filtered and fully paginated.

    No generic-first-18 cap and no reliance on a stale local NFT reference.
    """
    from nami_auto import _incoming_transfers, _owned_inventory, _current_owner
    refs = _incoming_transfers(rpc, wallet.lower(), sys.modules[__name__])
    ids = set(refs) | _owned_inventory(key, wallet.lower(), sys.modules[__name__])
    recognized = []
    for token_id in sorted(ids, key=int):
        current, burned = _current_owner(rpc, sys.modules[__name__], token_id)
        if burned or current != wallet.lower():
            continue
        record = read_nft(rpc, wallet, NAMI_MANAGER, token_id)
        if not record['dexAttributionVerified']:
            raise NamiError('Nami position provenance verification failed')
        recognized.append(record)
    return recognized, False


def main():
    parser=argparse.ArgumentParser(description='Verify and value Base Nami WETH/cbBTC Algebra NFTs without changing audited P&L')
    parser.add_argument('--diagnose-tx',help='Read-only NFT contract/ABI diagnostic for a Nami-related transaction; does not save a position')
    parser.add_argument('--tx',help='Transaction HASH known to have created/transferred your Nami LP NFT; used for first-time discovery')
    parser.add_argument('--scan-nfts',action='store_true',help='Inspect up to 18 recent wallet-owned NFTs for Algebra WETH/cbBTC candidates; preview only')
    parser.add_argument('--manager',help='Confirmed NFT manager contract address from your Nami LP')
    parser.add_argument('--token-id',help='Confirmed Nami LP NFT token ID')
    args=parser.parse_args()
    if args.tx and not HASH.fullmatch(args.tx): raise NamiError('--tx must be a valid Base transaction hash')
    if args.diagnose_tx and not HASH.fullmatch(args.diagnose_tx): raise NamiError('--diagnose-tx must be a valid Base transaction hash')
    if args.diagnose_tx and (args.tx or args.manager or args.scan_nfts): raise NamiError('Use one discovery/diagnostic method at a time')
    if (args.manager is None) != (args.token_id is None): raise NamiError('--manager and --token-id must be supplied together')
    if args.scan_nfts and (args.tx or args.manager): raise NamiError('Use one discovery method at a time')
    s.load_env(ROOT/'.env')
    key=os.getenv('ALCHEMY_KEY','').strip(); wallet=os.getenv('WALLET_ADDRESS','').strip().lower()
    if not key or not s.ADDRESS.fullmatch(wallet):
        raise NamiError('Wallet/Alchemy settings missing. Run setup.bat first.')
    rpc=s.RPC('base',key)
    refs=load_position_refs()
    if args.diagnose_tx:
        return diagnose_tx(rpc,wallet,args.diagnose_tx)
    if args.scan_nfts:
        candidates,partial=scan_wallet_nfts(rpc,key,wallet)
        if candidates:
            print('Potential wallet-owned Algebra WETH/cbBTC NFTs (confirm their contract matches Nami before saving):')
            for item in candidates:
                print(f'  Manager {item["manager"]} · token ID {item["tokenId"]} · {item["url"]}')
        else:
            print('No wallet-owned Algebra WETH/cbBTC NFT detected in the bounded NFT inventory.')
        if partial: print('NFT inventory may be incomplete (bounded to recent 100 NFTs and first 18 valid candidates).')
        print('READ-ONLY: no NFT approved, no portfolio changes. To use a confirmed Nami NFT, run --manager ADDRESS --token-id ID.')
        return 0
    if args.manager is not None:
        ref=validated_position_ref({'manager':args.manager,'tokenId':args.token_id})
        new={ (p['manager'],p['tokenId']):p for p in refs+[ref] }
        if len(new)>MAX_POSITIONS: raise NamiError('Too many position refs; files unchanged')
        refs=list(new.values())
        found,_=collect_verified(rpc,wallet,refs,strict=True)
        preview=build_preview(found)
        atomic_json(POSITIONS_PATH,{'schema':1,'positions':refs})
    elif args.tx:
        receipt=rpc.call('eth_getTransactionReceipt',[args.tx])
        candidates=candidates_from_receipt(receipt,wallet,args.tx)
        if not candidates:
            raise NamiError('No ERC721 transferred to your wallet in that transaction. The Nami NFT may have been minted in another transaction or held by a smart account.')
        verified,errs=collect_verified(rpc,wallet,candidates,strict=False)
        if not verified:
            raise NamiError('NFT events found but none prove a wallet-owned Algebra WETH/cbBTC position. '+ '; '.join(errs[:2]))
        newrefs = [validated_position_ref(x) for x in candidates if any(x['manager']==v['manager'] and x['tokenId']==v['tokenId'] for v in verified)]
        refs_by_id={(p['manager'],p['tokenId']):p for p in refs+newrefs}
        if len(refs_by_id)>MAX_POSITIONS: raise NamiError('Too many candidate positions; no files changed')
        refs=list(refs_by_id.values())
        # Prove every listed position before accepting additions.
        found,_=collect_verified(rpc,wallet,refs,strict=True)
        preview=build_preview(found)
        atomic_json(POSITIONS_PATH,{'schema':1,'positions':refs})
    elif refs:
        found,_=collect_verified(rpc,wallet,refs,strict=True)
        preview=build_preview(found)
    else:
        print('No saved Nami NFTs yet. Use --tx with the transaction that minted/transferred your Nami position.')
        print('No data modified. Existing P&L unchanged.')
        return 0
    atomic_json(PREVIEW_PATH,preview)
    print(f'Nami on-chain preview: {len(preview["positions"])} verified WETH/cbBTC NFT(s); ${preview["totalUsd"]:,.2f} supplementary LP value.')
    print('Not added to tracked portfolio total or audited P&L. No blockchain transactions were sent.')
    return 0

if __name__=='__main__':
    try: raise SystemExit(main())
    except (NamiError,s.SnapshotError,OSError,ValueError,json.JSONDecodeError) as exc:
        print('NAMI PREVIEW NOT UPDATED: '+str(exc),file=sys.stderr)
        raise SystemExit(1)
