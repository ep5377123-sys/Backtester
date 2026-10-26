// Smoke steps for `guban gui --smoke` (main.js runs each step in order, then screenshots the window).
// Each step resolves {ok, ms, detail}; a thrown error is a failed step. The worker crash steps run last.
'use strict';

(() => {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (cond, what, ms = 20000) => {
    for (const t0 = performance.now(); !cond(); await sleep(50)) if (performance.now() - t0 > ms) throw new Error(`timed out waiting for ${what}`);
  };
  const decoded = (img) => new Promise((res, rej) => {
    if (img.complete && img.naturalWidth) return res(img);
    img.onload = () => res(img);
    img.onerror = () => rej(new Error('image did not decode'));
  });
  const check = (cond, msg) => { if (!cond) throw new Error(msg); };
  const timed = async (fn) => {
    const t0 = performance.now();
    try {
      await until(() => studio.started, 'start-up');
      const detail = await fn();
      return { ok: true, ms: Math.round(performance.now() - t0), detail };
    } catch (err) {
      return { ok: false, ms: Math.round(performance.now() - t0), detail: err.message, frames: err.frames };
    }
  };
  const set = (id, value) => { $(`#data-${id}`).value = value; };

  const STEPS = {
    async ping() {
      return studio.api('ping');
    },
    async bridge_2mb() {
      // A1: a multi-MB PNG round trip; transfer = round trip minus the worker's drawing time
      const t0 = performance.now();
      const r = await studio.api('debug.chart', { noise: true, width: 12, height: 4 });
      const transfer = Math.round(performance.now() - t0 - r.draw_ms);
      const img = new Image();
      img.src = r.image;
      await decoded(img);
      check(r.bytes > 1.5e6, `test image only ${r.bytes} bytes`);
      check(transfer < 300, `transfer ${transfer} ms > 300 ms for ${r.bytes} bytes`);
      return { bytes: r.bytes, draw_ms: r.draw_ms, transfer_ms: transfer };
    },
    async data_load() {
      studio.showTab('data');
      studio.settings.set('windows', {}); // the smoke run starts from the fraction split
      set('symbol', 'NQ'); set('tf', '15m'); set('session', 'eth'); set('tz', 'America/New_York');
      set('start', '2019-01-01'); set('end', '2026-09-30'); set('fraction', '0.3'); set('dataset', 'is');
      const r = await studio.data.load();
      await decoded($('#data-chart'));
      check($('#data-table tbody').rows.length === 500, 'bar table has no 500-row page');
      check($('#data-quality').rows.length >= 10, 'quality report is short');
      check($('#data-oos-start').value === r.windows.oos.start, 'OOS dates not filled');
      return { label: r.label, bars: r.bars, is: r.windows.is, oos: r.windows.oos };
    },
    async data_overlap_error() {
      set('oos-start', '2023-01-01');
      await studio.data.setWindows();
      const box = $('#data-windows-error');
      check(!box.classList.contains('hidden') && box.textContent.includes('overlap'), 'no overlap error shown');
      check(studio.windowsOk === false, 'windows still marked valid');
      return box.textContent;
    },
    async data_swap() {
      await studio.data.split();
      await studio.data.swap();
      set('dataset', 'oos');
      const v = await studio.data.setWindows();
      check(v && v.windows.oos.start === '2019-01-01', 'swap did not move OOS to the start');
      await decoded($('#data-chart'));
      return v.windows;
    },
    async data_goto() {
      $('#data-goto').value = '2024-06-03';
      await studio.data.goPage(0, '2024-06-03');
      const first = $('#data-table tbody tr td').textContent;
      check(first.startsWith('2024-06-03'), `first row ${first}`);
      return first;
    },
    async strategy_load() {
      studio.showTab('strategy');
      studio.settings.set('inputs:strategies/vault_break.py', {});
      $('#st-file').value = 'strategies/vault_break.py';
      $('#st-class').value = '';
      const r = await studio.strategy.load();
      check($('#st-inputs').querySelectorAll('[data-input]').length === r.inputs.length && r.inputs.length > 0, 'input fields missing');
      return { class: r.class, inputs: r.inputs.length };
    },
    async strategy_apply() {
      await studio.data.split(); // in sample = the first 70%
      $('#data-dataset').value = 'is';
      await studio.data.setWindows();
      const r = await studio.strategy.apply();
      await decoded($('#st-chart'));
      check(r.trades.rows.length > 0, 'no trades');
      check($('#st-trades tbody').rows.length === r.trades.rows.length, 'trade table rows');
      const n = Object.fromEntries(r.counts);
      check(n['Long entries'] + n['Short entries'] > 0, 'no entries counted');
      return { trades: r.trades.rows.length, net: n['Net profit (default costs)'], window: r.window, seconds: r.seconds };
    },
    async strategy_trade_click() {
      const rows = $('#st-trades tbody').rows;
      const i = Math.min(10, rows.length - 1);
      const before = $('#st-chart').src;
      await studio.strategy.showTrade(i);
      await until(() => $('#st-chart').src !== before, 'the redrawn chart');
      await decoded($('#st-chart'));
      const [entry] = studio.strategy.state.applied.trades.bars[i];
      const start = Number($('#st-start').value);
      check(start <= entry && entry < start + Number($('#st-bars').value), `trade bar ${entry} not in view from ${start}`);
      return { trade: i + 1, entry_bar: entry, start };
    },
    async strategy_bad_input() {
      const input = $('#st-inputs [data-input]:not([type=checkbox])');
      const old = input.value;
      input.value = 'abc';
      $('#banner').classList.add('hidden');
      let failed = false;
      try { await studio.strategy.apply(); } catch (e) { failed = true; }
      input.value = old;
      check(failed && !$('#banner').classList.contains('hidden') && $('#banner').textContent.includes('abc'), 'no error banner for a bad input');
      return $('#banner div').textContent;
    },
    async backtest_run() {
      studio.showTab('backtest');
      $('#bt-sims').value = '2000';
      $('#bt-images').checked = false;
      const r = await studio.backtest.run();
      for (const id of ['monthly', 'equity', 'drawdown', 'daily', 'dailydd']) await decoded($(`#bt-${id}`));
      check($('#bt-sections #monte-carlo') && $('#bt-sections #evaluation'), 'Monte Carlo / Evaluation sections missing');
      check($('#bt-perf .tiles') && $('#bt-perf table.summary'), 'tiles / summary missing');
      return { trades: r.trades, net: r.net_profit, seconds: r.seconds };
    },
    async backtest_sections() {
      $('#bt-sections').scrollIntoView();
      return { sections: [...document.querySelectorAll('#bt-sections section')].map((s) => s.id || s.querySelector('h2')?.textContent) };
    },
    async backtest_save() {
      const r = await studio.backtest.save('reports/_gui_smoke/run');
      check(r.files.length === 3 && !$('#bt-open').disabled, 'save did not finish');
      $('main').scrollTop = 0;
      return r.files;
    },
    async optimize_grid() {
      studio.showTab('optimization');
      const rows = [...document.querySelectorAll('#op-grid [data-name]')];
      check(rows.length > 0, 'no grid rows (strategy not loaded?)');
      const want = { noise_mult: '0.4:0.8:0.2', vwap_exit_bars: '1,2' };
      for (const row of rows) {
        const on = row.querySelector('input[type=checkbox]');
        const values = row.querySelector('input.values');
        on.checked = row.dataset.name in want;
        values.disabled = !on.checked;
        if (on.checked) values.value = want[row.dataset.name];
      }
      $('#op-topn').value = '4';
      $('#op-mintrades').value = '30';
      $('#op-workers').value = '';
      const r = await studio.optimize.count();
      check(r && r.count === 6, `expected 6 combinations, got ${r && r.count}`);
      return r;
    },
    async optimize_stop() {
      // R7: Stop ends the optimization process within 5 s and the main worker keeps the loaded data
      const running = studio.optimize.run().then(() => 'finished', (e) => (e.stopped ? 'stopped' : `failed: ${e.message}`));
      await until(() => /In sample: [1-9]/.test($('#op-progress-text').textContent), 'in-sample progress', 120000);
      const t0 = performance.now();
      await studio.optimize.stop();
      const outcome = await running;
      const ms = Math.round(performance.now() - t0);
      check(outcome === 'stopped', `run ${outcome}`);
      check(ms < 5000, `stop took ${ms} ms`);
      const page = await studio.api('data.page', { offset: 0, limit: 1 });
      check(page.total > 0, 'main worker lost its data');
      return { outcome, stop_ms: ms, main_worker_bars: page.total };
    },
    async optimize_run() {
      const r = await studio.optimize.run();
      for (const id of ['equity', 'hists', 'scatter']) await decoded($(`#op-${id}`));
      check($('#op-table tbody').rows.length === 4, 'top-4 table');
      return { combinations: r.combinations, ranked: r.ranked, seconds: r.seconds, best: r.table.inputs[0] };
    },
    async optimize_use() {
      studio.optimize.select(0);
      const want = studio.optimize.state.result.table.inputs[0];
      studio.optimize.useSelected();
      for (const [k, v] of Object.entries(want)) check($(`#st-in-${k}`).value === v, `Strategy tab ${k} = ${$(`#st-in-${k}`).value}, want ${v}`);
      return want;
    },
    async optimize_save() {
      studio.showTab('optimization');
      const r = await studio.optimize.save('reports/_gui_smoke/opt');
      check(r.files.length === 3, 'files');
      return r.files;
    },
    async worker_crash() {
      // R1: a dead worker shows the banner with its stderr tail and a Restart button
      const reply = await window.guban.call('debug.exit', { code: 3 });
      check(!reply.ok, 'debug.exit replied instead of ending the worker');
      const banner = $('#banner');
      await until(() => banner.textContent.includes('exit code 3'), 'the worker-stopped banner', 5000);
      return banner.querySelector('div').textContent;
    },
    async worker_restart() {
      const restart = [...document.querySelectorAll('#banner button')].find((b) => b.textContent === 'Restart');
      check(restart, 'no Restart button');
      restart.click();
      await until(() => $('#worker-status').textContent.includes('ready'), 'the restarted worker', 10000);
      return studio.api('ping');
    },
  };

  studio.smoke = {
    steps: () => Object.keys(STEPS),
    run: (i) => timed(STEPS[Object.keys(STEPS)[i]]),
  };
})();
