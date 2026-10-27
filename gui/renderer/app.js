// Guban Studio renderer: tab switching, the worker call helper, busy buttons, the error banner, small DOM helpers.
// Every computation happens in the Python worker (window.guban.call, see preload.js); each tab lives in its own file
// (data.js, ...) and talks to the others through studio.on / studio.fire.
'use strict';

const $ = (sel, root = document) => root.querySelector(sel);

class WorkerError extends Error {
  constructor(error) {
    super(error.message);
    this.frames = error.frames || [];
    this.stopped = !!error.stopped;
    this.traceback = error.traceback || '';
  }
}

// Call a worker method ("main" worker unless named); resolves with its result, throws WorkerError with its message.
async function api(method, params, worker) {
  const reply = await window.guban.call(method, params, worker);
  if (!reply.ok) throw new WorkerError(reply.error);
  return reply.result;
}

// Build an element: el('td', {className: 'x', title: 'y'}, 'text' | node | [children]).
function el(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  for (const c of [].concat(children)) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

const fmtCell = (v) => (v === null || v === undefined ? '' : typeof v === 'number' ? (Number.isInteger(v) ? v.toLocaleString('en-US') : v.toLocaleString('en-US', { maximumFractionDigits: 4 })) : String(v));

// Fill a <table> with a header row and body rows; onRow(i, tr) decorates each body row.
function fillTable(table, columns, rows, onRow) {
  const head = el('thead', {}, el('tr', {}, columns.map((c) => el('th', {}, c))));
  const body = el('tbody', {}, rows.map((r, i) => {
    const tr = el('tr', {}, r.map((v) => el('td', {}, fmtCell(v))));
    if (onRow) onRow(i, tr);
    return tr;
  }));
  table.replaceChildren(head, body);
}

function fillSelect(select, items) {
  select.replaceChildren(...items.map(({ value, label }) => el('option', { value, textContent: label })));
}

// sticky: stays up when a new action starts (a dead worker needs Restart, not a retry)
function showError(err, title = 'Error', sticky = false) {
  const banner = $('#banner');
  if (sticky) banner.dataset.sticky = '1'; else delete banner.dataset.sticky;
  banner.replaceChildren(el('div', {}, `${title}: ${err.message}`));
  const detail = [...(err.frames || []), err.traceback || ''].join('').trim();
  if (detail) banner.append(el('pre', {}, detail));
  const close = el('button', { onclick: () => { banner.classList.add('hidden'); delete banner.dataset.sticky; } }, 'Dismiss');
  banner.append(el('div', { className: 'row' }, close));
  banner.classList.remove('hidden');
}

// The latest progress message from the worker, shown next to the busy timer.
let progressText = '';
window.guban.onEvent((msg) => {
  if (msg.event === 'progress' && msg.message) progressText = msg.message;
  studio.fire(`event:${msg.event}`, msg);
});

// Run fn with the button disabled and the elapsed time in the status bar; errors go to the banner and are rethrown.
async function busy(button, label, fn) {
  if (!$('#banner').dataset.sticky) $('#banner').classList.add('hidden'); // a new action replaces an old error
  const buttons = [].concat(button);
  for (const b of buttons) { b.disabled = true; b.classList.add('busy'); }
  progressText = '';
  const t0 = performance.now();
  const show = () => { $('#last-action').textContent = `${label}… ${((performance.now() - t0) / 1000).toFixed(0)} s${progressText ? ` · ${progressText}` : ''}`; };
  show();
  const tick = setInterval(show, 500);
  try {
    const result = await fn();
    $('#last-action').textContent = `${label}: ${((performance.now() - t0) / 1000).toFixed(1)} s`;
    return result;
  } catch (err) {
    $('#last-action').textContent = `${label}: ${err.stopped ? 'stopped' : 'failed'}`;
    if (!err.stopped) showError(err, label);
    throw err;
  } finally {
    clearInterval(tick);
    for (const b of buttons) { b.disabled = false; b.classList.remove('busy'); }
  }
}

function showTab(name) {
  for (const b of document.querySelectorAll('#tabs button')) b.classList.toggle('active', b.dataset.tab === name);
  for (const s of document.querySelectorAll('.tab')) s.classList.toggle('active', s.id === `tab-${name}`);
}

// Form values remembered between launches (per viewer, this machine only).
const settings = {
  get(key) {
    try { return JSON.parse(localStorage.getItem(`studio.${key}`)) || {}; } catch (e) { return {}; }
  },
  set(key, value) {
    try { localStorage.setItem(`studio.${key}`, JSON.stringify(value)); } catch (e) { /* storage unavailable: forget */ }
  },
};

// A grid editor over a strategy's inputs: a tick box and a values field per input, remembered per strategy file under
// `${prefix}:${file}`; count() asks the worker how many combinations the ticked values make. Used by the Optimization
// and Walk Forward tabs. ui: { container, error, count } elements.
function gridEditor(prefix, ui, fallbackPrefix) {
  const file = () => (studio.strategy.state.loaded || {}).file;
  const key = (pre) => `${pre}:${file()}`;
  const ed = { count: null };

  ed.specs = () => {
    const out = {};
    for (const row of ui.container.querySelectorAll('[data-name]')) {
      const [on, values] = [row.querySelector('input[type=checkbox]'), row.querySelector('input.values')];
      if (on.checked) out[row.dataset.name] = values.value.trim();
    }
    return out;
  };

  function save() {
    const saved = {};
    for (const row of ui.container.querySelectorAll('[data-name]')) {
      saved[row.dataset.name] = { on: row.querySelector('input[type=checkbox]').checked, values: row.querySelector('input.values').value };
    }
    settings.set(key(prefix), saved);
  }

  function showError(message) {
    ui.count.textContent = '';
    ui.error.textContent = message;
    ui.error.classList.remove('hidden');
  }

  ed.count = async () => {
    const grid = ed.specs();
    ed.n = null;
    if (!Object.keys(grid).length) { showError('Tick at least one input to optimize.'); return null; }
    try {
      const r = await api('optimize.count', { grid });
      ed.n = r.count;
      ui.count.textContent = `(${r.count.toLocaleString('en-US')} combination${r.count === 1 ? '' : 's'})`;
      ui.error.classList.add('hidden');
      return r;
    } catch (err) {
      showError(err.message);
      return null;
    }
  };

  let timer = null;
  const countSoon = () => { clearTimeout(timer); timer = setTimeout(ed.count, 300); };

  ed.render = (info) => {
    if (!info) {
      ui.container.replaceChildren(el('div', { className: 'muted' }, 'Load a strategy on the Strategy tab.'));
      return;
    }
    let saved = settings.get(key(prefix));
    if (!Object.keys(saved).length && fallbackPrefix) saved = settings.get(key(fallbackPrefix));
    const editor = el('div', { className: 'grid-editor' });
    for (const inp of info.inputs) {
      const s = saved[inp.name] || {};
      const on = el('input', { type: 'checkbox', checked: !!s.on, title: `optimize ${inp.name}` });
      const values = el('input', { className: 'values', value: s.values ?? (inp.kind === 'bool' ? 'true,false' : inp.value), disabled: !s.on, placeholder: inp.value });
      const row = el('div', { style: 'display: contents' }, [on, el('span', { className: 'gname', title: `${inp.kind}, default ${inp.value}` }, inp.name), values]);
      row.dataset.name = inp.name;
      on.onchange = () => { values.disabled = !on.checked; save(); ed.count(); };
      values.oninput = () => { save(); countSoon(); };
      editor.append(row);
    }
    ui.container.replaceChildren(editor);
    ed.count();
  };
  return ed;
}

// The "opt" worker runs one job at a time (an optimization or a walk-forward): the running tab holds it.
const optJob = {
  holder: null,
  take(name) {
    if (this.holder && this.holder !== name) throw new Error(`The ${this.holder} is running in the background process: wait for it or Stop it first`);
    this.holder = name;
    studio.fire('opt-job', name);
  },
  release(name) {
    if (this.holder === name) this.holder = null;
    studio.fire('opt-job', this.holder);
  },
};

const listeners = {};
const studio = {
  api, el, fillTable, fillSelect, busy, showTab, showError, settings, gridEditor, optJob,
  on(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
  fire(name, payload) { for (const fn of listeners[name] || []) fn(payload); },
  ready: [], // tab init functions, run in order once the worker answers
};
window.studio = studio;

async function ping() {
  const info = await api('ping');
  $('#worker-status').textContent = `Python worker ready: guban ${info.guban}, Python ${info.python}, pid ${info.pid}`;
  return info;
}

window.guban.onWorkerExit((info) => {
  if (info.worker !== 'main') return studio.fire('opt-worker-exit', info); // the Optimization tab reports it
  $('#worker-status').textContent = 'Python worker stopped';
  showError({ message: `Python worker stopped (exit code ${info.code})`, frames: [], traceback: info.stderr.join('\n') }, 'Worker', true);
  const restart = el('button', {
    onclick: async () => {
      $('#banner').classList.add('hidden');
      delete $('#banner').dataset.sticky;
      await window.guban.restart();
      await ping().catch((e) => showError(e, 'Worker'));
      studio.fire('worker-restarted');
    },
  }, 'Restart');
  $('#banner .row').prepend(restart);
});

document.addEventListener('DOMContentLoaded', async () => {
  for (const b of document.querySelectorAll('#tabs button')) b.onclick = () => showTab(b.dataset.tab);
  try {
    await ping();
    for (const init of studio.ready) await init();
  } catch (e) {
    showError(e, 'Start-up');
  }
  studio.started = true;
});
