// Optimization tab: a grid over the loaded strategy's inputs, optimized on the in-sample window, the top N rerun out of
// sample. Runs in the separate "opt" worker (Stop ends it); the main worker only counts combinations.
// Worker side: guban/gui/tab_optimize.py.
'use strict';

(() => {
  const { api, el, fillTable, fillSelect, busy, settings } = studio;
  const f = (id) => $(`#op-${id}`);
  const S = { options: null, result: null, running: false, stopping: false, selected: null };
  const grid = studio.gridEditor('grid', { container: f('grid'), error: f('grid-error'), count: f('count') });

  function renderWindows() {
    const w = studio.data.state.loaded ? studio.data.params().windows : null;
    f('windows').replaceChildren(...(w ? [['In sample', w.is], ['Out of sample', w.oos]].map(([k, [a, b]]) => el('tr', {}, [el('td', {}, k), el('td', {}, `${a} → ${b}`)])) : []));
  }

  function setRunning(on) {
    S.running = on;
    f('stop').disabled = !on;
    f('progress-row').classList.toggle('hidden', !on);
    if (on) { f('progress').removeAttribute('value'); f('progress-text').textContent = 'Starting the optimization process…'; }
  }

  async function run(confirmBig = false) {
    if (!studio.data.state.loaded) throw new Error('Load data on the Data tab first');
    if (studio.windowsOk === false) throw new Error('Fix the in-sample / out-of-sample dates on the Data tab first');
    const strat = studio.strategy.state.loaded;
    if (!strat) throw new Error('Load a strategy on the Strategy tab first');
    const c = await grid.count();
    if (!c) throw new Error(f('grid-error').textContent || 'Check the grid');
    if (c.count > S.options.max_combinations && !confirmBig) {
      if (!window.confirm(`${c.count.toLocaleString('en-US')} combinations is more than ${S.options.max_combinations.toLocaleString('en-US')}. Run them all?`)) return null;
      confirmBig = true;
    }
    const p = {
      data: studio.data.params(),
      strategy: { file: strat.file, class: strat.class },
      inputs: studio.strategy.inputValues(),
      grid: grid.specs(),
      metric: f('metric').value, min_trades: f('mintrades').value, top_n: f('topn').value, workers: f('workers').value,
      commission: $('#bt-commission').value, slippage: $('#bt-slippage').value, fill_mode: $('#bt-fill').value,
      confirm: confirmBig,
    };
    settings.set('optimize', { metric: p.metric, min_trades: p.min_trades, top_n: p.top_n, workers: p.workers });
    studio.optJob.take('optimization');
    setRunning(true);
    S.stopping = false;
    try {
      const r = await busy([f('run'), f('save')], 'Optimization', () => api('optimize.run', p, 'opt'));
      render(r);
      return r;
    } finally {
      setRunning(false);
      studio.optJob.release('optimization');
    }
  }

  function render(r) {
    S.result = r;
    S.selected = null;
    f('save').disabled = false;
    f('use').disabled = true;
    f('selected').textContent = '';
    f('equity').src = r.charts.equity || '';
    f('hists').src = r.charts.hists || '';
    f('scatter').src = r.charts.scatter || '';
    const s = r.seconds;
    f('summary').textContent = `(${r.top} of ${r.ranked.toLocaleString('en-US')} ranked runs; ${r.combinations.toLocaleString('en-US')} combinations on ${r.workers} processes; `
      + `in sample ${s.in_sample} s, retests ${s.top} s · ${r.costs})`;
    const t = r.table;
    const net = [t.columns.indexOf('IS net'), t.columns.indexOf('OOS net')];
    fillTable(f('table'), t.columns, t.rows, (i, tr) => {
      for (const k of net) tr.children[k].className = t.rows[i][k] >= 0 ? 'pos' : 'neg';
      tr.onclick = () => select(i);
    });
  }

  function select(i) {
    S.selected = i;
    for (const row of f('table').querySelectorAll('tbody tr')) row.classList.remove('selected');
    f('table').querySelectorAll('tbody tr')[i].classList.add('selected');
    f('use').disabled = false;
    f('selected').textContent = Object.entries(S.result.table.inputs[i]).map(([k, v]) => `${k} = ${v}`).join(', ');
  }

  function useSelected() {
    if (S.selected === null) return;
    studio.strategy.setInputs(S.result.table.inputs[S.selected]);
    studio.showTab('strategy');
    $('#last-action').textContent = 'Inputs set on the Strategy tab: click Apply';
  }

  async function stop() {
    S.stopping = true;
    f('progress-text').textContent = 'Stopping…';
    await window.guban.stop('opt');
  }

  async function save(out) {
    const r = await busy(f('save'), 'Save optimization', () => api('optimize.save', { out }, 'opt'));
    f('summary').textContent += ` · saved to ${r.folder}`;
    return r;
  }

  async function init() {
    const o = S.options = await api('optimize.options');
    fillSelect(f('metric'), o.metrics.map((m) => ({ value: m.key, label: m.label })));
    const saved = { ...o.defaults, workers: '', ...settings.get('optimize') };
    f('metric').value = saved.metric; f('mintrades').value = saved.min_trades; f('topn').value = saved.top_n; f('workers').value = saved.workers;
    f('workers').placeholder = `all ${o.cpus} cores`;
    f('run').onclick = () => run().catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Optimization'); });
    f('stop').onclick = () => stop();
    f('save').onclick = () => save(undefined).catch(() => {});
    f('use').onclick = useSelected;
  }

  studio.on('event:progress', (msg) => {
    if (msg.worker !== 'opt' || !S.running) return;
    if (msg.total) { f('progress').max = msg.total; f('progress').value = msg.done; }
    if (msg.message) f('progress-text').textContent = msg.message;
  });
  studio.on('opt-worker-exit', (info) => {
    if (S.stopping || studio.optJob.holder !== 'optimization') return;
    studio.showError({ message: `the optimization process ended unexpectedly (exit code ${info.code})`, frames: [], traceback: info.stderr.join('\n') }, 'Optimization');
  });
  studio.on('strategy-loaded', (info) => { S.result = null; f('save').disabled = true; grid.render(info); });
  studio.on('data-changed', renderWindows);
  studio.on('worker-restarted', () => grid.render(null));
  studio.on('opt-job', (holder) => { f('run').disabled = !!holder && holder !== 'optimization'; });
  studio.ready.push(init);
  studio.optimize = { run, stop, save, select, useSelected, count: grid.count, state: S };
})();
