// admin.js -- the eval-trace dashboard: lists every ask_calculation_engine
// call the server has logged, its grounding status, and its judge LLM
// verdict, so failures can be traced back to exactly which pipeline stage
// broke (intent resolution / computation / narration). Talks directly to
// the FastAPI admin API (admin.py) -- no build step, no framework.

const SERVER_URL = import.meta.env.VITE_SERVER_URL || 'http://localhost:8010';
const ADMIN_KEY_STORAGE = 'munshi_admin_key';
const REFRESH_INTERVAL_MS = 4000;

const keyGate = document.getElementById('key-gate');
const keyForm = document.getElementById('key-form');
const keyInput = document.getElementById('admin-key-input');
const keyError = document.getElementById('key-error');
const dashboard = document.getElementById('dashboard');
const summaryCards = document.getElementById('summary-cards');
const summaryStages = document.getElementById('summary-stages');
const logList = document.getElementById('log-list');
const refreshBtn = document.getElementById('refresh-btn');
const changeKeyBtn = document.getElementById('change-key-btn');
const statusFilter = document.getElementById('status-filter');
const judgeFilter = document.getElementById('judge-filter');
const sessionFilter = document.getElementById('session-filter');

let adminKey = localStorage.getItem(ADMIN_KEY_STORAGE) || '';
let refreshTimer = null;
let openLogId = null; // which row's detail panel is expanded, preserved across refreshes

function authHeaders() {
  return { 'X-Admin-Key': adminKey };
}

async function adminFetch(path, options = {}) {
  const resp = await fetch(`${SERVER_URL}${path}`, {
    ...options,
    headers: { ...authHeaders(), ...(options.headers || {}) },
  });
  if (resp.status === 401) {
    throw new Error('unauthorized');
  }
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`${resp.status}: ${body}`);
  }
  return resp.status === 204 ? null : resp.json();
}

function showDashboard() {
  keyGate.hidden = true;
  dashboard.hidden = false;
  startAutoRefresh();
}

function showKeyGate(message) {
  stopAutoRefresh();
  dashboard.hidden = true;
  keyGate.hidden = false;
  keyError.textContent = message || '';
}

function startAutoRefresh() {
  stopAutoRefresh();
  refreshAll();
  refreshTimer = setInterval(refreshAll, REFRESH_INTERVAL_MS);
}

function stopAutoRefresh() {
  if (refreshTimer) {
    clearInterval(refreshTimer);
    refreshTimer = null;
  }
}

async function refreshAll() {
  try {
    const [summary, entries] = await Promise.all([
      adminFetch('/admin/summary'),
      adminFetch(buildLogQuery()),
    ]);
    renderSummary({
      ...summary.query_log,
      active_sessions: summary.active_sessions,
      total_sessions: summary.total_sessions,
    });
    renderLog(entries);
  } catch (error) {
    if (error.message === 'unauthorized') {
      localStorage.removeItem(ADMIN_KEY_STORAGE);
      adminKey = '';
      showKeyGate('Invalid admin key.');
    } else {
      console.error('Admin refresh error:', error);
    }
  }
}

function buildLogQuery() {
  const params = new URLSearchParams();
  if (statusFilter.value) params.set('status', statusFilter.value);
  if (sessionFilter.value.trim()) params.set('session_id', sessionFilter.value.trim());
  const qs = params.toString();
  return `/admin/query-log${qs ? `?${qs}` : ''}`;
}

function judgeState(entry) {
  if (entry.judge_error) return 'error';
  if (entry.judge_verdict) return entry.judge_verdict; // "pass" | "fail"
  return 'pending';
}

let lastSummaryKey = null; // skip the rebuild below when nothing actually changed

function renderSummary(summary) {
  const statusEntries = Object.entries(summary.by_status || {});
  const cards = [
    { label: 'Total asks', value: summary.total_asks ?? 0 },
    { label: 'Active sessions', value: summary.active_sessions ?? 0 },
    { label: 'Cache hit rate', value: fmtPct(summary.cache_hit_rate_pct) },
    {
      label: 'Judge reviewed',
      value: summary.judge_reviewed ?? 0,
    },
    {
      label: 'Judge fail rate',
      value: fmtPct(summary.judge_fail_rate_pct),
      warn: (summary.judge_fail_rate_pct || 0) > 0,
    },
    ...statusEntries.map(([status, count]) => ({ label: status, value: count })),
  ];
  const stages = Object.entries(summary.judge_failed_stages || {});

  // Same teardown-every-4s issue as the log list (see renderLog) -- rebuild
  // the DOM only when the actual numbers changed, not on every auto-refresh
  // tick, so there's nothing here to flicker/repaint unnecessarily.
  const key = JSON.stringify([cards, stages]);
  if (key === lastSummaryKey) return;
  lastSummaryKey = key;

  summaryCards.innerHTML = '';
  for (const card of cards) {
    const div = document.createElement('div');
    div.className = `summary-card${card.warn ? ' warn' : ''}`;
    div.innerHTML = `<div class="value">${card.value}</div><div class="label">${card.label}</div>`;
    summaryCards.appendChild(div);
  }

  // Rendered as its own chip row, not a grid card -- the stage/count list
  // grows with however many distinct failure stages have been seen, which
  // broke the uniform stat-tile grid when stuffed into one (a 3-stage
  // breakdown wrapped across several lines at the same 22px card-value size
  // as "10" or "55%", blowing out that row's height).
  summaryStages.innerHTML = stages.length
    ? `<span class="summary-stages-label">Judge-flagged failure stages:</span>` +
      stages.map(([stage, count]) => `<span class="chip chip-warn">${escapeHtml(stage)}: ${count}</span>`).join('')
    : '';
}

function fmtPct(value) {
  return value === null || value === undefined ? '—' : `${value}%`;
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function renderLog(entries) {
  const filtered = judgeFilter.value
    ? entries.filter((e) => judgeState(e) === judgeFilter.value)
    : entries;

  if (!filtered.length) {
    logList.innerHTML = '<div class="answers-empty"><p>Koi query log entry nahi mili.</p></div>';
    return;
  }
  // The empty-state placeholder (or a stale full rebuild) may still be
  // sitting there from before -- clear it once so the keyed diff below
  // starts from a real row list, not a leftover <div class="answers-empty">.
  if (!logList.querySelector('.log-row')) {
    logList.innerHTML = '';
  }

  // Keyed reconciliation, not a full innerHTML='' + rebuild every refresh.
  // Auto-refresh fires every REFRESH_INTERVAL_MS regardless of whether
  // anything changed -- tearing down and recreating all rows every single
  // cycle destroyed and repainted every row's DOM/layers on a fixed timer,
  // which is what produced the intermittent overlapping/ghosted-row flash
  // during a refresh tick (two rows briefly occupying the same space as old
  // nodes were removed and new ones inserted in the same tick). Reusing
  // unchanged row elements removes the teardown entirely, so there's
  // nothing left to flicker -- and it stops resetting scroll position /
  // open-row state on every tick as a side benefit.
  const existingRows = new Map();
  logList.querySelectorAll('.log-row[data-log-id]').forEach((el) => existingRows.set(el.dataset.logId, el));

  let cursor = null; // last DOM node placed, so order stays correct
  for (const entry of filtered) {
    const renderKey = JSON.stringify([
      entry.status, judgeState(entry), entry.judge_reason, entry.judge_error,
      entry.narrated_text, entry.admin_reviewed, entry.admin_verdict,
    ]);
    const old = existingRows.get(entry.log_id);
    // Whether reused, replaced, or brand new, this id is accounted for --
    // delete it now so it's never mistaken for a stale row below.
    existingRows.delete(entry.log_id);

    let row;
    if (old && old.dataset.renderKey === renderKey) {
      row = old; // fully unchanged -- reuse the exact element, no DOM churn
    } else {
      // Data changed (e.g. an async judge verdict just landed) or this is a
      // brand-new entry -- build a fresh row. When an old element exists,
      // swap it out immediately with replaceWith: leaving `old` in the DOM
      // and only dropping the JS reference to it (as an earlier version of
      // this function did) orphans it forever, since the stale-removal pass
      // below only catches ids that were never visited at all -- an id that
      // WAS visited but merely got a new element was already deleted from
      // existingRows above, so it silently escaped that cleanup and its old
      // node just sat there duplicated. That's exactly what caused the
      // ever-growing row count.
      const wasOpen = old ? old.classList.contains('is-open') : entry.log_id === openLogId;
      row = renderLogRow(entry);
      row.dataset.renderKey = renderKey;
      if (wasOpen) row.classList.add('is-open');
      if (old) old.replaceWith(row);
    }

    const target = cursor ? cursor.nextSibling : logList.firstChild;
    if (row !== target) logList.insertBefore(row, target);
    cursor = row;
  }
  // Anything left in existingRows fell out of the filtered result (e.g. a
  // filter changed) -- those are the only rows actually removed.
  for (const stale of existingRows.values()) stale.remove();
}

function renderLogRow(entry) {
  const row = document.createElement('div');
  row.className = 'log-row';
  row.dataset.logId = entry.log_id;
  if (entry.log_id === openLogId) row.classList.add('is-open');

  const judge = judgeState(entry);
  const summary = document.createElement('div');
  summary.className = 'log-row-summary';
  summary.innerHTML = `
    <span class="log-row-expand-icon">&#9656;</span>
    <span class="log-row-time">${fmtTime(entry.ts)}</span>
    <span class="log-row-question">${escapeHtml(entry.question_text || '(no question text)')}</span>
    <span class="badge badge-status-${entry.status}">${entry.status}</span>
    <span class="badge badge-judge-${judge}">judge: ${judge}</span>
  `;
  summary.addEventListener('click', () => {
    openLogId = openLogId === entry.log_id ? null : entry.log_id;
    row.classList.toggle('is-open');
  });
  row.appendChild(summary);

  // A one-line preview of WHY it failed, visible without expanding --
  // the collapsed badge alone ("judge: fail") doesn't say why or at what
  // stage, which was the whole point of building this page.
  const reasonPreviewText = entry.judge_error || entry.judge_reason;
  if (judge === 'fail' || judge === 'error') {
    const preview = document.createElement('div');
    preview.className = 'log-row-reason-preview';
    const stagePrefix = entry.judge_failed_stage && entry.judge_failed_stage !== 'none'
      ? `[${entry.judge_failed_stage}] ` : '';
    preview.textContent = `${stagePrefix}${reasonPreviewText || '(no reason recorded)'}`;
    row.appendChild(preview);
  }

  row.appendChild(renderLogDetail(entry, judge));
  return row;
}

function renderLogDetail(entry, judge) {
  const detail = document.createElement('div');
  detail.className = 'log-row-detail';

  const copyBtn = (field) => `<button class="copy-btn" type="button" data-copy-field="${field}">Copy</button>`;

  // Judge reason first -- it's the whole reason this page exists, and
  // burying it below four other blocks meant it was easy to miss.
  const judgeReasonHtml = entry.judge_error
    ? `<div class="detail-block judge-reason-block">
        <h4>Judge Error</h4>
        <p>${escapeHtml(entry.judge_error)}</p>
      </div>`
    : entry.judge_reason
      ? `<div class="detail-block judge-reason-block">
          <h4>Judge Reason${entry.judge_failed_stage && entry.judge_failed_stage !== 'none' ? ` — failed at: ${escapeHtml(entry.judge_failed_stage)}` : ''} ${copyBtn('judge_reason')}</h4>
          <p>${escapeHtml(entry.judge_reason)}${entry.judge_downstream_impact ? `<br><em>Downstream impact: ${escapeHtml(entry.judge_downstream_impact)}</em>` : ''}</p>
        </div>`
      : judge === 'pending'
        ? `<div class="detail-block judge-reason-block"><h4>Judge Reason</h4><p class="detail-muted">Not judged yet -- click "Run Judge Now" below.</p></div>`
        : '';

  detail.innerHTML = `
    <div class="detail-grid">
      ${judgeReasonHtml}
      <div class="detail-block">
        <h4>Resolved Intent ${copyBtn('resolved_intent')}</h4>
        <pre>${escapeHtml(pretty(entry.resolved_intent))}</pre>
      </div>
      <div class="detail-block">
        <h4>Result ${copyBtn('result')}</h4>
        <pre>${escapeHtml(pretty(entry.result))}</pre>
      </div>
      <div class="detail-block">
        <h4>Trace (full) ${copyBtn('trace')}</h4>
        <pre>${escapeHtml(pretty(entry.trace))}</pre>
      </div>
      <div class="detail-block">
        <h4>Narrated Text ${copyBtn('narrated_text')}</h4>
        <p>${escapeHtml(entry.narrated_text || '—')}</p>
      </div>
      <div class="detail-actions">
        <button class="btn btn-outline run-judge-btn" type="button" ${judge === 'pending' ? '' : 'disabled'}>
          Run Judge Now
        </button>
        <button class="btn btn-outline mark-pass-btn" type="button">Mark Pass (admin)</button>
        <button class="btn btn-outline mark-fail-btn" type="button">Mark Fail (admin)</button>
        <button class="btn btn-outline copy-btn" type="button" data-copy-field="__full_entry">
          Copy Full Entry (JSON)
        </button>
        <span class="detail-action-status">
          ${entry.admin_reviewed ? `Admin reviewed: ${entry.admin_verdict}` : ''}
          ${entry.cache_hit ? ' · cache hit' : ''}
          ${entry.latency_ms ? ` · ${Math.round(entry.latency_ms)}ms` : ''}
        </span>
      </div>
    </div>
  `;

  const statusEl = detail.querySelector('.detail-action-status');

  const copySource = (field) => {
    if (field === '__full_entry') return JSON.stringify(entry, null, 2);
    if (field === 'narrated_text') return entry.narrated_text || '';
    if (field === 'judge_reason') return entry.judge_reason || '';
    return pretty(entry[field]);
  };
  detail.querySelectorAll('.copy-btn').forEach((btn) => {
    btn.addEventListener('click', async (event) => {
      event.stopPropagation();
      const field = btn.dataset.copyField;
      try {
        await navigator.clipboard.writeText(copySource(field));
        const original = btn.textContent;
        btn.textContent = 'Copied!';
        setTimeout(() => { btn.textContent = original; }, 1200);
      } catch (error) {
        console.error('Copy failed:', error);
        btn.textContent = 'Copy failed';
      }
    });
  });

  detail.querySelector('.run-judge-btn')?.addEventListener('click', async (event) => {
    event.stopPropagation();
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.textContent = 'Running...';
    try {
      await adminFetch(`/admin/query-log/${entry.log_id}/judge`, { method: 'POST' });
      await refreshAll();
    } catch (error) {
      statusEl.textContent = `Judge call failed: ${error.message}`;
      btn.disabled = false;
      btn.textContent = 'Run Judge Now';
    }
  });

  const review = async (event, verdict) => {
    event.stopPropagation();
    try {
      await adminFetch(`/admin/query-log/${entry.log_id}/review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ verdict }),
      });
      await refreshAll();
    } catch (error) {
      statusEl.textContent = `Review failed: ${error.message}`;
    }
  };
  detail.querySelector('.mark-pass-btn')?.addEventListener('click', (e) => review(e, 'pass'));
  detail.querySelector('.mark-fail-btn')?.addEventListener('click', (e) => review(e, 'fail'));

  return detail;
}

function pretty(value) {
  if (value === null || value === undefined) return '—';
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function escapeHtml(str) {
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

// ---------------- Wiring ----------------

keyForm.addEventListener('submit', (event) => {
  event.preventDefault();
  const value = keyInput.value.trim();
  if (!value) return;
  adminKey = value;
  localStorage.setItem(ADMIN_KEY_STORAGE, value);
  showDashboard();
});

changeKeyBtn.addEventListener('click', () => {
  localStorage.removeItem(ADMIN_KEY_STORAGE);
  adminKey = '';
  keyInput.value = '';
  showKeyGate();
});

refreshBtn.addEventListener('click', refreshAll);
statusFilter.addEventListener('change', refreshAll);
judgeFilter.addEventListener('change', refreshAll);
sessionFilter.addEventListener('input', () => {
  clearTimeout(sessionFilter._debounce);
  sessionFilter._debounce = setTimeout(refreshAll, 400);
});

if (adminKey) {
  keyInput.value = adminKey;
  showDashboard();
} else {
  showKeyGate();
}
