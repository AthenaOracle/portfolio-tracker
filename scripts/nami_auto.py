"""Read-only Nami Algebra ERC721 discovery and rollover reconciliation.

Only the official manager is supported. Verify current custody on-chain and
verify transfer receipts before valuing new NFTs; never infer deposits from NFTs.
"""
from __future__ import annotations
import re
from urllib.parse import urlencode

ZERO_ADDRESS = '0x' + '0' * 40
MAX_TRANSFERS = 800
MAX_NFT_PAGES = 10


def _token_id(value):
    if isinstance(value, bool):
        raise ValueError('Boolean token ID')
    if isinstance(value, int):
        num = value
    elif isinstance(value, str) and re.fullmatch(r'0[xX][0-9a-fA-F]+|[0-9]+', value):
        num = int(value, 16 if value.lower().startswith('0x') else 10)
    else:
        raise ValueError('Invalid NFT token ID')
    if not 0 < num < 2**256:
        raise ValueError('NFT token ID out of range')
    return str(num)


def _current_owner(rpc, nami, token_id):
    try:
        words = nami.s.abi_words(rpc.eth_call(nami.NAMI_MANAGER,
            nami.OWNER_OF + nami.s.abi_uint(int(token_id))))
    except nami.s.SnapshotError as error:
        # The previously verified ID was burned; do not mistake arbitrary RPC
        # errors for a closed position.
        if 'invalid token id' in str(error).lower() or 'nonexistent token' in str(error).lower():
            return None, True
        raise
    if len(words) != 1 or words[0] >= 2**160:
        raise nami.NamiError('Nami ownerOf returned an invalid response')
    return nami.s.extract_address(words[0]).lower(), False


def _incoming_transfers(rpc, owner, nami):
    """Complete paginated incoming history for one NFT manager (not arbitrary wallets)."""
    cursor = None
    seen = set()
    found = {}
    for _ in range(10):
        opts = {'fromBlock': '0x0', 'toBlock': 'latest', 'toAddress': owner,
                'contractAddresses': [nami.NAMI_MANAGER], 'category': ['erc721'],
                'excludeZeroValue': False, 'maxCount': '0x64', 'order': 'desc'}
        if cursor:
            opts['pageKey'] = cursor
        data = rpc.call('alchemy_getAssetTransfers', [opts])
        if not isinstance(data, dict) or not isinstance(data.get('transfers'), list):
            raise nami.NamiError('Incomplete Nami incoming NFT transfer response')
        for row in data['transfers']:
            if not isinstance(row, dict):
                continue
            address = (row.get('rawContract') or {}).get('address')
            if str(address).lower() != nami.NAMI_MANAGER:
                continue
            if str(row.get('to', '')).lower() != owner:
                continue
            tx = row.get('hash')
            if not isinstance(tx, str) or not nami.HASH.fullmatch(tx):
                raise nami.NamiError('Nami NFT transfer missing transaction hash')
            try:
                token = _token_id(row.get('erc721TokenId') or row.get('tokenId'))
            except ValueError as error:
                raise nami.NamiError('Nami NFT transfer had malformed token ID') from error
            # With descending order, first is the latest incoming reference.
            found.setdefault(token, {'manager': nami.NAMI_MANAGER, 'tokenId': token, 'sourceTx': tx.lower()})
            if len(found) > MAX_TRANSFERS:
                raise nami.NamiError('Nami transfer history safety cap exceeded')
        cursor = data.get('pageKey')
        if not cursor:
            return found
        if not isinstance(cursor, str) or cursor in seen:
            raise nami.NamiError('Invalid Nami transfer pagination token')
        seen.add(cursor)
    raise nami.NamiError('Nami transfer history not fully paginated; refusing partial total')


def _owned_inventory(key, owner, nami):
    """Contract-filtered NFT enumeration, unlike old first-18 generic scan."""
    cursor = None
    seen = set()
    ids = set()
    for _ in range(MAX_NFT_PAGES):
        args = {'owner': owner, 'contractAddresses[]': nami.NAMI_MANAGER,
                'withMetadata': 'false', 'pageSize': 100}
        if cursor:
            args['pageKey'] = cursor
        url = f'https://base-mainnet.g.alchemy.com/nft/v3/{key}/getNFTsForOwner?' + urlencode(args)
        data = nami.s.api_json(url)
        if not isinstance(data, dict) or not isinstance(data.get('ownedNfts'), list):
            raise nami.NamiError('Incomplete Nami NFT inventory response')
        for item in data['ownedNfts']:
            if not isinstance(item, dict):
                continue
            address = (item.get('contract') or {}).get('address')
            if str(address).lower() != nami.NAMI_MANAGER:
                continue
            try:
                ids.add(_token_id(item.get('tokenId')))
            except ValueError as error:
                raise nami.NamiError('Malformed Nami NFT inventory token ID') from error
        cursor = data.get('pageKey')
        if not cursor:
            return ids
        if not isinstance(cursor, str) or cursor in seen:
            raise nami.NamiError('Nami NFT inventory pagination cycle')
        seen.add(cursor)
    raise nami.NamiError('Nami NFT inventory incomplete; refusing partial total')


def discover_active(rpc, wallet, key, saved, previous_rows, nami):
    """Return verified active Nami refs and burned IDs; never silently drop a valued NFT.

    Incoming ERC721 transfer history provides source receipts. A second
    contract-filtered NFT inventory helps catch transfer indexing omissions.
    Neither index is trusted for custody; ownerOf and read_nft decide that.
    """
    owner = wallet.lower()
    known = {nami.validated_position_ref(r)['tokenId']: nami.validated_position_ref(r)
             for r in saved}
    transfer_refs = _incoming_transfers(rpc, owner, nami)
    inventory = _owned_inventory(key, owner, nami)
    token_ids = set(known) | set(transfer_refs) | inventory
    if len(token_ids) > MAX_TRANSFERS:
        raise nami.NamiError('Too many Nami token IDs; review before continuing')
    active = []
    burned = set()
    transferred_out = set()
    for token_id in sorted(token_ids, key=int):
        current, was_burned = _current_owner(rpc, nami, token_id)
        if was_burned:
            burned.add(token_id)
            continue
        if current != owner:
            transferred_out.add(token_id)
            continue
        ref = transfer_refs.get(token_id) or known.get(token_id)
        if ref is None or not ref.get('sourceTx'):
            raise nami.NamiError(f'Wallet owns Nami NFT {token_id} but mint/transfer source transaction is missing')
        # A valid ID and owner are insufficient. The exact source receipt must
        # prove transfer to this wallet, then check factory and pool provenance.
        tx = ref['sourceTx']
        receipt = rpc.call('eth_getTransactionReceipt', [tx])
        if not isinstance(receipt, dict) or receipt.get('status') not in ('0x1', '0x01'):
            raise nami.NamiError(f'Could not verify source receipt for Nami NFT {token_id}')
        if not any(r['manager'] == nami.NAMI_MANAGER and r['tokenId'] == token_id
                   for r in nami.candidates_from_receipt(receipt, owner, tx)):
            raise nami.NamiError(f'On-chain receipt does not prove Nami NFT {token_id} custody')
        position = nami.read_nft(rpc, owner, nami.NAMI_MANAGER, token_id)
        if not position['dexAttributionVerified'] or position['protocol'] != 'Nami':
            raise nami.NamiError(f'Nami NFT {token_id} has unverified pool or tokens')
        active.append(ref)
    for row in previous_rows:
        if not isinstance(row, dict) or row.get('protocol') != 'Nami' or row.get('usd') is None:
            continue
        old_id = str(row.get('nftId', ''))
        if old_id not in {r['tokenId'] for r in active} and old_id not in burned:
            raise nami.NamiError('Previously valued Nami NFT transferred or missing without verified burn; preserving snapshot')
    # NFT ownership inventory can be stale during a rebalance, but if it claims
    # an ID was owned and the transfer index omits that ID, fail closed above.
    return active, burned
