'use strict';
const el = id => document.getElementById(id);
const COLORS = {ethereum:'#94a7ff',base:'#78c7ff',arbitrum:'#7fe5d6',polygon:'#b9a9ff',bnb:'#ffce8b',other:'#a9b7cc'};
let namiPreview = null, performanceData = null, portfolio = null, settings = {}, period = 'all', typeFilter = 'all', currency = 'USD', preferredCurrency = 'USD', gbpRate = null, gbpDate = null;
let fxState = 'waiting';
try { if (localStorage.getItem('portfolio-currency') === 'GBP') preferredCurrency = 'GBP'; } catch (_) { /* private browsing may disallow storage */ }

function money(n, compact = false) {
  if (n === null || n === undefined || n === '') return '—';
  const value = Number(n);
  if (!Number.isFinite(value)) return '—';
  const converted = currency === 'GBP' ? value * (gbpRate || 1) : value;
  const unit = currency === 'GBP' ? '£' : '$';
  if (compact && Math.abs(converted) >= 1e12) return unit + (converted / 1e12).toFixed(2) + 'T';
  if (compact && Math.abs(converted) >= 1e9) return unit + (converted / 1e9).toFixed(2) + 'B';
  if (compact && Math.abs(converted) >= 1e6) return unit + (converted / 1e6).toFixed(2) + 'M';
  if (compact && Math.abs(converted) >= 1e3) return unit + (converted / 1e3).toFixed(1) + 'K';
  return new Intl.NumberFormat(currency === 'USD' ? 'en-US' : 'en-GB', {style:'currency', currency, maximumFractionDigits:2}).format(converted);
}
function unitPrice(value) {
  const n = Number(value) * (currency==='GBP' ? (gbpRate||1) : 1);
  if (!Number.isFinite(n) || n < 0) return '—';
  if (n === 0) return currency==='GBP'?'£0.00':'$0.00';
  if (n >= 0.01) return money(value);
  const symbol = currency === 'GBP' ? '£' : '$';
  return symbol + n.toLocaleString(currency==='GBP'?'en-GB':'en-US', {maximumSignificantDigits:4,maximumFractionDigits:9});
}

function formatQty(n) {
  if (n === null || n === undefined || n === '') return '—';
  const x = Number(n);
  return Number.isFinite(x) ? new Intl.NumberFormat('en-GB',{maximumSignificantDigits:7}).format(x) : '—';
}
const safeUsd = row => row.usd === null || row.usd === undefined || !Number.isFinite(Number(row.usd)) ? 0 : Number(row.usd);
function create(tag, cls, content) { const node = document.createElement(tag); if (cls) node.className = cls; if (content !== undefined) node.textContent = content; return node; }
function text(id, value) { if (el(id)) el(id).textContent = value; }
function formatDate(date) { const d = new Date(date); return Number.isNaN(d.getTime()) ? 'Unknown date' : d.toLocaleDateString('en-GB',{day:'numeric',month:'short',year:'numeric',timeZone:'UTC'}); }
function formatUtcTime(date) { const d = new Date(date); return Number.isNaN(d.getTime()) ? 'unknown time' : d.toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}) + ' UTC'; }
function initialTimestamp() {
  const configured = typeof settings.liveSince === 'string' && settings.liveSince.trim() ? settings.liveSince : null;
  const first = portfolio?.history?.[0]?.date;
  const chosen = configured || portfolio?.startedAt || (first ? first + 'T00:00:00Z' : null);
  if (!chosen) return null;
  const millis = Date.parse(chosen);
  return Number.isFinite(millis) ? millis : null;
}
function renderDays() {
  const start = initialTimestamp();
  text('days-live', start === null ? '—' : String(Math.max(0, Math.floor((Date.now() - start) / 86400000))));
  const badge = el('days-live');
  if (badge) badge.title = start === null ? 'Starts with your first snapshot' : `Counting completed 24-hour periods since ${new Date(start).toLocaleString('en-GB',{timeZone:'UTC'})} UTC`;
}
async function fetchJson(path) {
  const response = await fetch(path + '?v=' + Date.now(), {cache:'no-store'});
  if (!response.ok) throw Error(`Failed to load ${path}`);
  return response.json();
}
function setCurrencyButtons() {
  document.querySelectorAll('[data-currency]').forEach(btn => { const active = btn.dataset.currency === currency; btn.classList.toggle('active', active); btn.setAttribute('aria-pressed', String(active)); });
  text('hero-currency', '● ' + currency);
  const gbpButton = document.querySelector('[data-currency="GBP"]');
  if (gbpButton) gbpButton.title = gbpRate
    ? `USD to GBP reference rate ${gbpRate.toFixed(5)} as of ${formatDate(gbpDate)}`
    : 'GBP conversion requires an exchange rate';
  text('history-description','Cumulative investment return (%) · compared with Treasury cash and S&P 500');
}
function adoptFx(data) {
  const rate = Number(data?.rate);
  if (!Number.isFinite(rate) || rate <= 0 || rate >= 2 || typeof data?.date !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(data.date)) return false;
  const ageDays = (Date.now() - Date.parse(data.date + 'T00:00:00Z')) / 86400000;
  if (!Number.isFinite(ageDays) || ageDays > 7 || ageDays < -2) return false;
  gbpRate = rate; gbpDate = data.date; fxState = 'ready';
  return true;
}
async function refreshFx() {
  // Frankfurter publishes a daily reference rate. A cached rate remains labelled with its date.
  try {
    const cached = JSON.parse(localStorage.getItem('portfolio-fx') || 'null');
    if (cached && adoptFx(cached)) { setCurrencyButtons(); if (portfolio) render(); return; }
  } catch (_) { /* Storage may be unavailable */ }
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 9000);
  try {
    const response = await fetch('https://api.frankfurter.dev/v2/rate/usd/gbp', {signal: ctl.signal, cache:'no-store'});
    if (!response.ok) throw Error('FX response unavailable');
    const data = await response.json();
    if (!adoptFx(data)) throw Error('Invalid FX response');
    try { localStorage.setItem('portfolio-fx', JSON.stringify({rate:gbpRate,date:gbpDate})); } catch (_) { /* optional */ }
  } catch (_) {
    if (!gbpRate) { fxState = 'unavailable'; currency = 'USD'; }
  } finally {
    clearTimeout(timer);
    if (gbpRate && preferredCurrency === 'GBP') currency = 'GBP';
    setCurrencyButtons();
    if (portfolio) render();
  }
}
function init() {
  document.querySelectorAll('[data-period]').forEach(btn => btn.addEventListener('click', () => {
    period = btn.dataset.period;
    document.querySelectorAll('[data-period]').forEach(x => x.classList.toggle('active', x === btn));
    renderChart();
    renderPerformanceSummary();
  renderHeroAccounting();
  }));
  document.querySelectorAll('[data-currency]').forEach(btn => btn.addEventListener('click', async () => {
    const desired = btn.dataset.currency;
    if (desired === 'GBP' && !gbpRate) {
      await refreshFx();
      if (!gbpRate) { text('fx-label', 'Could not load the GBP rate. Please check your connection.'); return; }
    }
    currency = desired; preferredCurrency = desired;
    try { localStorage.setItem('portfolio-currency', currency); } catch (_) { /* optional */ }
    setCurrencyButtons(); if (portfolio) render();
  }));
  el('search-input').addEventListener('input',renderAssets);
  el('refresh-btn').addEventListener('click', requestPortfolioRefresh);
  el('demo-btn').addEventListener('click',() => load(true));
  setInterval(renderDays, 30000);
  setCurrencyButtons(); renderDays();
  load();
  // No network FX request for the default USD view.
  try { const cached = JSON.parse(localStorage.getItem('portfolio-fx') || 'null'); if (cached) adoptFx(cached); } catch (_) {}
  setCurrencyButtons();
}
async function load(demo = false) {
  try {
    settings = await fetchJson('config.json');
    document.title = (settings.title || 'Portfolio') + ' | Portfolio Tracker';
  } catch (_) { /* Keep the default title */ }
  try {
    const result = await fetchJson(demo ? 'examples/demo-data.json' : 'data.json');
    if (result.schema !== 2 || !result.last || !Array.isArray(result.last.rows) || !Array.isArray(result.history)) throw Error('Invalid snapshot');
    portfolio = result;
    performanceData = null; namiPreview = null;
    if (!demo) {
      try {
        const perf = await fetchJson('performance.json');
        if (perf.schema === 1 && perf.snapshotAt === result.last.at && perf.firstDate === result.history[0]?.date && Array.isArray(perf.series)) performanceData = perf;
      } catch (_) { /* The snapshot can be read before performance generation finishes. */ }
    }
    if (!demo && !(result.last.rows || []).some(row => row.protocol === 'Nami' && row.usd != null)) { try { const n = await fetchJson('nami-preview.json'); if (n.schema === 1 && n.provider === 'Nami on-chain (Alchemy)' && n.includedInAuditedTotal === false && Array.isArray(n.positions)) namiPreview = n; } catch (_) { /* optional on-chain position preview unavailable */ } }
    el('dashboard').classList.remove('hidden'); el('empty-state').classList.add('hidden');
    text('sync-label', demo ? 'Illustrative example · not connected' : `Snapshot · ${formatUtcTime(result.last.at || result.last.date)}${performanceData?.hasUnverifiedLatest ? ' · P&L awaiting review' : ''}`);
    render();
  } catch (_) {
    if (demo) { text('sync-label','Example data not available'); return; }
    portfolio = null; el('dashboard').classList.add('hidden'); el('empty-state').classList.remove('hidden');
    text('sync-label','No valid snapshot yet'); renderDays();
  }
}
// V8: clicking Refresh requests one *shared* new snapshot; it never exposes API credentials.
// On GitHub Pages, a configured Cloudflare endpoint securely dispatches GitHub Actions.
// On localhost, the local Python server starts one guarded background snapshot.
let refreshInProgress = false;
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
function refreshMessage(message) { text('sync-label', message); }
function snapshotTimestamp() { return portfolio?.last?.at || null; }
async function requestPortfolioRefresh() {
  if (refreshInProgress) return;
  const button = el('refresh-btn');
  const oldStamp = snapshotTimestamp();
  const isLocal = ['127.0.0.1','localhost'].includes(location.hostname);
  const configuredEndpoint = typeof settings.refreshEndpoint === 'string' ? settings.refreshEndpoint.trim() : '';
  refreshInProgress = true;
  button.disabled = true;
  button.textContent = '↻ Checking…';
  try {
    if (!isLocal && !configuredEndpoint) {
      await load();
      refreshMessage('Latest published snapshot loaded · live refresh is not enabled yet');
      return;
    }
    // A public site's refresh endpoint must be HTTPS, not an arbitrary HTTP origin.
    if (!isLocal && !/^https:\/\/[^/]+\/refresh\/?$/.test(configuredEndpoint)) {
      throw new Error('Public refresh endpoint is not configured correctly');
    }
    const endpoint = isLocal ? '/api/refresh' : configuredEndpoint;
    const response = await fetch(endpoint, {method:'POST',headers:{'Content-Type':'application/json'},body:'{}',cache:'no-store'});
    const outcome = await response.json();
    if (!response.ok) throw new Error(outcome.message || 'Refresh service unavailable');
    if (outcome.status === 'cooldown') {
      await load();
      const until = outcome.nextAllowedAt ? new Date(outcome.nextAllowedAt).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}) : 'later';
      refreshMessage(`Using latest published snapshot · next on-chain refresh available after ${until}`);
      return;
    }
    if (!['queued','started','running'].includes(outcome.status)) throw new Error('Unknown refresh response');
    refreshMessage(isLocal ? 'Reading Base wallet and LP positions…' : 'Update queued · publishing a fresh portfolio snapshot…');
    button.textContent = '↻ Updating…';
    const maxPolls = isLocal ? 40 : 18;
    const pollInterval = isLocal ? 5000 : 15000;
    for (let attempt = 0; attempt < maxPolls; attempt++) {
      await delay(pollInterval);
      if (isLocal) {
        const state = await fetchJson('/api/status');
        if (state.status === 'failed') throw new Error('Snapshot failed on the local server; check the terminal output');
        if (state.status === 'done') {
          await load();
          refreshMessage('Portfolio updated · ' + formatDate(snapshotTimestamp()));
          return;
        }
      } else {
        // Reading data.json does not call Alchemy. The GitHub Pages deploy may take a few minutes.
        try {
          const latest = await fetchJson('data.json');
          if (latest?.last?.at && latest.last.at !== oldStamp) {
            await load();
            refreshMessage('Portfolio updated · ' + formatDate(latest.last.at));
            return;
          }
        } catch (_) { /* The Pages deploy can briefly be unavailable; keep checking. */ }
      }
    }
    await load();
    refreshMessage(isLocal ? 'Update still running · check your local terminal' : 'Update queued · visit again shortly to see the new published snapshot');
  } catch (err) {
    await load();
    refreshMessage('Refresh unavailable · ' + (err?.message || 'try again later').slice(0, 110));
  } finally {
    refreshInProgress = false;
    button.disabled = false;
    button.textContent = '↻ Refresh portfolio';
  }
}

function render() {
  if (!portfolio) return;
  const last = portfolio.last, rows = last.rows || [];
  text('portfolio-total',money(last.totalUsd));
  renderPerformanceSummary();
  renderHeroAccounting();
  for (const [kind,field] of [['token','tokens'],['lp','lp'],['manual','manual']]) {
    const filtered = rows.filter(r => r.type === kind && r.usd != null);
    text(field+'-total',money(filtered.reduce((acc,r) => acc + safeUsd(r), 0)));
    const label = kind === 'lp' ? 'position' : kind === 'token' ? 'asset' : 'entry';
    text(field+'-count',`${filtered.length} valued ${label}${filtered.length === 1 ? '' : 's'}`);
  }
  text('tvl-total',last.tvlUsd == null ? 'Unavailable' : money(last.tvlUsd, true));
  renderDays(); setCurrencyButtons(); renderLPs(); renderAllocation(); renderChart(); renderAssets();
}
function renderPerformanceSummary() {
  const change = el('day-change');
  change.className = 'change';
  const item = performanceData?.periodSummary?.[period];
  const partial = performanceData?.mode === 'partially-verified' || !!performanceData?.hasUnverifiedLatest;
  const verifiedAt = performanceData?.verifiedThroughAt;
  const verifiedLabel = verifiedAt ? new Date(verifiedAt).toLocaleString('en-GB',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit',timeZone:'UTC'}) + ' UTC' : 'the last verified snapshot';
  if (!item || !Number.isFinite(item.usd)) {
    change.textContent = partial
      ? `New transfers need review · last verified ${verifiedLabel}`
      : performanceData?.mode === 'audited-automatic' || performanceData?.mode === 'flow-adjusted-estimate'
        ? 'Not enough verified history for this period'
        : 'Awaiting verified deposits and withdrawals';
    return;
  }
  const amount = Number(item.usd), percent = Number(item.pct);
  const sign = amount > 0 ? '+' : amount < 0 ? '−' : '';
  const pctText = Number.isFinite(percent)
    ? ` (${percent > 0 ? '+' : ''}${percent.toFixed(2)}%)` : '';
  const pendingCount = Number(performanceData?.audit?.pendingCount || portfolio?.capitalAudit?.pending?.length || 0);
  change.textContent = `${sign}${money(Math.abs(amount))}${pctText}${partial ? ' · last verified result' : ''}`;
  change.title = `Net wallet + LP performance excluding external capital, ${item.from} to ${item.to}; approximate return from dated snapshots`;
  change.classList.add(amount < 0 ? 'negative' : amount > 0 ? 'positive' : '');
}
function signedMoney(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return '—';
  return (number > 0 ? '+' : number < 0 ? '−' : '') + money(Math.abs(number));
}
function renderHeroAccounting() {
  const audit = performanceData?.audit;
  text('hero-valued-at', 'Latest valuation · ' + formatUtcTime(portfolio?.last?.at));
  const capital = Number(audit?.netExternalCapitalUsd);
  text('hero-capital', Number.isFinite(capital) && audit
    ? 'Verified external capital since baseline · ' + signedMoney(capital)
    : 'External capital · awaiting audit');
  const bridge = Number(audit?.coverageAdjustmentUsd);
  const bridgedCount = Number(audit?.coverageAdjustmentCount || 0);
  const includedNami = (portfolio?.last?.rows || []).some(row => row.protocol === 'Nami' && row.usd != null);
  text('hero-coverage', includedNami
    ? `Nami included in total${bridgedCount && Number.isFinite(bridge) ? ' · '+money(bridge)+' coverage adjustment, not profit' : ''}`
    : 'Nami supplementary · not yet included in total');
  const pending = Number(audit?.pendingCount ?? portfolio?.capitalAudit?.pending?.length ?? 0);
  const verified = performanceData?.verifiedThroughAt;
  text('hero-audit', pending > 0
    ? `${pending} transfer${pending === 1 ? '' : 's'} to review · P&L verified to ${verified ? formatUtcTime(verified) : 'an earlier snapshot'}`
    : performanceData?.mode === 'audited-automatic'
      ? 'Capital transfers verified · P&L current to latest snapshot'
      : 'Investment P&L awaiting verification');
}
function renderLPs() {
  const target = el('lp-cards'); target.replaceChildren();
  const lps = (portfolio.last.rows || []).filter(r => r.type === 'lp');
  if (!lps.length && !(namiPreview?.positions?.length)) { target.append(create('p','allocation-empty','No LP positions yet.')); return; }
  let pending = 0;
  for (const lp of lps) {
    if (lp.usd == null) pending++;
    const card = create('article','lp-balance-card'), header = create('div','lp-card-head');
    const left = create('div','lp-pair-title'); left.append(create('span','lp-mark','◈'),create('span',null,(lp.protocol === 'Nami' ? 'Nami · ' : '') + (lp.name || 'Liquidity position')));
    header.append(left,create('div','lp-card-value',money(lp.usd)));
    card.append(header);
    const group = create('div','lp-component-list');
    if (Array.isArray(lp.underlying)) for (const part of lp.underlying) {
      const item = create('div','lp-component');
      item.append(create('span','lp-token',part.symbol || 'Token'),create('span','lp-amount',`${formatQty(part.amount)} ${part.symbol || ''}`));
      group.append(item);
    }
    else group.append(create('div','lp-component','Underlying tokens not available'));
    card.append(group);
    const foot = create('div','lp-card-footer');
    foot.append(create('span',null,lp.usd === null || lp.usd === undefined ? 'Not available · position not identified' : (lp.source === 'manual quantities' ? 'Manual value · not live' : (lp.source === 'on-chain best effort' ? 'Detected on-chain · limited history' : 'Detected on-chain'))));
    if (typeof lp.url === 'string' && /^https:\/\//.test(lp.url)) { const link = create('a',null,'View pool'); link.href=lp.url; link.target='_blank'; link.rel='noopener noreferrer'; foot.append(link); }
    card.append(foot); target.append(card);
  }
  appendSupplementaryNami(target);
}

// Nami is displayed once alongside other LPs, not in a second holdings list.
// Its independent valuation is intentionally EXCLUDED from audited metrics.
function appendSupplementaryNami(target) {
  if (!namiPreview || namiPreview.status !== 'verified-position' || !Array.isArray(namiPreview.positions)) return;
  const reading = Date.parse(namiPreview.at || '');
  const snapshot = Date.parse(portfolio?.last?.at || '');
  const stale = !Number.isFinite(reading) || (Date.now()-reading) > 48*3600000;
  const differentTime = Number.isFinite(reading) && Number.isFinite(snapshot) && Math.abs(reading-snapshot)>3600000;
  const accountedIds = new Set((portfolio?.last?.rows || []).filter(r=>r.type==='lp').map(r=>r.id));
  for (const pos of namiPreview.positions) {
    if (accountedIds.has(pos.id)) continue;
    const card = create('article','lp-balance-card supplementary-lp');
    const head = create('div','lp-card-head');
    const label = create('div','lp-pair-title');
    label.append(create('span','lp-mark','◈'),create('span',null,'Nami · '+(pos.name||'WETH / cbBTC')));
    head.append(label,create('div','lp-card-value',money(pos.usd)));
    card.append(head);
    const group=create('div','lp-component-list');
    for (const part of pos.underlying || []) {
      const item=create('div','lp-component');
      item.append(create('span','lp-token',part.symbol || 'Token'),create('span','lp-amount',formatQty(part.amount)+' '+(part.symbol||'')));
      group.append(item);
    }
    card.append(group);
    const foot = create('div','lp-card-footer');
    const note=(stale ? 'Older preview · ' : differentTime ? 'Different valuation time · ' : '')+'Supplementary · excluded from tracked total and P&L';
    foot.append(create('span','supplementary-note',note));
    if (typeof pos.url==='string' && /^https:\/\/basescan\.org\//.test(pos.url)) {
      const link=create('a',null,'View NFT'); link.href=pos.url;link.target='_blank';link.rel='noopener noreferrer';foot.append(link);
    }
    card.append(foot);target.append(card);
  }
}

function renderAllocation() {
  // Reuse the two existing summary cards instead of creating a duplicate panel.
  const rows = portfolio?.last?.rows || [];
  const overall = Number(portfolio?.last?.totalUsd);
  for (const kind of ['token', 'lp']) {
    const subtotal = rows.filter(row => row.type === kind).reduce((sum, row) => sum + safeUsd(row), 0);
    const percentage = Number.isFinite(overall) && overall > 0
      ? Math.max(0, Math.min(100, (subtotal / overall) * 100)) : 0;
    const prefix = kind === 'token' ? 'tokens' : 'lp';
    text(prefix + '-share', percentage.toFixed(1) + '%');
    const fill = el(prefix + '-share-bar');
    if (fill) fill.style.width = percentage + '%';
    const track = el(prefix + '-share-track');
    if (track) track.setAttribute('aria-valuenow', percentage.toFixed(1));
  }
}
function renderAssets() {
  if (!portfolio) return;
  const q=(el('search-input')?.value || '').trim().toLowerCase();
  const rows=(portfolio.last.rows||[]).filter(r => r.type === 'token');
  const entries=rows.filter(r => !q || [r.name,r.symbol].join(' ').toLowerCase().includes(q));
  const body=el('assets-body');body.replaceChildren();
  text('assets-subtitle',`${entries.length} of ${rows.length} wallet token balances · Base`);
  el('assets-empty').classList.toggle('hidden',entries.length !== 0);
  for (const r of entries) {
    const tr=create('tr'), asset=create('td'), line=create('div','asset-cell');
    const avatar=create('div','asset-icon',String(r.symbol || '?').slice(0,2).toUpperCase());
    const title=create('div','asset-name');
    if (typeof r.url==='string' && /^https:\/\//.test(r.url)) {
      const link=create('a',null,r.name||r.symbol||'Token');link.href=r.url;link.target='_blank';link.rel='noopener noreferrer';title.append(link);
    } else title.textContent=r.name||r.symbol||'Token';
    line.append(avatar,title);asset.append(line);
    const bal=create('td','asset-balance',`${formatQty(r.amount)} ${r.symbol || ''}`);
    tr.append(asset,bal,create('td','value-cell',money(r.usd)));
    body.append(tr);
  }
}
let chartOutsidePointerHandler = null;
function renderChart() {
  if (!portfolio) return;
  const area = el('chart-area'); area.replaceChildren();
  area.setAttribute('role', 'group');
  area.setAttribute('aria-label', 'Interactive investment returns chart. Hover, tap or use arrow keys to inspect each date.');
  if (chartOutsidePointerHandler) {
    document.removeEventListener('pointerdown', chartOutsidePointerHandler);
    chartOutsidePointerHandler = null;
  }
  const subtitle = el('performance-warning');
  const summaries = el('performance-legend'); summaries.replaceChildren();
  // V18: never compare a pre-close wallet value with an S&P close recorded
  // later on the same date. New versions supply strictly close-aligned rows.
  // Older performance files keep their legacy plot until an upgrade is applied.
  const comparison = performanceData?.closeComparison;
  const aligned = comparison?.mode === 'close-aligned' && Array.isArray(comparison.series) && comparison.series.length >= 2;
  // V22: the all-time performance chart must NEVER replace its entire history
  // with the smaller close-aligned benchmark subset. That subset has its own status.
  const raw = Array.isArray(performanceData?.series) ? performanceData.series : [];
  const alignmentStatus = el('comparison-status');
  if (alignmentStatus) {
    if (aligned) {
      alignmentStatus.textContent = 'Historical performance · ' + comparison.series.length +
        ' close-aligned market sessions available separately; earlier historical dates are retained.';
      alignmentStatus.title = 'This full-history chart includes wallet snapshots that are not all time-matched to market closes. ' + (comparison.note || '');
    } else {
      const count = Array.isArray(comparison?.series) ? comparison.series.length : 0;
      alignmentStatus.textContent = 'Historical comparison · snapshot times NOT aligned with US market close. '+
        (count ? `${count} of 2 close-aligned sessions collected.` : 'Waiting for first verified close-aligned snapshot.');
      alignmentStatus.title = 'Previous verified chart retained for context only. Do not interpret it as a same-time S&P comparison.';
    }
  }
  // A comparison must not include dates before the verified portfolio baseline.
  // Otherwise the 1D view can choose a null pre-baseline row as its origin,
  // erasing all three series even when the market observations are available.
  const anchor = performanceData?.portfolioStartDate || performanceData?.baselineDate || null;
  const eligible = raw.filter(row => /^\d{4}-\d{2}-\d{2}$/.test(row.date) && (!anchor || row.date >= anchor));
  const latestDay = eligible.length ? Date.parse(eligible[eligible.length - 1].at || eligible[eligible.length - 1].date + 'T00:00:00Z') : NaN;
  const periodDays = period === 'all' ? null : Number(period);
  const source = eligible.filter(row => periodDays === null ||
    latestDay - Date.parse(row.at || row.date + 'T00:00:00Z') <= periodDays * 86400000);
  // V22: a shorter selected window is a view filter, NOT deleted history.
  if (alignmentStatus && eligible.length) {
    const view = period === 'all' ? 'ALL history' : `${period}D`;
    const hidden = source.length < eligible.length;
    alignmentStatus.textContent += ` · Showing ${source.length} of ${eligible.length} recorded dates (${view}${hidden ? '; select ALL to see every date' : ''}).`;
  }
  const initial = source[0] || {};
  const keys = [
    {key:'portfolio',label:'My portfolio',color:'#82cfff'},
    {key:'cash',label:'3M Treasury cash',color:'#8ce5c1'},
    {key:'sp500',label:'S&P 500 return',color:'#d5aaff'}
  ];
  const data = source.map(row => {
    const point = {date:row.date, at:row.at||null};
    for (const line of keys) {
      const start = initial[line.key], current = row[line.key];
      const startNumber = typeof start === 'number' ? start : NaN;
      const value = typeof current === 'number' ? current : NaN;
      point[line.key] = Number.isFinite(startNumber) && Number.isFinite(value) && (1 + startNumber / 100) > 0
        ? 100 * ((1 + value / 100) / (1 + startNumber / 100) - 1)
        : null;
    }
    return point;
  });
  text('history-span', data.length ? `${formatDate(data[0].date)} – ${formatDate(data[data.length-1].date)}` : 'No benchmark history yet');
  for (const item of keys) {
    const lastValid = [...data].reverse().find(p => Number.isFinite(p[item.key]));
    const node = create('div','performance-legend-item');
    if (!lastValid) node.classList.add('series-unavailable');
    node.title = lastValid ? `${item.label}: latest ${lastValid[item.key].toFixed(3)}%` : `${item.label}: awaiting benchmark data / a valid starting date`;
    const dot = create('span','performance-legend-dot'); dot.style.backgroundColor = item.color;
    const amount = lastValid ? `${lastValid[item.key]>=0?'+':''}${lastValid[item.key].toFixed(2)}%` : '—';
    node.append(dot,create('span','performance-legend-label',item.label),create('strong','performance-legend-value',amount));
    summaries.append(node);
  }
  // Preserve the underlying status in the DOM for troubleshooting. V12 hides
  // this long explanation from the otherwise minimal card.
  if (!performanceData) subtitle.textContent = 'Performance data not generated for this snapshot.';
  else subtitle.textContent = performanceData.warning || '';
  if (!data.length) {
    area.append(create('p','performance-empty',comparison ? 'Waiting for verified historical observations; no values have been invented.' : 'The chart starts with your first confirmed performance snapshot.'));
    return;
  }

  // Size the SVG viewBox from the actual card dimensions, not a fixed 760px
  // canvas. The old fixed width plus height:auto caused axes and labels to
  // overlap the card footer at desktop sizes.
  const W = Math.max(300,Math.round(area.clientWidth || 760));
  const H = Math.max(200,Math.round(area.clientHeight || 264));
  const L = W < 450 ? 52 : 68, R = W < 450 ? 14 : 24, T = 18, B = 40;
  const inner = W-L-R, usable = H-T-B;
  const values=[0];
  for (const point of data) for (const key of keys) if(Number.isFinite(point[key.key])) values.push(point[key.key]);
  const min=Math.min(...values), max=Math.max(...values);
  const padding=Math.max(0.035,(max-min)*0.18);
  const low=min-padding, high=max+padding;
  const x=i=>L+(data.length===1?inner/2:i/(data.length-1)*inner);
  const y=v=>T+(high-v)/(high-low)*usable;
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
  svg.setAttribute('preserveAspectRatio','xMidYMid meet');
  svg.setAttribute('role','group');
  svg.setAttribute('tabindex','0');
  svg.setAttribute('aria-label',`Portfolio, Treasury and S&P 500 returns; ${data.length} daily observation${data.length===1?'':'s'}. Use left and right arrow keys to inspect returns, Enter to pin, and Escape to dismiss.`);
  const add=(tag,attrs,container=svg)=>{
    const node=document.createElementNS('http://www.w3.org/2000/svg',tag);
    for(const [k,v] of Object.entries(attrs)) node.setAttribute(k,String(v));
    container.append(node); return node;
  };
  for(let i=0;i<5;i++) {
    const val=low+(high-low)*i/4, yy=y(val);
    add('line',{x1:L,y1:yy,x2:W-R,y2:yy,stroke:'#314867','stroke-width':1,'stroke-dasharray':'3 6'});
    const label=add('text',{x:L-11,y:yy+4,fill:'#a2bad0','font-size':W<450?11:12,'text-anchor':'end'});
    label.textContent=`${val>0?'+':''}${val.toFixed(Math.abs(val)<1?2:1)}%`;
  }
  if(low<0&&high>0) add('line',{x1:L,y1:y(0),x2:W-R,y2:y(0),stroke:'#70869b','stroke-width':1.2});

  keys.forEach((line,lineIndex)=>{
    let chunk=[];
    const flush=()=>{
      if(chunk.length>1) add('polyline',{
        points:chunk.map(([i,v])=>`${x(i)},${y(v)}`).join(' '),
        fill:'none',stroke:line.color,'stroke-width':line.key==='portfolio'?3.1:2.6,
        'stroke-linecap':'round','stroke-linejoin':'round'
      });
      chunk=[];
    };
    data.forEach((point,i)=>{
      if(Number.isFinite(point[line.key])) chunk.push([i,point[line.key]]);
      else flush();
    });
    flush();
    // Always show a distinctive colored end marker. One-day histories only
    // contain a point, so separate the three overlapping zero markers by 9px.
    let lastIndex=-1;
    for(let i=data.length-1;i>=0;i--) if(Number.isFinite(data[i][line.key])) {lastIndex=i;break;}
    if(lastIndex>=0){
      const offset=data.length===1 ? (lineIndex-1)*9 : 0;
      add('circle',{cx:x(lastIndex)+offset,cy:y(data[lastIndex][line.key]),r:data.length===1?4.8:4.1,
        fill:line.color,stroke:'#0d2237','stroke-width':1.5});
    }
  });
  const ticks=Math.min(W<450?3:5,data.length);
  for(let j=0;j<ticks;j++){
    const i=ticks===1?0:Math.round(j*(data.length-1)/(ticks-1));
    const label=add('text',{x:x(i),y:H-10,fill:'#97b0c7','font-size':W<450?10:12,'text-anchor':'middle'});
    const sameDate = data.length > 1 && data[0].date === data[data.length-1].date;
    label.textContent = sameDate && data[i].at
      ? new Date(data[i].at).toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit',timeZone:'UTC'})
      : formatDate(data[i].date).replace(/ \d{4}$/,'');
  }
  // V19.9: one lightweight, colour-matched tooltip. Values are taken from
  // the already-rendered percent-return series; no additional API calls.
  const tooltip = create('div','return-tooltip');
  tooltip.hidden = true;
  tooltip.setAttribute('aria-hidden','true');
  const tooltipDate = create('div','return-tooltip-date');
  const tooltipRows = new Map();
  tooltip.append(tooltipDate);
  for (const line of keys) {
    const row = create('div','return-tooltip-row');
    row.style.borderLeftColor = line.color;
    const label = create('span','return-tooltip-label',line.label);
    const value = create('strong','return-tooltip-value','—');
    value.style.color = line.color;
    row.append(label,value);
    tooltip.append(row);
    tooltipRows.set(line.key,value);
  }
  area.append(svg,tooltip);
  // A transparent hit surface makes hovering work between thin SVG lines.
  // It is last in the drawing order, before the non-interactive guide.
  add('rect',{x:0,y:0,width:W,height:H,fill:'transparent','pointer-events':'all'});

  // The marker is a visual guide; it never intercepts pointer or touch events.
  const guide = add('g',{'class':'return-chart-guide',opacity:0,'pointer-events':'none'});
  const guideLine = add('line',{x1:0,y1:T,x2:0,y2:H-B,stroke:'#8ba9c6','stroke-width':1.2,'stroke-dasharray':'3 5'},guide);
  const guideDots = keys.map(line => add('circle',{cx:0,cy:0,r:4.6,fill:line.color,stroke:'#071b2c','stroke-width':1.5},guide));
  let selected = -1;
  let pinned = false;
  const formatPct = (value, decimals) => {
    if (!Number.isFinite(value)) return '—';
    const rounded = Math.abs(value) < Math.pow(10,-decimals) / 2 ? 0 : value;
    return `${rounded > 0 ? '+' : rounded < 0 ? '−' : ''}${Math.abs(rounded).toFixed(decimals)}%`;
  };
  const indexFromSvgX = svgX => data.length === 1 ? 0 : Math.max(0,Math.min(data.length-1,Math.round((svgX-L)/inner*(data.length-1))));
  function showReturn(index, svgX=x(index), svgY=T+12) {
    selected = index;
    const point = data[index];
    tooltipDate.textContent = formatDate(point.date);
    for (const [j,line] of keys.entries()) {
      tooltipRows.get(line.key).textContent = formatPct(point[line.key],line.key==='cash'?4:2);
      const valid = Number.isFinite(point[line.key]);
      guideDots[j].style.display = valid ? '' : 'none';
      if (valid) {
        guideDots[j].setAttribute('cx',x(index));
        guideDots[j].setAttribute('cy',y(point[line.key]));
      }
    }
    guideLine.setAttribute('x1',x(index)); guideLine.setAttribute('x2',x(index));
    guide.setAttribute('opacity','1');
    tooltip.hidden = false; tooltip.setAttribute('aria-hidden','false');
    const rect = area.getBoundingClientRect();
    const scaleX = rect.width / W, scaleY = rect.height / H;
    const anchorX = svgX * scaleX, anchorY = svgY * scaleY;
    const tipWidth = tooltip.offsetWidth || 190, tipHeight = tooltip.offsetHeight || 130;
    const rightSide = anchorX + tipWidth + 22 <= rect.width;
    const left = rightSide ? anchorX + 14 : anchorX - tipWidth - 14;
    tooltip.style.left = `${Math.max(6,Math.min(left,rect.width-tipWidth-6))}px`;
    tooltip.style.top = `${Math.max(6,Math.min(anchorY-tipHeight/2,rect.height-tipHeight-6))}px`;
  }
  function hideReturn() {
    selected = -1;
    tooltip.hidden = true; tooltip.setAttribute('aria-hidden','true');
    guide.setAttribute('opacity','0');
  }
  function svgPosition(event) {
    const rect = svg.getBoundingClientRect();
    if (!rect.width || !rect.height) return {x:L,y:T};
    return {x:(event.clientX-rect.left)*W/rect.width,y:(event.clientY-rect.top)*H/rect.height};
  }
  svg.addEventListener('pointermove', event => {
    if (pinned || (event.pointerType === 'touch' && event.buttons)) return;
    const pos = svgPosition(event);
    if (pos.x < L-12 || pos.x > W-R+12) { hideReturn(); return; }
    showReturn(indexFromSvgX(pos.x),pos.x,pos.y);
  });
  svg.addEventListener('pointerleave',()=>{ if (!pinned) hideReturn(); });
  svg.addEventListener('click',event=>{
    const pos=svgPosition(event), index=indexFromSvgX(pos.x);
    if (pinned && selected === index) { pinned=false; hideReturn(); return; }
    pinned=true; showReturn(index,pos.x,pos.y);
  });
  svg.addEventListener('focus',()=>{ if (selected < 0) showReturn(data.length-1); });
  svg.addEventListener('blur',()=>{ if (!pinned) hideReturn(); });
  svg.addEventListener('keydown',event=>{
    if (event.key === 'Escape') { pinned=false; hideReturn(); event.preventDefault(); return; }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      const next=selected<0?data.length-1:Math.max(0,Math.min(data.length-1,selected+(event.key==='ArrowRight'?1:-1)));
      showReturn(next); event.preventDefault();
    } else if (event.key === 'Enter' || event.key === ' ') {
      if (selected<0) showReturn(data.length-1);
      pinned = !pinned;
      if (!pinned) hideReturn();
      event.preventDefault();
    }
  });
  chartOutsidePointerHandler = event => {
    if (!area.contains(event.target)) { pinned=false; hideReturn(); }
  };
  document.addEventListener('pointerdown',chartOutsidePointerHandler);
}
// Redraw only on window resizing; viewing the chart never queries Alchemy.
let portfolioChartResizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(portfolioChartResizeTimer);
  portfolioChartResizeTimer = setTimeout(() => { if (portfolio) renderChart(); }, 150);
});
init();
