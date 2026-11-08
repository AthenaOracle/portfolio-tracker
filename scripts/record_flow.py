#!/usr/bin/env python3
"""Optionally record one external deposit/withdrawal to the daily performance ledger."""
from pathlib import Path
from datetime import date
import argparse, json, math, sys

ROOT=Path(__file__).resolve().parents[1]

def main():
    p=argparse.ArgumentParser(description='Log deposits (+) or withdrawals (-) in USD, but not internal LP trades')
    p.add_argument('--date', default=date.today().isoformat(),help='UTC YYYY-MM-DD of external transfer')
    p.add_argument('--usd',type=float,help='USD value at transfer time: positive deposit, negative withdrawal')
    p.add_argument('--note',default='',help='Optional short label (public if ledger committed)')
    p.add_argument('--start',action='store_true',help='Mark this date as the beginning of complete cash flow tracking')
    args=p.parse_args()
    date.fromisoformat(args.date)
    if args.usd is None and not args.start: raise ValueError('Provide --usd for a cash flow, or --start to set the baseline')
    if args.usd is not None and (not math.isfinite(args.usd) or args.usd == 0): raise ValueError('Flow value must be nonzero and finite')
    path=ROOT/'cashflows.json'
    data=json.loads(path.read_text(encoding='utf8')) if path.exists() else {'completeFrom':'','flows':[]}
    if args.start:
        data['completeFrom']=args.date
    if args.usd is not None:
        data.setdefault('flows',[]).append({'date':args.date,'usd':round(args.usd,2),'note':args.note[:140]})
    tmp=path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data,indent=2)+'\n',encoding='utf8')
    tmp.replace(path)
    if args.usd is not None: print(f'Saved external cash flow {args.usd:+.2f} USD on {args.date}. Internal swaps and LP moves should NOT be recorded.')
    if args.start: print(f'Performance baseline set to {args.date}. Use only if all later deposits/withdrawals will be logged.')

if __name__=='__main__':
    try:main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print('FLOW ERROR:',exc,file=sys.stderr);sys.exit(1)
