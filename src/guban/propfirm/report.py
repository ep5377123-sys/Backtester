"""Self-contained HTML page for a prop-firm Monte Carlo run (R10): outcome tiles, equity paths, days-to-pass and
max-drawdown distributions, degradation scenarios, the size sweep, and the trade/daily-pool statistics.
Same look and offline plotly.js as guban.report; every chart has a table (or tiles) with the same numbers.

The notebook's charts are redrawn with two changes: the outcome donut became tiles, and the degradation bars use
one color (the notebook's green-to-red ramp colored bars by their position, not their value).
"""

from __future__ import annotations

import html
import math
from pathlib import Path

import numpy as np

from guban.propfirm.sim import FAIL_DAILY, FAIL_DD, FAIL_TIME, PASS, PropFirmResult
from guban.report import _CSS, _header, _json, _notes, _plotly_js, fmt_value

DD_BINS = 60


def write_propfirm_report(res: PropFirmResult, path: str | Path, meta: dict) -> Path:
    """Write propfirm.html. meta: {"title", "subtitle", "details": {label: value}, "notes"}."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(meta.get('title', 'Prop-firm Monte Carlo'))}</title>",
        f"<style>{_CSS}{_EXTRA_CSS}</style>",
        f"<script>{_plotly_js()}</script>",
        "</head><body><main>",
        _header(meta),
        _tiles(res),
        _paths_html(res),
        "<div class='pair'>",
        _chart("Days to pass", "dtp", "Simulated trading days until the profit target was reached, passed evaluations only."),
        _chart("Max drawdown", "mdd", "Worst end-of-day drawdown in each evaluation, USD, all evaluations."),
        "</div>",
        _degradation_html(res),
        _sweep_html(res),
        _stats_html(res),
        _notes(meta),
        "</main>",
        f"<script>const D = {_json(_data(res))};</script><script>{_JS}</script>",
        "</body></html>",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# --- data ----------------------------------------------------------------------------------------


def _num(v, nd: int = 4):
    return None if v is None or not math.isfinite(v) else round(float(v), nd)


def _data(res: PropFirmResult) -> dict:
    r, m = res.setup.rules, res.main
    lines = {}
    for key, code in (("pass", PASS), ("dd", FAIL_DD), ("time", FAIL_TIME)):
        xs, ys = [], []
        for row in res.paths[res.path_outcomes == code]:
            for d, v in enumerate(row, start=1):
                if math.isfinite(v):
                    xs.append(d)
                    ys.append(round(float(v), 2))
            xs.append(None)
            ys.append(None)
        lines[key] = {"x": xs, "y": ys, "n": int((res.path_outcomes == code).sum())}
    dtp = m.days_to_pass
    counts = np.bincount(dtp, minlength=r.max_trading_days + 1)[1:] if len(dtp) else np.zeros(r.max_trading_days, int)
    hist, edges = np.histogram(m.max_dd, bins=DD_BINS)
    s = m.summary()
    best = res.best_size
    return {
        "target": r.profit_target, "limit": r.max_drawdown, "maxDays": r.max_trading_days, "nPaths": len(res.paths), "paths": lines,
        "dtp": counts.tolist(), "dtpMedian": s["median_days_to_pass"],
        "mdd": {"x0": edges[:-1].round(2).tolist(), "x1": edges[1:].round(2).tolist(), "n": hist.tolist(), "median": s["p50_max_dd"]},
        "degr": [{"label": sc.label, "pr": _num(sr.pass_rate), "lo": _num(sr.pass_rate_ci[0]), "hi": _num(sr.pass_rate_ci[1]),
                  "dd": _num(sr.rate(FAIL_DD))} for sc, sr in res.scenarios],
        "sweep": [{"size": sr.size, "label": _size_label(sr.size, res.stats["contracts"]), "pr": _num(sr.pass_rate),
                   "lo": _num(sr.pass_rate_ci[0]), "hi": _num(sr.pass_rate_ci[1]),
                   "days": _num(sr.summary()["avg_days_to_pass"], 2), "mdd": _num(sr.summary()["avg_max_dd"], 2)} for sr in res.sweep],
        "best": None if best is None else res.sweep.index(best),
    }  # fmt: skip


def _size_label(size: float, contracts: float | None) -> str:
    return f"{size:g}×" + (f" ({size * contracts:g})" if contracts is not None else "")


# --- html pieces ---------------------------------------------------------------------------------


def _pct(v) -> str:
    return fmt_value(v, "pct") if v is not None else "n/a"


def _tile(k: str, v: str, sub: str = "") -> str:
    return f"<div class='tile'><div class='k'>{html.escape(k)}</div><div class='v'>{v}</div>" + (f"<div class='s'>{html.escape(sub)}</div>" if sub else "") + "</div>"


def _tiles(res: PropFirmResult) -> str:
    m, s = res.main.summary(), res.summary()
    lo, hi = m["pass_rate_ci"]
    n = m["n_sims"]
    tiles = [
        _tile("Pass rate", _pct(m["pass_rate"]), f"95% CI {_pct(lo)} – {_pct(hi)}"),
        _tile("Fail: drawdown", _pct(m["fail_dd"] / n), f"{m['fail_dd']:,} of {n:,}"),
        _tile("Fail: time", _pct(m["fail_time"] / n), f"{m['fail_time']:,} of {n:,}"),
        _tile("Fail: daily limit", _pct(m["fail_daily"] / n), f"{m['fail_daily']:,} of {n:,}" if res.setup.rules.daily_loss_limit > 0 else "no daily limit"),
        _tile("Avg days to pass", fmt_value(m["avg_days_to_pass"], "num"), f"median {fmt_value(m['median_days_to_pass'], 'num')}"),
        _tile("Expected attempts", fmt_value(s["expected_attempts"], "num") if s["expected_attempts"] else "∞", "evaluations per pass"),
        _tile("Verdict", html.escape(s["verdict"]), s["verdict_text"]),
    ]
    return "<section class='tiles'>" + "".join(tiles) + "</section>"


def _chart(title: str, div: str, cap: str, cls: str = "") -> str:
    return f"<section><h2>{html.escape(title)}</h2><p class='cap'>{html.escape(cap)}</p><div id='{div}' class='chart {cls}'></div></section>"


def _paths_html(res: PropFirmResult) -> str:
    r = res.setup.rules
    cap = (f"{len(res.paths):,} simulated evaluations at size {res.setup.size:g} (seed + 5000), equity after each day, USD. "
           f"Dashed lines: the profit target ({fmt_value(r.profit_target, 'usd')}) and the drawdown limit measured from the start "
           f"(−{r.max_drawdown:,.0f}); the real limit trails the end-of-day high. A failed path stops on its failure day; a passed one stays flat.")  # fmt: skip
    return f"<section><h2>Equity paths</h2><p class='cap'>{html.escape(cap)}</p><div id='paths' class='chart tall'></div></section>"


def _rate_table(rows: list[list[str]], head: list[str]) -> str:
    h = "<tr>" + "".join(f"<th{' class=l' if i == 0 else ''}>{html.escape(x)}</th>" for i, x in enumerate(head)) + "</tr>"
    body = "".join("<tr>" + "".join(f"<td{' class=l' if i == 0 else ''}>{c}</td>" for i, c in enumerate(row)) + "</tr>" for row in rows)
    return f"<div class='wrap'><table>{h}{body}</table></div>"


def _degradation_html(res: PropFirmResult) -> str:
    if not res.scenarios:
        return ""
    rows = []
    for sc, sr in res.scenarios:
        s = sr.summary()
        rows.append([html.escape(sc.label), _pct(sc.win_rate_adj), _pct(sc.pnl_shrink), _pct(s["pass_rate"]),
                     f"{_pct(s['pass_rate_ci'][0])} – {_pct(s['pass_rate_ci'][1])}", _pct(sr.rate(FAIL_DD)), _pct(sr.rate(FAIL_TIME)),
                     fmt_value(s["avg_days_to_pass"], "num"), fmt_value(s["avg_max_dd"], "usd")])  # fmt: skip
    st = res.setup
    cap = (f"What if live trading is worse? {st.sweep_sims:,} simulations per scenario at size {st.size:g} (seed + 1000). PnL shrink scales "
           "every pool day; WR adjust turns that share of the pool's days (winning ones, picked at random) into the mean losing day. Bars: pass rate with its 95% CI.")  # fmt: skip
    return (
        f"<section><h2>Degradation scenarios</h2><p class='cap'>{html.escape(cap)}</p><div id='degr' class='chart short'></div>"
        + _rate_table(rows, ["Scenario", "WR adjust", "PnL shrink", "Pass rate", "95% CI", "Fail: drawdown", "Fail: time", "Avg days to pass", "Avg max drawdown"])
        + "</section>"
    )


def _sweep_html(res: PropFirmResult) -> str:
    if not res.sweep:
        return ""
    best = res.best_size
    c = res.stats["contracts"]
    rows = []
    for sr in res.sweep:
        s = sr.summary()
        mark = " <span class='badge'>best</span>" if sr is best else ""
        rows.append([f"{sr.size:g}×{mark}", "–" if c is None else f"{sr.size * c:g}", _pct(s["pass_rate"]),
                     f"{_pct(s['pass_rate_ci'][0])} – {_pct(s['pass_rate_ci'][1])}", _pct(sr.rate(FAIL_DD)), _pct(sr.rate(FAIL_TIME)),
                     fmt_value(s["avg_days_to_pass"], "num"), fmt_value(s["avg_max_dd"], "usd")])  # fmt: skip
    cap = (f"Pass rate by size: a multiplier on the trade file's daily P&L" + (f" (1× = {c:g} contracts per trade, as in the file)" if c is not None else "")
           + f". {res.setup.sweep_sims:,} simulations per size, all from the same seed (seed + 2000). Bars: pass rate with its 95% CI.")  # fmt: skip
    return (
        f"<section><h2>Size sweep</h2><p class='cap'>{html.escape(cap)}</p><div id='sweep' class='chart'></div>"
        + _rate_table(rows, ["Size", "Contracts", "Pass rate", "95% CI", "Fail: drawdown", "Fail: time", "Avg days to pass", "Avg max drawdown"])
        + "</section>"
    )


def _stats_html(res: PropFirmResult) -> str:
    t, d, m, s = res.stats, res.daily, res.main.summary(), res.summary()
    usd = lambda v: fmt_value(v, "usd")  # noqa: E731
    lo, hi = t["ev_usd_ci"]
    rlo, rhi = t["ev_r_ci"]
    trade_rows = [
        ("Trades", f"{t['trades']:,}"),
        ("First / last entry", f"{t['first']:%Y-%m-%d} / {t['last']:%Y-%m-%d}"),
        ("Win rate (file profit / after added costs)", f"{_pct(t['win_rate_gross'])} / {_pct(t['win_rate_net'])}"),
        ("Avg trade (file profit / after added costs)", f"{usd(t['avg_trade_gross'])} / {usd(t['avg_trade_net'])}"),
        ("Avg winner / loser (after added costs)", f"{usd(t['avg_winner'])} / {usd(t['avg_loser'])}"),
        ("EV per trade, R", f"{t['ev_r']:.4f}"),
        ("Bootstrap 95% CI of EV, USD", f"{usd(lo)} – {usd(hi)}"),
        ("Bootstrap 95% CI of EV, R", f"{rlo:.4f} – {rhi:.4f}"),
        ("P(EV ≤ 0)", f"{t['p_ev_usd_le_0']:.3f}"),
        ("Added costs, total", usd(t["costs_total"])),
    ]
    pool_rows = [
        ("Weekdays", f"{d['date'].min():%Y-%m-%d} → {d['date'].max():%Y-%m-%d}: {len(d):,}"),
        ("With trades", f"{int(d['is_trade_day'].sum()):,} ({_pct(float(d['is_trade_day'].mean()))})"),
        ("Avg trades per weekday", f"{d['n_trades'].mean():.2f}"),
        ("Avg / best / worst pool day", f"{usd(float(d['net_pnl_usd'].mean()))} / {usd(float(d['net_pnl_usd'].max()))} / {usd(float(d['net_pnl_usd'].min()))}"),
        ("Max drawdown avg / P90 / P95 / worst", f"{usd(m['avg_max_dd'])} / {usd(m['p90_max_dd'])} / {usd(m['p95_max_dd'])} / {usd(m['worst_max_dd'])}"),
        ("Avg equity at pass", usd(m["avg_equity_at_pass"])),
    ]
    cum = "".join(f"<tr><td class='l'>{n}</td><td>{_pct(p)}</td></tr>" for n, p in s["cumulative"].items())
    kv = lambda rows: "".join(f"<tr><td class='l'>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>" for k, v in rows)  # noqa: E731
    return (
        "<div class='pair'>"
        f"<section><h2>Trades</h2><p class='cap'>Notebook cell 4. Added costs: ${res.setup.commission + res.setup.slippage:,.2f} per contract per trade; "
        f"1R = ${res.setup.risk_per_contract:,.0f} per contract.</p><div class='wrap'><table class='kv'>{kv(trade_rows)}</table></div></section>"
        "<section><h2>Daily pool</h2><p class='cap'>Notebook cell 5: net P&amp;L by entry date on every weekday; the simulation draws from these days.</p>"
        f"<div class='wrap'><table class='kv'>{kv(pool_rows)}</table></div>"
        f"<h2 class='gap'>Passing within N attempts</h2><div class='wrap'><table class='kv'><tr><th class='l'>Attempts</th><th>P(pass ≥ 1)</th></tr>{cum}</table></div></section>"
        "</div>"
    )  # fmt: skip


_EXTRA_CSS = """
.tile .s { font-size:12px; color:var(--muted); }
.pair { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:0 16px; }
table.kv td.l { white-space:normal; }
h2.gap { margin-top:16px; }
.badge { font-size:11px; border:1px solid var(--axis); border-radius:999px; padding:0 6px; color:var(--ink2); margin-left:4px; }
"""

_JS = r"""
(function () {
  const css = () => { const s = getComputedStyle(document.documentElement); const v = n => s.getPropertyValue(n).trim();
    return { ink: v('--ink'), ink2: v('--ink2'), muted: v('--muted'), grid: v('--grid'), axis: v('--axis'), surface: v('--surface'),
             s1: v('--s1'), good: v('--good'), bad: v('--bad') }; };
  const pct = v => v === null ? 'n/a' : (v * 100).toFixed(1) + '%';
  const money = x => (x < 0 ? '−' : '') + Math.abs(x).toLocaleString('en-US', { maximumFractionDigits: 0 });
  const cfg = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d', 'toggleSpikelines'] };
  const layout = (c, extra) => Object.assign({
    paper_bgcolor: c.surface, plot_bgcolor: c.surface, font: { family: 'system-ui, -apple-system, Segoe UI, sans-serif', size: 12, color: c.ink2 },
    margin: { l: 64, r: 16, t: 8, b: 44 }, showlegend: false, hovermode: 'closest',
    hoverlabel: { bgcolor: c.surface, bordercolor: c.axis, font: { color: c.ink } },
    xaxis: { gridcolor: c.grid, linecolor: c.axis, zeroline: false },
    yaxis: { gridcolor: c.grid, linecolor: c.axis, zerolinecolor: c.axis },
  }, extra || {});
  const vline = (x, color) => ({ type: 'line', xref: 'x', yref: 'paper', x0: x, x1: x, y0: 0, y1: 1, line: { color, width: 2, dash: 'dash' } });
  const note = (x, text, c) => ({ x, y: 1, xref: 'x', yref: 'paper', yanchor: 'bottom', text, showarrow: false, font: { size: 11, color: c.ink2 } });

  function paths(c) {
    const P = D.paths, n = D.nPaths;
    const tr = (k, name, color, op) => ({ type: 'scatter', mode: 'lines', x: P[k].x, y: P[k].y, name: `${name} (${pct(P[k].n / n)})`,
      line: { color, width: 1 }, opacity: op, connectgaps: false, hovertemplate: `${name}<br>day %{x}<br>equity %{y:,.0f}<extra></extra>` });
    const traces = [tr('time', 'Time fail', c.muted, 0.35), tr('dd', 'Drawdown fail', c.bad, 0.3), tr('pass', 'Passed', c.good, 0.3)];
    const ref = (y, color, name) => ({ type: 'scatter', mode: 'lines', x: [1, D.maxDays], y: [y, y], name, line: { color, width: 2, dash: 'dash' }, hoverinfo: 'skip' });
    traces.push(ref(D.target, c.good, `Target ${money(D.target)}`), ref(-D.limit, c.bad, `Drawdown limit −${money(D.limit)}`));
    const L = layout(c, { showlegend: true, legend: { orientation: 'h', y: 1.02, yanchor: 'bottom', x: 0, font: { color: c.ink2 } }, margin: { l: 64, r: 16, t: 36, b: 44 } });
    L.xaxis = Object.assign(L.xaxis, { title: { text: 'Trading day' }, range: [1, D.maxDays], dtick: 1 });
    L.yaxis = Object.assign(L.yaxis, { title: { text: 'Equity, USD' }, tickformat: ',.0f' });
    Plotly.react('paths', traces, L, cfg);
  }

  function dtp(c) {
    const x = D.dtp.map((_, i) => i + 1);
    const L = layout(c, { bargap: 0.15, margin: { l: 56, r: 16, t: 22, b: 44 } });
    L.xaxis = Object.assign(L.xaxis, { title: { text: 'Trading days to pass' }, dtick: 2 });
    L.yaxis = Object.assign(L.yaxis, { title: { text: 'Evaluations' }, tickformat: ',d' });
    if (D.dtpMedian !== null) { L.shapes = [vline(D.dtpMedian, c.ink2)]; L.annotations = [note(D.dtpMedian, `median ${D.dtpMedian}`, c)]; }
    Plotly.react('dtp', [{ type: 'bar', x, y: D.dtp, marker: { color: c.s1 }, hovertemplate: 'day %{x}: %{y:,} passes<extra></extra>' }], L, cfg);
  }

  function mdd(c) {
    const M = D.mdd, x = M.x0.map((a, i) => (a + M.x1[i]) / 2), w = M.x0.map((a, i) => M.x1[i] - a);
    const text = M.x0.map((a, i) => `${money(a)} – ${money(M.x1[i])}`);
    const L = layout(c, { bargap: 0.1, margin: { l: 56, r: 16, t: 22, b: 44 } });
    L.xaxis = Object.assign(L.xaxis, { title: { text: 'Max drawdown, USD' }, tickformat: ',.0f' });
    L.yaxis = Object.assign(L.yaxis, { title: { text: 'Evaluations' }, tickformat: ',d' });
    L.shapes = [vline(D.limit, c.bad), vline(M.median, c.ink2)];
    L.annotations = [note(D.limit, `limit ${money(D.limit)}`, c), note(M.median, `median ${money(M.median)}`, c)];
    L.annotations[0].xanchor = M.median <= D.limit ? 'left' : 'right';
    L.annotations[1].xanchor = M.median <= D.limit ? 'right' : 'left';
    Plotly.react('mdd', [{ type: 'bar', x, y: M.n, width: w, text, textposition: 'none', marker: { color: c.s1 },
      hovertemplate: '%{text}: %{y:,}<extra></extra>' }], L, cfg);
  }

  function rateBars(id, rows, cat, horizontal, c, best) {
    const pr = rows.map(r => r.pr), plus = rows.map(r => r.hi - r.pr), minus = rows.map(r => r.pr - r.lo);
    const text = rows.map(r => `${r.label}<br>pass ${pct(r.pr)} (95% CI ${pct(r.lo)} – ${pct(r.hi)})` +
      (r.days !== undefined ? `<br>avg days to pass ${r.days === null ? 'n/a' : r.days}<br>avg max drawdown ${r.mdd === null ? 'n/a' : money(r.mdd)}` : `<br>drawdown fails ${pct(r.dd)}`));
    const err = { type: 'data', array: plus, arrayminus: minus, color: c.ink2, thickness: 1.2, width: 4 };
    const colors = rows.map((_, i) => best === null || best === undefined || i === best ? c.s1 : c.s1 + '99');
    const t = { type: 'bar', text, textposition: 'none', hovertemplate: '%{text}<extra></extra>', marker: { color: colors } };
    const L = layout(c);
    if (horizontal) {
      Object.assign(t, { orientation: 'h', y: cat, x: pr, error_x: err });
      L.margin = { l: 200, r: 24, t: 8, b: 44 };
      L.xaxis = Object.assign(L.xaxis, { title: { text: 'Pass rate' }, tickformat: '.0%', rangemode: 'tozero' });
      L.yaxis = Object.assign(L.yaxis, { type: 'category', autorange: 'reversed', gridcolor: 'rgba(0,0,0,0)' });
    } else {
      Object.assign(t, { x: cat, y: pr, error_y: err });
      L.xaxis = Object.assign(L.xaxis, { type: 'category', title: { text: 'Size (contracts)' } });
      L.yaxis = Object.assign(L.yaxis, { title: { text: 'Pass rate' }, tickformat: '.0%', rangemode: 'tozero' });
      if (best !== null) L.annotations = [{ x: cat[best], y: rows[best].hi, yanchor: 'bottom', text: 'best', showarrow: false, font: { size: 11, color: c.ink2 } }];
    }
    Plotly.react(id, [t], L, cfg);
  }

  function drawAll() {
    const c = css();
    paths(c); dtp(c); mdd(c);
    if (D.degr.length) rateBars('degr', D.degr, D.degr.map(r => r.label), true, c, null);
    if (D.sweep.length) rateBars('sweep', D.sweep, D.sweep.map(r => r.label), false, c, D.best);
  }
  drawAll();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', drawAll);
  new MutationObserver(drawAll).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
"""
