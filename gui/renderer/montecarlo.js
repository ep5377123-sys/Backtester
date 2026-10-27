// Monte Carlo tab: one trade source (the Backtest run, the walk-forward's out-of-sample trades, or a file) and two
// separate simulations of it, as sub-tabs: the trade Monte Carlo (`guban run`'s) and the prop-firm evaluation
// (`guban propfirm`). Worker side: guban/gui/tab_montecarlo.py.
'use strict';

(() => {
  const { api, el, fillSelect, busy, settings } = studio;
  const f = (id) => $(`#mc-${id}`);
  const pf = (id) => $(`#pf-${id}`);
  const S = { sources: null, trade: null, tradeSource: null, stale: false, propfirm: null, propfirmSource: null, rules: null, report: null };
  const FIELDS = { sims: 'mc_sims', seed: 'mc_seed', ddlimit: 'mc_dd_limit', risk: 'mc_risk', oos: 'mc_oos_from' };

  // report.py's HTML fragments: parsed, never run (they hold no scripts), then moved into the page
  const fragment = (html) => [...new DOMParser().parseFromString(html || '', 'text/html').body.childNodes];

  // The trade Monte Carlo's settings: the Backtest tab's Run (evaluation) and Save use them too.
  const tradeSettings = () => Object.fromEntries(Object.entries(FIELDS).map(([id, key]) => [key, f(id).value]));
  const sourceParams = () => ({ source: f('source').value, file: f('file').value.trim() });
  const sourceKey = () => { const p = sourceParams(); return p.source === 'file' ? `file:${p.file}` : p.source; };

  function showSource() {
    f('file').classList.toggle('hidden', f('source').value !== 'file');
    f('browse').classList.toggle('hidden', f('source').value !== 'file');
  }

  async function refreshSources() {
    const r = await api('mc.sources');
    S.sources = r.sources;
    const current = f('source').value || settings.get('montecarlo').source || 'backtest';
    fillSelect(f('source'), r.sources.map((s) => ({ value: s.key, label: s.available ? s.label : `${s.label} (none yet)` })));
    f('source').value = current;
    showSource();
    return r;
  }

  // a source that ran again since a simulation used it: its results are stale until the next Run
  function markStale(kind) {
    if (S.tradeSource === kind || S.propfirmSource === kind) {
      S.stale = true;
      f('stale').classList.remove('hidden');
    }
    refreshSources().catch(() => {});
  }

  function remember() {
    settings.set('montecarlo', { ...settings.get('montecarlo'), ...tradeSettings(), ...sourceParams() });
  }

  function sourceRan(label) {
    S.stale = false;
    f('stale').classList.add('hidden');
    f('source-label').textContent = label;
  }

  async function runTrade() {
    const p = { ...sourceParams(), ...tradeSettings() };
    remember();
    const r = await busy([f('run')], 'Trade Monte Carlo', () => api('mc.trade_run', p));
    sourceRan(r.source);
    S.tradeSource = sourceKey();
    S.trade = r;
    f('trade-out').replaceChildren(...fragment(r.html));
    f('info').textContent = `${r.source} · ${r.span} · ${r.seconds} s`;
    return r;
  }

  async function browse() {
    const path = await window.guban.pickFile([{ name: 'Trade lists', extensions: ['csv', 'xlsx'] }]);
    if (path) { f('file').value = path; remember(); }
    return path;
  }

  // --- prop-firm evaluation ---------------------------------------------------------------------------------------

  const fieldKey = () => `propfirm:${pf('rules').value}`;

  function fieldValues() {
    return Object.fromEntries([...pf('fields').querySelectorAll('[data-field]')].map((n) => [n.dataset.field, n.value]));
  }

  async function loadRules(name) {
    const r = S.rules = await api('mc.propfirm_rules', { rules: name });
    fillSelect(pf('rules'), r.rules.map((n) => ({ value: n, label: `${n}.toml` })));
    pf('rules').value = r.selected;
    const saved = settings.get(`propfirm:${r.selected}`);
    const nodes = r.fields.flatMap((x) => {
      const id = `pf-in-${x.key}`;
      const value = saved[x.key] ?? x.value;
      const input = x.kind === 'bool'
        ? el('select', { id }, ['true', 'false'].map((v) => el('option', { value: v, textContent: v })))
        : el('input', { id, value, title: `${x.section}.${x.key}; file value ${x.value}` });
      input.value = value;
      input.dataset.field = x.key;
      input.dataset.file = x.value;
      input.onchange = () => { settings.set(fieldKey(), fieldValues()); markEdited(); };
      return [el('label', { htmlFor: id }, x.label), input];
    });
    pf('fields').replaceChildren(...nodes, el('label', {}, 'Scenarios'), el('div', { className: 'muted' }, r.scenarios.join(' · ') || 'none'));
    pf('notes').replaceChildren(...r.notes.map((n) => el('li', {}, n)));
    markEdited();
    return r;
  }

  // fields that differ from the rules file are marked
  function markEdited() {
    for (const n of pf('fields').querySelectorAll('[data-field]')) n.classList.toggle('edited', n.value !== n.dataset.file);
  }

  function resetRules() {
    settings.set(fieldKey(), {});
    return loadRules(pf('rules').value);
  }

  async function runPropfirm() {
    const p = { ...sourceParams(), rules: pf('rules').value, fields: fieldValues() };
    remember();
    const r = await busy([pf('run'), pf('save')], 'Prop-firm evaluation', () => api('mc.propfirm_run', p));
    sourceRan(r.source);
    S.propfirmSource = sourceKey();
    S.propfirm = r;
    S.report = null;
    pf('save').disabled = false;
    pf('open').disabled = true;
    pf('out').replaceChildren(...fragment(r.html));
    pf('info').textContent = `${r.source} · ${r.span} · ${r.seconds} s${r.edited.length ? ` · edited: ${r.edited.join(', ')}` : ''}`;
    return r;
  }

  async function savePropfirm(out) {
    const r = await busy([pf('save'), pf('run')], 'Save prop-firm', () => api('mc.propfirm_save', { out }));
    S.report = r.report;
    pf('open').disabled = false;
    pf('info').append(` · saved to ${r.folder}`);
    return r;
  }

  function showSub(name) {
    for (const b of document.querySelectorAll('#mc-subtabs button')) b.classList.toggle('active', b.dataset.sub === name);
    for (const s of document.querySelectorAll('#tab-montecarlo .sub')) s.classList.toggle('active', s.id === `mc-sub-${name}`);
    settings.set('montecarlo', { ...settings.get('montecarlo'), sub: name });
  }

  async function init() {
    const r = await refreshSources();
    const old = settings.get('backtest'); // Monte Carlo settings lived on the Backtest tab before Phase 20
    const saved = { ...r.defaults, ...Object.fromEntries(Object.values(FIELDS).filter((k) => old[k] !== undefined).map((k) => [k, old[k]])), ...settings.get('montecarlo') };
    for (const [id, key] of Object.entries(FIELDS)) f(id).value = saved[key] ?? '';
    f('file').value = saved.file || '';
    f('source').onchange = () => { showSource(); remember(); };
    f('file').onchange = remember;
    f('browse').onclick = () => browse().catch((e) => studio.showError(e, 'Browse'));
    f('run').onclick = () => runTrade().catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Trade Monte Carlo'); });
    for (const b of document.querySelectorAll('#mc-subtabs button')) b.onclick = () => showSub(b.dataset.sub);
    await loadRules(settings.get('montecarlo').rules);
    pf('rules').onchange = () => { settings.set('montecarlo', { ...settings.get('montecarlo'), rules: pf('rules').value }); loadRules(pf('rules').value).catch((e) => studio.showError(e, 'Rules')); };
    pf('reset').onclick = () => resetRules().catch((e) => studio.showError(e, 'Rules'));
    pf('run').onclick = () => runPropfirm().catch((e) => { if (!(e instanceof WorkerError)) studio.showError(e, 'Prop-firm evaluation'); });
    pf('save').onclick = () => savePropfirm(undefined).catch(() => {});
    pf('open').onclick = () => api('backtest.open', { path: S.report }).catch((e) => studio.showError(e, 'Open report'));
    showSub(settings.get('montecarlo').sub || 'trade');
  }

  studio.on('backtested', () => markStale('backtest'));
  studio.on('walkforward-done', () => markStale('walkforward'));
  studio.on('worker-restarted', () => { pf('save').disabled = true; refreshSources().catch(() => {}); });
  studio.ready.push(init);
  studio.montecarlo = { runTrade, runPropfirm, savePropfirm, loadRules, resetRules, tradeSettings, sourceParams, showSub, refreshSources, state: S };
})();
