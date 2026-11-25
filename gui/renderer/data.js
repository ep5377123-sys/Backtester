'use strict';

(() => {
  const { api, el, fillTable, fillSelect, busy, settings } = studio;
  const f = (id) => $(`#data-${id}`);
  const S = { options: null, loaded: false, offset: 0, total: 0, limit: 500 };

  const form = () => ({
    symbol: f('symbol').value, tf: f('tf').value.trim(), session: f('session').value, tz: f('tz').value.trim(),
    start: f('start').value, end: f('end').value, roll: f('roll').value, oos_fraction: Number(f('fraction').value),
  });
  const windowsForm = () => ({
    is: [f('is-start').value, f('is-end').value], oos: [f('oos-start').value, f('oos-end').value], dataset: f('dataset').value,
  });

  function symbolInfo() {
    return S.options.symbols.find((s) => s.symbol === f('symbol').value) || {};
  }

  function showAvailability() {
    const s = symbolInfo();
    f('avail').textContent = s.available ? `Imported 1-minute bars: ${s.first} → ${s.last}` : `${f('symbol').value} is not imported yet: Load imports it from the store first.`;
  }

  function onSymbolChange() {
    const s = symbolInfo();
    if (s.available) { f('start').value = s.first; f('end').value = s.last; }
    showAvailability();
  }

  function renderWindows(v) {
    const w = v.windows;
    f('is-start').value = w.is.start; f('is-end').value = w.is.end;
    f('oos-start').value = w.oos.start; f('oos-end').value = w.oos.end;
    f('dataset').value = v.dataset;
    f('windows').replaceChildren(...['is', 'oos', 'full'].map((k) => el('tr', { className: k === v.dataset ? 'selected' : '' }, [
      el('td', {}, w[k].label), el('td', {}, `${w[k].start} → ${w[k].end}`), el('td', {}, `${w[k].bars.toLocaleString('en-US')} bars`),
    ])));
    f('chart').src = v.chart;
    f('windows-error').classList.add('hidden');
    studio.windowsOk = true;
    settings.set('windows', windowsForm());
  }

  function renderPage(p) {
    S.offset = p.offset; S.total = p.total;
    fillTable(f('table'), p.columns, p.rows);
    const last = Math.min(p.offset + p.rows.length, p.total);
    f('rows').textContent = `rows ${(p.offset + 1).toLocaleString('en-US')}–${last.toLocaleString('en-US')} of ${p.total.toLocaleString('en-US')}`;
    $('#tab-data .table-wrap').scrollTop = 0;
  }

  function renderQuality(items) {
    const mark = { ok: '✓', bad: '✗', warn: '!', info: '·' };
    f('quality').replaceChildren(...items.map((q) => el('tr', {}, [
      el('td', {}, q.label), el('td', { className: `st ${q.status}` }, mark[q.status]), el('td', {}, q.value),
    ])));
    f('quality-empty').classList.add('hidden');
  }

  async function load() {
    const params = form();
    const saved = settings.get('windows');
    if (saved.is) Object.assign(params, { windows: { is: saved.is, oos: saved.oos }, dataset: saved.dataset });
    const r = await busy(f('load'), 'Load data', () => api('data.load', params));
    settings.set('data', params);
    S.loaded = true;
    f('label').textContent = r.label;
    renderWindows(r);
    renderQuality(r.quality);
    renderPage(r.page);
    studio.fire('data-changed', r);
    return r;
  }

  async function importData() {
    const r = await busy(f('import'), 'Update from store', () => api('data.import', { symbol: f('symbol').value, roll: f('roll').value }));
    Object.assign(symbolInfo(), r);
    onSymbolChange();
    return r;
  }

  async function setWindows() {
    if (!S.loaded) return null;
    try {
      const v = await api('data.windows', windowsForm());
      renderWindows(v);
      studio.fire('data-changed', v);
      return v;
    } catch (err) {
      f('windows-error').textContent = err.message;
      f('windows-error').classList.remove('hidden');
      studio.windowsOk = false;
      return null;
    }
  }

  async function split() {
    if (!S.loaded) return null;
    const v = await busy(f('split'), 'Split', () => api('data.split', { fraction: Number(f('fraction').value) }));
    renderWindows(v);
    studio.fire('data-changed', v);
    return v;
  }

  function swap() {
    const [a, b, c, d] = ['is-start', 'is-end', 'oos-start', 'oos-end'].map((k) => f(k).value);
    f('is-start').value = c; f('is-end').value = d; f('oos-start').value = a; f('oos-end').value = b;
    return setWindows();
  }

  async function goPage(offset, gotoDate) {
    if (!S.loaded) return;
    renderPage(await api('data.page', gotoDate ? { goto: gotoDate, limit: S.limit } : { offset, limit: S.limit }));
  }

  async function init() {
    const o = S.options = await api('data.options');
    fillSelect(f('symbol'), o.symbols.map((s) => ({ value: s.symbol, label: `${s.symbol} · ${s.name}` })));
    fillSelect(f('session'), o.sessions.map((s) => ({ value: s.key, label: `${s.key} · ${s.name}` })));
    fillSelect(f('dataset'), Object.entries(o.datasets).map(([value, label]) => ({ value, label })));
    f('tf-list').replaceChildren(...o.timeframes.map((v) => el('option', { value: v })));
    f('tz-list').replaceChildren(...o.timezones.map((v) => el('option', { value: v })));
    const saved = { ...o.defaults, ...settings.get('data') };
    f('symbol').value = saved.symbol; f('tf').value = saved.tf; f('session').value = saved.session; f('tz').value = saved.tz;
    f('roll').value = saved.roll || 'volume'; f('fraction').value = saved.oos_fraction;
    onSymbolChange();
    if (saved.start) f('start').value = saved.start;
    if (saved.end) f('end').value = saved.end;
    f('dataset').value = settings.get('windows').dataset || o.defaults.dataset;

    f('symbol').onchange = onSymbolChange;
    f('load').onclick = () => load().catch(() => {});
    f('import').onclick = () => importData().catch(() => {});
    f('split').onclick = () => split().catch(() => {});
    f('swap').onclick = () => swap();
    for (const k of ['is-start', 'is-end', 'oos-start', 'oos-end', 'dataset']) f(k).onchange = () => setWindows();
    f('prev').onclick = () => goPage(Math.max(0, S.offset - S.limit)).catch((e) => studio.showError(e, 'Bars'));
    f('next').onclick = () => goPage(Math.min(Math.max(0, S.total - 1), S.offset + S.limit)).catch((e) => studio.showError(e, 'Bars'));
    f('goto').onchange = () => goPage(0, f('goto').value).catch((e) => studio.showError(e, 'Bars'));
  }

  studio.on('worker-restarted', () => {
    if (!S.loaded) return;
    S.loaded = false;
    f('label').textContent = 'none loaded (the worker restarted: click Load)';
  });
  studio.ready.push(init);
  const params = () => {
    const w = windowsForm();
    return { ...form(), windows: { is: w.is, oos: w.oos }, dataset: w.dataset };
  };
  studio.data = { load, split, swap, setWindows, goPage, params, form: f, state: S };
})();
