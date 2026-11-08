#!/usr/bin/env python3
"""Daily benchmark and cashflow-adjusted performance calculation.

Does not use Alchemy. This is *not* money-weighted IRR, nor investment APR.
Flow adjustment assumes signed external cash flows occur at the end of their
calendar day because V9 history only stores daily closing valuations.
Internal swaps, LP rebalances and claiming emissions are NOT external flows.
"""
import argparse
import bisect
import csv
from datetime import date, datetime, timedelta, timezone
from io import StringIO
import json
import hashlib
import math
import os
from pathlib import Path
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
FRED = 'https://fred.stlouisfed.org/graph/fredgraph.csv?id='
YAHOO_SP500 = 'https://query1.finance.yahoo.com/v8/finance/chart/%5EGSPC?interval=1d&range=3mo'
VERIFIED_SP500 = Path(__file__).resolve().parent / 'verified_sp500_2026_11.json'



def daily_history(data):
    history = data.get('history', [])
    if not isinstance(history, list):
        raise ValueError('Expected data.json history list')
    results = []
    for item in sorted(history, key=lambda row: str(row.get('date', ''))):
        try:
            day = date.fromisoformat(item['date'])
            total = float(item['totalUsd'])
            if not math.isfinite(total) or total < 0:
                continue
            if results and results[-1][0] == day:
                results[-1] = (day, total)
            else:
                results.append((day, total))
        except (TypeError, ValueError, KeyError):
            continue
    return results


def parse_flows(payload):
    if not isinstance(payload, dict):
        raise ValueError('cashflows.json must contain an object')
    start_text = payload.get('completeFrom', '')
    start = date.fromisoformat(start_text) if start_text else None
    flows = {}
    for flow in payload.get('flows', []):
        if not isinstance(flow, dict):
            raise ValueError('Each cashflow must be an object')
        day = date.fromisoformat(flow['date'])
        amount = float(flow['usd'])
        if not math.isfinite(amount) or abs(amount) > 1e12:
            raise ValueError('Invalid cash flow amount')
        flows[day] = flows.get(day, 0.0) + amount
    return start, flows


def portfolio_path(history, coverage_start, flows):
    if coverage_start is None:
        return {}, None, 'Cash-flow log not confirmed. Portfolio return is hidden to avoid treating deposits as gains.'
    relevant = [(d, v) for d, v in history if d >= coverage_start]
    if not relevant:
        return {}, None, 'Waiting for the first snapshot on or after the selected performance start date.'
    baseline = relevant[0][0]
    # A cashflow before the coverage date cannot be known; it is already embedded
    # in the first baseline valuation.
    output = {baseline.isoformat(): 0.0}
    previous, prev_value = relevant[0]
    cumulative = 1.0
    for d, value in relevant[1:]:
        f = sum(a for fd, a in flows.items() if previous < fd <= d)
        if prev_value <= 0 or value - f < 0:
            return {}, baseline, 'Unable to estimate portfolio return from these values/flows. Check the ledger.'
        period_return = (value - f) / prev_value
        if not math.isfinite(period_return) or period_return < 0:
            return {}, baseline, 'Invalid daily return; check the cash-flow ledger.'
        cumulative *= period_return
        output[d.isoformat()] = round((cumulative - 1) * 100, 5)
        previous, prev_value = d, value
    return output, baseline, None


def period_summaries(history, start, flows, portfolio):
    """Daily-snapshot net USD P&L, with external flows subtracted once.

    A window is displayed only when the date at its beginning was actually
    recorded. Returns are time-weighted estimates using closing snapshots,
    not money-weighted IRR and not realized profit only.
    """
    pending = {'1': None, '7': None, '30': None, 'all': None}
    if start is None or not portfolio:
        return pending
    known = [(day, value) for day, value in history if day.isoformat() in portfolio]
    if len(known) < 2:
        return pending
    end_day, end_value = known[-1]
    baseline_day = known[0][0]
    by_day = dict(known)
    results = {}
    for name, days in [('1',1), ('7',7), ('30',30), ('all',None)]:
        beginning = baseline_day if days is None else end_day - timedelta(days=days)
        if beginning not in by_day or beginning >= end_day:
            results[name] = None
            continue
        first_value = by_day[beginning]
        net_flow = sum(amount for day, amount in flows.items() if beginning < day <= end_day)
        profit = round(end_value - first_value - net_flow,2)
        first_index = 1 + portfolio.get(beginning.isoformat(),0)/100
        end_index = 1 + portfolio.get(end_day.isoformat(),0)/100
        percent = round(100*(end_index/first_index-1), 5) if first_index > 0 and end_index >= 0 else None
        results[name] = {'usd':profit, 'pct':percent, 'from':beginning.isoformat(),
                         'to':end_day.isoformat(), 'estimate':True}
    return results


def parse_fred_csv(text):
    reader = csv.DictReader(StringIO(text.lstrip('\ufeff')))
    if not reader.fieldnames or len(reader.fieldnames) < 2:
        raise ValueError('Invalid benchmark CSV header')
    datekey = reader.fieldnames[0]
    valuekey = reader.fieldnames[1]
    data = {}
    for row in reader:
        try:
            d = date.fromisoformat(row[datekey])
            v = float(row[valuekey])
            if math.isfinite(v) and v >= 0:
                data[d] = v
        except (ValueError, TypeError, KeyError):
            continue
    if not data:
        raise ValueError('Benchmark CSV has no values')
    return data


def fetch_fred(series, timeout=11):
    req = Request(FRED + series, headers={'User-Agent': 'PortfolioTracker/1.0 (personal research; two daily series)', 'Accept': 'text/csv'})
    with urlopen(req, timeout=timeout) as response:
        body = response.read(1500000)
    return parse_fred_csv(body.decode('utf-8-sig'))



def parse_yahoo_sp500(payload, now=None):
    """Read *completed* S&P 500 daily-close bars, not today's live partial bar.

    Yahoo is an optional backup for delayed FRED observations; malformed,
    non-US-dollar, out-of-range, or future timestamps are discarded.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('Expected timezone-aware timestamp')
    try:
        if payload['chart'].get('error'):
            raise ValueError('Yahoo market API returned an error')
        result = payload['chart']['result'][0]
        if (result.get('meta', {}).get('currency') or 'USD') != 'USD':
            raise ValueError('Index provider returned unexpected currency')
        timestamps = result['timestamp']
        closes = result['indicators']['quote'][0]['close']
    except (KeyError, TypeError, IndexError) as exc:
        raise ValueError('Invalid S&P 500 provider response') from exc
    if not isinstance(timestamps, list) or not isinstance(closes, list) or len(timestamps) != len(closes):
        raise ValueError('Invalid S&P 500 daily bar arrays')
    latest_completed = us_market_anchor_day(now - timedelta(minutes=30))
    observed = {}
    for t, v in zip(timestamps, closes):
        if not isinstance(t,(int,float)) or isinstance(t,bool):
            continue
        try:
            d = datetime.fromtimestamp(t,timezone.utc).date()
            value = float(v)
            if d > latest_completed or d.weekday() >= 5:
                continue
            if math.isfinite(value) and 100 < value < 100000:
                observed[d] = round(value, 4)
        except (ValueError,TypeError,OverflowError,OSError):
            continue
    if not observed:
        raise ValueError('No completed daily S&P 500 closing bars available')
    return observed


def fetch_yahoo_sp500(timeout=9,now=None):
    req=Request(YAHOO_SP500,headers={
        'User-Agent':'Mozilla/5.0 PortfolioTracker/1.0',
        'Accept':'application/json'})
    with urlopen(req,timeout=timeout) as response:
        body=response.read(800000)
    return parse_yahoo_sp500(json.loads(body.decode('utf-8')),now=now)


def merge_index_prices(cache, prices, provider, max_change_pct=25):
    """Merge rather than overwrite newer confirmed closes with older FRED data.

    Trust existing values when an optional backup conflicts materially with an
    existing day. Reject clearly suspect one-day jumps from the backup feed.
    """
    cache.setdefault('SP500',{})
    source_map=cache.setdefault('_sp500SourceByDate',{})
    current=cache['SP500']
    accepted=0
    for d,value in sorted(prices.items()):
        key=d.isoformat() if isinstance(d,date) else str(d)
        try:
            day=date.fromisoformat(key)
            value=float(value)
            if not math.isfinite(value) or not 100 < value < 100000:
                continue
        except (ValueError,TypeError):
            continue
        if key in current:
            previous=float(current[key])
            if abs(value/previous-1)>0.002:  # don't quietly rewrite inconsistent vendor history
                continue
            # Keep a prior verified close, rather than doing unnecessary rewrites.
            continue
        earlier=[(date.fromisoformat(k),float(v)) for k,v in current.items() if k < key and len(k)==10]
        if earlier:
            prev_d,prev_v=max(earlier)
            if (day-prev_d).days<=6 and abs(value/prev_v-1)*100 > max_change_pct:
                continue
        current[key]=value
        source_map[key]=provider
        accepted+=1
    return accepted


def verified_seed_prices(path=VERIFIED_SP500):
    """One-time, cited historical observations; never invent future closes."""
    if not path.exists():
        return {},''
    payload=json.loads(path.read_text(encoding='utf-8'))
    if payload.get('series') != 'SP500' or not payload.get('sourceUrl','').startswith('https://'):
        raise ValueError('Invalid historical benchmark seed metadata')
    values={}
    for k,v in (payload.get('closes') or {}).items():
        day=date.fromisoformat(k)
        number=float(v)
        if not math.isfinite(number) or not 100 < number < 100000:
            raise ValueError('Invalid seeded S&P 500 close')
        values[day]=number
    return values,payload['sourceName']


def should_retry_alternative(stored, now, force=False):
    if force: return True
    attempted=(stored.get('_attemptedAtUtc') or {}).get('SP500_ALT')
    if attempted:
        try:
            last=datetime.fromisoformat(attempted.replace('Z','+00:00')).astimezone(timezone.utc)
            if now-last < timedelta(hours=6): return False
        except (TypeError,ValueError,AttributeError): pass
    latest=max((date.fromisoformat(k) for k in (stored.get('SP500') or {}) if len(k)==10),default=None)
    return latest is None or latest < last_business_day(now.date())


def latest_before(obs, day, max_age=7):
    if not obs:
        return None, None
    days = sorted(obs)
    i = bisect.bisect_right(days, day)-1
    if i < 0 or (day-days[i]).days > max_age:
        return None, None
    return obs[days[i]], days[i]


def last_business_day(today):
    """Most recent weekday strictly before today (publication availability is separate)."""
    result = today - timedelta(days=1)
    while result.weekday() >= 5:
        result -= timedelta(days=1)
    return result


def benchmark_should_fetch(series, stored, now, manual_refresh=False):
    """Bounded retries for stale market data; no wallet/network dependency.

    Normal successful benchmark fetches occur at most once per UTC day. If the
    S&P 500 cache lacks even the previous business day's close, allow another
    attempt after six hours. A manual explicit refresh can bypass this limit.
    """
    if manual_refresh:
        return True
    cached = stored.get(series) or {}
    attempted = (stored.get('_attemptedAtUtc') or {}).get(series)
    today = now.date()
    last_date = (stored.get('_fetchedAt') or {}).get(series)
    since_attempt = None
    if attempted:
        try:
            last_attempt = datetime.fromisoformat(attempted.replace('Z', '+00:00'))
            if last_attempt.tzinfo is not None:
                since_attempt = now - last_attempt.astimezone(timezone.utc)
        except (ValueError, TypeError, AttributeError):
            pass
    # Cache errors must not initiate repeated network calls on every reload.
    if not cached or last_date != today.isoformat():
        return since_attempt is None or since_attempt >= timedelta(hours=6)
    if series != 'SP500':
        return False
    try:
        newest = max(date.fromisoformat(d) for d in cached if isinstance(d, str))
    except (ValueError, TypeError):
        return False
    if newest >= last_business_day(today):
        return False
    # Legacy daily cache: wait for explicit manual refresh before retrying.
    return since_attempt is not None and since_attempt >= timedelta(hours=6)


def us_market_anchor_day(baseline_at):
    """Last S&P market-close date at the actual wallet baseline timestamp.

    U.S. regular stock-market close is 16:00 Eastern, which is 20:00 UTC in
    daylight time and 21:00 UTC in standard time. DST boundaries fall on
    Sundays (markets closed), so day-level precision is sufficient here.
    No timezone package is required on Windows.
    """
    baseline_at = baseline_at.astimezone(timezone.utc)
    yr = baseline_at.year
    second_march_sun = 1 + (6 - date(yr,3,1).weekday()) % 7 + 7
    first_nov_sun = 1 + (6 - date(yr,11,1).weekday()) % 7
    dst = date(yr,3,second_march_sun) <= baseline_at.date() < date(yr,11,first_nov_sun)
    close_utc = 20 if dst else 21
    return baseline_at.date() if baseline_at.hour >= close_utc else baseline_at.date()-timedelta(days=1)


def market_paths(dates, baseline, sp500, treasury, sp_anchor_day=None):
    """Daily-close S&P comparison anchored at the first close ON/AFTER start day.

    Never give the portfolio credit for the market session preceding its
    baseline. For a November 4 portfolio snapshot taken at 20:44 UTC, the
    November 4 S&P close (21:00 UTC) is an approximate zero benchmark; the
    November 3 to November 4 rally is *not* a post-baseline return.

    This is a close-to-close daily series, not the live SPX intraday move.
    A latest close must be available to show the next day: no carry-forward
    zero is treated as a genuine observed return. `sp_anchor_day` is kept for
    old callers but intentionally ignored: an earlier close cannot represent
    a same-day wallet baseline without misleading pre-baseline gains.
    """
    sp = {}
    cash = {}
    sp_start = sp_start_date = None
    if baseline is not None:
        candidates = [d for d in sp500 if baseline <= d <= baseline + timedelta(days=7)]
        if candidates:
            sp_start_date = min(candidates)
            sp_start = sp500[sp_start_date]
    cash_index = 1.0 if treasury and baseline is not None and latest_before(treasury, baseline)[0] is not None else None
    previous = baseline
    for d in dates:
        k = d.isoformat()
        if baseline is None or d < baseline:
            sp[k] = None; cash[k] = None; continue
        sp_val, sp_date = latest_before(sp500, d)
        has_new_close = (sp_date is not None and sp_start_date is not None
                         and sp_date > sp_start_date)
        if sp_date is not None and d > baseline and sp_date < last_business_day(d):
            has_new_close = False
        sp[k] = (0.0 if d == baseline and sp_start is not None and sp_start_date == baseline
                 else round(100*(sp_val/sp_start-1), 5)
                 if sp_start and sp_val is not None and has_new_close else None)
        if previous is not None:
            for n in range((d-previous).days):
                day = previous + timedelta(days=n)
                rate, _ = latest_before(treasury, day)
                if rate is None:
                    cash_index = None
                    break
                if cash_index is not None:
                    cash_index *= (1 + rate/100)**(1/365)
        cash[k] = round((cash_index-1)*100, 5) if cash_index is not None else None
        previous = d
    return sp, cash



def eastern_wall_clock(instant):
    """US Eastern wall clock from a UTC instant, without a Windows tzdata dependency.

    US DST starts at 07:00 UTC on the second Sunday of March and ends at
    06:00 UTC on the first Sunday of November (rules in effect since 2007).
    """
    utc = instant.astimezone(timezone.utc)
    year = utc.year
    second_sunday_march = 1 + (6 - date(year, 3, 1).weekday()) % 7 + 7
    first_sunday_november = 1 + (6 - date(year, 11, 1).weekday()) % 7
    starts = datetime(year, 3, second_sunday_march, 7, tzinfo=timezone.utc)
    ends = datetime(year, 11, first_sunday_november, 6, tzinfo=timezone.utc)
    eastern_offset = -4 if starts <= utc < ends else -5
    return utc + timedelta(hours=eastern_offset)


# Confirmed 1 PM NYSE early-close sessions. They are deliberately excluded
# from the 4 PM close-aligned comparison; the wallet snapshot still runs.
# https://www.nyse.com/trade/hours-calendars (calendar published for 2026-28).
EARLY_CLOSE_DAYS = {
    date(2026, 11, 27), date(2026, 12, 24),
    date(2027, 11, 26),
    date(2028, 7, 3), date(2028, 11, 24),
}

def after_regular_close(instant, minimum=15, maximum=120):
    """Return (market date, lag minutes) only near a *regular* 4 PM ET close.

    A weekend, pre-close refresh, or very late snapshot is NOT aligned.
    Market holidays/early closes are also filtered by an actual same-date
    S&P close later; early closes should not be claimed as precisely aligned.
    """
    eastern = eastern_wall_clock(instant)
    market_day = eastern.date()
    if market_day.weekday() >= 5 or market_day in EARLY_CLOSE_DAYS:
        return None
    lag = eastern.hour * 60 + eastern.minute + eastern.second / 60 - 16 * 60
    if minimum <= lag <= maximum:
        return market_day, lag
    return None


def build_close_comparison(verified, returns, sp500, treasury):
    """Match audited wallet snapshots to completed index closes of SAME session.

    This re-bases the *comparison chart only*. The audited P&L baseline and
    period summaries are not changed. Missing S&P dates never become zeros,
    and earlier unaligned wallet observations cannot be paired with a later
    market close merely because the calendar date matches.
    """
    best = {}
    for at, _ in verified:
        result = returns.get(at.isoformat())
        if result is None:
            continue
        clock = after_regular_close(at)
        if clock is None:
            continue
        day, lag = clock
        if day not in sp500:  # wait for a completed, dated market close
            continue
        # Pick the closest snapshot to 4:30 PM ET on each date; don't let a
        # later manual refresh replace a better-aligned daily observation.
        score = (abs(lag - 30), lag)
        if day not in best or score < best[day][0]:
            best[day] = (score, at, lag, result)
    if not best:
        return {'mode': 'awaiting-close-snapshot', 'anchorAt': None,
                'series': [], 'schedule': '16:30 America/New_York',
                'note': 'Awaiting a verified portfolio snapshot after a completed US market close.'}
    days = sorted(best)
    base_day = days[0]
    base_index = 1 + best[base_day][3] / 100
    sp, cash = market_paths(days, base_day, sp500, treasury)
    series = []
    for day in days:
        _, at, lag, percent = best[day]
        index = 1 + percent / 100
        adjusted = round((index / base_index - 1) * 100, 6) if base_index > 0 else None
        series.append({'date': day.isoformat(),
                       'at': at.isoformat().replace('+00:00', 'Z'),
                       'portfolio': adjusted,
                       'cash': cash.get(day.isoformat()),
                       'sp500': sp.get(day.isoformat()),
                       'minutesAfterClose': round(lag, 1),
                       'marketCloseDate': day.isoformat()})
    return {'mode': 'close-aligned' if len(series) >= 2 else 'building-close-history',
            'anchorAt': best[base_day][1].isoformat().replace('+00:00', 'Z'),
            'series': series, 'schedule': '16:30 America/New_York',
            'note': ('Daily price comparisons use audited wallet observations near the '
                     'same-session 4 PM New York close; values are approximations, '
                     'not an exact 4 PM on-chain portfolio valuation.')}


def compute(snapshot, ledger, sp500, treasury):
    hist = daily_history(snapshot)
    start, flows = parse_flows(ledger)
    portfolio, portfolio_base, warning = portfolio_path(hist, start, flows)
    periods = period_summaries(hist, start, flows, portfolio) if not warning else {'1':None,'7':None,'30':None,'all':None}
    baseline = portfolio_base if portfolio_base is not None else (hist[0][0] if hist else None)
    sp, cash = market_paths([d for d,_ in hist], baseline, sp500, treasury)
    lastdate = hist[-1][0] if hist else None
    last_rate, last_rate_date = latest_before(treasury, lastdate) if lastdate else (None,None)
    _, sp_date = latest_before(sp500, lastdate) if lastdate else (None,None)
    result = {
        'schema': 1,
        'snapshotAt': snapshot.get('last', {}).get('at'),
        'firstDate': hist[0][0].isoformat() if hist else None,
        'generatedAt': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'mode': 'flow-adjusted-estimate' if portfolio and not warning else 'pending',
        'periodSummary': periods,
        'warning': warning,
        'baselineDate': baseline.isoformat() if baseline else None,
        'portfolioStartDate': portfolio_base.isoformat() if portfolio_base else None,
        'benchmarks': {
            'cash': {'label': '3M US Treasury (modeled)', 'source': 'FRED DGS3MO', 'annualYieldPct': last_rate, 'observationDate': last_rate_date.isoformat() if last_rate_date else None},
            'sp500': {'label': 'S&P 500 price index', 'source': 'FRED SP500', 'observationDate': sp_date.isoformat() if sp_date else None, 'dividendsIncluded': False}
        },
        'series': [{'date': d.isoformat(), 'portfolio': portfolio.get(d.isoformat()), 'cash': cash.get(d.isoformat()), 'sp500': sp.get(d.isoformat())} for d,_ in hist]
    }
    return result



def audited_observations(snapshot):
    result = []
    for item in snapshot.get('observations', []):
        try:
            at = datetime.fromisoformat(str(item['at']).replace('Z','+00:00'))
            if at.tzinfo is None: continue
            at = at.astimezone(timezone.utc)
            value = float(item['totalUsd'])
            if not math.isfinite(value) or value < 0: continue
            result.append((at, value))
        except (TypeError, ValueError, KeyError): continue
    return sorted(set(result), key=lambda x:x[0])


def compute_auto(snapshot, sp500, treasury):
    """Flow-adjusted market P&L from all wallet tokens + LPs together.

    Modified Dietz uses transfer timestamps to weight external capital within
    snapshot intervals. Fees, emissions, swaps and price moves are INCLUDED in
    whole-portfolio valuation; claims aren't additional cash flows.
    """
    audit = snapshot.get('capitalAudit', {})
    historic = daily_history(snapshot)
    all_points = audited_observations(snapshot)
    baseline_text = audit.get('baselineAt')
    try:
        baseline = datetime.fromisoformat(baseline_text.replace('Z','+00:00')).astimezone(timezone.utc)
    except (AttributeError, ValueError): baseline = None
    all_points = [item for item in all_points if baseline is not None and item[0] >= baseline]
    # A temporarily missing LP balance is not a genuine drawdown. Keep that
    # observation for chart dates, but do not calculate returns from its value.
    # Once a later snapshot is complete, returns can resume over the gap.
    incomplete_times = set()
    for observation in snapshot.get('observations', []):
        if not isinstance(observation, dict) or observation.get('complete', True) is not False:
            continue
        try:
            at = datetime.fromisoformat(str(observation['at']).replace('Z','+00:00'))
            if at.tzinfo:
                incomplete_times.add(at.astimezone(timezone.utc))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    points = [point for point in all_points if point[0] not in incomplete_times]
    warning = None
    # Never erase already-verified P&L when a NEW transfer needs review.
    # Audit suspension applies only to observations occurring on/after the
    # earliest uncertain event. A scan failure freezes at the last audited
    # snapshot. An incomplete LP valuation also cannot become an investment loss.
    pending_times = []
    for transfer in audit.get('pending',[]):
        try:
            pending_times.append(datetime.fromisoformat(str(transfer['at']).replace('Z','+00:00')).astimezone(timezone.utc))
        except (KeyError, TypeError, ValueError, AttributeError):
            pending_times.append(baseline if baseline else datetime.min.replace(tzinfo=timezone.utc))
    cutoff = min(pending_times) if pending_times else None
    status = audit.get('status')
    if audit.get('pending'):
        warning = 'New capital transfers need review; prior verified returns remain visible.'
    if status != 'active' and not audit.get('pending'):
        warning = 'Capital transaction scan is incomplete; prior verified returns remain visible.'
    if status != 'active' and not audit.get('pending') or status not in ('active','review-needed'):
        try:
            checked = datetime.fromisoformat(str(audit.get('lastScanAt') or baseline_text).replace('Z','+00:00')).astimezone(timezone.utc)
            cutoff = min(cutoff, checked + timedelta(microseconds=1)) if cutoff else checked + timedelta(microseconds=1)
        except (ValueError, TypeError, AttributeError):
            cutoff = baseline
    # A missing valuation blocks that observation only. When the latest point
    # is incomplete, show the last verified result; when a later complete
    # snapshot arrives, bridge across the missing value without rebaselining.
    if all_points and all_points[-1][0] in incomplete_times:
        warning = 'LP valuation incomplete; prior verified returns remain visible.'
    if cutoff is not None:
        verified = [point for point in points if point[0] < cutoff]
    else:
        verified = points
    if not points or baseline is None or points[0][0] != baseline:
        warning = 'Verified performance baseline missing. Run one fresh wallet snapshot.'
        verified = []
    raw_flows = []
    for row in audit.get('flows',[]):
        try:
            at = datetime.fromisoformat(str(row['at']).replace('Z','+00:00')).astimezone(timezone.utc)
            amount = float(row['usd'])
            if not math.isfinite(amount) or abs(amount)>1e12: raise ValueError('Invalid flow')
            if at>baseline: raw_flows.append((at, amount))
        except (TypeError, ValueError, KeyError, AttributeError):
            warning = 'Invalid capital ledger entry; verify transfer history.'
    raw_flows.sort()
    # A discovery bridge is NOT a deposit or a reward. It offsets value from
    # positions that already existed but were missing in older tracker snapshots.
    # Keep it separate from net external capital for the hero card and audit.
    coverage = []
    seen_coverage_ids = set()
    for row in audit.get('coverageAdjustments', []):
        try:
            at = datetime.fromisoformat(str(row['at']).replace('Z', '+00:00')).astimezone(timezone.utc)
            value = float(row['usd'])
            identifier = str(row['id'])
            if not identifier or identifier in seen_coverage_ids or not math.isfinite(value) or value < 0 or value > 1e12:
                raise ValueError('Invalid coverage record')
            if at > baseline:
                coverage.append((at, value))
            seen_coverage_ids.add(identifier)
        except (ValueError, TypeError, KeyError, AttributeError):
            warning = 'Invalid valuation coverage adjustment; verify migrated LP history.'
            verified = []
            break
    coverage.sort()
    returns = {}
    aggregate = 1.0
    if verified:
        returns[verified[0][0].isoformat()] = 0.0
        for (a,begin),(b,end) in zip(verified,verified[1:]):
            span = (b-a).total_seconds()
            if span<=0: warning='Non-increasing snapshot times'; returns={}; break
            inside=[(when,usd) for when,usd in raw_flows if a<when<=b]
            net_flow=sum(usd for _,usd in inside)
            # Coverage changes take effect at the valuation checkpoint: they
            # should neither create profit nor be counted as external capital.
            new_coverage=sum(usd for when,usd in coverage if a < when <= b)
            weighted=sum(usd*((b-when).total_seconds()/span) for when,usd in inside)
            denominator=begin+weighted
            if denominator<=0 or not math.isfinite(denominator):
                warning='Unable to calculate P&L around a deposit or withdrawal; review the ledger.'; returns={}; break
            interval=(end-begin-net_flow-new_coverage)/denominator
            if not math.isfinite(interval) or 1+interval<=0:
                warning='Invalid return; check capital flows and asset coverage.'; returns={}; break
            aggregate*=1+interval
            returns[b.isoformat()]=round((aggregate-1)*100,6)
    # A malformed flow or baseline invalidates all values, unlike a recent
    # pending transfer which only blocks data after its transaction timestamp.
    if warning and ('Invalid capital ledger' in warning or 'baseline missing' in warning or 'Invalid valuation coverage' in warning):
        returns={}
    period_summaries = {'1':None,'7':None,'30':None,'all':None}
    if len(verified)>1 and returns:
        end_at,end_value=verified[-1]
        for name,days in (('1',1),('7',7),('30',30),('all',None)):
            if days is None:
                first_at,first_value=verified[0]
            else:
                cutoff=end_at-timedelta(days=days)
                if days==1:
                    # Same-day manual snapshots can show genuine since-snapshot
                    # P&L, but the label/title must identify the shorter window.
                    recent=[p for p in verified[:-1] if p[0]>=cutoff]
                    if not recent: continue
                    first_at,first_value=recent[0]
                else:
                    prior=[p for p in verified[:-1] if p[0]<=cutoff]
                    if not prior: continue
                    first_at,first_value=prior[-1]
                    if (cutoff-first_at)>timedelta(hours=12): continue
            if first_at>=end_at: continue
            net=sum(value for when,value in raw_flows if first_at<when<=end_at)
            profit=end_value-first_value-net-sum(value for when,value in coverage if first_at<when<=end_at)
            first_index=1+returns[first_at.isoformat()]/100
            last_index=1+returns[end_at.isoformat()]/100
            pct=100*(last_index/first_index-1) if first_index>0 else None
            period_summaries[name]={'usd':round(profit,2),'pct':round(pct,5) if pct is not None else None,
              'from':first_at.isoformat(),'to':end_at.isoformat(),
              'estimate':True,'method':'modified-dietz','hours':round((end_at-first_at).total_seconds()/3600,2)}
    dates=[d.date() for d,_ in all_points]
    base_day=baseline.date() if baseline else None
    sp, cash=market_paths(dates,base_day,sp500,treasury)
    # The S&P 500 close on the baseline date is the daily comparison's
    # zero reference, not the prior session's close. Intraday losses must
    # never be represented as a completed market close.
    # Chart: one observation per calendar day, selecting the LAST VERIFIED
    # intraday point. An unverified refresh must not replace the verified
    # observation for the same date with a missing blue line.
    by_day = {}
    for at,_ in all_points:
        k=at.date().isoformat()
        existing=by_day.get(k)
        value=returns.get(at.isoformat())
        if existing is None or value is not None or existing['portfolio'] is None:
            by_day[k]={'date':k,'at':at.isoformat().replace('+00:00','Z'),
                       'portfolio':value,'cash':cash.get(k),'sp500':sp.get(k)}
    series=[by_day[day] for day in sorted(by_day)]
    close_comparison = build_close_comparison(verified, returns, sp500, treasury)
    verified_at = verified[-1][0].isoformat().replace('+00:00','Z') if verified and returns else None
    has_unverified = bool(all_points and (not verified or verified[-1][0] < all_points[-1][0]))
    mode = ('partially-verified' if warning and verified_at and len(verified)>1
            else 'pending' if warning else 'audited-automatic')
    last_day=dates[-1] if dates else None
    rate,rate_day=latest_before(treasury,last_day) if last_day else (None,None)
    _,sp_day=latest_before(sp500,last_day) if last_day else (None,None)
    return {'schema':1,'snapshotAt':snapshot.get('last',{}).get('at'),
      'firstDate':historic[0][0].isoformat() if historic else None,
      'generatedAt':datetime.now(timezone.utc).isoformat(timespec='seconds'),
      'mode':mode,'verifiedThroughAt':verified_at,'hasUnverifiedLatest':has_unverified,
      'periodSummary':period_summaries,'warning':warning,
      'closeComparison':close_comparison,
      'baselineDate':base_day.isoformat() if base_day else None,
      'portfolioStartDate':base_day.isoformat() if base_day else None,
      'audit':{'status':audit.get('status'),'pendingCount':len(audit.get('pending',[])),
               'verifiedFrom':baseline_text,'recognisedTransfers':len(audit.get('flows',[])),
               'netExternalCapitalUsd':round(sum(value for _,value in raw_flows), 2),
               'coverageAdjustmentUsd':round(sum(value for _,value in coverage), 2),
               'coverageAdjustmentCount':len(coverage)},
      'benchmarks':{'cash':{'label':'3M US Treasury (modeled)','source':'FRED DGS3MO',
                  'annualYieldPct':rate,'observationDate':rate_day.isoformat() if rate_day else None},
                    'sp500':{'label':'S&P 500 price index','source':'FRED SP500',
                     'observationDate':sp_day.isoformat() if sp_day else None,'dividendsIncluded':False}},
      'series':series}


def main():
    p = argparse.ArgumentParser(description='Generate published percentage-return benchmarks without Alchemy requests')
    p.add_argument('--snapshot', default=str(ROOT/'data.json'))
    p.add_argument('--ledger', default=str(ROOT/'cashflows.json'))
    p.add_argument('--output', default=str(ROOT/'performance.json'))
    p.add_argument('--no-fetch', action='store_true', help='Use cached benchmarks only (for offline testing)')
    p.add_argument('--force', action='store_true', help='Recalculate output, retaining normal market-cache limits')
    p.add_argument('--refresh-benchmarks', action='store_true', help='Explicitly fetch fresh Treasury and S&P data without any Alchemy request')
    args = p.parse_args()
    source = Path(args.snapshot)
    if not source.exists():
        print('Performance: no snapshot yet; nothing to generate.')
        return
    snap = json.loads(source.read_text(encoding='utf-8'))
    ledgerpath = Path(args.ledger)
    ledger = json.loads(ledgerpath.read_text(encoding='utf-8')) if ledgerpath.exists() else {'completeFrom':'','flows':[]}
    ledger_digest = hashlib.sha256(json.dumps(ledger, sort_keys=True).encode('utf-8')).hexdigest()
    output=Path(args.output)
    private_state=ROOT/'performance-state.json'
    # Always recalculate locally from the saved snapshot. This is inexpensive
    # and lets a stale S&P close be rechecked after its six-hour retry window;
    # the dedicated market cache still prevents repeated HTTP requests.
    # Keep the last known market closes if FRED temporarily fails; the output
    # labels observation dates and never invents market rates/returns.
    cache_file = ROOT / 'benchmark-cache.json'
    stored = json.loads(cache_file.read_text(encoding='utf-8')) if cache_file.exists() else {}
    observed = {}
    now = datetime.now(timezone.utc)
    for series in ('SP500','DGS3MO'):
        fetch = not args.no_fetch and benchmark_should_fetch(series, stored, now, args.refresh_benchmarks)
        if fetch:
            # Store attempted time even if upstream is unavailable, so a failed
            # fetch cannot cause a burst of retries when visitors refresh.
            stored.setdefault('_attemptedAtUtc', {})[series] = now.isoformat(timespec='seconds').replace('+00:00','Z')
            try:
                raw = fetch_fred(series)
                if series == 'SP500':
                    # Do not discard a later Yahoo/verified close when FRED lags.
                    merge_index_prices(stored, raw, 'FRED SP500')
                    observed[series] = {date.fromisoformat(k):v for k,v in stored['SP500'].items()}
                else:
                    observed[series] = raw
                    stored[series] = {d.isoformat():v for d,v in raw.items()}
                stored.setdefault('_fetchedAt', {})[series] = now.date().isoformat()
                newest = max(raw)
                print(f'Performance: {series} latest market observation {newest.isoformat()} (retrieved).')
            except Exception as exc:
                print(f'Performance: {series} fetch unavailable ({type(exc).__name__}); using cached data if present.')
        if series not in observed:
            observed[series] = {}
            for d,v in (stored.get(series) or {}).items():
                try: observed[series][date.fromisoformat(d)] = float(v)
                except (ValueError,TypeError): pass
    # A provider-independent historical bootstrap fills this specific known
    # post-baseline market close even if FRED and Yahoo are temporarily stale.
    # Verified published Nov 3/4 close values; immutable factual dates only.
    try:
        verified, verified_source = verified_seed_prices()
        if verified:
            new_seed=merge_index_prices(stored,verified,verified_source)
            if new_seed:
                print(f'Performance: added {new_seed} independently verified S&P historical close(s).')
    except (OSError,ValueError,TypeError,json.JSONDecodeError) as exc:
        print(f'Performance: verified historical seed unavailable ({type(exc).__name__}).')
    current_sp=stored.get('SP500') or {}
    if not args.no_fetch and should_retry_alternative(stored,now,args.refresh_benchmarks):
        stored.setdefault('_attemptedAtUtc',{})['SP500_ALT']=now.isoformat(timespec='seconds').replace('+00:00','Z')
        try:
            alternative=fetch_yahoo_sp500(now=now)
            added=merge_index_prices(stored,alternative,'Yahoo Finance ^GSPC (daily close)')
            print(f'Performance: secondary S&P close feed checked; {added} new dated close(s) saved.')
        except Exception as exc:
            print(f'Performance: secondary S&P feed unavailable ({type(exc).__name__}); retaining verified closes.')
    observed['SP500']={date.fromisoformat(k):float(v) for k,v in current_sp.items()}
    if stored:
        tmp=cache_file.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(stored, separators=(',',':')),encoding='utf-8')
        tmp.replace(cache_file)
    result = (compute_auto(snap,observed['SP500'],observed['DGS3MO'])
              if isinstance(snap.get('capitalAudit'),dict) else compute(snap,ledger,observed['SP500'],observed['DGS3MO']))
    sp_meta=result.get('benchmarks',{}).get('sp500',{})
    newest=sp_meta.get('observationDate')
    if newest:
        sp_meta['source']=(stored.get('_sp500SourceByDate') or {}).get(newest,'FRED SP500 (legacy cache)')
    tmp=output.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    tmp.replace(output)
    # A private local fingerprint avoids publishing a hash of cash-flow amounts.
    state_tmp=private_state.with_suffix('.json.tmp')
    state_tmp.write_text(json.dumps({'generatedAt':result['generatedAt'],'snapshotAt':result['snapshotAt'],'ledgerDigest':ledger_digest}),encoding='utf-8')
    state_tmp.replace(private_state)
    count = sum(x['portfolio'] is not None for x in result['series'])
    latest_sp = result.get('benchmarks',{}).get('sp500',{}).get('observationDate')
    baseline_sp = result.get('baselineDate')
    print(f'Performance: {len(result["series"])} dates, {count} portfolio-return points, mode={result["mode"]}. No Alchemy requests.')
    source_label=(result.get('benchmarks',{}).get('sp500') or {}).get('source','unknown')
    print(f'Benchmark S&P 500: latest close {latest_sp or "unavailable"}; baseline {baseline_sp or "unavailable"}; source {source_label}.')
    if latest_sp and baseline_sp and latest_sp < baseline_sp:
        print('Benchmark S&P 500: no newer closing price yet; no return line will be invented.')
        print(r'To check the market source again manually: py -3 scripts\performance.py --refresh-benchmarks')

if __name__=='__main__':
    try: main()
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print('PERFORMANCE FAILED: ' + str(exc))
        raise SystemExit(1)
