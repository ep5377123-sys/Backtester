// Backtest tab: rerun the applied strategy with this tab's capital / costs / fill mode / Monte Carlo settings and show
// what `guban run` reports (report.html's sections, styled dark) plus monthly bars and daily histograms.
// Save results writes the run folder `guban run` writes. Worker side: guban/gui/tab_backtest.py.
'use strict';

(() => {
  const { api, el, busy, settings } = studio;
  const f = (id) => $(`#bt-${id}`);
  const S = { result: null, report: null, defaults: null };
  const FIELDS = { capital: 'capital', commission: 'commission', slippage: 'slippage', fill: 'fill_mode', sims: 'mc_sims', seed: 'mc_seed', ddlimit: 'mc_dd_limit', risk: 'mc_risk', oos: 'mc_oos_from' };

  const params = () => Object.fromEntries(Object.entries(FIELDS).map(([id, key]) => [key, f(id).value]));

  // report.py's HTML fragments: parsed, never run (they hold no scripts), then moved into the page
  function fragment(html) {
    const doc = new DOMParser().parseFromString(html || '', 'text/html');
    return [...doc.body.childNodes];
  }

  function clear(message) {
    S.result = null;
    for (const id of ['monthly', 'equity', 'daily', 'drawdown', 'dailydd']) f(id).removeAttribute('src');
    f('perf').replaceChildren(el('h2', {}, 'Performance'), el('div', { className: 'muted' }, message));
    f('sections').replaceChildren();
  }

  async function fillDefaults() {
    if (!studio.data.state.loaded) return;
    const d = S.defaults = await api('backtest.defaults');
    const saved = settings.get('backtest');
    f('capital').value = saved.capital ?? d.capital;
    f('fill').value = saved.fill_mode || d.fill_mode;
    f('sims').value = saved.mc_sims ?? d.mc_sims;
    f('seed').value = saved.mc_seed ?? d.mc_seed;
    for (const k of ['ddlimit', 'risk', 'oos']) f(k).value = saved[FIELDS[k]] ?? '';
    // costs are per contract: the loaded symbol's defaults unless this symbol had its own
    const costs = (saved.costs || {})[d.contract] || {};
    f('commission').value = costs.commission ?? d.commission;
    f('slippage').value = costs.slippage ?? d.slippage;
  }

  function saveSettings(p) {
    const saved = settings.get('backtest');
    const costs = { ...(saved.costs || {}), [S.defaults.contract]: { commission: p.commission, slippage: p.slippage } };
    settings.set('backtest', { ...p, costs });
  }

  async function run() {
    if (!studio.strategy.state.applied) throw new Error('Apply a strategy on the Strategy tab first');
    if (!S.defaults) await fillDefaults();
    const p = params();
    const r = await busy([f('run'), f('save')], 'Backtest', () => api('backtest.run', p));
    saveSettings(p);
    S.result = r;
    S.report = null;
    f('open').disabled = true;
    const c = r.charts;
    f('monthly').src = c.monthly || '';
    f('equity').src = c.equity || '';
    f('drawdown').src = c.drawdown || '';
    f('daily').src = c.daily_pnl || '';
    f('dailydd').src = c.daily_dd || '';
    const h = r.html;
    f('perf').replaceChildren(el('h2', {}, 'Performance'), ...fragment(h.tiles), ...fragment(h.summary));
    const notes = r.notes.length ? [el('section', {}, [el('h2', {}, 'Notes'), el('ul', { className: 'notes-list' }, r.notes.map((n) => el('li', {}, n)))])] : [];
    f('sections').replaceChildren(...fragment(h.evaluation), ...fragment(h.monte_carlo), ...fragment(h.monthly), ...notes);
    const s = r.seconds;
    f('info').replaceChildren(...[r.title, r.window, r.costs,
      `${r.trades.toLocaleString('en-US')} trades · backtest ${s.backtest} s · Monte Carlo ${s.monte_carlo} s · charts ${s.charts} s`].map((t) => el('div', {}, t)));
    studio.fire('backtested', r);
    return r;
  }

  async function save(out) {
    if (!S.result) throw new Error('Run the backtest first');
    const r = await busy([f('save'), f('run')], 'Save results', () => api('backtest.save', { trade_charts: f('images').checked, out }));
    S.report = r.report;
    f('open').disabled = false;
    f('info').append(el('div', {}, `Saved: ${r.folder}`));
    return r;
  }

  async function init() {
    f('run').onclick = () => run().catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Backtest'); });
    f('save').onclick = () => save(undefined).catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Save results'); });
    f('open').onclick = () => api('backtest.open', { path: S.report }).catch((e) => studio.showError(e, 'Open report'));
    f('images').checked = !!settings.get('backtest').trade_images;
    f('images').onchange = () => settings.set('backtest', { ...settings.get('backtest'), trade_images: f('images').checked });
  }

  studio.on('data-changed', () => { S.defaults = null; fillDefaults().catch(() => {}); if (S.result) clear('The data changed: apply the strategy and run again.'); });
  studio.on('applied', () => { if (S.result) clear('A new Apply on the Strategy tab: run again.'); });
  studio.on('strategy-loaded', () => { if (S.result) clear('A new strategy was loaded: apply it and run again.'); });
  studio.on('worker-restarted', () => { S.defaults = null; if (S.result) clear('The worker restarted: load, apply and run again.'); });
  studio.ready.push(init);
  studio.backtest = { run, save, state: S };
})();
