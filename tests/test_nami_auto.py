import unittest
import types
from decimal import Decimal
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import nami_auto

M = '0xd0f883ea4e7596705522d3ab5589d0c582de99be'
W = '0x1111111111111111111111111111111111111111'
B = '0x2222222222222222222222222222222222222222'
TX = '0x' + 'a' * 64

class MockError(Exception): pass

class MockRPC:
    def __init__(self, owned=('2781',), burned=('307',), malformed=False):
        self.owned=set(owned); self.burned=set(burned); self.malformed=malformed; self.calls=[]
    def call(self, method, params):
        self.calls.append(method)
        if method=='alchemy_getAssetTransfers':
            if self.malformed: return {'transfers':[], 'pageKey':'repeat'}
            return {'transfers':[{'hash':TX,'rawContract':{'address':M},'to':W,'erc721TokenId':hex(int(t))} for t in self.owned]}
        if method=='eth_getTransactionReceipt': return {'status':'0x1','hash':params[0]}
        raise AssertionError((method, params))
    def eth_call(self, manager, data):
        t=str(int(data[-64:], 16))
        if t in self.burned: raise MockError('base: eth_call: RPC error 3: execution reverted: ERC721: invalid token ID')
        return ('%064x' % int(W if t in self.owned else B, 16))

class NamiMock:
    NAMI_MANAGER=M; OWNER_OF='6352211e'; HASH=__import__('re').compile('^0x[0-9a-fA-F]{64}$')
    SnapshotError=MockError; NamiError=MockError
    s=types.SimpleNamespace(SnapshotError=MockError,
        abi_words=lambda x: [int(x,16)], abi_uint=lambda x:'%064x'%int(x),
        extract_address=lambda x:'0x%040x'%x,
        api_json=lambda url: {'ownedNfts':[{'contract':{'address':M},'tokenId':'2781'}]})
    @staticmethod
    def validated_position_ref(r): return r
    @staticmethod
    def candidates_from_receipt(receipt, owner, tx): return [{'manager':M,'tokenId':'2781'}]
    @staticmethod
    def read_nft(rpc, owner, mgr, token): return {'dexAttributionVerified':True, 'protocol':'Nami'}

class TestNamiAuto(unittest.TestCase):
    def test_rollover(self):
        r=MockRPC()
        active,burned=nami_auto.discover_active(r,W,'secret', [{'manager':M,'tokenId':'307','sourceTx':TX}],
            [{'protocol':'Nami','nftId':'307','usd':7.17}],NamiMock)
        self.assertEqual([x['tokenId'] for x in active],['2781'])
        self.assertEqual(burned,{'307'})
    def test_no_missing_active_token(self):
        class IncompleteNami(NamiMock):
            s=types.SimpleNamespace(**{k:v for k,v in vars(NamiMock.s).items() if k!='api_json'},
                api_json=lambda url: {'ownedNfts':[{'contract':{'address':M},'tokenId':'999'}]})
        with self.assertRaises(MockError):
            nami_auto.discover_active(MockRPC(owned=('2781','999')),W,'secret',[],[],IncompleteNami)
    def test_transferred_out_fails_closed(self):
        with self.assertRaises(MockError):
            nami_auto.discover_active(MockRPC(owned=(), burned=()),W,'secret',
                [{'manager':M,'tokenId':'307','sourceTx':TX}], [{'protocol':'Nami','nftId':'307','usd':7}],NamiMock)
    def test_bad_token_id(self):
        for t in ('foo', '-1','0',None):
            with self.assertRaises(ValueError): nami_auto._token_id(t)
    def test_transfers_full_history(self):
        r=MockRPC()
        ids=nami_auto._incoming_transfers(r,W,NamiMock)
        self.assertIn('2781',ids)
        self.assertEqual(ids['2781']['sourceTx'], TX)

if __name__=='__main__': unittest.main()
