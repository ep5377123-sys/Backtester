"""Self-contained HTML backtest report (R6): summary metrics, equity curve, drawdown, a trade Monte Carlo (Phase 11,
guban.montecarlo), monthly net profit, a price chart with trades, and the trade list. plotly.js is inlined, so the
file opens offline.

Charts are drawn in the browser from compact JSON:
- equity and drawdown: one point per session (equity at the session's last bar; the session's worst intraday
  drawdown, see guban.metrics.intraday_drawdown).
- price chart: a trade viewer. Each trade is shown on the strategy bars around it, `CONTEXT_BARS` either side;
  trades close together share one window. Windows longer than `MAX_CANDLES` bars are merged k bars per candle,
  so a report stays small (a few MB beyond plotly.js) whatever the history length.
"""

from __future__ import annotations

import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from guban.metrics import ACCOUNT_STATS, TRADE_STATS, daily_equity, intraday_drawdown, session_keys

CONTEXT_BARS = 24
WINDOW_BARS = 600  # trades are grouped into one window while it stays within this many bars
MAX_CANDLES = 1500
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def write_report(
    result,
    bars: pd.DataFrame,
    path: str | Path,
    metrics: dict,
    meta: dict,
    sections: list[str] | None = None,
    bands: list[dict] | None = None,
    monte_carlo=None,
) -> Path:
    """Write report.html for a BacktestResult run on `bars` (the frame passed to run_backtest).

    meta: {"title", "subtitle", "details": {label: value}, "inputs": {name: value}, "notes": [str]}.
    result.equity may cover only some of the bars (a stitched walk-forward curve); trade bar numbers index `bars`.
    sections: extra HTML sections placed after the header. bands: [{"x0", "x1", "xm": "YYYY-MM-DD", "label"}]
    periods marked on the equity and drawdown charts (every other one shaded; x1 exclusive).
    monte_carlo: a guban.montecarlo.MonteCarloResult (a section after the drawdown chart), {"skipped": reason}, or None.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tr = result.trades
    sessions = bars["session_date"].reindex(result.equity.index) if "session_date" in bars.columns else None
    daily = daily_equity(result.equity, sessions)
    dd = intraday_drawdown(result.equity, result.equity_low, result.equity_high, result.bar_drawdown)
    daily_dd = dd.groupby(session_keys(dd.index, sessions)).min()
    data = {
        "tick": result.spec.tick_size if result.spec else 0.01,
        "days": [d.strftime("%Y-%m-%d") for d in daily.index],
        "equity": _r(daily.to_numpy(), 2),
        "dd": _r(daily_dd.to_numpy(), 2),
        "bands": bands or [],
        "mc": monte_carlo.report_data() if monte_carlo is not None and not isinstance(monte_carlo, dict) else None,
        **_trade_windows(tr, bars, result.spec.tick_size if result.spec else 0.01),
    }
    band_cap = " Labels and alternate shading mark the out-of-sample windows." if bands else ""
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(meta.get('title', 'Backtest report'))}</title>",
        f"<style>{_CSS}</style>",
        f"<script>{_plotly_js()}</script>",
        "</head><body><main>",
        _header(meta),
        *(sections or []),
        _tiles(metrics),
        f"<section><h2>Equity</h2><p class='cap'>Closed net profit plus open profit at each session's last bar, USD.{band_cap}</p>"
        "<div id='equity' class='chart'></div></section>",
        "<section><h2>Drawdown</h2><p class='cap'>Worst intraday drawdown each session, from the running equity peak (tracked along the intrabar path), USD.</p>"
        "<div id='drawdown' class='chart short'></div></section>",
        _monte_carlo_html(monte_carlo),
        _summary_table(metrics),
        _monthly_table(tr),
        _viewer_html(len(tr)),
        _trades_html(len(tr)),
        _notes(meta),
        "</main>",
        f"<script>const D = {_json(data)};</script>",
        f"<script>{_JS}</script>",
        "</body></html>",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


# --- data ----------------------------------------------------------------------------------------


def _r(a, nd: int) -> list:
    return [None if not np.isfinite(v) else round(float(v), nd) for v in np.asarray(a, float)]


def _trade_windows(tr: pd.DataFrame, bars: pd.DataFrame, tick: float) -> dict:
    """Group trades into bar windows and encode each window's candles compactly.

    Window: t0 (bar close time, minutes since 1970 in chart wall-clock time), dt (minutes from t0 per candle),
    base (ticks), o/h/l/c (ticks from base). Trades: [no, dir, qty, entry time, entry name, entry price,
    exit time, exit name, exit price, net, cum net, run-up, drawdown, ambiguous, window, entry candle, exit candle].
    """
    n = len(bars)
    wall = bars.index.tz_localize(None) if bars.index.tz is not None else bars.index
    minutes = (wall.as_unit("s").asi8 // 60).astype(np.int64)
    ticks = {k: np.rint(bars[k].to_numpy(float) / tick).astype(np.int64) for k in ("open", "high", "low", "close")}
    groups: list[list[int]] = []  # [start, end, first trade, last trade]
    for k, (eb, xb) in enumerate(zip(tr["entry_bar"].to_numpy(int), tr["exit_bar"].to_numpy(int))):
        s, e = max(0, eb - CONTEXT_BARS), min(n - 1, xb + CONTEXT_BARS)
        g = groups[-1] if groups else None
        if g and s <= g[1] and max(e, g[1]) - g[0] < WINDOW_BARS:
            g[1], g[3] = max(e, g[1]), k
        else:
            groups.append([s, e, k, k])
    windows, trade_rows = [], []
    fmt = lambda t: t.strftime("%Y-%m-%d %H:%M")  # noqa: E731
    for w, (s, e, k0, k1) in enumerate(groups):
        m = e - s + 1
        step = max(1, math.ceil(m / MAX_CANDLES))
        idx = np.arange(s, e + 1)
        grp = (idx - s) // step
        cuts = np.flatnonzero(np.r_[True, np.diff(grp) != 0])
        last = np.r_[cuts[1:], len(idx)] - 1
        o = ticks["open"][s:e + 1][cuts]
        h = np.maximum.reduceat(ticks["high"][s:e + 1], cuts)
        lo = np.minimum.reduceat(ticks["low"][s:e + 1], cuts)
        c = ticks["close"][s:e + 1][last]
        t = minutes[s:e + 1][last]
        base = int(o[0])
        windows.append({
            "t0": int(t[0]), "dt": (t - t[0]).tolist(), "base": base, "step": step,
            "o": (o - base).tolist(), "h": (h - base).tolist(), "l": (lo - base).tolist(), "c": (c - base).tolist(),
        })  # fmt: skip
        for k in range(k0, k1 + 1):
            r = tr.iloc[k]
            trade_rows.append([
                int(r.trade_no), 1 if r.direction == "long" else -1, int(r.qty),
                fmt(r.entry_time), str(r.entry_name), float(r.entry_price),
                fmt(r.exit_time), str(r.exit_name), float(r.exit_price),
                round(float(r.net_pnl), 2), round(float(r.cum_net_pnl), 2),
                round(float(r.runup), 2), round(float(r.drawdown), 2), int(bool(r.ambiguous)),
                w, int((r.entry_bar - s) // step), int((r.exit_bar - s) // step),
            ])  # fmt: skip
    return {"windows": windows, "trades": trade_rows}


def _json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")


def _plotly_js() -> str:
    from plotly.offline import get_plotlyjs

    return get_plotlyjs()


# --- html pieces ---------------------------------------------------------------------------------


def fmt_value(v, kind: str) -> str:
    if v is None:
        return "n/a"
    if kind == "time":
        return pd.Timestamp(v).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, float) and math.isinf(v):
        return "∞"
    if kind == "usd":
        return f"{v:,.2f}".replace("-", "−")
    if kind == "pct":
        return f"{v:.1%}".replace("-", "−")
    if kind == "ratio":
        return f"{v:.2f}".replace("-", "−")
    if kind == "num":
        return f"{v:,.1f}"
    return f"{v:,}"


def _header(meta: dict) -> str:
    rows = "".join(f"<div><dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd></div>" for k, v in meta.get("details", {}).items())
    inputs = meta.get("inputs") or {}
    inp = "".join(f"<span class='chip'>{html.escape(k)} = {html.escape(repr(v))}</span>" for k, v in inputs.items())
    return (
        f"<header><h1>{html.escape(meta.get('title', 'Backtest report'))}</h1>"
        f"<p class='sub'>{html.escape(meta.get('subtitle', ''))}</p>"
        f"<dl class='meta'>{rows}</dl>" + (f"<div class='chips'>{inp}</div>" if inp else "") + "</header>"
    )


def _tiles(m: dict) -> str:
    a, acc = m["all"], m["account"]
    tiles = [
        ("Net profit", fmt_value(a["net_profit"], "usd")),
        ("Profit factor", fmt_value(a["profit_factor"], "ratio")),
        ("Trades", fmt_value(a["total_trades"], "int")),
        ("Percent profitable", fmt_value(a["percent_profitable"], "pct")),
        ("Max drawdown", fmt_value(acc["max_drawdown_intraday"], "usd")),
        ("Return on account", fmt_value(acc["return_on_account"], "pct")),
        ("Sharpe (daily)", fmt_value(acc["sharpe_daily"], "ratio")),
    ]
    return "<section class='tiles'>" + "".join(f"<div class='tile'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in tiles) + "</section>"


def _summary_table(m: dict) -> str:
    head = "<tr><th class='l'></th><th>All trades</th><th>Long trades</th><th>Short trades</th></tr>"
    rows = [
        f"<tr><td class='l'>{html.escape(label)}</td>" + "".join(f"<td>{fmt_value(m[side][key], kind)}</td>" for side in ("all", "long", "short")) + "</tr>"
        for key, label, kind in TRADE_STATS
    ]
    acc = [
        f"<tr><td class='l'>{html.escape(label)}</td><td>{fmt_value(m['account'][key], kind)}</td><td></td><td></td></tr>"
        for key, label, kind in ACCOUNT_STATS
    ]
    return (
        "<section><h2>Performance summary</h2><div class='wrap'><table class='summary'>"
        + head + "".join(rows) + "<tr class='sep'><td colspan='4'></td></tr>" + "".join(acc)
        + "</table></div></section>"
    )  # fmt: skip


def _monte_carlo_html(mc) -> str:
    if mc is None:
        return ""
    if isinstance(mc, dict):
        return f"<section id='monte-carlo'><h2>Monte Carlo</h2><p class='muted'>Monte Carlo skipped: {html.escape(mc['skipped'])}.</p></section>"
    rs, ro = mc.method_summary("resample"), mc.method_summary("reorder")
    usd = lambda v: fmt_value(v, "usd")  # noqa: E731
    pct = lambda v: fmt_value(v, "pct")  # noqa: E731

    def stats(items: list[tuple[str, str]]) -> str:
        return "<div class='mc-stats'>" + "".join(f"<div><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in items) + "</div>"

    limit = [(f"Max DD reaches {usd(mc.dd_limit)}", None)] if mc.dd_limit is not None else []
    cols = {
        "resample": ("Resample", f"Each run draws {mc.trades:,} trades from the list at random, with replacement: net profit and drawdown both vary.",
                     [("Net profit ≤ 0", pct(rs["p_net_le_0"])), ("Max DD deeper than backtest", pct(rs["p_dd_worse_than_backtest"]))]
                     + [(k, pct(rs["p_dd_reaches_limit"])) for k, _ in limit]),
        "reorder": ("Reorder", f"Each run shuffles the same {mc.trades:,} trades: net profit is the backtest's in every run; only the path varies.",
                    [("Net profit (every run)", usd(mc.net)), ("Max DD deeper than backtest", pct(ro["p_dd_worse_than_backtest"]))]
                    + [(k, pct(ro["p_dd_reaches_limit"])) for k, _ in limit]),
    }  # fmt: skip
    col_html = []
    for name, (title, cap, items) in cols.items():
        net_chart = (f"<p class='cap mc-label'>Net profit per run (dashed: backtest)</p><div id='mc-net-{name}' class='chart short'></div>"
                     if name == "resample" else "")  # fmt: skip
        col_html.append(
            f"<div class='mc-col'><h3>{title}</h3><p class='cap'>{cap}</p>{stats(items)}"
            f"<p class='cap mc-label'>Cumulative net profit by trade: 5th–95th and 25th–75th percentile bands, median, backtest</p>"
            f"<div id='mc-band-{name}' class='chart'></div>"
            f"<p class='cap mc-label'>Max drawdown per run (dashed: backtest)</p><div id='mc-dd-{name}' class='chart short'></div>{net_chart}</div>"
        )
    heads = "".join(f"<th>{'Median' if p == 50 else f'{p}th'}</th>" for p in (5, 25, 50, 75, 95))
    rows = [("Resample · net profit", rs["net_profit"], mc.net), ("Resample · max drawdown", rs["max_drawdown"], mc.max_dd),
            ("Reorder · max drawdown", ro["max_drawdown"], mc.max_dd)]  # fmt: skip
    body = "".join(
        f"<tr><td class='l'>{label}</td>" + "".join(f"<td>{usd(v)}</td>" for v in vals.values()) + f"<td class='b'>{usd(bt)}</td></tr>"
        for label, vals, bt in rows
    )
    return (
        "<section id='monte-carlo'><h2>Monte Carlo</h2>"
        f"<p class='cap'>{mc.sims:,} runs per method on this backtest's {mc.trades:,} trades (net of costs), seed {mc.seed}. "
        f"Drawdowns here are close-to-close (closed-trade equity from 0): compare them with the backtest's close-to-close max drawdown, "
        f"{usd(mc.max_dd)}, not the intraday figure in the tiles above. Trades are drawn one at a time, so trades that tend to win or lose "
        "together (for example on the same day) are treated as independent, which can make drawdowns look milder than they are.</p>"
        f"<div class='mc-grid'>{''.join(col_html)}</div>"
        f"<div class='wrap'><table class='mc-table'><tr><th class='l'>Percentile of runs</th>{heads}<th>Backtest</th></tr>{body}</table></div>"
        "<p class='cap'>Drawdowns are negative, so the 5th percentile is the deepest 5% of runs.</p></section>"
    )


def _monthly_table(tr: pd.DataFrame) -> str:
    if tr.empty:
        return ""
    t = tr["exit_time"]
    wall = t.dt.tz_localize(None) if t.dt.tz is not None else t
    pnl = tr.groupby([wall.dt.year, wall.dt.month])["net_pnl"].sum()
    head = "<tr><th class='l'>Year</th>" + "".join(f"<th>{m}</th>" for m in MONTHS) + "<th>Year</th></tr>"
    rows = []
    for y in sorted({k[0] for k in pnl.index}):
        cells = [f"<td>{fmt_value(float(pnl[(y, mo)]), 'usd')}</td>" if (y, mo) in pnl.index else "<td class='muted'>–</td>" for mo in range(1, 13)]
        total = float(pnl.loc[y].sum())
        rows.append(f"<tr><td class='l'>{y}</td>{''.join(cells)}<td class='b'>{fmt_value(total, 'usd')}</td></tr>")
    return (
        "<section><h2>Net profit by month</h2><p class='cap'>By exit month (chart time), USD.</p>"
        f"<div class='wrap'><table class='monthly'>{head}{''.join(rows)}</table></div></section>"
    )


def _viewer_html(n: int) -> str:
    if not n:
        return "<section><h2>Price chart with trades</h2><p class='muted'>No trades.</p></section>"
    return (
        "<section id='viewer-sec'><h2>Price chart with trades</h2>"
        "<div class='controls'><button id='prev' type='button' aria-label='Previous trade'>◀</button>"
        f"<label>Trade <input id='tno' type='number' min='1' max='{n}' value='1'></label> <span class='muted'>of {n:,}</span>"
        "<button id='next' type='button' aria-label='Next trade'>▶</button><span id='tinfo' class='tinfo'></span></div>"
        "<div id='viewer' class='chart tall'></div>"
        "<p class='cap'>▲ long entry · ▼ short entry · ● exit (outlined: loss) · the selected trade is highlighted. "
        "← / → step through trades. Gaps between sessions are closed up.</p></section>"
    )


def _trades_html(n: int) -> str:
    if not n:
        return ""
    return (
        "<section><h2>List of trades</h2>"
        "<div class='controls'><label>Show <select id='tfilter'><option value='all'>All</option><option value='win'>Winners</option>"
        "<option value='loss'>Losers</option><option value='long'>Long</option><option value='short'>Short</option>"
        "<option value='amb'>Ambiguous fills</option></select></label>"
        "<span id='tpage' class='muted'></span><button id='tprev' type='button'>Previous</button><button id='tnext' type='button'>Next</button></div>"
        "<div class='wrap'><table id='tlist' class='trades'></table></div>"
        "<p class='cap'>Click a trade to show it on the price chart. Run-up and drawdown are after entry-side costs, as MC reports them.</p></section>"
    )


def _notes(meta: dict) -> str:
    notes = meta.get("notes") or []
    if not notes:
        return ""
    return "<section><h2>Notes</h2><ul class='notes'>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul></section>"


_CSS = """
:root { color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --ring:rgba(11,11,11,0.10); --s1:#2a78d6; --up:#2a78d6; --down:#e34948; --good:#0ca30c; --bad:#d03b3b; --hl:rgba(42,120,214,0.10); }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --ring:rgba(255,255,255,0.10); --s1:#3987e5; --up:#3987e5; --down:#e66767; --good:#0ca30c; --bad:#d03b3b; --hl:rgba(57,135,229,0.18); } }
:root[data-theme="dark"] { color-scheme: dark;
  --page:#0d0d0d; --surface:#1a1a19; --ink:#ffffff; --ink2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --ring:rgba(255,255,255,0.10); --s1:#3987e5; --up:#3987e5; --down:#e66767; --good:#0ca30c; --bad:#d03b3b; --hl:rgba(57,135,229,0.18); }
* { box-sizing:border-box; }
body { margin:0; background:var(--page); color:var(--ink); font:14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width:1200px; margin:0 auto; padding:24px 16px 48px; }
h1 { font-size:22px; margin:0 0 2px; } h2 { font-size:16px; margin:0 0 4px; }
.sub, .cap, .muted { color:var(--ink2); } .cap { font-size:12px; margin:0 0 8px; } .muted { color:var(--muted); }
section, header { background:var(--surface); border:1px solid var(--ring); border-radius:10px; padding:16px; margin:0 0 16px; }
.meta { display:grid; grid-template-columns:repeat(auto-fill, minmax(190px, 1fr)); gap:6px 16px; margin:12px 0 0; }
.meta dt { font-size:12px; color:var(--muted); } .meta dd { margin:0; }
.chips { margin-top:10px; display:flex; flex-wrap:wrap; gap:6px; }
.chip { font:12px ui-monospace, Consolas, monospace; border:1px solid var(--ring); border-radius:999px; padding:1px 8px; color:var(--ink2); }
.tiles { display:grid; grid-template-columns:repeat(auto-fit, minmax(140px, 1fr)); gap:12px; }
.tile .k { font-size:12px; color:var(--ink2); } .tile .v { font-size:22px; font-weight:600; }
.chart { width:100%; height:320px; } .chart.short { height:220px; } .chart.tall { height:460px; }
.wrap { overflow-x:auto; }
table { border-collapse:collapse; font-variant-numeric:tabular-nums; font-size:13px; width:100%; }
th, td { padding:4px 8px; text-align:right; white-space:nowrap; border-bottom:1px solid var(--grid); }
th { color:var(--ink2); font-weight:600; } .l { text-align:left; } td.b { font-weight:600; }
tr.sep td { border-bottom:1px solid var(--axis); padding:2px; }
table.trades tbody tr { cursor:pointer; } table.trades tbody tr:hover { background:var(--hl); }
table.trades tbody tr.sel { background:var(--hl); box-shadow:inset 3px 0 0 var(--s1); }
.controls { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin:8px 0; }
button, input, select { font:inherit; color:var(--ink); background:var(--page); border:1px solid var(--axis); border-radius:6px; padding:3px 10px; }
button { cursor:pointer; } button:hover { border-color:var(--ink2); } input[type=number] { width:90px; }
.tinfo { color:var(--ink2); font-size:13px; }
.notes { margin:0; padding-left:18px; color:var(--ink2); }
h3 { font-size:14px; margin:0 0 2px; }
.mc-grid { display:grid; grid-template-columns:repeat(2, minmax(0, 1fr)); gap:24px; margin:12px 0; }
@media (max-width:800px) { .mc-grid { grid-template-columns:minmax(0, 1fr); } }
.mc-stats { display:flex; flex-wrap:wrap; gap:8px 24px; margin:8px 0 12px; }
.mc-stats .k { font-size:12px; color:var(--ink2); } .mc-stats .v { font-size:18px; font-weight:600; }
.mc-label { margin:8px 0 0; }
"""

_JS = r"""
(function () {
  const css = () => { const s = getComputedStyle(document.documentElement); const v = n => s.getPropertyValue(n).trim();
    return { ink: v('--ink'), ink2: v('--ink2'), muted: v('--muted'), grid: v('--grid'), axis: v('--axis'), surface: v('--surface'),
             s1: v('--s1'), up: v('--up'), down: v('--down'), good: v('--good'), bad: v('--bad') }; };
  const money = x => (x < 0 ? '−' : '') + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const cfg = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d', 'toggleSpikelines'] };
  const layout = (c, extra) => Object.assign({
    paper_bgcolor: c.surface, plot_bgcolor: c.surface, font: { family: 'system-ui, -apple-system, Segoe UI, sans-serif', size: 12, color: c.ink2 },
    margin: { l: 64, r: 16, t: 8, b: 36 }, showlegend: false, hovermode: 'x',
    hoverlabel: { bgcolor: c.surface, bordercolor: c.axis, font: { color: c.ink } },
    xaxis: { gridcolor: c.grid, linecolor: c.axis, zeroline: false, showspikes: true, spikemode: 'across', spikethickness: 1, spikecolor: c.muted, spikedash: 'solid' },
    yaxis: { gridcolor: c.grid, linecolor: c.axis, zerolinecolor: c.axis, tickformat: ',.0f' },
  }, extra || {});

  function drawCurves() {
    const c = css(), B = D.bands;
    const shapes = B.filter((_, i) => i % 2 === 0).map(b => ({ type: 'rect', xref: 'x', yref: 'paper', x0: b.x0, x1: b.x1, y0: 0, y1: 1,
      fillcolor: c.s1, opacity: 0.08, line: { width: 0 }, layer: 'below' }));
    const labels = B.map(b => ({ x: b.xm, y: 1, xref: 'x', yref: 'paper', yanchor: 'bottom', text: b.label, showarrow: false, font: { size: 10, color: c.muted } }));
    Plotly.react('equity', [{ x: D.days, y: D.equity, type: 'scatter', mode: 'lines', line: { color: c.s1, width: 2 },
      hovertemplate: '%{x}<br>Equity %{y:,.2f}<extra></extra>' }], layout(c, B.length ? { shapes, annotations: labels, margin: { l: 64, r: 16, t: 22, b: 36 } } : {}), cfg);
    Plotly.react('drawdown', [{ x: D.days, y: D.dd, type: 'scatter', mode: 'lines', fill: 'tozeroy', line: { color: c.bad, width: 2 },
      fillcolor: c.bad + '33', hovertemplate: '%{x}<br>Drawdown %{y:,.2f}<extra></extra>' }], layout(c, { shapes }), cfg);
  }

  // --- Monte Carlo ---
  function drawHist(id, h, color, actual, label, limit) {
    const c = css(), e = h.edges, xs = h.counts.map((_, i) => (e[i] + e[i + 1]) / 2);
    const vline = (x, dash, color) => ({ type: 'line', xref: 'x', yref: 'paper', x0: x, x1: x, y0: 0, y1: 1, line: { color, width: 1.5, dash } });
    const note = (x, text) => ({ x, y: 1, xref: 'x', yref: 'paper', yanchor: 'bottom', text, showarrow: false, font: { size: 10, color: c.ink2 } });
    const shapes = [vline(actual, 'dash', c.ink)], annotations = [note(actual, 'Backtest')];
    if (limit != null) { shapes.push(vline(-limit, 'dot', c.muted)); annotations.push(note(-limit, 'Limit')); }
    Plotly.react(id, [{ type: 'bar', x: xs, y: h.counts, width: e[1] - e[0], marker: { color, line: { width: 0 } }, opacity: 0.85,
      customdata: h.counts.map((_, i) => [e[i], e[i + 1]]),
      hovertemplate: `${label} %{customdata[0]:,.0f} to %{customdata[1]:,.0f}<br>%{y:,} runs<extra></extra>` }],
      layout(c, { shapes, annotations, bargap: 0, hovermode: 'closest', margin: { l: 56, r: 16, t: 22, b: 36 } }), cfg);
  }
  function drawMC() {
    const M = D.mc; if (!M) return;
    const c = css(), x = M.x;
    for (const name of ['resample', 'reorder']) {
      const m = M[name], b = m.bands;
      const band = (lo, hi, alpha) => [
        { x, y: lo, type: 'scatter', mode: 'lines', line: { width: 0 }, hoverinfo: 'skip' },
        { x, y: hi, type: 'scatter', mode: 'lines', line: { width: 0 }, fill: 'tonexty', fillcolor: c.s1 + alpha, hoverinfo: 'skip' }];
      const q = x.map((_, j) => [b[0][j], b[1][j], b[3][j], b[4][j]]);
      Plotly.react('mc-band-' + name, [...band(b[0], b[4], '26'), ...band(b[1], b[3], '4d'),
        { x, y: b[2], type: 'scatter', mode: 'lines', line: { color: c.s1, width: 2 }, customdata: q,
          hovertemplate: 'After %{x:,} trades<br>95th %{customdata[3]:,.2f}<br>75th %{customdata[2]:,.2f}<br>Median %{y:,.2f}' +
            '<br>25th %{customdata[1]:,.2f}<br>5th %{customdata[0]:,.2f}<extra></extra>' },
        { x, y: M.path, type: 'scatter', mode: 'lines', line: { color: c.ink, width: 1.5 }, hovertemplate: 'Backtest %{y:,.2f}<extra></extra>' }],
        layout(c, { margin: { l: 56, r: 16, t: 8, b: 36 } }), cfg);
      drawHist('mc-dd-' + name, m.dd_hist, c.bad, M.dd, 'Max drawdown', M.limit);
      if (m.net_hist) drawHist('mc-net-' + name, m.net_hist, c.s1, M.net, 'Net profit', null);
    }
  }

  // --- trade viewer ---
  const T = D.trades, byNo = new Map(T.map((t, i) => [t[0], i]));
  let cur = 0;
  const pad = n => String(n).padStart(2, '0');
  const label = m => { const d = new Date(m * 60000); return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())} ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`; };
  function drawViewer() {
    if (!T.length) return;
    const c = css(), t = T[cur], w = D.windows[t[14]], tk = D.tick;
    const x = w.dt.map(m => label(w.t0 + m)), px = a => a.map(v => (w.base + v) * tk);
    const traces = [{ type: 'candlestick', x, open: px(w.o), high: px(w.h), low: px(w.l), close: px(w.c), hoverinfo: 'x+y',
      increasing: { line: { color: c.up, width: 1 }, fillcolor: c.up }, decreasing: { line: { color: c.down, width: 1 }, fillcolor: c.down } }];
    const inWin = T.filter(r => r[14] === t[14]);
    for (const r of inWin) {
      const sel = r === t, op = sel ? 1 : 0.45, win = r[9] >= 0;
      const tip = `#${r[0]} ${r[1] > 0 ? 'long' : 'short'} ${r[2]}<br>in  ${r[3]} ${r[4]} @ ${r[5]}<br>out ${r[6]} ${r[7]} @ ${r[8]}<br>net ${money(r[9])}`;
      traces.push({ type: 'scatter', mode: 'lines', x: [x[r[15]], x[r[16]]], y: [r[5], r[8]], line: { color: c.ink2, width: sel ? 2 : 1 }, opacity: op, hoverinfo: 'skip' });
      traces.push({ type: 'scatter', mode: 'markers', x: [x[r[15]]], y: [r[5]], opacity: op, hovertemplate: tip + '<extra></extra>',
        marker: { symbol: r[1] > 0 ? 'triangle-up' : 'triangle-down', size: sel ? 14 : 10, color: c.ink, line: { color: c.surface, width: 2 } } });
      traces.push({ type: 'scatter', mode: 'markers', x: [x[r[16]]], y: [r[8]], opacity: op, hovertemplate: tip + '<extra></extra>',
        marker: { symbol: win ? 'circle' : 'circle-open', size: sel ? 13 : 9, color: win ? c.good : c.bad, line: { color: win ? c.surface : c.bad, width: 2 } } });
    }
    const lo = Math.max(0, t[15] - 60), hi = Math.min(x.length - 1, t[16] + 60);
    const L = layout(c, { hovermode: 'closest', margin: { l: 72, r: 16, t: 8, b: 56 } });
    L.xaxis = Object.assign(L.xaxis, { type: 'category', rangeslider: { visible: false }, nticks: 8, range: [lo - 0.5, hi + 0.5] });
    L.yaxis.tickformat = ',.2f';
    Plotly.react('viewer', traces, L, cfg);
    document.getElementById('tno').value = t[0];
    document.getElementById('tinfo').textContent =
      `${t[1] > 0 ? 'Long' : 'Short'} ${t[2]} · ${t[3]} → ${t[6]} · ${t[7]} · net ${money(t[9])}` + (w.step > 1 ? ` · ${w.step} bars per candle` : '') + (t[13] ? ' · ambiguous fill' : '');
    markRow();
  }
  function go(i) { cur = Math.max(0, Math.min(T.length - 1, i)); drawViewer(); }

  // --- trade list ---
  const PAGE = 50; let rows = T.map((_, i) => i), page = 0;
  const heads = ['#', 'Dir', 'Qty', 'Entry time', 'Entry signal', 'Entry price', 'Exit time', 'Exit signal', 'Exit price', 'Net profit', 'Cum. net', 'Run-up', 'Drawdown', 'Ambig.'];
  function drawTable() {
    const tb = document.getElementById('tlist'); if (!tb) return;
    const pages = Math.max(1, Math.ceil(rows.length / PAGE)); page = Math.max(0, Math.min(page, pages - 1));
    const part = rows.slice(page * PAGE, (page + 1) * PAGE);
    const td = (v, l) => `<td${l ? " class='l'" : ''}>${v}</td>`;
    tb.innerHTML = '<thead><tr>' + heads.map((h, i) => `<th${[1, 3, 4, 6, 7].includes(i) ? " class='l'" : ''}>${h}</th>`).join('') + '</tr></thead><tbody>' +
      part.map(i => { const r = T[i]; return `<tr data-i='${i}'>` + td(r[0]) + td(r[1] > 0 ? 'Long' : 'Short', 1) + td(r[2]) + td(r[3], 1) + td(r[4], 1) + td(r[5]) +
        td(r[6], 1) + td(r[7], 1) + td(r[8]) + td(money(r[9])) + td(money(r[10])) + td(money(r[11])) + td(money(r[12])) + td(r[13] ? 'yes' : '') + '</tr>'; }).join('') + '</tbody>';
    document.getElementById('tpage').textContent = `${rows.length.toLocaleString()} trades · page ${page + 1} of ${pages}`;
    markRow();
  }
  function markRow() { document.querySelectorAll('#tlist tbody tr').forEach(tr => tr.classList.toggle('sel', +tr.dataset.i === cur)); }
  const filters = { all: () => true, win: r => r[9] > 0, loss: r => r[9] < 0, long: r => r[1] > 0, short: r => r[1] < 0, amb: r => r[13] === 1 };

  function wire() {
    if (!T.length) return;
    document.getElementById('prev').onclick = () => go(cur - 1);
    document.getElementById('next').onclick = () => go(cur + 1);
    document.getElementById('tno').onchange = e => { const i = byNo.get(+e.target.value); if (i !== undefined) go(i); };
    document.getElementById('viewer-sec').addEventListener('keydown', e => {
      if (e.target.tagName === 'INPUT') return;
      if (e.key === 'ArrowLeft') { go(cur - 1); e.preventDefault(); } if (e.key === 'ArrowRight') { go(cur + 1); e.preventDefault(); } });
    document.getElementById('viewer-sec').tabIndex = 0;
    document.getElementById('tfilter').onchange = e => { const f = filters[e.target.value]; rows = T.map((_, i) => i).filter(i => f(T[i])); page = 0; drawTable(); };
    document.getElementById('tprev').onclick = () => { page--; drawTable(); };
    document.getElementById('tnext').onclick = () => { page++; drawTable(); };
    document.getElementById('tlist').onclick = e => { const tr = e.target.closest('tr[data-i]'); if (!tr) return;
      go(+tr.dataset.i); document.getElementById('viewer-sec').scrollIntoView({ behavior: 'smooth', block: 'start' }); };
  }

  function drawAll() { drawCurves(); drawMC(); drawViewer(); }
  wire(); drawTable(); drawAll();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', drawAll);
  new MutationObserver(drawAll).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
"""
