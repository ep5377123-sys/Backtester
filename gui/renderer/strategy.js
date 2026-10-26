// Strategy tab: pick a strategy file → Load / Reload (re-import) → edit inputs → Apply on the Data tab's dataset;
// counts, the trade list and a candlestick chart paged by Start index / Bars to plot (a trade click jumps to it).
// Worker side: guban/gui/tab_strategy.py.
'use strict';

(() => {
  const { api, el, fillTable, fillSelect, busy, settings } = studio;
  const f = (id) => $(`#st-${id}`);
  const S = { loaded: null, applied: null, total: 0 };

  const inputKey = () => `inputs:${f('file').value}`;

  function inputValues() {
    const out = {};
    for (const node of f('inputs').querySelectorAll('[data-input]')) out[node.dataset.input] = node.type === 'checkbox' ? String(node.checked) : node.value;
    return out;
  }

  function renderInputs(info, keep) {
    const saved = { ...settings.get(inputKey()), ...(keep || {}) };
    const nodes = info.inputs.flatMap((inp) => {
      const id = `st-in-${inp.name}`;
      const value = saved[inp.name] ?? inp.value;
      const input = inp.kind === 'bool'
        ? el('input', { id, type: 'checkbox', checked: String(value) === 'true' })
        : el('input', { id, value, title: `${inp.kind}; default ${inp.value}` });
      input.dataset.input = inp.name;
      input.dataset.default = inp.value;
      return [el('label', { htmlFor: id }, inp.name), input];
    });
    f('inputs').replaceChildren(...(nodes.length ? nodes : [el('div', { className: 'muted' }, 'This strategy has no inputs.')]));
  }

  function clearResults(message) {
    S.applied = null;
    f('counts').replaceChildren();
    f('info').textContent = message;
    f('trades').replaceChildren();
    f('trade-count').textContent = '';
    f('chart').removeAttribute('src');
    f('range').textContent = '';
  }

  async function load(reload = false) {
    const keep = reload ? inputValues() : null;
    const r = await busy([f('load'), f('reload')], reload ? 'Reload strategy' : 'Load strategy',
      () => api('strategy.load', { file: f('file').value, class: f('class').value.trim() }));
    S.loaded = r;
    settings.set('strategy', { file: r.file, class: f('class').value.trim() });
    f('label').textContent = `${r.class} (${r.file})`;
    const extras = [r.doc, r.extra_columns.length ? `Extra bar columns: ${r.extra_columns.join(', ')}.` : '', r.plots.length ? `Plots: ${r.plots.join(', ')}.` : ''];
    f('doc').textContent = extras.filter(Boolean).join(' ');
    renderInputs(r, keep);
    clearResults('Apply the strategy to the Data tab\'s current dataset.');
    studio.fire('strategy-loaded', r);
    return r;
  }

  function renderChart(c) {
    f('chart').src = c.image;
    f('start').value = c.start;
    f('bars').value = c.count;
    S.total = c.total;
    f('range').textContent = `bars ${(c.start + 1).toLocaleString('en-US')}–${Math.min(c.start + c.count, c.total).toLocaleString('en-US')} of ${c.total.toLocaleString('en-US')}`;
  }

  async function apply() {
    if (!studio.data.state.loaded) throw new Error('Load data on the Data tab first');
    if (studio.windowsOk === false) throw new Error('Fix the in-sample / out-of-sample dates on the Data tab first');
    if (!S.loaded) await load();
    const inputs = inputValues();
    const r = await busy(f('apply'), 'Apply strategy', () => api('strategy.apply', { inputs, start: Number(f('start').value) || 0, bars: Number(f('bars').value) || 500 }));
    settings.set(inputKey(), inputs);
    S.applied = r;
    f('counts').replaceChildren(...r.counts.map(([k, v]) => el('tr', {}, [el('td', {}, k), el('td', {}, typeof v === 'number' ? v.toLocaleString('en-US') : v)])));
    f('info').replaceChildren(...[`${r.dataset}: ${r.window}`, r.costs, `Backtest ${r.seconds} s.`, ...r.notes].map((t) => el('div', {}, t)));
    const pnl = r.trades.columns.indexOf('net P&L');
    fillTable(f('trades'), r.trades.columns, r.trades.rows, (i, tr) => {
      const cell = tr.children[pnl];
      cell.className = r.trades.rows[i][pnl] >= 0 ? 'pos' : 'neg';
      tr.onclick = () => showTrade(i).catch((e) => studio.showError(e, 'Chart'));
    });
    f('trade-count').textContent = `(${r.trades.rows.length.toLocaleString('en-US')})`;
    renderChart(r.chart);
    studio.fire('applied', r);
    return r;
  }

  async function chart(start) {
    if (!S.applied) return null;
    const c = await api('strategy.chart', { start, bars: Number(f('bars').value) || 500 });
    renderChart(c);
    return c;
  }

  async function showTrade(i) {
    for (const row of f('trades').querySelectorAll('tbody tr')) row.classList.remove('selected');
    const row = f('trades').querySelectorAll('tbody tr')[i];
    row.classList.add('selected');
    row.scrollIntoView({ block: 'nearest' });
    const [entry, exit] = S.applied.trades.bars[i];
    const count = Number(f('bars').value) || 500;
    const span = exit - entry;
    return chart(Math.max(0, entry - Math.max(20, Math.round((count - span) / 3))));
  }

  async function init() {
    const { files } = await api('strategy.list');
    fillSelect(f('file'), files.map((x) => ({ value: x.file, label: x.name })));
    const saved = settings.get('strategy');
    if (saved.file && files.some((x) => x.file === saved.file)) f('file').value = saved.file;
    f('class').value = saved.class || '';
    f('load').onclick = () => load().catch(() => {});
    f('reload').onclick = () => load(true).catch(() => {});
    f('apply').onclick = () => apply().catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Apply strategy'); });
    f('file').onchange = () => { S.loaded = null; f('label').textContent = 'none loaded'; };
    const step = () => Math.max(20, Math.round((Number(f('bars').value) || 500) * 0.8));
    const onErr = (e) => studio.showError(e, 'Chart');
    f('prev').onclick = () => chart(Math.max(0, (Number(f('start').value) || 0) - step())).catch(onErr);
    f('next').onclick = () => chart((Number(f('start').value) || 0) + step()).catch(onErr);
    f('start').onchange = () => chart(Number(f('start').value) || 0).catch(onErr);
    f('bars').onchange = () => chart(Number(f('start').value) || 0).catch(onErr);
  }

  studio.on('data-changed', () => { if (S.applied) clearResults('The data or the dataset changed: Apply again.'); });
  studio.on('worker-restarted', () => { S.loaded = null; if (S.applied) clearResults('The worker restarted: Load and Apply again.'); });
  studio.ready.push(init);
  // set input fields (e.g. an optimization result's inputs) and remember them
  function setInputs(values) {
    for (const node of f('inputs').querySelectorAll('[data-input]')) {
      if (!(node.dataset.input in values)) continue;
      if (node.type === 'checkbox') node.checked = String(values[node.dataset.input]) === 'true';
      else node.value = values[node.dataset.input];
    }
    settings.set(inputKey(), inputValues());
  }
  studio.strategy = { load, apply, chart, showTrade, inputValues, setInputs, state: S };
})();
