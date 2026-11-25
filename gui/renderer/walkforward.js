'use strict';

(() => {
  const { api, el, fillTable, fillSelect, busy, settings } = studio;
  const f = (id) => $(`#wf-${id}`);
  const S = { options: null, result: null, running: false, stopping: false, report: null };
  const grid = studio.gridEditor('wfgrid', { container: f('grid'), error: f('grid-error'), count: f('count') }, 'grid');
  const LAYOUT = { is: 'is_months', oos: 'oos_months', step: 'step_months', folds: 'folds', start: 'start' };

  const fragment = (html) => [...new DOMParser().parseFromString(html || '', 'text/html').body.childNodes];

  function layout() {
    const p = Object.fromEntries(Object.entries(LAYOUT).map(([id, key]) => [key, f(id).value]));
    p.anchored = f('mode').value === 'anchored';
    return p;
  }

  function remember() {
    settings.set('walkforward', { ...layout(), mode: f('mode').value, metric: f('metric').value, min_trades: f('mintrades').value, workers: f('workers').value });
  }

  async function preview() {
    if (!studio.data.state.loaded) { f('windows').replaceChildren(); return null; }
    try {
      const r = await api('wf.windows', layout());
      fillTable(f('windows'), r.columns, r.rows);
      f('windows-error').classList.add('hidden');
      return r;
    } catch (err) {
      f('windows').replaceChildren();
      f('windows-error').textContent = err.message;
      f('windows-error').classList.remove('hidden');
      return null;
    }
  }
  let timer = null;
  const previewSoon = () => { clearTimeout(timer); timer = setTimeout(() => { remember(); preview(); }, 300); };

  function setRunning(on) {
    S.running = on;
    f('stop').disabled = !on;
    f('progress-row').classList.toggle('hidden', !on);
    if (on) { f('progress').removeAttribute('value'); f('progress-text').textContent = 'Starting the walk-forward process…'; }
  }

  async function run(confirmBig = false) {
    if (!studio.data.state.loaded) throw new Error('Load data on the Data tab first');
    const strat = studio.strategy.state.loaded;
    if (!strat) throw new Error('Load a strategy on the Strategy tab first');
    const folds = await preview();
    if (!folds) throw new Error(f('windows-error').textContent || 'Check the folds');
    const c = await grid.count();
    if (!c) throw new Error(f('grid-error').textContent || 'Check the grid');
    if (c.count > S.options.max_combinations && !confirmBig) {
      if (!window.confirm(`${c.count.toLocaleString('en-US')} combinations × ${folds.folds} folds is more than ${S.options.max_combinations.toLocaleString('en-US')} combinations. Run them all?`)) return null;
      confirmBig = true;
    }
    const p = {
      data: studio.data.params(),
      strategy: { file: strat.file, class: strat.class },
      inputs: studio.strategy.inputValues(),
      grid: grid.specs(),
      ...layout(),
      metric: f('metric').value, min_trades: f('mintrades').value, workers: f('workers').value,
      commission: $('#bt-commission').value, slippage: $('#bt-slippage').value, fill_mode: $('#bt-fill').value,
      confirm: confirmBig,
    };
    remember();
    studio.optJob.take('walk-forward');
    setRunning(true);
    S.stopping = false;
    try {
      const r = await busy([f('run'), f('save')], 'Walk-forward', () => api('wf.run', p, 'opt'));
      render(r);
      await api('wf.register', { strategy: r.strategy, tz: r.tz });
      studio.fire('walkforward-done', r);
      return r;
    } finally {
      setRunning(false);
      studio.optJob.release('walk-forward');
    }
  }

  function render(r) {
    S.result = r;
    S.report = null;
    f('save').disabled = false;
    f('open').disabled = true;
    f('equity').src = r.charts.equity || '';
    f('drawdown').src = r.charts.drawdown || '';
    f('folds-chart').src = r.charts.folds || '';
    const s = r.seconds;
    const info = el('div', { className: 'note' }, `${r.combinations.toLocaleString('en-US')} combinations × ${r.windows.length} folds from ${r.start} on ${r.workers} processes; `
      + `walk-forward ${s.walk_forward} s, charts ${s.charts} s · ${r.costs}`);
    f('perf').replaceChildren(el('h2', {}, 'Out of sample, stitched'), ...fragment(r.html.tiles), info);
    f('section').replaceChildren(...fragment(r.html.section), ...fragment(r.html.summary));
    f('use').disabled = !r.last_inputs;
    f('last').textContent = r.last_inputs ? Object.entries(r.last_inputs).map(([k, v]) => `${k} = ${v}`).join(', ') : 'no fold picked inputs';
  }

  function useLast() {
    if (!S.result || !S.result.last_inputs) return;
    studio.strategy.setInputs(S.result.last_inputs);
    studio.showTab('strategy');
    $('#last-action').textContent = 'Inputs set on the Strategy tab: click Apply';
  }

  async function stop() {
    S.stopping = true;
    f('progress-text').textContent = 'Stopping…';
    await window.guban.stop('opt');
  }

  async function save(out) {
    const r = await busy([f('save'), f('run')], 'Save walk-forward', () => api('wf.save', { out, trade_charts: f('images').checked }, 'opt'));
    S.report = r.report;
    f('open').disabled = false;
    $('#wf-perf .note').append(` · saved to ${r.folder}`);
    return r;
  }

  async function init() {
    const o = S.options = { ...(await api('wf.options')), max_combinations: (await api('optimize.options')).max_combinations };
    fillSelect(f('metric'), o.metrics.map((m) => ({ value: m.key, label: m.label })));
    const d = o.defaults;
    const saved = { ...d, mode: d.anchored ? 'anchored' : 'rolling', workers: '', start: '', ...settings.get('walkforward') };
    for (const [id, key] of Object.entries(LAYOUT)) f(id).value = saved[key] ?? '';
    f('mode').value = saved.mode;
    f('metric').value = saved.metric;
    f('mintrades').value = saved.min_trades;
    f('workers').value = saved.workers;
    f('workers').placeholder = `all ${(await api('optimize.options')).cpus} cores`;
    f('images').checked = !!saved.trade_images;
    for (const id of [...Object.keys(LAYOUT), 'mode']) f(id).oninput = previewSoon;
    f('mode').onchange = previewSoon;
    for (const id of ['metric', 'mintrades', 'workers']) f(id).onchange = remember;
    f('images').onchange = () => settings.set('walkforward', { ...settings.get('walkforward'), trade_images: f('images').checked });
    f('run').onclick = () => run().catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Walk-forward'); });
    f('stop').onclick = () => stop();
    f('save').onclick = () => save(undefined).catch(() => {});
    f('open').onclick = () => api('backtest.open', { path: S.report }).catch((e) => studio.showError(e, 'Open report'));
    f('use').onclick = useLast;
    preview();
  }

  studio.on('event:progress', (msg) => {
    if (msg.worker !== 'opt' || !S.running) return;
    if (msg.total) { f('progress').max = msg.total; f('progress').value = msg.done; }
    if (msg.message) f('progress-text').textContent = msg.message;
  });
  studio.on('opt-worker-exit', (info) => {
    if (S.stopping || studio.optJob.holder !== 'walk-forward') return;
    studio.showError({ message: `the walk-forward process ended unexpectedly (exit code ${info.code})`, frames: [], traceback: info.stderr.join('\n') }, 'Walk-forward');
  });
  studio.on('opt-job', (holder) => { f('run').disabled = !!holder && holder !== 'walk-forward'; });
  studio.on('strategy-loaded', (info) => { S.result = null; f('save').disabled = true; grid.render(info); });
  studio.on('data-changed', () => preview());
  studio.on('worker-restarted', () => grid.render(null));
  studio.ready.push(init);
  studio.walkforward = { run, stop, save, preview, useLast, count: grid.count, state: S };
})();
