
'use strict';

const el = id => document.getElementById(id);

const COLORS = {
  ethereum:'#94a7ff',
  base:'#78c7ff',
  arbitrum:'#7fe5d6',
  polygon:'#b9a9ff',
  bnb:'#ffce8b',
  other:'#a9b7cc'
};

let namiPreview = null;
let performanceData = null;
let portfolio = null;
let settings = {};
let period = 'all';
let typeFilter = 'all';
let currency = 'USD';
let preferredCurrency = 'USD';
let gbpRate = null;
let gbpDate = null;
let fxState = 'waiting';

try {
  if (localStorage.getItem('portfolio-currency') === 'GBP') {
    preferredCurrency = 'GBP';
  }
} catch (_) {
  // Private browsing may disallow storage.
}

function money(n, compact = false) {
  if (n === null || n === undefined || n === '') return '—';

  const value = Number(n);
  if (!Number.isFinite(value)) return '—';

  const converted = currency === 'GBP'
    ? value * (gbpRate || 1)
    : value;

  const unit = currency === 'GBP' ? '£' : '$';

  if (compact && Math.abs(converted) >= 1e12)
    return unit + (converted / 1e12).toFixed(2) + 'T';

  if (compact && Math.abs(converted) >= 1e9)
    return unit + (converted / 1e9).toFixed(2) + 'B';

  if (compact && Math.abs(converted) >= 1e6)
    return unit + (converted / 1e6).toFixed(2) + 'M';

  if (compact && Math.abs(converted) >= 1e3)
    return unit + (converted / 1e3).toFixed(1) + 'K';

  return new Intl.NumberFormat(
    currency === 'USD' ? 'en-US' : 'en-GB',
    {
      style:'currency',
      currency,
      maximumFractionDigits:2
    }
  ).format(converted);
}

function unitPrice(value) {
  const n = Number(value) *
    (currency === 'GBP' ? (gbpRate || 1) : 1);

  if (!Number.isFinite(n) || n < 0) return '—';
  if (n === 0) return currency === 'GBP' ? '£0.00' : '$0.00';
  if (n >= 0.01) return money(value);

  const symbol = currency === 'GBP' ? '£' : '$';

  return symbol + n.toLocaleString(
    currency === 'GBP' ? 'en-GB' : 'en-US',
    {
      maximumSignificantDigits:4,
      maximumFractionDigits:9
    }
  );
}

function formatQty(n) {
  if (n === null || n === undefined || n === '') return '—';

  const x = Number(n);

  return Number.isFinite(x)
    ? new Intl.NumberFormat(
        'en-GB',
        {maximumSignificantDigits:7}
      ).format(x)
    : '—';
}

const safeUsd = row =>
  row.usd === null ||
  row.usd === undefined ||
  !Number.isFinite(Number(row.usd))
    ? 0
    : Number(row.usd);

function create(tag, cls, content) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (content !== undefined) node.textContent = content;
  return node;
}

function text(id, value) {
  if (el(id)) el(id).textContent = value;
}

function formatDate(date) {
  const d = new Date(date);

  return Number.isNaN(d.getTime())
    ? 'Unknown date'
    : d.toLocaleDateString('en-GB', {
        day:'numeric',
        month:'short',
        year:'numeric',
        timeZone:'UTC'
      });
}

function formatUtcTime(date) {
  const d = new Date(date);

  return Number.isNaN(d.getTime())
    ? 'unknown time'
    : d.toLocaleString('en-GB', {
        day:'numeric',
        month:'short',
        hour:'2-digit',
        minute:'2-digit',
        timeZone:'UTC'
      }) + ' UTC';
}

// ------------------------------------------------------------
// PORTFOLIO AGE
// ------------------------------------------------------------

function initialTimestamp() {
  const configured =
    typeof settings.liveSince === 'string' &&
    settings.liveSince.trim()
      ? settings.liveSince
      : null;

  const first = portfolio?.history?.[0]?.date;

  const chosen =
    configured ||
    portfolio?.startedAt ||
    (first ? first + 'T00:00:00Z' : null);

  if (!chosen) return null;

  const millis = Date.parse(chosen);
  return Number.isFinite(millis) ? millis : null;
}

// Calendar anniversary arithmetic avoids treating every
// calendar month as exactly 30 days.
function completedCalendarMonths(start, now) {
  const from = new Date(start);
  const until = new Date(now);

  if (
    !Number.isFinite(start) ||
    !Number.isFinite(now) ||
    now < start
  ) return 0;

  const startMonth =
    from.getUTCFullYear() * 12 + from.getUTCMonth();

  const endMonth =
    until.getUTCFullYear() * 12 + until.getUTCMonth();

  let count = Math.max(0, endMonth - startMonth);

  if (!count) return 0;

  const totalMonths = from.getUTCMonth() + count;
  const year =
    from.getUTCFullYear() + Math.floor(totalMonths / 12);
  const month = totalMonths % 12;

  const finalDay =
    new Date(Date.UTC(year, month + 1, 0)).getUTCDate();

  const anniversary = Date.UTC(
    year,
    month,
    Math.min(from.getUTCDate(), finalDay),
    from.getUTCHours(),
    from.getUTCMinutes(),
    from.getUTCSeconds(),
    from.getUTCMilliseconds()
  );

  if (now < anniversary) count--;

  return Math.max(0, count);
}

// Change the displayed age from days to weeks,
// months and eventually years.
function renderDays() {
  const start = initialTimestamp();
  const badge = el('days-live');
  const caption = document.querySelector('.days-caption');

  if (start === null) {
    if (badge) {
      badge.textContent = '—';
      badge.title = 'Starts with the first snapshot';
    }

    if (caption) caption.textContent = 'DAYS LIVE';
    return;
  }

  const now = Date.now();

  const days = Math.max(
    0,
    Math.floor((now - start) / 86400000)
  );

  const months = completedCalendarMonths(start, now);

  let count = days;
  let unit = 'DAYS LIVE';

  if (months >= 24) {
    count = Math.floor(months / 12);
    unit = 'YEARS LIVE';
  } else if (months >= 2) {
    count = months;
    unit = 'MONTHS LIVE';
  } else if (days >= 14) {
    count = Math.floor(days / 7);
    unit = 'WEEKS LIVE';
  }

  if (badge) {
    badge.textContent = String(count);

    badge.title =
      `${days} completed days since ` +
      formatUtcTime(new Date(start).toISOString());
  }

  if (caption) caption.textContent = unit;
}

// ------------------------------------------------------------
// DATA LOADING AND CURRENCY
// ------------------------------------------------------------

async function fetchJson(path) {
  const response = await fetch(
    path + '?v=' + Date.now(),
    {cache:'no-store'}
  );

  if (!response.ok) {
    throw Error(`Failed to load ${path}`);
  }

  return response.json();
}

function setCurrencyButtons() {
  document.querySelectorAll('[data-currency]').forEach(btn => {
    const active = btn.dataset.currency === currency;

    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });

  text('hero-currency', '● ' + currency);

  const gbpButton =
    document.querySelector('[data-currency="GBP"]');

  if (gbpButton) {
    gbpButton.title = gbpRate
      ? `USD to GBP reference rate ${gbpRate.toFixed(5)} as of ${formatDate(gbpDate)}`
      : 'GBP conversion requires an exchange rate';
  }

  text(
    'history-description',
    'Cumulative investment return (%) · compared with Treasury cash and S&P 500'
  );
}

function adoptFx(data) {
  const rate = Number(data?.rate);

  if (
    !Number.isFinite(rate) ||
    rate <= 0 ||
    rate >= 2 ||
    typeof data?.date !== 'string' ||
    !/^\d{4}-\d{2}-\d{2}$/.test(data.date)
  ) return false;

  const ageDays =
    (Date.now() - Date.parse(data.date + 'T00:00:00Z')) /
    86400000;

  if (
    !Number.isFinite(ageDays) ||
    ageDays > 7 ||
    ageDays < -2
  ) return false;

  gbpRate = rate;
  gbpDate = data.date;
  fxState = 'ready';

  return true;
}

async function refreshFx() {
  // Frankfurter publishes daily reference rates.
  // Cached rates retain their observation date.

  try {
    const cached = JSON.parse(
      localStorage.getItem('portfolio-fx') || 'null'
    );

    if (cached && adoptFx(cached)) {
      if (preferredCurrency === 'GBP') currency = 'GBP';

      setCurrencyButtons();
      if (portfolio) render();
      return;
    }
  } catch (_) {
    // Storage may be unavailable.
  }

  const ctl = new AbortController();

  const timer = setTimeout(
    () => ctl.abort(),
    9000
  );

  try {
    const response = await fetch(
      'https://api.frankfurter.dev/v2/rate/usd/gbp',
      {
        signal:ctl.signal,
        cache:'no-store'
      }
    );

    if (!response.ok) {
      throw Error('FX response unavailable');
    }

    const data = await response.json();

    if (!adoptFx(data)) {
      throw Error('Invalid FX response');
    }

    try {
      localStorage.setItem(
        'portfolio-fx',
        JSON.stringify({
          rate:gbpRate,
          date:gbpDate
        })
      );
    } catch (_) {
      // Optional cache.
    }
  } catch (_) {
    if (!gbpRate) {
      fxState = 'unavailable';
      currency = 'USD';
    }
  } finally {
    clearTimeout(timer);

    if (gbpRate && preferredCurrency === 'GBP') {
      currency = 'GBP';
    }

    setCurrencyButtons();

    if (portfolio) render();
  }
}

// ------------------------------------------------------------
// INITIALISATION
// ------------------------------------------------------------

function init() {
  document.querySelectorAll('[data-period]').forEach(btn => {
    btn.addEventListener('click', () => {
      period = btn.dataset.period;

      document.querySelectorAll('[data-period]').forEach(x => {
        x.classList.toggle('active', x === btn);
      });

      renderChart();
      renderPerformanceSummary();
      renderHeroAccounting();
    });
  });

  document.querySelectorAll('[data-currency]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const desired = btn.dataset.currency;

      if (desired === 'GBP' && !gbpRate) {
        await refreshFx();

        if (!gbpRate) {
          text(
            'fx-label',
            'Could not load the GBP rate. Please check your connection.'
          );
          return;
        }
      }

      currency = desired;
      preferredCurrency = desired;

      try {
        localStorage.setItem(
          'portfolio-currency',
          currency
        );
      } catch (_) {
        // Optional preference.
      }

      setCurrencyButtons();

      if (portfolio) render();
    });
  });

  el('search-input').addEventListener(
    'input',
    renderAssets
  );

  el('refresh-btn').addEventListener(
    'click',
    requestPortfolioRefresh
  );

  el('demo-btn').addEventListener(
    'click',
    () => load(true)
  );

  setInterval(renderDays, 30000);

  setCurrencyButtons();
  renderDays();

  load();

  // No extra FX network request for the default USD view.
  try {
    const cached = JSON.parse(
      localStorage.getItem('portfolio-fx') || 'null'
    );

    if (
      cached &&
      adoptFx(cached) &&
      preferredCurrency === 'GBP'
    ) {
      currency = 'GBP';
    }
  } catch (_) {}

  setCurrencyButtons();
}

async function load(demo = false) {
  try {
    settings = await fetchJson('config.json');

    document.title =
      (settings.title || 'Portfolio') +
      ' | Portfolio Tracker';
  } catch (_) {
    // Keep default title.
  }

  try {
    const result = await fetchJson(
      demo ? 'examples/demo-data.json' : 'data.json'
    );

    if (
      result.schema !== 2 ||
      !result.last ||
      !Array.isArray(result.last.rows) ||
      !Array.isArray(result.history)
    ) {
      throw Error('Invalid snapshot');
    }

    portfolio = result;
    performanceData = null;
    namiPreview = null;

    if (!demo) {
      try {
        const perf = await fetchJson('performance.json');

        if (
          perf.schema === 1 &&
          perf.snapshotAt === result.last.at &&
          perf.firstDate === result.history[0]?.date &&
          Array.isArray(perf.series)
        ) {
          performanceData = perf;
        }
      } catch (_) {
        // Snapshot and performance files may briefly
        // be out of sync during generation.
      }
    }

    const hasNami = (result.last.rows || []).some(
      row => row.protocol === 'Nami' && row.usd != null
    );

    if (!demo && !hasNami) {
      try {
        const n = await fetchJson('nami-preview.json');

        if (
          n.schema === 1 &&
          n.provider === 'Nami on-chain (Alchemy)' &&
          n.includedInAuditedTotal === false &&
          Array.isArray(n.positions)
        ) {
          namiPreview = n;
        }
      } catch (_) {
        // Optional position preview.
      }
    }

    el('dashboard').classList.remove('hidden');
    el('empty-state').classList.add('hidden');

    text(
      'sync-label',
      demo
        ? 'Illustrative example · not connected'
        : `Snapshot · ${formatUtcTime(result.last.at || result.last.date)}${performanceData?.hasUnverifiedLatest ? ' · P&L awaiting review' : ''}`
    );

    render();
  } catch (_) {
    if (demo) {
      text('sync-label', 'Example data not available');
      return;
    }

    portfolio = null;

    el('dashboard').classList.add('hidden');
    el('empty-state').classList.remove('hidden');

    text('sync-label', 'No valid snapshot yet');
    renderDays();
  }
}

// ------------------------------------------------------------
// LOCAL AND PUBLIC REFRESH
// ------------------------------------------------------------

// The public refresh endpoint dispatches GitHub Actions
// without exposing credentials to browser visitors.
// Localhost uses the local Python snapshot server.

let refreshInProgress = false;

const delay = ms =>
  new Promise(resolve => setTimeout(resolve, ms));

function refreshMessage(message) {
  text('sync-label', message);
}

function snapshotTimestamp() {
  return portfolio?.last?.at || null;
}

async function requestPortfolioRefresh() {
  if (refreshInProgress) return;

  const button = el('refresh-btn');
  const oldStamp = snapshotTimestamp();

  const isLocal = [
    '127.0.0.1',
    'localhost'
  ].includes(location.hostname);

  const configuredEndpoint =
    typeof settings.refreshEndpoint === 'string'
      ? settings.refreshEndpoint.trim()
      : '';

  refreshInProgress = true;
  button.disabled = true;
  button.textContent = '↻ Checking…';

  try {
    if (!isLocal && !configuredEndpoint) {
      await load();

      refreshMessage(
        'Latest published snapshot loaded · live refresh is not enabled yet'
      );
      return;
    }

    if (
      !isLocal &&
      !/^https:\/\/[^/]+\/refresh\/?$/.test(configuredEndpoint)
    ) {
      throw new Error(
        'Public refresh endpoint is not configured correctly'
      );
    }

    const endpoint = isLocal
      ? '/api/refresh'
      : configuredEndpoint;

    const response = await fetch(endpoint, {
      method:'POST',
      headers:{
        'Content-Type':'application/json'
      },
      body:'{}',
      cache:'no-store'
    });

    const outcome = await response.json();

    if (!response.ok) {
      throw new Error(
        outcome.message || 'Refresh service unavailable'
      );
    }

    if (outcome.status === 'cooldown') {
      await load();

      const until = outcome.nextAllowedAt
        ? new Date(outcome.nextAllowedAt).toLocaleTimeString(
            [],
            {
              hour:'2-digit',
              minute:'2-digit'
            }
          )
        : 'later';

      refreshMessage(
        `Using latest published snapshot · next on-chain refresh available after ${until}`
      );

      return;
    }

    if (
      !['queued','started','running'].includes(outcome.status)
    ) {
      throw new Error('Unknown refresh response');
    }

    refreshMessage(
      isLocal
        ? 'Reading Base wallet and LP positions…'
        : 'Update queued · publishing a fresh portfolio snapshot…'
    );

    button.textContent = '↻ Updating…';

    const maxPolls = isLocal ? 40 : 18;
    const pollInterval = isLocal ? 5000 : 15000;

    for (let attempt = 0; attempt < maxPolls; attempt++) {
      await delay(pollInterval);

      if (isLocal) {
        const state = await fetchJson('/api/status');

        if (state.status === 'failed') {
          throw new Error(
            'Snapshot failed on the local server; check the terminal output'
          );
        }

        if (state.status === 'done') {
          await load();

          refreshMessage(
            'Portfolio updated · ' +
            formatDate(snapshotTimestamp())
          );

          return;
        }
      } else {
        // Reading published JSON does not call Alchemy.
        try {
          const latest = await fetchJson('data.json');

          if (
            latest?.last?.at &&
            latest.last.at !== oldStamp
          ) {
            await load();

            refreshMessage(
              'Portfolio updated · ' +
              formatDate(latest.last.at)
            );

            return;
          }
        } catch (_) {
          // GitHub Pages may briefly be unavailable during deployment.
        }
      }
    }

    await load();

    refreshMessage(
      isLocal
        ? 'Update still running · check your local terminal'
        : 'Update queued · visit again shortly to see the new published snapshot'
    );
  } catch (err) {
    await load();

    refreshMessage(
      'Refresh unavailable · ' +
      (err?.message || 'try again later').slice(0,110)
    );
  } finally {
    refreshInProgress = false;
    button.disabled = false;
    button.textContent = '↻ Refresh portfolio';
  }
}

// ------------------------------------------------------------
// PORTFOLIO SUMMARY
// ------------------------------------------------------------

function render() {
  if (!portfolio) return;

  const last = portfolio.last;
  const rows = last.rows || [];

  text('portfolio-total', money(last.totalUsd));

  renderPerformanceSummary();
  renderHeroAccounting();

  for (const [kind,field] of [
    ['token','tokens'],
    ['lp','lp'],
    ['manual','manual']
  ]) {
    const filtered = rows.filter(
      r => r.type === kind && r.usd != null
    );

    text(
      field + '-total',
      money(
        filtered.reduce(
          (acc,r) => acc + safeUsd(r),
          0
        )
      )
    );

    const label =
      kind === 'lp' ? 'position' :
      kind === 'token' ? 'asset' : 'entry';

    text(
      field + '-count',
      `${filtered.length} valued ${label}${filtered.length === 1 ? '' : 's'}`
    );
  }

  text(
    'tvl-total',
    last.tvlUsd == null
      ? 'Unavailable'
      : money(last.tvlUsd, true)
  );

  renderDays();
  setCurrencyButtons();
  renderLPs();
  renderAllocation();
  renderChart();
  renderAssets();
}

// ------------------------------------------------------------
// FLOW-ADJUSTED PERFORMANCE
// ------------------------------------------------------------

// The backend calculates investment P&L.
// The browser displays it without changing the accounting.

function renderPerformanceSummary() {
  const change = el('day-change');
  if (!change) return;

  change.className = 'change';

  const item =
    performanceData?.periodSummary?.[period];

  const partial =
    performanceData?.mode === 'partially-verified' ||
    !!performanceData?.hasUnverifiedLatest;

  if (!item || !Number.isFinite(Number(item.usd))) {
    const pending = Number(
      performanceData?.audit?.pendingCount ||
      portfolio?.capitalAudit?.pending?.length ||
      0
    );

    change.textContent = pending || partial
      ? 'P&L awaiting review'
      : period === 'all'
        ? 'P&L history unavailable'
        : `Not enough history for ${period}D P&L`;

    change.title =
      'The selected period needs verified portfolio observations.';

    return;
  }

  const amount = Number(item.usd);
  const percent = Number(item.pct);

  const sign =
    amount > 0 ? '+' :
    amount < 0 ? '−' : '';

  const pctText = Number.isFinite(percent)
    ? ` (${percent > 0 ? '+' : ''}${percent.toFixed(2)}%)`
    : '';

  const hours = Number(item.hours);

  // A 1D period may contain less than a full day
  // of actual portfolio observations.
  const shortWindow =
    period === '1' &&
    Number.isFinite(hours) &&
    hours > 0 &&
    hours < 23.5;

  const duration = shortWindow
    ? ` · ${hours < 10 ? hours.toFixed(1) : hours.toFixed(0)}h`
    : '';

  change.textContent =
    `${sign}${money(Math.abs(amount))}${pctText}` +
    `${duration}${partial ? ' · verified portion' : ''}`;

  change.title =
    `Flow-adjusted portfolio P&L, ${item.from} to ${item.to}` +
    `${shortWindow ? ` (${hours.toFixed(2)} hours, not a full day)` : ''}. ` +
    'This is an estimate from dated valuations.';

  change.classList.add(
    amount < 0 ? 'negative' :
    amount > 0 ? 'positive' : ''
  );
}

function signedMoney(value) {
  const number = Number(value);

  if (!Number.isFinite(number)) return '—';

  return (
    number > 0 ? '+' :
    number < 0 ? '−' : ''
  ) + money(Math.abs(number));
}

// Suppress routine healthy-accounting messages.
// Keep exceptions and real financial movements visible.

function renderHeroAccounting() {
  const audit = performanceData?.audit;

  text(
    'hero-valued-at',
    'Latest valuation · ' +
    formatUtcTime(portfolio?.last?.at)
  );

  const setNote = (id, message, tooltip = '') => {
    const node = el(id);
    if (!node) return;

    node.textContent = message;
    node.title = tooltip;
    node.classList.toggle('hidden', !message);
  };

  const capital = audit
    ? Number(audit.netExternalCapitalUsd)
    : NaN;

  setNote(
    'hero-capital',
    Number.isFinite(capital) && Math.abs(capital) >= 0.005
      ? 'Net transfers · ' + signedMoney(capital)
      : '',
    'Deposits less withdrawals since the audited portfolio baseline; not investment profit.'
  );

  const includedNami =
    (portfolio?.last?.rows || []).some(
      row => row.protocol === 'Nami' && row.usd != null
    );

  const bridge = audit
    ? Number(audit.coverageAdjustmentUsd)
    : NaN;

  if (!includedNami) {
    setNote(
      'hero-coverage',
      'Nami excluded from total',
      'Any supplementary Nami preview is excluded from the audited portfolio valuation and P&L.'
    );
  } else if (
    Number.isFinite(bridge) &&
    Math.abs(bridge) >= 0.005
  ) {
    setNote(
      'hero-coverage',
      'History adjustment · ' + signedMoney(bridge),
      'Historical position-coverage correction, not investment profit or an external deposit.'
    );
  } else {
    setNote('hero-coverage', '');
  }

  const pending = Number(
    audit?.pendingCount ??
    portfolio?.capitalAudit?.pending?.length ??
    0
  );

  const mode = performanceData?.mode;

  const auditMessage =
    pending > 0
      ? `${pending} transfer${pending === 1 ? '' : 's'} to review`
      : mode &&
        mode !== 'audited-automatic' &&
        mode !== 'flow-adjusted-estimate'
          ? 'P&L verification pending'
          : '';

  setNote(
    'hero-audit',
    auditMessage,
    'A complete capital review is required before reporting new verified investment performance.'
  );

  const container =
    el('hero-capital')?.closest('.hero-accounting');

  if (container) {
    container.classList.toggle(
      'hidden',
      [
        'hero-capital',
        'hero-coverage',
        'hero-audit'
      ].every(
        id => el(id)?.classList.contains('hidden')
      )
    );
  }
}

// ------------------------------------------------------------
// LIQUIDITY POSITIONS
// ------------------------------------------------------------

function renderLPs() {
  const target = el('lp-cards');
  target.replaceChildren();

  const lps =
    (portfolio.last.rows || []).filter(
      r => r.type === 'lp'
    );

  if (!lps.length && !(namiPreview?.positions?.length)) {
    target.append(
      create('p','allocation-empty','No LP positions yet.')
    );
    return;
  }

  for (const lp of lps) {
    const card = create('article','lp-balance-card');

    const header = create('div','lp-card-head');
    const left = create('div','lp-pair-title');

    left.append(
      create('span','lp-mark','◈'),
      create(
        'span',
        null,
        (lp.protocol === 'Nami' ? 'Nami · ' : '') +
        (lp.name || 'Liquidity position')
      )
    );

    header.append(
      left,
      create('div','lp-card-value',money(lp.usd))
    );

    card.append(header);

    const group = create(
      'div',
      'lp-component-list'
    );

    if (Array.isArray(lp.underlying)) {
      for (const part of lp.underlying) {
        const item = create('div','lp-component');

        item.append(
          create(
            'span',
            'lp-token',
            part.symbol || 'Token'
          ),
          create(
            'span',
            'lp-amount',
            `${formatQty(part.amount)} ${part.symbol || ''}`
          )
        );

        group.append(item);
      }
    } else {
      group.append(
        create(
          'div',
          'lp-component',
          'Underlying tokens unavailable'
        )
      );
    }

    card.append(group);

    const foot = create('div','lp-card-footer');

    const status =
      lp.usd == null ? 'Value unavailable' :
      lp.source === 'manual quantities' ? 'Manual estimate' :
      'On-chain';

    if (status) {
      const note = create('span',null,status);

      note.title =
        lp.source === 'on-chain best effort'
          ? 'Current valuation is on-chain; historical range coverage may be incomplete.'
          : '';

      foot.append(note);
    }

    if (
      typeof lp.url === 'string' &&
      /^https:\/\//.test(lp.url)
    ) {
      const link = create('a',null,'View pool');

      link.href = lp.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';

      foot.append(link);
    }

    if (foot.childNodes.length) {
      card.append(foot);
    }

    target.append(card);
  }

  appendSupplementaryNami(target);
}

// Supplementary Nami previews are not counted in
// audited portfolio values or investment P&L.

function appendSupplementaryNami(target) {
  if (
    !namiPreview ||
    namiPreview.status !== 'verified-position' ||
    !Array.isArray(namiPreview.positions)
  ) return;

  const reading = Date.parse(namiPreview.at || '');
  const snapshot = Date.parse(portfolio?.last?.at || '');

  const stale =
    !Number.isFinite(reading) ||
    (Date.now() - reading) > 48 * 3600000;

  const differentTime =
    Number.isFinite(reading) &&
    Number.isFinite(snapshot) &&
    Math.abs(reading - snapshot) > 3600000;

  const accountedIds = new Set(
    (portfolio?.last?.rows || [])
      .filter(r => r.type === 'lp')
      .map(r => r.id)
  );

  for (const pos of namiPreview.positions) {
    if (accountedIds.has(pos.id)) continue;

    const card = create(
      'article',
      'lp-balance-card supplementary-lp'
    );

    const head = create('div','lp-card-head');
    const label = create('div','lp-pair-title');

    label.append(
      create('span','lp-mark','◈'),
      create(
        'span',
        null,
        'Nami · ' + (pos.name || 'WETH / cbBTC')
      )
    );

    head.append(
      label,
      create('div','lp-card-value',money(pos.usd))
    );

    card.append(head);

    const group = create(
      'div',
      'lp-component-list'
    );

    for (const part of pos.underlying || []) {
      const item = create('div','lp-component');

      item.append(
        create(
          'span',
          'lp-token',
          part.symbol || 'Token'
        ),
        create(
          'span',
          'lp-amount',
          formatQty(part.amount) + ' ' + (part.symbol || '')
        )
      );

      group.append(item);
    }

    card.append(group);

    const foot = create('div','lp-card-footer');

    const note =
      (stale
        ? 'Older snapshot · '
        : differentTime
          ? 'Different snapshot time · '
          : '') +
      'Excluded from total';

    foot.append(
      create(
        'span',
        'supplementary-note',
        note
      )
    );

    if (
      typeof pos.url === 'string' &&
      /^https:\/\/basescan\.org\//.test(pos.url)
    ) {
      const link = create('a',null,'View NFT');

      link.href = pos.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';

      foot.append(link);
    }

    card.append(foot);
    target.append(card);
  }
}

// ------------------------------------------------------------
// ALLOCATION
// ------------------------------------------------------------

function renderAllocation() {
  const rows = portfolio?.last?.rows || [];
  const overall = Number(portfolio?.last?.totalUsd);

  for (const kind of ['token','lp']) {
    const subtotal = rows
      .filter(row => row.type === kind)
      .reduce(
        (sum,row) => sum + safeUsd(row),
        0
      );

    const percentage =
      Number.isFinite(overall) && overall > 0
        ? Math.max(
            0,
            Math.min(
              100,
              (subtotal / overall) * 100
            )
          )
        : 0;

    const prefix =
      kind === 'token' ? 'tokens' : 'lp';

    text(
      prefix + '-share',
      percentage.toFixed(1) + '%'
    );

    const fill = el(prefix + '-share-bar');

    if (fill) {
      fill.style.width = percentage + '%';
    }

    const track = el(prefix + '-share-track');

    if (track) {
      track.setAttribute(
        'aria-valuenow',
        percentage.toFixed(1)
      );
    }
  }
}

// ------------------------------------------------------------
// WALLET ASSETS
// ------------------------------------------------------------

function renderAssets() {
  if (!portfolio) return;

  const q =
    (el('search-input')?.value || '')
      .trim()
      .toLowerCase();

  const rows =
    (portfolio.last.rows || [])
      .filter(r => r.type === 'token');

  const entries = rows.filter(r =>
    !q ||
    [r.name,r.symbol]
      .join(' ')
      .toLowerCase()
      .includes(q)
  );

  const body = el('assets-body');
  body.replaceChildren();

  text(
    'assets-subtitle',
    `${entries.length} of ${rows.length} wallet token balances · Base`
  );

  el('assets-empty').classList.toggle(
    'hidden',
    entries.length !== 0
  );

  for (const r of entries) {
    const tr = create('tr');
    const asset = create('td');
    const line = create('div','asset-cell');

    const avatar = create(
      'div',
      'asset-icon',
      String(r.symbol || '?')
        .slice(0,2)
        .toUpperCase()
    );

    const title = create(
      'div',
      'asset-name'
    );

    if (
      typeof r.url === 'string' &&
      /^https:\/\//.test(r.url)
    ) {
      const link = create(
        'a',
        null,
        r.name || r.symbol || 'Token'
      );

      link.href = r.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';

      title.append(link);
    } else {
      title.textContent =
        r.name || r.symbol || 'Token';
    }

    line.append(avatar,title);
    asset.append(line);

    const bal = create(
      'td',
      'asset-balance',
      `${formatQty(r.amount)} ${r.symbol || ''}`
    );

    tr.append(
      asset,
      bal,
      create(
        'td',
        'value-cell',
        money(r.usd)
      )
    );

    body.append(tr);
  }
}

// ------------------------------------------------------------
// INVESTMENT PERFORMANCE CHART
// ------------------------------------------------------------

let chartOutsidePointerHandler = null;

function renderChart() {
  if (!portfolio) return;

  const area = el('chart-area');
  area.replaceChildren();

  area.setAttribute('role','group');
  area.setAttribute(
    'aria-label',
    'Interactive investment returns chart. Hover, tap or use arrow keys to inspect each date.'
  );

  if (chartOutsidePointerHandler) {
    document.removeEventListener(
      'pointerdown',
      chartOutsidePointerHandler
    );

    chartOutsidePointerHandler = null;
  }

  const subtitle = el('performance-warning');
  const summaries = el('performance-legend');

  summaries.replaceChildren();

  // Preserve the complete historical series even when
  // close-aligned benchmark observations become available.

  const comparison = performanceData?.closeComparison;

  const aligned =
    comparison?.mode === 'close-aligned' &&
    Array.isArray(comparison.series) &&
    comparison.series.length >= 2;

  const raw =
    Array.isArray(performanceData?.series)
      ? performanceData.series
      : [];

  const alignmentStatus = el('comparison-status');

  if (alignmentStatus) {
    const count =
      Array.isArray(comparison?.series)
        ? comparison.series.length
        : 0;

    alignmentStatus.textContent = aligned
      ? `${count} aligned market sessions · mixed-time history`
      : 'Benchmark comparison not yet close-aligned';

    alignmentStatus.title =
      comparison?.note ||
      'Historical wallet valuations and stock-market closes may have different timestamps. Do not interpret their daily values as a same-time comparison.';
  }

  // Keep dates on or after the audited portfolio baseline.

  const anchor =
    performanceData?.portfolioStartDate ||
    performanceData?.baselineDate ||
    null;

  const eligible = raw
    .filter(
      row =>
        typeof row?.date === 'string' &&
        /^\d{4}-\d{2}-\d{2}$/.test(row.date) &&
        (!anchor || row.date >= anchor)
    )
    .sort(
      (a,b) => a.date.localeCompare(b.date)
    );

  const latestDay = eligible.length
    ? Date.parse(
        eligible[eligible.length - 1].at ||
        eligible[eligible.length - 1].date +
          'T00:00:00Z'
      )
    : NaN;

  const periodDays =
    period === 'all'
      ? null
      : Number(period);

  const source = eligible.filter(
    row =>
      periodDays === null ||
      latestDay -
        Date.parse(
          row.at || row.date + 'T00:00:00Z'
        ) <= periodDays * 86400000
  );

  // Period filtering changes only the view.
  // Historical points are never deleted.

  if (alignmentStatus && eligible.length) {
    if (period !== 'all') {
      alignmentStatus.textContent +=
        ` · ${source.length} daily point${source.length === 1 ? '' : 's'} in ${period}D view`;
    }

    const windowHours = Number(
      performanceData?.periodSummary?.['1']?.hours
    );

    if (
      period === '1' &&
      Number.isFinite(windowHours) &&
      windowHours > 0 &&
      windowHours < 23.5
    ) {
      alignmentStatus.textContent +=
        ` · P&L spans ${windowHours.toFixed(1)}h`;
    }
  }

  const initial = source[0] || {};

  // Retain the original chart colours.
  const keys = [
    {
      key:'portfolio',
      label:'My portfolio',
      color:'#82cfff'
    },
    {
      key:'cash',
      label:'3M Treasury cash',
      color:'#8ce5c1'
    },
    {
      key:'sp500',
      label:'S&P 500 return',
      color:'#d5aaff'
    }
  ];

  // Rebase each displayed series to the selected view's start.
  const data = source.map(row => {
    const point = {
      date:row.date,
      at:row.at || null
    };

    for (const line of keys) {
      const start = initial[line.key];
      const current = row[line.key];

      const startNumber =
        typeof start === 'number'
          ? start
          : NaN;

      const value =
        typeof current === 'number'
          ? current
          : NaN;

      point[line.key] =
        Number.isFinite(startNumber) &&
        Number.isFinite(value) &&
        (1 + startNumber / 100) > 0
          ? 100 * (
              (1 + value / 100) /
              (1 + startNumber / 100) -
              1
            )
          : null;
    }

    return point;
  });

  text(
    'history-span',
    data.length
      ? `${formatDate(data[0].date)} – ${formatDate(data[data.length - 1].date)}`
      : 'No benchmark history yet'
  );

  // ----------------------------------------------------------
  // COLOUR-MATCHED LEGEND
  // ----------------------------------------------------------

  for (const item of keys) {
    const lastValid = [...data]
      .reverse()
      .find(
        p => Number.isFinite(p[item.key])
      );

    const node = create(
      'div',
      'performance-legend-item'
    );

    if (!lastValid) {
      node.classList.add('series-unavailable');
    }

    node.title = lastValid
      ? `${item.label}: latest ${lastValid[item.key].toFixed(3)}%`
      : `${item.label}: awaiting benchmark data / a valid starting date`;

    const dot = create(
      'span',
      'performance-legend-dot'
    );

    dot.style.backgroundColor = item.color;

    const amount = lastValid
      ? `${lastValid[item.key] >= 0 ? '+' : ''}${lastValid[item.key].toFixed(2)}%`
      : '—';

    node.append(
      dot,
      create(
        'span',
        'performance-legend-label',
        item.label
      ),
      create(
        'strong',
        'performance-legend-value',
        amount
      )
    );

    summaries.append(node);
  }

  if (!performanceData) {
    subtitle.textContent =
      'Performance data not generated for this snapshot.';
  } else {
    subtitle.textContent =
      performanceData.warning || '';
  }

  if (data.length < 2 && period !== 'all') {
    area.append(
      create(
        'p',
        'performance-empty',
        'Not enough daily observations to plot this window.'
      )
    );

    return;
  }

  if (!data.length) {
    area.append(
      create(
        'p',
        'performance-empty',
        'Performance history will appear after verified snapshots.'
      )
    );

    return;
  }

  // ----------------------------------------------------------
  // RESPONSIVE SVG DIMENSIONS
  // ----------------------------------------------------------

  const W = Math.max(
    300,
    Math.round(area.clientWidth || 760)
  );

  const H = Math.max(
    200,
    Math.round(area.clientHeight || 264)
  );

  const L = W < 450 ? 52 : 68;
  const R = W < 450 ? 14 : 24;
  const T = 18;
  const B = 40;

  const inner = W - L - R;
  const usable = H - T - B;

  const values = [0];

  for (const point of data) {
    for (const key of keys) {
      if (Number.isFinite(point[key.key])) {
        values.push(point[key.key]);
      }
    }
  }

  const min = Math.min(...values);
  const max = Math.max(...values);

  const padding = Math.max(
    0.035,
    (max - min) * 0.18
  );

  const low = min - padding;
  const high = max + padding;

  // ----------------------------------------------------------
  // TIME-BASED X AXIS
  // ----------------------------------------------------------

  // Use actual calendar dates rather than assuming every
  // stored observation is exactly one day apart.
  // Missing dates therefore produce real horizontal gaps.

  const times = data.map(
    point => Date.parse(point.date + 'T12:00:00Z')
  );

  const validTimeScale =
    times.every(Number.isFinite) &&
    data.length > 1 &&
    times[times.length - 1] > times[0];

  const x = i =>
    L + (
      data.length === 1
        ? inner / 2
        : validTimeScale
          ? (
              (times[i] - times[0]) /
              (times[times.length - 1] - times[0])
            ) * inner
          : i / (data.length - 1) * inner
    );

  const y = v =>
    T + (high - v) / (high - low) * usable;

  const svg = document.createElementNS(
    'http://www.w3.org/2000/svg',
    'svg'
  );

  svg.setAttribute(
    'viewBox',
    `0 0 ${W} ${H}`
  );

  svg.setAttribute(
    'preserveAspectRatio',
    'xMidYMid meet'
  );

  svg.setAttribute('role','group');
  svg.setAttribute('tabindex','0');

  svg.setAttribute(
    'aria-label',
    `Portfolio, Treasury and S&P 500 returns; ${data.length} daily observation${data.length === 1 ? '' : 's'}. Use left and right arrow keys to inspect returns, Enter to pin, and Escape to dismiss.`
  );

  const add = (tag,attrs,container = svg) => {
    const node = document.createElementNS(
      'http://www.w3.org/2000/svg',
      tag
    );

    for (const [k,v] of Object.entries(attrs)) {
      node.setAttribute(k,String(v));
    }

    container.append(node);
    return node;
  };

  // ----------------------------------------------------------
  // CHART GRID
  // ----------------------------------------------------------

  for (let i = 0; i < 5; i++) {
    const val = low + (high - low) * i / 4;
    const yy = y(val);

    add('line',{
      x1:L,
      y1:yy,
      x2:W-R,
      y2:yy,
      stroke:'#314867',
      'stroke-width':1,
      'stroke-dasharray':'3 6'
    });

    const label = add('text',{
      x:L-11,
      y:yy+4,
      fill:'#a2bad0',
      'font-size':W<450?11:12,
      'text-anchor':'end'
    });

    label.textContent =
      `${val > 0 ? '+' : ''}` +
      `${val.toFixed(Math.abs(val) < 1 ? 2 : 1)}%`;
  }

  // Zero-return reference.
  if (low < 0 && high > 0) {
    add('line',{
      x1:L,
      y1:y(0),
      x2:W-R,
      y2:y(0),
      stroke:'#70869b',
      'stroke-width':1.2
    });
  }

  // ----------------------------------------------------------
  // PERFORMANCE LINES
  // ----------------------------------------------------------

  keys.forEach((line,lineIndex) => {
    let chunk = [];

    const flush = () => {
      if (chunk.length > 1) {
        add('polyline',{
          points:chunk.map(
            ([i,v]) => `${x(i)},${y(v)}`
          ).join(' '),
          fill:'none',
          stroke:line.color,
          'stroke-width':
            line.key === 'portfolio' ? 3.1 : 2.6,
          'stroke-linecap':'round',
          'stroke-linejoin':'round'
        });
      }

      chunk = [];
    };

    data.forEach((point,i) => {
      if (Number.isFinite(point[line.key])) {
        chunk.push([i,point[line.key]]);
      } else {
        flush();
      }
    });

    flush();

    // Coloured marker at the final valid point.
    let lastIndex = -1;

    for (let i = data.length - 1; i >= 0; i--) {
      if (Number.isFinite(data[i][line.key])) {
        lastIndex = i;
        break;
      }
    }

    if (lastIndex >= 0) {
      const offset = data.length === 1
        ? (lineIndex - 1) * 9
        : 0;

      add('circle',{
        cx:x(lastIndex) + offset,
        cy:y(data[lastIndex][line.key]),
        r:data.length === 1 ? 4.8 : 4.1,
        fill:line.color,
        stroke:'#0d2237',
        'stroke-width':1.5
      });
    }
  });

  // ----------------------------------------------------------
  // DAY / WEEK / MONTH / YEAR DATE LABELS
  // ----------------------------------------------------------

  // Short histories show every date, including on mobile.
  // Longer histories display fewer labels without deleting
  // or removing any plotted observations.

  const calendarSpanDays = validTimeScale
    ? (
        times[times.length - 1] - times[0]
      ) / 86400000
    : 0;

  const formatAxisDate = dateText => {
    const day = new Date(
      dateText + 'T12:00:00Z'
    );

    if (!Number.isFinite(day.getTime())) {
      return dateText;
    }

    const opts =
      calendarSpanDays > 3650
        ? {
            year:'numeric',
            timeZone:'UTC'
          }
        : calendarSpanDays > 365
          ? {
              month:'short',
              year:'2-digit',
              timeZone:'UTC'
            }
          : {
              day:'numeric',
              month:'short',
              timeZone:'UTC'
            };

    return day.toLocaleDateString(
      'en-GB',
      opts
    );
  };

  const maxTicks = Math.min(
    data.length,
    Math.max(
      2,
      Math.floor(
        inner / (calendarSpanDays > 365 ? 95 : 68)
      ) + 1
    )
  );

  const desiredTicks = data.length <= 7
    ? data.length
    : maxTicks;

  const tickIndices = [];

  if (data.length <= 7 || !validTimeScale) {
    for (let j = 0; j < desiredTicks; j++) {
      const i = desiredTicks === 1
        ? 0
        : Math.round(
            j * (data.length - 1) /
            (desiredTicks - 1)
          );

      if (
        tickIndices[tickIndices.length - 1] !== i
      ) {
        tickIndices.push(i);
      }
    }
  } else {
    // Select labels at evenly spaced screen positions,
    // rather than equally spaced array indices.

    for (let j = 0; j < desiredTicks; j++) {
      const target =
        L + inner * j / (desiredTicks - 1);

      let left = 0;
      let right = data.length - 1;

      while (left < right) {
        const mid = Math.floor(
          (left + right) / 2
        );

        if (x(mid) < target) {
          left = mid + 1;
        } else {
          right = mid;
        }
      }

      const chosen =
        left > 0 &&
        target - x(left - 1) <= x(left) - target
          ? left - 1
          : left;

      if (
        tickIndices[tickIndices.length - 1] !== chosen
      ) {
        tickIndices.push(chosen);
      }
    }
  }

  for (const i of tickIndices) {
    const label = add('text',{
      x:x(i),
      y:H-10,
      fill:'#97b0c7',
      'font-size':W<450?10:12,
      'text-anchor':'middle'
    });

    label.textContent =
      formatAxisDate(data[i].date);
  }

  // ----------------------------------------------------------
  // INTERACTIVE, COLOUR-MATCHED TOOLTIP
  // ----------------------------------------------------------

  const tooltip = create(
    'div',
    'return-tooltip'
  );

  tooltip.hidden = true;

  tooltip.setAttribute(
    'aria-hidden',
    'true'
  );

  const tooltipDate = create(
    'div',
    'return-tooltip-date'
  );

  const tooltipRows = new Map();

  tooltip.append(tooltipDate);

  for (const line of keys) {
    const row = create(
      'div',
      'return-tooltip-row'
    );

    row.style.borderLeftColor = line.color;

    const label = create(
      'span',
      'return-tooltip-label',
      line.label
    );

    const value = create(
      'strong',
      'return-tooltip-value',
      '—'
    );

    value.style.color = line.color;

    row.append(label,value);
    tooltip.append(row);

    tooltipRows.set(line.key,value);
  }

  area.append(svg,tooltip);

  // Transparent surface for pointer interactions.
  add('rect',{
    x:0,
    y:0,
    width:W,
    height:H,
    fill:'transparent',
    'pointer-events':'all'
  });

  const guide = add(
    'g',
    {
      'class':'return-chart-guide',
      opacity:0,
      'pointer-events':'none'
    }
  );

  const guideLine = add(
    'line',
    {
      x1:0,
      y1:T,
      x2:0,
      y2:H-B,
      stroke:'#8ba9c6',
      'stroke-width':1.2,
      'stroke-dasharray':'3 5'
    },
    guide
  );

  const guideDots = keys.map(line =>
    add(
      'circle',
      {
        cx:0,
        cy:0,
        r:4.6,
        fill:line.color,
        stroke:'#071b2c',
        'stroke-width':1.5
      },
      guide
    )
  );

  let selected = -1;
  let pinned = false;

  const formatPct = (value, decimals) => {
    if (!Number.isFinite(value)) return '—';

    const rounded =
      Math.abs(value) < Math.pow(10,-decimals) / 2
        ? 0
        : value;

    return (
      rounded > 0 ? '+' :
      rounded < 0 ? '−' : ''
    ) +
    Math.abs(rounded).toFixed(decimals) +
    '%';
  };

  // ----------------------------------------------------------
  // FIND NEAREST DATE
  // ----------------------------------------------------------

  const indexFromSvgX = svgX => {
    if (data.length <= 1) return 0;

    const clipped = Math.max(
      L,
      Math.min(W-R,svgX)
    );

    let left = 0;
    let right = data.length - 1;

    while (left < right) {
      const mid = Math.floor(
        (left + right) / 2
      );

      if (x(mid) < clipped) {
        left = mid + 1;
      } else {
        right = mid;
      }
    }

    return (
      left > 0 &&
      clipped - x(left - 1) <= x(left) - clipped
    )
      ? left - 1
      : left;
  };

  // ----------------------------------------------------------
  // SHOW TOOLTIP
  // ----------------------------------------------------------

  function showReturn(
    index,
    svgX = x(index),
    svgY = T + 12
  ) {
    selected = index;

    const point = data[index];

    tooltipDate.textContent =
      formatDate(point.date);

    for (const [j,line] of keys.entries()) {
      tooltipRows.get(line.key).textContent =
        formatPct(
          point[line.key],
          line.key === 'cash' ? 4 : 2
        );

      const valid =
        Number.isFinite(point[line.key]);

      guideDots[j].style.display =
        valid ? '' : 'none';

      if (valid) {
        guideDots[j].setAttribute(
          'cx',
          x(index)
        );

        guideDots[j].setAttribute(
          'cy',
          y(point[line.key])
        );
      }
    }

    guideLine.setAttribute(
      'x1',
      x(index)
    );

    guideLine.setAttribute(
      'x2',
      x(index)
    );

    guide.setAttribute('opacity','1');

    tooltip.hidden = false;

    tooltip.setAttribute(
      'aria-hidden',
      'false'
    );

    const rect = area.getBoundingClientRect();

    const scaleX = rect.width / W;
    const scaleY = rect.height / H;

    const anchorX = svgX * scaleX;
    const anchorY = svgY * scaleY;

    const tipWidth =
      tooltip.offsetWidth || 190;

    const tipHeight =
      tooltip.offsetHeight || 130;

    const rightSide =
      anchorX + tipWidth + 22 <= rect.width;

    const left = rightSide
      ? anchorX + 14
      : anchorX - tipWidth - 14;

    tooltip.style.left = `${
      Math.max(
        6,
        Math.min(
          left,
          rect.width - tipWidth - 6
        )
      )
    }px`;

    tooltip.style.top = `${
      Math.max(
        6,
        Math.min(
          anchorY - tipHeight / 2,
          rect.height - tipHeight - 6
        )
      )
    }px`;
  }

  function hideReturn() {
    selected = -1;

    tooltip.hidden = true;

    tooltip.setAttribute(
      'aria-hidden',
      'true'
    );

    guide.setAttribute('opacity','0');
  }

  // ----------------------------------------------------------
  // POINTER AND KEYBOARD INTERACTIONS
  // ----------------------------------------------------------

  function svgPosition(event) {
    const rect = svg.getBoundingClientRect();

    if (!rect.width || !rect.height) {
      return {x:L,y:T};
    }

    return {
      x:(event.clientX - rect.left) * W / rect.width,
      y:(event.clientY - rect.top) * H / rect.height
    };
  }

  svg.addEventListener(
    'pointermove',
    event => {
      if (
        pinned ||
        (
          event.pointerType === 'touch' &&
          event.buttons
        )
      ) return;

      const pos = svgPosition(event);

      if (
        pos.x < L-12 ||
        pos.x > W-R+12
      ) {
        hideReturn();
        return;
      }

      showReturn(
        indexFromSvgX(pos.x),
        pos.x,
        pos.y
      );
    }
  );

  svg.addEventListener(
    'pointerleave',
    () => {
      if (!pinned) hideReturn();
    }
  );

  svg.addEventListener(
    'click',
    event => {
      const pos = svgPosition(event);
      const index = indexFromSvgX(pos.x);

      if (
        pinned &&
        selected === index
      ) {
        pinned = false;
        hideReturn();
        return;
      }

      pinned = true;

      showReturn(
        index,
        pos.x,
        pos.y
      );
    }
  );

  svg.addEventListener(
    'focus',
    () => {
      if (selected < 0) {
        showReturn(data.length - 1);
      }
    }
  );

  svg.addEventListener(
    'blur',
    () => {
      if (!pinned) hideReturn();
    }
  );

  svg.addEventListener(
    'keydown',
    event => {
      if (event.key === 'Escape') {
        pinned = false;
        hideReturn();
        event.preventDefault();
        return;
      }

      if (
        event.key === 'ArrowLeft' ||
        event.key === 'ArrowRight'
      ) {
        const next = selected < 0
          ? data.length - 1
          : Math.max(
              0,
              Math.min(
                data.length - 1,
                selected +
                  (
                    event.key === 'ArrowRight'
                      ? 1
                      : -1
                  )
              )
            );

        showReturn(next);
        event.preventDefault();
      } else if (
        event.key === 'Enter' ||
        event.key === ' '
      ) {
        if (selected < 0) {
          showReturn(data.length - 1);
        }

        pinned = !pinned;

        if (!pinned) hideReturn();

        event.preventDefault();
      }
    }
  );

  chartOutsidePointerHandler = event => {
    if (!area.contains(event.target)) {
      pinned = false;
      hideReturn();
    }
  };

  document.addEventListener(
    'pointerdown',
    chartOutsidePointerHandler
  );
}

// ------------------------------------------------------------
// RESPONSIVE CHART RESIZING
// ------------------------------------------------------------

// Resizing redraws the existing chart.
// It does not request any new Alchemy data.

let portfolioChartResizeTimer = null;

window.addEventListener('resize', () => {
  clearTimeout(portfolioChartResizeTimer);

  portfolioChartResizeTimer = setTimeout(
    () => {
      if (portfolio) renderChart();
    },
    150
  );
});

init();
