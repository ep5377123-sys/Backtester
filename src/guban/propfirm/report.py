"""Self-contained HTML page for a prop-firm Monte Carlo run (R10): outcome tiles, equity paths, days-to-pass and
max-drawdown distributions, degradation scenarios, the size sweep, and the trade/daily-pool statistics.
Same look as guban.report; charts are inline matplotlib images (Phase 16, guban.charts), and every chart has a
table (or tiles) with the same numbers.

The notebook's charts are redrawn with two changes: the outcome donut became tiles, and the degradation bars use
one color (the notebook's green-to-red ramp colored bars by their position, not their value).
"""

from __future__ import annotations

import html
import math
from pathlib import Path

import numpy as np

from guban.charts import COLORS, MONEY, figure, money, img_tag, styled, to_data_uri, value_formatter
from guban.propfirm.sim import FAIL_DD, FAIL_TIME, PASS, PropFirmResult
from guban.report import _CSS, _header, _notes, fmt_value

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
        "</head><body><main>",
        _header(meta),
        propfirm_body(res),
        _notes(meta),
        "</main>",
        "</body></html>",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def propfirm_body(res: PropFirmResult) -> str:
    """The page between its header and its notes: outcome tiles, paths, distributions, degradation, sweep, statistics."""
    return "\n".join([
        _tiles(res),
        _paths_html(res),
        "<div class='pair'>",
        _chart("Days to pass", "dtp", "Simulated trading days until the profit target was reached, passed evaluations only.", _dtp_png(res)),
        _chart("Max drawdown", "mdd", "Worst end-of-day drawdown in each evaluation, USD, all evaluations.", _mdd_png(res)),
        "</div>",
        _degradation_html(res),
        _sweep_html(res),
        _stats_html(res),
    ])


def _size_label(size: float, contracts: float | None) -> str:
    return f"{size:g}×" + (f" ({size * contracts:g})" if contracts is not None else "")



@styled
def _paths_png(res: PropFirmResult) -> str:
    from matplotlib.collections import LineCollection
    from matplotlib.ticker import MaxNLocator

    r, n = res.setup.rules, len(res.paths)
    fig, ax = figure(9.6, 4.2)
    for code, name, color, alpha in ((FAIL_TIME, "Time fail", COLORS["muted"], 0.35), (FAIL_DD, "Drawdown fail", COLORS["bad"], 0.3), (PASS, "Passed", COLORS["good"], 0.3)):
        rows = res.paths[res.path_outcomes == code]
        segs = [np.column_stack([np.arange(1, len(v) + 1), v])[np.isfinite(v)] for v in rows]
        ax.add_collection(LineCollection(segs, colors=color, alpha=alpha, linewidths=0.8, label=f"{name} ({len(rows) / n:.1%})" if n else name))
    ax.axhline(r.profit_target, color=COLORS["good"], ls="--", lw=1.6, label=f"Target {money(r.profit_target)}")
    ax.axhline(-r.max_drawdown, color=COLORS["bad"], ls="--", lw=1.6, label=f"Drawdown limit {money(-r.max_drawdown)}")
    ax.set_xlim(1, r.max_trading_days)
    ax.autoscale(axis="y")
    ax.set_xlabel("Trading day")
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.set_ylabel("Equity, USD")
    ax.yaxis.set_major_formatter(MONEY)
    for h in ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=5).legend_handles:
        h.set_alpha(1)
    return to_data_uri(fig)


def _vline(ax, x: float, color: str, text: str, ha: str = "center") -> None:
    ax.axvline(x, color=color, ls="--", lw=1.4)
    ax.text(x, 1.0, text, transform=ax.get_xaxis_transform(), ha=ha, va="bottom", fontsize=8)


@styled
def _dtp_png(res: PropFirmResult) -> str:
    from matplotlib.ticker import MaxNLocator

    r, m = res.setup.rules, res.main
    dtp = m.days_to_pass
    counts = np.bincount(dtp, minlength=r.max_trading_days + 1)[1:] if len(dtp) else np.zeros(r.max_trading_days, int)
    fig, ax = figure(4.8, 2.8)
    ax.bar(np.arange(1, len(counts) + 1), counts, width=0.85, color=COLORS["s1"])
    ax.set_xlim(0.4, len(counts) + 0.6)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    med = m.summary()["median_days_to_pass"]
    if med is not None:
        _vline(ax, med, COLORS["ink2"], f"median {med:g}")
    ax.set_xlabel("Trading days to pass")
    ax.set_ylabel("Evaluations")
    ax.yaxis.set_major_formatter(value_formatter("int"))
    return to_data_uri(fig)


@styled
def _mdd_png(res: PropFirmResult) -> str:
    limit, m = res.setup.rules.max_drawdown, res.main
    med = m.summary()["p50_max_dd"]
    fig, ax = figure(4.8, 2.8)
    ax.hist(m.max_dd, bins=DD_BINS, color=COLORS["s1"], rwidth=0.9)
    left = med is not None and med <= limit
    _vline(ax, limit, COLORS["bad"], f"limit {money(limit)}", "left" if left else "right")
    if med is not None:
        _vline(ax, med, COLORS["ink2"], f"median {money(med)}", "right" if left else "left")
    ax.set_xlabel("Max drawdown, USD")
    ax.set_ylabel("Evaluations")
    ax.xaxis.set_major_formatter(MONEY)
    ax.yaxis.set_major_formatter(value_formatter("int"))
    return to_data_uri(fig)


def _degr_rows(res: PropFirmResult) -> list[tuple]:
    return [(sc.label, sr.pass_rate, *sr.pass_rate_ci, True) for sc, sr in res.scenarios]


def _sweep_rows(res: PropFirmResult) -> list[tuple]:
    best = res.best_size
    return [(_size_label(sr.size, res.stats["contracts"]), sr.pass_rate, *sr.pass_rate_ci, best is None or sr is best) for sr in res.sweep]


@styled
def _rate_png(rows: list[tuple], horizontal: bool) -> str:
    """Pass rate bars with their 95% CI; rows: (label, rate, lo, hi, emphasized). The sweep marks its best size."""
    labels = [r[0] for r in rows]
    pr = np.array([np.nan if r[1] is None else r[1] for r in rows], float)
    lo = np.array([np.nan if r[2] is None else r[2] for r in rows], float)
    hi = np.array([np.nan if r[3] is None else r[3] for r in rows], float)
    colors = [COLORS["s1"] if r[4] else COLORS["s1"] + "99" for r in rows]
    err = np.vstack([pr - lo, hi - pr])
    pos = np.arange(len(rows))
    kw = dict(color=colors, ecolor=COLORS["ink2"], capsize=3, error_kw={"elinewidth": 1.2})
    if horizontal:
        fig, ax = figure(9.6, max(1.8, 0.34 * len(rows) + 0.9))
        ax.barh(pos, pr, xerr=err, height=0.65, **kw)
        ax.set_yticks(pos, labels=labels)
        ax.invert_yaxis()
        ax.grid(axis="y", visible=False)
        ax.set_xlim(left=0)
        ax.set_xlabel("Pass rate")
        ax.xaxis.set_major_formatter(value_formatter("pct"))
    else:
        fig, ax = figure(9.6, 3.2)
        ax.bar(pos, pr, yerr=err, width=0.7, **kw)
        ax.set_xticks(pos, labels=labels)
        ax.grid(axis="x", visible=False)
        ax.set_ylim(bottom=0)
        ax.set_xlabel("Size (contracts)")
        ax.set_ylabel("Pass rate")
        ax.yaxis.set_major_formatter(value_formatter("pct"))
        best = [i for i, r in enumerate(rows) if r[4]]
        if len(best) == 1 and math.isfinite(hi[best[0]]):
            ax.text(best[0], hi[best[0]], "best", ha="center", va="bottom", fontsize=8)
    return to_data_uri(fig)



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


def _chart(title: str, div: str, cap: str, uri: str) -> str:
    return f"<section><h2>{html.escape(title)}</h2><p class='cap'>{html.escape(cap)}</p>{img_tag(uri, title, id=div)}</section>"


def _paths_html(res: PropFirmResult) -> str:
    r = res.setup.rules
    cap = (f"{len(res.paths):,} simulated evaluations at size {res.setup.size:g} (seed + 5000), equity after each day, USD. "
           f"Dashed lines: the profit target ({fmt_value(r.profit_target, 'usd')}) and the drawdown limit measured from the start "
           f"(−{r.max_drawdown:,.0f}); the real limit trails the end-of-day high. A failed path stops on its failure day; a passed one stays flat.")
    return f"<section><h2>Equity paths</h2><p class='cap'>{html.escape(cap)}</p>{img_tag(_paths_png(res), 'Equity paths', id='paths')}</section>"


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
                     fmt_value(s["avg_days_to_pass"], "num"), fmt_value(s["avg_max_dd"], "usd")])
    st = res.setup
    cap = (f"What if live trading is worse? {st.sweep_sims:,} simulations per scenario at size {st.size:g} (seed + 1000). PnL shrink scales "
           "every pool day; WR adjust turns that share of the pool's days (winning ones, picked at random) into the mean losing day. Bars: pass rate with its 95% CI.")
    return (
        f"<section><h2>Degradation scenarios</h2><p class='cap'>{html.escape(cap)}</p>" + img_tag(_rate_png(_degr_rows(res), True), "Degradation scenarios", id="degr")
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
                     fmt_value(s["avg_days_to_pass"], "num"), fmt_value(s["avg_max_dd"], "usd")])
    cap = (f"Pass rate by size: a multiplier on the trade file's daily P&L" + (f" (1× = {c:g} contracts per trade, as in the file)" if c is not None else "")
           + f". {res.setup.sweep_sims:,} simulations per size, all from the same seed (seed + 2000). Bars: pass rate with its 95% CI.")
    return (
        f"<section><h2>Size sweep</h2><p class='cap'>{html.escape(cap)}</p>" + img_tag(_rate_png(_sweep_rows(res), False), "Size sweep", id="sweep")
        + _rate_table(rows, ["Size", "Contracts", "Pass rate", "95% CI", "Fail: drawdown", "Fail: time", "Avg days to pass", "Avg max drawdown"])
        + "</section>"
    )


def _stats_html(res: PropFirmResult) -> str:
    t, d, m, s = res.stats, res.daily, res.main.summary(), res.summary()
    usd = lambda v: fmt_value(v, "usd")
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
    kv = lambda rows: "".join(f"<tr><td class='l'>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>" for k, v in rows)
    return (
        "<div class='pair'>"
        f"<section><h2>Trades</h2><p class='cap'>Notebook cell 4. Added costs: ${res.setup.commission + res.setup.slippage:,.2f} per contract per trade; "
        f"1R = ${res.setup.risk_per_contract:,.0f} per contract.</p><div class='wrap'><table class='kv'>{kv(trade_rows)}</table></div></section>"
        "<section><h2>Daily pool</h2><p class='cap'>Notebook cell 5: net P&amp;L by entry date on every weekday; the simulation draws from these days.</p>"
        f"<div class='wrap'><table class='kv'>{kv(pool_rows)}</table></div>"
        f"<h2 class='gap'>Passing within N attempts</h2><div class='wrap'><table class='kv'><tr><th class='l'>Attempts</th><th>P(pass ≥ 1)</th></tr>{cum}</table></div></section>"
        "</div>"
    )


_EXTRA_CSS = """
.tile .s { font-size:12px; color:var(--muted); }
.pair { display:grid; grid-template-columns:repeat(auto-fit, minmax(340px, 1fr)); gap:0 16px; }
table.kv td.l { white-space:normal; }
h2.gap { margin-top:16px; }
.badge { font-size:11px; border:1px solid var(--axis); border-radius:999px; padding:0 6px; color:var(--ink2); margin-left:4px; }
"""
