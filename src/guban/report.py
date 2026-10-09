"""Self-contained HTML backtest report (R6): summary metrics, equity curve, drawdown, a trade Monte Carlo (Phases 11
and 16, guban.montecarlo), monthly net profit, a price chart per trade, and the trade list.

Charts are matplotlib images (Phase 16, guban.charts), so the page is light-only:
- equity, drawdown and Monte Carlo charts are inline PNGs: report.html opens offline from one file. Equity and
  drawdown have one point per session (equity at the session's last bar; the session's worst intraday drawdown, see
  guban.metrics.intraday_drawdown).
- the price viewer shows one pre-rendered image per trade, `trades/trade_<no>.png` next to the report
  (guban.trade_charts), loaded when shown; ◀ / ▶, the trade number box and the trade list switch them. A missing
  image (a --no-trade-charts run) shows the `guban chart` command that draws it.
"""

from __future__ import annotations

import html
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from guban.charts import COLORS, MONEY, figure, img_tag, styled, to_data_uri
from guban.metrics import ACCOUNT_STATS, TRADE_STATS, daily_equity, intraday_drawdown, session_keys

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
MC_BINS = 60
MC_TITLES = {"resample": "Resample", "reorder": "Reorder", "day": "Whole days"}


def write_report(
    result,
    bars: pd.DataFrame,
    path: str | Path,
    metrics: dict,
    meta: dict,
    sections: list[str] | None = None,
    bands: list[dict] | None = None,
    monte_carlo=None,
    trade_charts: bool = True,
    workers: int | None = None,
) -> Path:
    """Write report.html for a BacktestResult run on `bars` (the frame passed to run_backtest), and with
    `trade_charts` one image per trade in trades/ next to it (on `workers` processes, guban.trade_charts).

    meta: {"title", "subtitle", "details": {label: value}, "inputs": {name: value}, "notes": [str]}.
    result.equity may cover only some of the bars (a stitched walk-forward curve); trade bar numbers index `bars`.
    sections: extra HTML sections placed after the header. bands: [{"x0", "x1", "xm": "YYYY-MM-DD", "label"}]
    periods marked on the equity and drawdown charts (every other one shaded; x1 exclusive).
    monte_carlo: a guban.montecarlo.MonteCarloResult (a section after the drawdown chart), {"skipped": reason}, or None.
    """
    from guban.trade_charts import FOLDER, write_trade_charts

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tr = result.trades
    tick = result.spec.tick_size if result.spec else 0.01
    sessions = bars["session_date"].reindex(result.equity.index) if "session_date" in bars.columns else None
    daily = daily_equity(result.equity, sessions)
    dd = intraday_drawdown(result.equity, result.equity_low, result.equity_high, result.bar_drawdown)
    daily_dd = dd.groupby(session_keys(dd.index, sessions)).min()
    images = trade_charts and len(tr) > 0
    if images:
        write_trade_charts(tr, bars, path.parent / FOLDER, tick, workers)
    elif (path.parent / FOLDER).is_dir():  # an earlier run's images would show the wrong trades
        for old in (path.parent / FOLDER).glob("trade_*.png"):
            old.unlink()
    data = {"trades": _trade_rows(tr), "img": f"{FOLDER}/", "run": path.parent.as_posix()}
    eq_png, dd_png = _equity_pngs(daily, daily_dd, bands or [])
    band_cap = " Labels and alternate shading mark the out-of-sample windows." if bands else ""
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(meta.get('title', 'Backtest report'))}</title>",
        f"<style>{_CSS}</style>",
        "</head><body><main>",
        _header(meta),
        *(sections or []),
        _tiles(metrics),
        f"<section><h2>Equity</h2><p class='cap'>Closed net profit plus open profit at each session's last bar, USD.{band_cap}</p>"
        + (img_tag(eq_png, "Equity curve", id="equity") if eq_png else "<p class='muted'>No equity data.</p>") + "</section>",
        "<section><h2>Drawdown</h2><p class='cap'>Worst intraday drawdown each session, from the running equity peak (tracked along the intrabar path), USD.</p>"
        + (img_tag(dd_png, "Drawdown", id="drawdown") if dd_png else "<p class='muted'>No equity data.</p>") + "</section>",
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


def _trade_rows(tr: pd.DataFrame) -> list[list]:
    """[no, dir, qty, entry time, entry name, entry price, exit time, exit name, exit price, net, cum net, run-up,
    drawdown, ambiguous, R multiple, planned RR] per trade, for the trade list and the viewer's info line (R and RR:
    null without a stop / target)."""
    fmt = lambda t: t.strftime("%Y-%m-%d %H:%M")  # noqa: E731
    num = lambda v: None if v is None or not math.isfinite(v) else round(float(v), 3)  # noqa: E731
    nan = [math.nan] * len(tr)
    r_mult = tr["r_multiple"].tolist() if "r_multiple" in tr else nan
    plan_rr = tr["planned_rr"].tolist() if "planned_rr" in tr else nan
    return [
        [int(r.trade_no), 1 if r.direction == "long" else -1, int(r.qty), fmt(r.entry_time), str(r.entry_name), float(r.entry_price),
         fmt(r.exit_time), str(r.exit_name), float(r.exit_price), round(float(r.net_pnl), 2), round(float(r.cum_net_pnl), 2),
         round(float(r.runup), 2), round(float(r.drawdown), 2), int(bool(r.ambiguous)), num(r_mult[k]), num(plan_rr[k])]
        for k, r in enumerate(tr.itertuples(index=False))
    ]  # fmt: skip


def _json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")


# --- charts --------------------------------------------------------------------------------------


def _date_axis(ax) -> None:
    import matplotlib.dates as mdates

    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))


def _shade(ax, bands: list[dict], labels: bool) -> None:
    for i, b in enumerate(bands):
        if i % 2 == 0:
            ax.axvspan(np.datetime64(b["x0"]), np.datetime64(b["x1"]), color=COLORS["s1"], alpha=0.08, lw=0, zorder=0)
        if labels:
            ax.text(np.datetime64(b["xm"]), 1.0, b["label"], transform=ax.get_xaxis_transform(), ha="center", va="bottom",
                    fontsize=7, color=COLORS["muted"])  # fmt: skip


@styled
def _equity_pngs(daily: pd.Series, daily_dd: pd.Series, bands: list[dict]) -> tuple[str | None, str | None]:
    if not len(daily):
        return None, None
    x = pd.to_datetime([d.strftime("%Y-%m-%d") for d in daily.index]).to_numpy()
    fig, ax = figure(9.6, 3.2)
    ax.plot(x, daily.to_numpy(float), color=COLORS["s1"], lw=1.5)
    _shade(ax, bands, labels=True)
    ax.yaxis.set_major_formatter(MONEY)
    _date_axis(ax)
    eq = to_data_uri(fig)
    fig, ax = figure(9.6, 2.2)
    d = daily_dd.to_numpy(float)
    ax.fill_between(x, d, 0, color=COLORS["bad"], alpha=0.2, lw=0)
    ax.plot(x, d, color=COLORS["bad"], lw=1.1)
    _shade(ax, bands, labels=False)
    ax.yaxis.set_major_formatter(MONEY)
    _date_axis(ax)
    return eq, to_data_uri(fig)


@styled
def _mc_band_png(m, unit: str) -> str:
    from matplotlib.collections import LineCollection

    fig, ax = figure(4.4, 2.8)  # shown ~1/3 of the page wide: a small figure keeps its text readable
    x, b = m.x, m.bands
    ax.fill_between(x, b[0], b[4], color=COLORS["s1"], alpha=0.14, lw=0, label="5th–95th")
    ax.fill_between(x, b[1], b[3], color=COLORS["s1"], alpha=0.28, lw=0, label="25th–75th")
    if m.paths is not None and len(m.paths):
        seg = np.stack([np.broadcast_to(x, m.paths.shape), m.paths], axis=-1)
        ax.add_collection(LineCollection(seg, colors=COLORS["ink"], alpha=0.07, linewidths=0.5, label=f"{len(m.paths)} runs"))
    ax.plot(x, b[2], color=COLORS["s1"], lw=1.6, label="Median")
    ax.plot(x, m.backtest, color=COLORS["ink"], lw=1.2, label="Backtest")
    ax.set_xlabel("Trading days" if unit == "day" else "Trades")
    ax.yaxis.set_major_formatter(MONEY)
    ax.legend(loc="upper left", ncols=2)
    return to_data_uri(fig)


@styled
def _hist_png(values: np.ndarray, color: str, backtest: float, limit: float | None = None) -> str:
    fig, ax = figure(4.4, 1.9)
    ax.hist(values, bins=MC_BINS, color=color, alpha=0.85)
    tx = ax.get_xaxis_transform()
    ax.axvline(backtest, color=COLORS["ink"], ls="--", lw=1.2)
    ax.text(backtest, 1.0, "Backtest", transform=tx, ha="center", va="bottom", fontsize=8)
    if limit is not None:
        ax.axvline(-limit, color=COLORS["muted"], ls=":", lw=1.2)
        ax.text(-limit, 1.0, "Limit", transform=tx, ha="center", va="bottom", fontsize=8)
    ax.xaxis.set_major_formatter(MONEY)
    ax.set_ylabel("Runs")
    return to_data_uri(fig)


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


def _table(head: list[str], rows: list[list[str]], cls: str = "mc-table") -> str:
    """A table whose first column is left-aligned labels (cells are HTML)."""
    cell = lambda tag, i, v: f"<{tag} class='l'>{v}</{tag}>" if i == 0 else f"<{tag}>{v}</{tag}>"  # noqa: E731
    th = "".join(cell("th", i, h) for i, h in enumerate(head))
    body = "".join("<tr>" + "".join(cell("td", i, v) for i, v in enumerate(r)) + "</tr>" for r in rows)
    return f"<div class='wrap'><table class='{cls}'><tr>{th}</tr>{body}</table></div>"


def _pct_head(percentiles) -> list[str]:
    return [f"{'Median' if p == 50 else f'{p}th'}" for p in percentiles]


def _monte_carlo_html(mc) -> str:
    if mc is None:
        return ""
    if isinstance(mc, dict):
        return f"<section id='monte-carlo'><h2>Monte Carlo</h2><p class='muted'>Monte Carlo skipped: {html.escape(mc['skipped'])}.</p></section>"
    from guban.montecarlo import DEPTH_PERCENTILES, PERCENTILES, UNITS

    usd = lambda v: fmt_value(v, "usd")  # noqa: E731
    pct = lambda v: fmt_value(v, "pct")  # noqa: E731
    names = [n for n in ("resample", "reorder", "day") if n in mc.methods]
    sums = {n: mc.method_summary(n) for n in names}
    caps = {
        "resample": f"Each run draws {mc.trades:,} trades from the list at random, with replacement: net profit and drawdown both vary.",
        "reorder": f"Each run shuffles the same {mc.trades:,} trades: net profit is the backtest's in every run; only the path varies.",
        "day": f"Each run draws {mc.days or 0:,} trading days (the sessions in which a trade closed) at random, with replacement, keeping "
               "each day's trades together and in order: losses that cluster on one day cluster here too.",
    }  # fmt: skip

    def stats(name: str) -> str:
        s = sums[name]
        items = [("Net profit (every run)", usd(mc.net))] if name == "reorder" else [("Net profit ≤ 0", pct(s["p_net_le_0"]))]
        items.append(("Max DD deeper than backtest", pct(s["p_dd_worse_than_backtest"])))
        if mc.dd_limit is not None:
            items.append((f"Max DD reaches {usd(mc.dd_limit)}", pct(s["p_dd_reaches_limit"])))
        return "<div class='mc-stats'>" + "".join(f"<div><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in items) + "</div>"

    cols = []
    for name in names:
        m = mc.methods[name]
        net_chart = ("<p class='cap mc-label'>Net profit per run (dashed: backtest)</p>"
                     + img_tag(_hist_png(m.net, COLORS["s1"], mc.net), f"{MC_TITLES[name]}: net profit per run", id=f"mc-net-{name}")
                     if name != "reorder" else "")  # fmt: skip
        cols.append(
            f"<div class='mc-col'><h3>{MC_TITLES[name]}</h3><p class='cap'>{caps[name]}</p>{stats(name)}"
            f"<p class='cap mc-label'>Cumulative net profit by {'trading day' if UNITS[name] == 'day' else 'trade'}: 5th–95th and 25th–75th "
            "percentile bands, median, the first runs (thin), backtest</p>"
            + img_tag(_mc_band_png(m, UNITS[name]), f"{MC_TITLES[name]}: cumulative net profit bands", id=f"mc-band-{name}")
            + "<p class='cap mc-label'>Max drawdown per run (dashed: backtest)</p>"
            + img_tag(_hist_png(m.max_dd, COLORS["bad"], mc.max_dd, mc.dd_limit), f"{MC_TITLES[name]}: max drawdown per run", id=f"mc-dd-{name}")
            + f"{net_chart}</div>"
        )
    skipped = "".join(f"<p class='muted'>{MC_TITLES.get(k, k)} method skipped: {html.escape(v)}.</p>" for k, v in mc.skipped.items())

    rows = []
    for name in names:
        if name != "reorder":
            rows.append([f"{MC_TITLES[name]} · net profit", *(usd(v) for v in sums[name]["net_profit"].values()), f"<b>{usd(mc.net)}</b>"])
        rows.append([f"{MC_TITLES[name]} · max drawdown", *(usd(v) for v in sums[name]["max_drawdown"].values()), f"<b>{usd(mc.max_dd)}</b>"])
    pct_table = _table(["Percentile of runs", *_pct_head(PERCENTILES), "Backtest"], rows)
    depth_rows = [[MC_TITLES[n], *(usd(v) for v in sums[n]["max_drawdown_depth"].values()), f"<b>{usd(-mc.max_dd)}</b>"] for n in names]
    depth_table = _table(["Max drawdown depth", *_pct_head(DEPTH_PERCENTILES), "Backtest"], depth_rows)

    return (
        "<section id='monte-carlo'><h2>Monte Carlo</h2>"
        f"<p class='cap'>{mc.sims:,} runs per method on this backtest's {mc.trades:,} trades (net of costs), seed {mc.seed}. "
        f"Drawdowns here are close-to-close (closed-trade equity from 0): compare them with the backtest's close-to-close max drawdown, "
        f"{usd(mc.max_dd)}, not the intraday figure in the tiles above. Resample and reorder draw trades one at a time, so trades that "
        "tend to win or lose together (for example on the same day) are treated as independent, which can make drawdowns look milder "
        "than they are; the whole-days method keeps each day's trades together.</p>"
        f"<div class='mc-grid'>{''.join(cols)}</div>{skipped}"
        f"{pct_table}<p class='cap'>Drawdowns are negative, so the 5th percentile is the deepest 5% of runs.</p>"
        f"{depth_table}<p class='cap'>Depth = the max drawdown as a positive amount: the 95th percentile is deeper than 95% of runs.</p>"
        f"{_ev_html(mc.ev)}{_horizons_html(mc.horizons)}</section>"
    )


EV_GROUPS = {"all": "All trades", "long": "Long", "short": "Short", "in_sample": "In sample", "out_of_sample": "Out of sample"}


def _ev_html(ev: dict) -> str:
    if not ev or not ev.get("groups"):
        return ""
    usd = lambda v: fmt_value(v, "usd")  # noqa: E731
    r2 = lambda v: fmt_value(v, "ratio")  # noqa: E731
    level = f"{ev['ci']:.0%}"
    rows = []
    for key, g in ev["groups"].items():
        label = EV_GROUPS.get(key, key)
        if key in ("in_sample", "out_of_sample"):
            label += f" ({'before' if key == 'in_sample' else 'from'} {ev['oos_from']})"
        r = g["r"]
        rows.append([label, f"{g['trades']:,}", fmt_value(g["win_rate"], "pct"), usd(g["usd"]["mean"]),
                     f"{usd(g['usd']['lo'])} to {usd(g['usd']['hi'])}", r2(r["mean"]) if r else "n/a",
                     f"{r2(r['lo'])} to {r2(r['hi'])}" if r else "n/a", fmt_value(g["p_ev_le_0"], "pct")])  # fmt: skip
    risk = ev["risk_per_contract"]
    if risk is None:
        r_cap = f"No R: {ev['risk_source']}."
    elif ev["risk_source"].startswith("per-trade stop"):
        r_cap = (f"R = net ÷ the trade's risk to its initial stop (average {usd(risk)} per contract); "
                 f"{ev['risk_source'].removeprefix('per-trade stop').strip(' ()') or 'every trade had a stop'}.")  # fmt: skip
    else:
        r_cap = f"R = net / (contracts × {usd(risk)} risk per contract; {ev['risk_source']})."
    skipped = ev.get("skipped") or {}
    sk = (" Too few trades for: " + ", ".join(f"{EV_GROUPS.get(k, k)} ({v})" for k, v in skipped.items()) + ".") if skipped else ""
    return (
        "<h3 class='mc-h'>Expected value per trade</h3>"
        + _table(["Trades", "Count", "Win rate", "EV (USD)", f"{level} CI (USD)", "EV (R)", f"{level} CI (R)", "P(EV ≤ 0)"], rows)
        + f"<p class='cap'>Mean net profit per trade with a bootstrap {level} confidence interval (the trades resampled with replacement, "
        f"once per run); P(EV ≤ 0) is the share of bootstrap means at or below 0. {r_cap}{sk}</p>"
    )


def _horizons_html(horizons: dict) -> str:
    from guban.montecarlo import DEPTH_PERCENTILES

    rows = []
    for name, hz in horizons.items():
        unit = "trading days" if name == "day" else "trades"
        for h in hz:
            n, d = h["net_profit"], h["max_drawdown_depth"]
            rows.append([f"{MC_TITLES[name]} · {h['n']:,} {unit}", fmt_value(n["p5"], "usd"), fmt_value(n["p50"], "usd"), fmt_value(n["p95"], "usd"),
                         fmt_value(h["p_net_lt_0"], "pct"), *(fmt_value(d[f"p{p}"], "usd") for p in DEPTH_PERCENTILES)])  # fmt: skip
    if not rows:
        return ""
    head = ["After", "Net 5th", "Net median", "Net 95th", "P(net < 0)", *(f"DD depth {h.lower()}" for h in _pct_head(DEPTH_PERCENTILES))]
    return (
        "<h3 class='mc-h'>Horizons</h3>" + _table(head, rows)
        + "<p class='cap'>Net profit and max drawdown depth after each run's first trades (resample) or trading days (whole days): "
        "what a shorter stretch of this strategy can look like. Horizons longer than the backtest are left out.</p>"
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
    chart = (
        "<img id='viewer' class='chart' alt='Price chart of the selected trade'>"
        "<p id='noimg' class='muted' hidden></p>"
        "<p class='cap'>▲ long entry · ▼ short entry · ● exit (outlined: loss) · the selected trade is shaded, other trades in view "
        "are faded. ← / → step through trades. Gaps between sessions are closed up.</p>"
    )  # fmt: skip
    return (
        "<section id='viewer-sec'><h2>Price chart with trades</h2>"
        "<div class='controls'><button id='prev' type='button' aria-label='Previous trade'>◀</button>"
        f"<label>Trade <input id='tno' type='number' min='1' max='{n}' value='1'></label> <span class='muted'>of {n:,}</span>"
        f"<button id='next' type='button' aria-label='Next trade'>▶</button><span id='tinfo' class='tinfo'></span></div>{chart}</section>"
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


# light only: the chart images have fixed colours (guban.charts.COLORS)
_CSS = """
:root { color-scheme: light;
  --page:#f9f9f7; --surface:#fcfcfb; --ink:#0b0b0b; --ink2:#52514e; --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --ring:rgba(11,11,11,0.10); --s1:#2a78d6; --up:#2a78d6; --down:#e34948; --good:#0ca30c; --bad:#d03b3b; --hl:rgba(42,120,214,0.10); }
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
img.chart { display:block; width:100%; height:auto; }
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
h3 { font-size:14px; margin:0 0 2px; } h3.mc-h { margin:16px 0 4px; }
.mc-grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:24px; margin:12px 0; }
.mc-stats { display:flex; flex-wrap:wrap; gap:8px 24px; margin:8px 0 12px; }
.mc-stats .k { font-size:12px; color:var(--ink2); } .mc-stats .v { font-size:18px; font-weight:600; }
.mc-label { margin:8px 0 0; }
table.mc-table { margin:12px 0 4px; }
"""

_JS = r"""
(function () {
  const money = x => (x < 0 ? '−' : '') + Math.abs(x).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const ratio = x => x === null ? '' : (x < 0 ? '−' : '') + Math.abs(x).toFixed(2);  // R and RR; empty without a stop

  // --- trade viewer: one pre-rendered image per trade ---
  const T = D.trades, byNo = new Map(T.map((t, i) => [t[0], i]));
  const img = document.getElementById('viewer');
  const src = i => D.img + 'trade_' + T[i][0] + '.png';
  const noimg = document.getElementById('noimg');
  if (img) {  // images are drawn by guban run / wfo (--trade-charts) or later by guban chart
    img.onload = () => { img.hidden = false; noimg.hidden = true; };
    img.onerror = () => {
      img.hidden = true; noimg.hidden = false;
      const run = D.run.includes(' ') ? `"${D.run}"` : D.run;
      noimg.textContent = `No chart image for trade ${T[cur][0]}. Draw it with: uv run guban chart ${run} --trade ${T[cur][0]} (or without --trade for every trade), then reload.`;
    };
  }
  let cur = 0;
  function show() {
    if (!T.length) return;
    const t = T[cur];
    if (img) {
      img.src = src(cur);
      img.alt = `Price chart of trade ${t[0]}`;
      for (const j of [cur - 1, cur + 1]) if (j >= 0 && j < T.length) new Image().src = src(j);  // step without a wait
    }
    document.getElementById('tno').value = t[0];
    document.getElementById('tinfo').textContent =
      `${t[1] > 0 ? 'Long' : 'Short'} ${t[2]} · ${t[3]} → ${t[6]} · ${t[7]} · net ${money(t[9])}` + (t[14] !== null ? ` · ${ratio(t[14])} R` : '') +
      (t[15] !== null ? ` · planned ${ratio(t[15])} RR` : '') + (t[13] ? ' · ambiguous fill' : '');
    markRow();
  }
  function go(i) { cur = Math.max(0, Math.min(T.length - 1, i)); show(); }

  // --- trade list ---
  const PAGE = 50; let rows = T.map((_, i) => i), page = 0;
  const heads = ['#', 'Dir', 'Qty', 'Entry time', 'Entry signal', 'Entry price', 'Exit time', 'Exit signal', 'Exit price', 'Net profit', 'Cum. net', 'Run-up', 'Drawdown', 'R', 'RR', 'Ambig.'];
  function drawTable() {
    const tb = document.getElementById('tlist'); if (!tb) return;
    const pages = Math.max(1, Math.ceil(rows.length / PAGE)); page = Math.max(0, Math.min(page, pages - 1));
    const part = rows.slice(page * PAGE, (page + 1) * PAGE);
    const td = (v, l) => `<td${l ? " class='l'" : ''}>${v}</td>`;
    tb.innerHTML = '<thead><tr>' + heads.map((h, i) => `<th${[1, 3, 4, 6, 7].includes(i) ? " class='l'" : ''}>${h}</th>`).join('') + '</tr></thead><tbody>' +
      part.map(i => { const r = T[i]; return `<tr data-i='${i}'>` + td(r[0]) + td(r[1] > 0 ? 'Long' : 'Short', 1) + td(r[2]) + td(r[3], 1) + td(r[4], 1) + td(r[5]) +
        td(r[6], 1) + td(r[7], 1) + td(r[8]) + td(money(r[9])) + td(money(r[10])) + td(money(r[11])) + td(money(r[12])) + td(ratio(r[14])) + td(ratio(r[15])) + td(r[13] ? 'yes' : '') + '</tr>'; }).join('') + '</tbody>';
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

  wire(); drawTable(); show();
})();
"""
