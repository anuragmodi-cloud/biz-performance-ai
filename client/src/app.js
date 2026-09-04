import { VoiceClient } from './voiceClient.js';

const answersList = document.getElementById('answers-list');

function formatValue(value, key) {
  if (typeof value !== 'number') return value ?? '—';
  const isPercent = /pct|percent|margin|rate/i.test(key || '');
  if (isPercent) return `${value.toFixed(2)}%`;
  if (Math.abs(value) >= 1000) return `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`;
  return value.toLocaleString('en-IN', { maximumFractionDigits: 2 });
}

function headlineFor(result) {
  if (!result) return null;
  if (typeof result.value === 'number') {
    return { text: formatValue(result.value, result.metric), unit: result.unit || '' };
  }
  return null;
}

function itemsFor(result) {
  if (!result || !Array.isArray(result.items)) return null;
  return result.items.map((item) => {
    const nameKey = Object.keys(item).find((k) => /_name$/.test(k) || k === 'category' || k === 'transaction_type');
    const valueKey = Object.keys(item).find(
      (k) => k !== nameKey && !/_id$/.test(k) && typeof item[k] === 'number'
    );
    return { name: nameKey ? item[nameKey] : Object.values(item)[0], value: formatValue(item[valueKey], valueKey) };
  });
}

function diagnosticSummary(result) {
  if (!result || !result.current || !result.previous) return null;
  const c = result.current;
  const p = result.previous;
  const rows = [];
  if ('revenue' in c) rows.push({ name: 'Revenue (current vs previous)', value: `${formatValue(c.revenue)} vs ${formatValue(p.revenue)}` });
  if ('gross_profit' in c) rows.push({ name: 'Gross profit', value: `${formatValue(c.gross_profit)} vs ${formatValue(p.gross_profit)}` });
  if ('gross_margin_pct' in c) rows.push({ name: 'Gross margin', value: `${c.gross_margin_pct}% vs ${p.gross_margin_pct}%` });
  return rows.length ? rows : null;
}

function renderAnswerCard(answer) {
  const result = answer.result;
  const headline = headlineFor(result);
  const items = itemsFor(result);
  const diagnostic = diagnosticSummary(result);
  const tables = answer.trace_summary?.tables_queried || [];
  const computations = answer.trace_summary?.computations || [];

  const card = document.createElement('div');
  card.className = 'answer-card';

  const questionEl = document.createElement('div');
  questionEl.className = 'answer-question';
  questionEl.textContent = `"${answer.question_text}"`;
  card.appendChild(questionEl);

  if (headline) {
    const h = document.createElement('div');
    h.className = 'answer-headline';
    h.innerHTML = `${headline.text}${headline.unit ? `<span class="unit">${headline.unit}</span>` : ''}`;
    card.appendChild(h);
  } else if (!items && !diagnostic && result?.error) {
    const h = document.createElement('div');
    h.className = 'answer-headline';
    h.style.fontSize = '15px';
    h.style.color = 'var(--muted)';
    h.textContent = result.error;
    card.appendChild(h);
  }

  const list = items || diagnostic;
  if (list) {
    const ul = document.createElement('ul');
    ul.className = 'answer-items';
    for (const row of list) {
      const li = document.createElement('li');
      li.innerHTML = `<span class="name">${row.name}</span><span class="value">${row.value}</span>`;
      ul.appendChild(li);
    }
    card.appendChild(ul);
  }

  const meta = document.createElement('div');
  meta.className = 'answer-meta';
  for (const t of tables) {
    const chip = document.createElement('span');
    chip.className = 'chip';
    chip.textContent = t;
    meta.appendChild(chip);
  }
  for (const c of computations) {
    const chip = document.createElement('span');
    chip.className = 'chip chip-alt';
    chip.textContent = c;
    meta.appendChild(chip);
  }
  if (answer.cache_hit) {
    const chip = document.createElement('span');
    chip.className = 'chip chip-cache';
    chip.textContent = '⚡ cached';
    meta.appendChild(chip);
  }
  if (meta.children.length) card.appendChild(meta);

  if (answer.narrated_text) {
    const n = document.createElement('div');
    n.className = 'answer-narration';
    n.textContent = answer.narrated_text;
    card.appendChild(n);
  }

  return card;
}

function renderAnswers(answers) {
  const empty = answersList.querySelector('.answers-empty');
  if (empty) empty.remove();
  answersList.innerHTML = '';
  // answers arrive most-recent-first from the server
  for (const answer of answers) {
    answersList.appendChild(renderAnswerCard(answer));
  }
}

new VoiceClient(
  {
    connectBtn: 'connect-btn',
    micBtn: 'mic-btn',
    micIcon: 'mic-icon',
    micLabel: 'mic-label',
    micStatus: 'mic-status',
    micMeterFill: 'mic-meter-fill',
    conversationLog: 'conversation-log',
    sessionPill: 'session-pill',
    orb: 'orb',
    orbStatus: 'orb-status',
    textAskForm: 'text-ask-form',
    textAskInput: 'text-ask-input',
    textAskSend: 'text-ask-send',
  },
  {
    onAnswers: renderAnswers,
  }
);

