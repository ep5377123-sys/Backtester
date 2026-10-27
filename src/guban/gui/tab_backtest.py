"""Backtest tab: rerun the applied strategy (its inputs, the Data tab's dataset) with this tab's capital, costs and fill
mode, then what `guban run` reports: metrics, evaluation, equity and drawdown, monthly table; plus the tab's own charts
(monthly net bars, daily P&L and daily drawdown histograms). The trade Monte Carlo is on the Monte Carlo tab: Run takes
its settings (the evaluation's R Monte Carlo uses them) and Save runs it, so the run folder is the one `guban run`
writes (trades.csv, metrics.json with monte_carlo, report.html).

Windowed datasets use the windowed result (tab_strategy.run_window): metrics over bars [lo, hi) as
metrics.window_metrics; per-trade lookups (trading days, rolls, session exposure) index the run's bars [:hi].
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from guban.gui.handlers import State, method
from guban.gui.tab_data import DATA_DIR, DATASETS, loaded
from guban.gui.tab_strategy import Applied, applied, run_window

REPORTS = Path("reports")
DEFAULTS = {"capital": 1_000_000.0, "fill_mode": "magnifier"}


@dataclass
class Backtested:
    """One Run of the Backtest tab (State.backtest)."""

    a: Applied
    capital: float
    costs_from: str
    metrics: dict
    mc_settings: dict  # the Monte Carlo tab's trade settings at Run (tab_montecarlo.trade_settings)
    evaluation: dict | None
    exposure: dict
    rolls: dict
    seconds: dict


def _num(params: dict, key: str, kind=float, default=None, low=None):
    v = params.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    try:
        v = kind(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key.replace('_', ' ')} must be a number, got {params.get(key)!r}") from None
    if low is not None and v < low:
        raise ValueError(f"{key.replace('_', ' ')} must be at least {low}, got {v}")
    return v


def _window_bars(a: Applied) -> pd.DataFrame:
    return a.bars.iloc[a.lo : a.hi]


def _stats_box(ax, values: np.ndarray) -> None:
    from guban.charts import COLORS

    s = pd.Series(values)
    text = f"Mean {s.mean():,.0f}\nMedian {s.median():,.0f}\nStd {s.std():,.0f}\nSkew {s.skew():.2f}".replace("-", "−")
    ax.text(0.98, 0.95, text, transform=ax.transAxes, ha="right", va="top", fontsize=8, color=COLORS["ink"],
            bbox={"boxstyle": "round,pad=0.4", "facecolor": COLORS["surface"], "edgecolor": COLORS["axis"]})  # fmt: skip


def _hist(values: np.ndarray, color: str, xlabel: str) -> str | None:
    from guban.charts import MONEY, figure, styled, to_data_uri

    if len(values) < 2:
        return None

    @styled
    def draw():
        fig, ax = figure(4.8, 2.9)
        ax.hist(values, bins=60, color=color, alpha=0.9)
        ax.xaxis.set_major_formatter(MONEY)
        ax.set_xlabel(xlabel)
        ax.set_ylabel("Days")
        _stats_box(ax, values)
        return to_data_uri(fig)

    return draw()


def _monthly_png(tr: pd.DataFrame) -> str | None:
    from guban.charts import COLORS, MONEY, figure, styled, to_data_uri

    if tr.empty:
        return None
    t = tr["exit_time"].dt.tz_localize(None)
    pnl = tr.groupby(t.dt.to_period("M"))["net_pnl"].sum()
    pnl = pnl.reindex(pd.period_range(pnl.index.min(), pnl.index.max(), freq="M"), fill_value=0.0)

    @styled
    def draw():
        import matplotlib.dates as mdates

        fig, ax = figure(9.6, 2.6)
        x = pnl.index.to_timestamp().to_numpy()
        v = pnl.to_numpy(float)
        ax.bar(x, v, width=np.timedelta64(20, "D"), color=np.where(v >= 0, COLORS["s1"], COLORS["bad"]))
        ax.axhline(0, color=COLORS["axis"], lw=0.8)
        loc = mdates.AutoDateLocator()
        ax.xaxis.set_major_locator(loc)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
        ax.yaxis.set_major_formatter(MONEY)
        ax.set_ylabel("Net by month")
        return to_data_uri(fig)

    return draw()


def charts(b: Backtested) -> dict:
    """The tab's chart images (dark theme in the worker)."""
    from guban.charts import COLORS
    from guban.metrics import daily_equity, intraday_drawdown, session_keys
    from guban.report import _equity_pngs

    res = b.a.result
    bars_w = _window_bars(b.a)
    sessions = bars_w["session_date"].reindex(res.equity.index) if "session_date" in bars_w.columns else None
    daily = daily_equity(res.equity, sessions)
    dd = intraday_drawdown(res.equity, res.equity_low, res.equity_high, res.bar_drawdown)
    daily_dd = dd.groupby(session_keys(dd.index, sessions)).min()
    eq, ddp = _equity_pngs(daily, daily_dd, [])
    pnl = daily.diff().fillna(daily.iloc[0]).to_numpy(float) if len(daily) else np.array([])
    return {
        "monthly": _monthly_png(res.trades),
        "equity": eq,
        "drawdown": ddp,
        "daily_pnl": _hist(pnl[pnl != 0], COLORS["up"], "Daily net P&L (USD), days with a P&L"),
        "daily_dd": _hist(daily_dd.to_numpy(float), COLORS["bad"], "Daily drawdown from the running peak (USD)"),
    }


def report_html(b: Backtested) -> dict:
    """report.html's own sections, as HTML fragments for the tab (styled by the renderer)."""
    from guban.report import _evaluation_html, _monthly_table, _summary_table, _tiles

    tr = b.a.result.trades
    return {
        "tiles": _tiles(b.metrics),
        "evaluation": _evaluation_html(b.evaluation),
        "summary": _summary_table(b.metrics),
        "monthly": _monthly_table(tr),
    }


def notes(b: Backtested) -> list[str]:
    from guban.cli import _exposure_notes, _trade_list
    from guban.metrics import ROLL_NOTE

    a, out = b.a, []
    if b.rolls["trades_held_across"]:
        out.append(ROLL_NOTE.format(n=b.rolls["trades_held_across"], nos=_trade_list(b.rolls["trade_nos"])))
    out += _exposure_notes(b.exposure)
    if a.result.open_position is not None:
        p = a.result.open_position
        out.append(f"A position is still open at the end ({p.direction:+d} x{p.qty} from {p.entry_time:%Y-%m-%d %H:%M}); it is not in the trade statistics.")
    missing = a.result.bars_without_sub_bars + a.result.bars_with_partial_sub_bars
    if missing:
        out.append(f"{missing:,} bars had no complete 1-minute data and were filled from their own OHLC.")
    tr = a.result.trades
    if len(tr) and tr["ambiguous"].any():
        out.append(f"{int(tr['ambiguous'].sum())} trade(s) have an ambiguous fill: the exit order inside one sub-bar came from the OHLC path rule, not the data.")
    return out


@method("backtest.defaults")
def backtest_defaults(state: State, params: dict, emit) -> dict:
    """Default costs for the loaded symbol (the contract spec's)."""
    from guban.config import get_contract

    spec = get_contract(loaded(state).symbol)
    return {**DEFAULTS, "commission": spec.commission, "slippage": spec.slippage, "contract": spec.symbol}


@method("backtest.run")
def backtest_run(state: State, params: dict, emit) -> dict:
    from guban.cli import _costs, _mc_trade_info
    from guban.evaluation import evaluate
    from guban.gui.tab_montecarlo import trade_settings
    from guban.metrics import backtest_metrics, roll_summary, session_exposure

    prev = applied(state)
    capital = _num(params, "capital", float, DEFAULTS["capital"], low=1.0)
    commission = _num(params, "commission", float, None, low=0.0)
    slippage = _num(params, "slippage", float, None, low=0.0)
    fill_mode = str(params.get("fill_mode") or DEFAULTS["fill_mode"])
    s = trade_settings(params)
    t0 = time.perf_counter()
    a = run_window(state, prev.cls, prev.inputs, commission, slippage, fill_mode, emit)
    _, _, costs_from = _costs(a.spec, commission, slippage)
    t1 = time.perf_counter()
    tr = a.result.trades
    metrics = backtest_metrics(a.result, _window_bars(a))
    info = _mc_trade_info(tr, a.bars)
    evaluation = evaluate(tr, metrics, info["days"], s["sims"], s["seed"], s["risk"]) if len(tr) else None
    t2 = time.perf_counter()
    b = Backtested(a, capital, costs_from, metrics, s, evaluation, session_exposure(tr, a.bars), roll_summary(tr, a.bars), {})
    state.backtest = b
    emit("progress", message="Drawing charts…")
    images = charts(b)
    t3 = time.perf_counter()
    b.seconds = {"backtest": round(t1 - t0, 2), "evaluation": round(t2 - t1, 2), "charts": round(t3 - t2, 2)}
    lo_t, hi_t = a.bars.index[a.lo], a.bars.index[a.hi - 1]
    return {
        "title": f"{a.cls.__name__} · {a.spec.symbol} {loaded(state).tf} · {DATASETS[a.dataset].lower()}",
        "window": f"{lo_t:%Y-%m-%d %H:%M} → {hi_t:%Y-%m-%d %H:%M}",
        "costs": f"commission ${a.commission:,.2f}, slippage ${a.slippage:,.2f} per contract round trip ({costs_from}); {fill_mode} fills",
        "trades": len(tr),
        "net_profit": round(a.result.net_profit, 2),
        "seconds": b.seconds,
        "charts": images,
        "html": report_html(b),
        "notes": notes(b),
    }


def run_folder(b: Backtested, d) -> Path:
    from guban.cli import run_dir

    suffix = "" if b.a.dataset == "full" else f"_{b.a.dataset}"
    return run_dir(Path(b.a.file or b.a.cls.__name__), d.symbol, b.a.spec.symbol, d.tf, d.session, suffix)


@method("backtest.save")
def backtest_save(state: State, params: dict, emit) -> dict:
    """Write the run folder `guban run` writes: trades.csv, metrics.json, report.html (light charts, like the CLI's), with
    the trade Monte Carlo run on the settings the Run used."""
    from guban.charts import theme
    from guban.cli import _details, _mc_trade_info
    from guban.gui.tab_montecarlo import trade_monte_carlo
    from guban.metrics import jsonable
    from guban.report import write_report

    b = state.backtest
    if b is None:
        raise ValueError("Nothing to save: run the backtest first")
    d = loaded(state)
    a = b.a
    out = Path(params["out"]) if params.get("out") else run_folder(b, d)
    trade_charts = bool(params.get("trade_charts", False))
    data_from = d.bars.index[0].date().isoformat()
    mc = trade_monte_carlo(a.result.trades, _mc_trade_info(a.result.trades, a.bars), b.mc_settings, emit)
    emit("progress", message=f"Writing {out}…")
    path = a.result.write_trades_csv(out / "trades.csv", b.capital)
    first, last = a.bars.index[0], a.bars.index[a.hi - 1]
    meta_json = {"strategy": a.cls.__name__, "file": a.file, "symbol": d.symbol, "contract": a.spec.symbol, "tf": d.tf,
                 "session": d.session, "tz": d.tz, "fill_mode": a.fill_mode, "history_from": None,
                 "commission": a.commission, "slippage": a.slippage, "costs": b.costs_from, "data_from": data_from,
                 "capital": b.capital, "inputs": a.inputs, "first_bar": first, "last_bar": last, "bars": a.hi,
                 "dataset": DATASETS[a.dataset], "window": [a.bars.index[a.lo], last], "written_by": "Guban Studio",
                 "contract_rolls": b.rolls, "session_exposure": b.exposure}  # fmt: skip
    mc_json = {} if mc is None else {"monte_carlo": mc if isinstance(mc, dict) else mc.summary()}
    ev_json = {} if b.evaluation is None else {"evaluation": b.evaluation}
    (out / "metrics.json").write_text(json.dumps(jsonable({"run": meta_json, **b.metrics, **mc_json, **ev_json}), indent=2, default=str) + "\n", encoding="utf-8")
    meta = {
        "title": f"{a.cls.__name__} · {a.spec.symbol} {d.tf}",
        "subtitle": f"{DATASETS[a.dataset]} {a.bars.index[a.lo]:%Y-%m-%d} → {last:%Y-%m-%d} · {a.file} · Guban Studio",
        "details": {**_details(d.symbol, d.tf, d.session, None, a.spec, d.tz, a.fill_mode, a.commission, a.slippage, b.costs_from, data_from),
                    "Bars": f"{a.hi - a.lo:,}", "First / last bar": f"{a.bars.index[a.lo]:%Y-%m-%d %H:%M} / {last:%Y-%m-%d %H:%M}"},
        "inputs": a.inputs,
        "notes": notes(b),
    }  # fmt: skip
    if trade_charts:
        emit("progress", message=f"Drawing {len(a.result.trades):,} trade images…")
    with theme("light"):  # the CLI's report look, whatever the window's theme
        report = write_report(a.result, a.bars, out / "report.html", b.metrics, meta, monte_carlo=mc, trade_charts=trade_charts,
                              evaluation=b.evaluation)  # fmt: skip
    return {"folder": str(out), "files": [str(path), str(out / "metrics.json"), str(report)], "report": str(report.resolve())}


@method("backtest.open")
def backtest_open(state: State, params: dict, emit) -> dict:
    """Open a saved report.html in the default browser."""
    import webbrowser

    path = Path(str(params.get("path", "")))
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found: save the results first")
    webbrowser.open(path.resolve().as_uri())
    return {"opened": str(path)}
