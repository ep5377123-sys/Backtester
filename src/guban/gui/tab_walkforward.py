"""Walk Forward tab: guban.wfo.walk_forward on the Data tab's bars, with the Optimization tab's grid widget.

The default layout is rolling, 12-month in sample / 12-month out of sample, stepped 12 months, 4 folds, placed so the
last fold ends at the Data tab's end (wfo.fold_start); a Start date overrides. The windows are checked against the
Data tab's bars (wf.windows previews them in the main worker; wf.run checks again), so every fold is whole inside them.

wf.run and wf.save run in the "opt" worker like the optimization (Stop kills it): it rebuilds the bars from the Data
tab's form (tab_data.load_data) and calls walk_forward with the DataSpec `guban wfo` would build, so Save writes the
folder `guban wfo` writes for the same settings (cli.write_wfo_folder). wf.run also writes the stitched out-of-sample
trades to tab_montecarlo.WALKFORWARD_DIR (trades.csv, plus days.csv: each trade's exit session) for the Monte Carlo
tab, and wf.register tells the main worker they are there.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

from guban.gui.handlers import State, method
from guban.gui.tab_data import Loaded, load_data, loaded
from guban.gui.tab_strategy import coerce_inputs

DEFAULTS = {"is_months": 12, "oos_months": 12, "step_months": 12, "folds": 4, "anchored": False, "metric": "net_profit", "min_trades": 30}
CAPITAL = 1_000_000.0


def _int(params: dict, key: str, low: int = 1) -> int:
    v = params.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return DEFAULTS[key]
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ValueError(f"{key.replace('_', ' ')} must be a whole number, got {params.get(key)!r}") from None
    if v < low:
        raise ValueError(f"{key.replace('_', ' ')} must be at least {low}, got {v}")
    return v


def layout(params: dict) -> dict:
    """The window layout from the form: is/oos/step months, folds, anchored, start (None = end-aligned)."""
    start = str(params.get("start") or "").strip() or None
    return {"is_months": _int(params, "is_months"), "oos_months": _int(params, "oos_months"), "step_months": _int(params, "step_months"),
            "folds": _int(params, "folds"), "anchored": bool(params.get("anchored", DEFAULTS["anchored"])), "start": start}


def windows(d: Loaded, lay: dict) -> list:
    """The folds inside the Data tab's bars: whole, `folds` of them, ending at the bars' last session by default."""
    from guban.metrics import session_keys
    from guban.wfo import fold_start, make_windows

    sess = session_keys(d.bars.index, d.bars["session_date"] if "session_date" in d.bars.columns else None)
    start = lay["start"] or fold_start(sess[-1], lay["is_months"], lay["oos_months"], lay["step_months"], lay["folds"])
    if not lay["start"] and start < sess[0]:
        months = lay["is_months"] + lay["step_months"] * (lay["folds"] - 1) + lay["oos_months"]
        raise ValueError(f"not enough data: {lay['folds']} folds of {lay['is_months']} + {lay['oos_months']} months (step {lay['step_months']}) "
                         f"need {months} months, from {start:%Y-%m-%d}; the Data tab's bars start {sess[0]:%Y-%m-%d}: load more data "
                         "or use fewer / shorter folds")
    return make_windows(sess[0], sess[-1], lay["is_months"], lay["oos_months"], lay["step_months"], lay["anchored"], start, lay["folds"])


def _window_rows(ws) -> list[list]:
    return [[f"W{w.no}", f"{w.is_start:%Y-%m-%d} → {w.is_end - pd.Timedelta(days=1):%Y-%m-%d}",
             f"{w.oos_start:%Y-%m-%d} → {w.oos_end - pd.Timedelta(days=1):%Y-%m-%d}"] for w in ws]


@method("wf.options")
def wf_options(state: State, params: dict, emit) -> dict:
    from guban.optimize import RESULT_METRICS

    return {"metrics": [{"key": k, "label": label} for k, label, _, _ in RESULT_METRICS], "defaults": DEFAULTS}


@method("wf.windows")
def wf_windows(state: State, params: dict, emit) -> dict:
    """The folds the form describes on the loaded bars, for the preview table."""
    lay = layout(params)
    ws = windows(loaded(state), lay)
    return {"columns": ["Fold", "In sample", "Out of sample"], "rows": _window_rows(ws), "start": f"{ws[0].is_start:%Y-%m-%d}", "folds": len(ws)}


def _folds_chart(wf) -> str | None:
    """Per fold: in-sample vs out-of-sample net profit per year, and each varying input's chosen value."""
    from guban.charts import COLORS, MONEY, figure, styled, to_data_uri
    from guban.optimize import varying

    s = wf.summary
    names = varying(wf.grid)[:4]
    if s.empty:
        return None

    @styled
    def draw():
        fig, axes = figure(9.6, 2.8, cols=1 + len(names))
        axes = np.atleast_1d(axes)
        x = np.arange(len(s))
        labels = [f"W{w}" for w in s["window"]]
        ax = axes[0]
        ax.bar(x - 0.2, s["is_net_per_year"].astype(float).fillna(0), 0.4, color=COLORS["s1"], label="In sample / yr")
        ax.bar(x + 0.2, s["oos_net_per_year"].astype(float).fillna(0), 0.4, color=COLORS["s3"], label="Out of sample / yr")
        ax.axhline(0, color=COLORS["axis"], lw=0.8)
        ax.set_xticks(x, labels)
        ax.yaxis.set_major_formatter(MONEY)
        ax.legend(loc="upper left", fontsize=7)
        ax.set_title("Net profit per year", fontsize=9)
        for ax, name in zip(axes[1:], names):
            vals = list(wf.grid[name])
            pos = [vals.index(v) if not pd.isna(v) and v in vals else np.nan for v in s[name]]
            ax.plot(x, pos, "o-", color=COLORS["s2"])
            ax.set_xticks(x, labels)
            ax.set_yticks(range(len(vals)), [str(v) for v in vals])
            ax.set_ylim(-0.5, len(vals) - 0.5)
            ax.set_title(f"{name} chosen", fontsize=9)
        return to_data_uri(fig)

    return draw()


def _write_for_monte_carlo(wf) -> Path:
    from guban.gui.tab_montecarlo import WALKFORWARD_DIR

    WALKFORWARD_DIR.mkdir(parents=True, exist_ok=True)
    path = wf.oos_result().write_trades_csv(WALKFORWARD_DIR / "trades.csv", CAPITAL)
    tr, bars = wf.trades, wf.bars
    days = bars["session_date"].to_numpy()[tr["exit_bar"].to_numpy(int)] if "session_date" in bars.columns else pd.DatetimeIndex(tr["exit_time"]).date
    pd.DataFrame({"day": pd.to_datetime(days).strftime("%Y-%m-%d")}).to_csv(WALKFORWARD_DIR / "days.csv", index=False, lineterminator="\n")
    return path


@method("wf.run")
def wf_run(state: State, params: dict, emit) -> dict:
    """Runs in the "opt" worker."""
    from guban.cli import _costs
    from guban.config import get_contract
    from guban.engine import Costs
    from guban.gui.tab_optimize import _grid, _strategy_class, _throttled
    from guban.metrics import daily_equity, intraday_drawdown, session_keys
    from guban.optimize import METRICS, DataSpec
    from guban.report import _equity_pngs, _summary_table, _tiles
    from guban.wfo import walk_forward
    from guban.wfo_report import oos_bands, wfo_section

    t0 = time.perf_counter()
    emit("progress", message="Loading data…")
    d = load_data(params.get("data") or {}, emit)
    path, cls = _strategy_class(params.get("strategy") or {})
    grid = _grid(cls, params.get("grid"))
    fixed = coerce_inputs(cls, {k: v for k, v in (params.get("inputs") or {}).items() if k not in grid})
    lay = layout(params)
    ws = windows(d, lay)
    start = f"{ws[0].is_start:%Y-%m-%d}"
    metric = str(params.get("metric") or DEFAULTS["metric"])
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    min_trades = _int(params, "min_trades", low=0)
    workers = int(params["workers"]) if params.get("workers") not in (None, "") else None
    fill_mode = str(params.get("fill_mode") or "magnifier")
    spec = get_contract(d.symbol)
    num = lambda k: None if params.get(k) in (None, "") else float(params[k])
    commission, slippage, costs_from = _costs(spec, num("commission"), num("slippage"))
    data = DataSpec(d.symbol, d.tf, d.session, d.tz, fill_mode=fill_mode, data_dir=str(_data_dir()), data_from=d.first.isoformat(),
                    extra_columns=tuple(cls.extra_columns))
    n = int(np.prod([len(v) for v in grid.values()]))
    wf = walk_forward(str(path), grid, spec, Costs(commission=commission, slippage=slippage), data=data, fixed=fixed, class_name=cls.__name__,
                      is_months=lay["is_months"], oos_months=lay["oos_months"], step_months=lay["step_months"], anchored=lay["anchored"],
                      start=start, folds=lay["folds"], metric=metric, min_trades=min_trades, workers=workers, confirm=bool(params.get("confirm")),
                      progress=_throttled(emit, "Walk-forward runs"))
    t1 = time.perf_counter()
    emit("progress", message="Drawing charts…")
    _write_for_monte_carlo(wf)
    sessions = wf.bars["session_date"].reindex(wf.equity.index) if "session_date" in wf.bars.columns else None
    daily = daily_equity(wf.equity, sessions)
    dd = intraday_drawdown(wf.equity, wf.equity_low, wf.equity_high, wf.bar_drawdown)
    eq, ddp = _equity_pngs(daily, dd.groupby(session_keys(dd.index, sessions)).min(), oos_bands(wf))
    s = wf.summary
    chosen = s[s["combo"].notna()]
    last_inputs = {g: str(chosen.iloc[-1][g]) for g in grid} if len(chosen) else None
    state.walkforward = {"wf": wf, "path": path, "cls": cls, "d": d, "grid": grid, "fixed": fixed, "folds": lay["folds"], "n": n,
                         "costs": (commission, slippage, costs_from), "fill_mode": fill_mode, "workers": workers}
    t2 = time.perf_counter()
    return {
        "strategy": cls.__name__,
        "tz": d.tz,
        "combinations": n,
        "workers": wf.workers,
        "start": start,
        "windows": _window_rows(wf.windows),
        "costs": f"commission ${commission:,.2f}, slippage ${slippage:,.2f} per contract round trip ({costs_from}); {fill_mode} fills",
        "seconds": {"walk_forward": round(t1 - t0, 1), "charts": round(t2 - t1, 1)},
        "trades": len(wf.trades),
        "html": {"section": wfo_section(wf), "tiles": _tiles(wf.metrics), "summary": _summary_table(wf.metrics)},
        "charts": {"equity": eq, "drawdown": ddp, "folds": _folds_chart(wf)},
        "last_inputs": last_inputs,
    }


def _data_dir() -> Path:
    from guban.gui import tab_data

    return tab_data.DATA_DIR


@method("wf.register")
def wf_register(state: State, params: dict, emit) -> dict:
    """Main worker: the opt worker's walk-forward wrote its out-of-sample trades for the Monte Carlo tab."""
    state.walkforward = {"strategy": str(params.get("strategy") or ""), "tz": str(params.get("tz") or "")}
    return {"registered": True}


@method("wf.save")
def wf_save(state: State, params: dict, emit) -> dict:
    """Runs in the "opt" worker: the folder `guban wfo` writes for the same settings."""
    from guban.charts import theme
    from guban.cli import run_dir, write_wfo_folder

    w = state.walkforward
    if w is None or "wf" not in w:
        raise ValueError("Nothing to save: run the walk-forward first")
    d, cls, path = w["d"], w["cls"], w["path"]
    out = Path(params["out"]) if params.get("out") else run_dir(path, d.symbol, d.symbol, d.tf, d.session, "_wfo")
    commission, slippage, costs_from = w["costs"]
    trade_charts = bool(params.get("trade_charts", False))
    emit("progress", message=f"Writing {out}…")
    with theme("light"):
        trades, csvs, html_path = write_wfo_folder(
            w["wf"], out, cls=cls, strategy_file=path, symbol=d.symbol, spec=w["wf"].spec, tf=d.tf, session=d.session, tz=d.tz,
            fill_mode=w["fill_mode"], history_from=None, commission=commission, slippage=slippage, costs_from=costs_from,
            data_from=d.first.isoformat(), fixed=w["fixed"], capital=CAPITAL, combinations=w["n"], folds=w["folds"],
            trade_charts=trade_charts, workers=w["workers"], written_by="Guban Studio")
    return {"folder": str(out), "files": [str(trades), *map(str, csvs), str(out / "wfo.json"), str(html_path)], "report": str(html_path.resolve())}
