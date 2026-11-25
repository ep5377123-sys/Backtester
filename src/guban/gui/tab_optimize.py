"""Optimization tab: grid-search the strategy's inputs on the in-sample window, then rerun the top N combinations on
the out-of-sample window and compare (equity curves, Sharpe and drawdown histograms, a table of both).

optimize.run runs in its own worker process (Electron's "opt" worker), so Stop can end it, pool and all, without
losing the main worker's data: it rebuilds the bars and windows from the Data tab's form (tab_data.load_data) and
re-imports the strategy file. Scoring is guban.optimize.optimize(window=in sample) and window_runs: each window is its
own run, flat at its start and closed at its end, warm-up from the first loaded bar.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import numpy as np
import pandas as pd

from guban.gui.handlers import State, method
from guban.gui.tab_data import DATA_DIR, load_data
from guban.gui.tab_strategy import coerce_inputs

DEFAULTS = {"metric": "net_profit", "min_trades": 30, "top_n": 100}
TABLE_METRICS = [("net_profit", "net"), ("profit_factor", "PF"), ("sharpe_daily", "Sharpe"), ("max_drawdown_intraday", "max DD"), ("total_trades", "trades")]


def _grid(cls, specs: dict) -> dict[str, list]:
    from guban.optimize import parse_grid

    specs = {k: str(v).strip() for k, v in (specs or {}).items() if str(v).strip()}
    if not specs:
        raise ValueError("Tick at least one input to optimize and give its values, e.g. 0.4:1.0:0.1 or 1,2,3")
    return parse_grid([f"{k}={v}" for k, v in specs.items()], cls.inputs)


def _strategy_class(params: dict):
    from guban.engine.strategy import load_strategy_class

    path = Path(str(params.get("file", "")))
    if not path.is_file():
        raise FileNotFoundError(f"strategy file {path} not found: load a strategy on the Strategy tab")
    return path, load_strategy_class(path, params.get("class") or None)


@method("optimize.options")
def optimize_options(state: State, params: dict, emit) -> dict:
    from guban.optimize import MAX_COMBINATIONS, RESULT_METRICS

    return {"metrics": [{"key": k, "label": label} for k, label, _, _ in RESULT_METRICS], "defaults": DEFAULTS,
            "cpus": os.cpu_count() or 1, "max_combinations": MAX_COMBINATIONS}


@method("optimize.count")
def optimize_count(state: State, params: dict, emit) -> dict:
    """Combinations of a grid (and each input's values), for the form's live count."""
    from guban.gui.tab_strategy import _strategy

    grid = _grid(_strategy(state), params.get("grid"))
    return {"count": int(np.prod([len(v) for v in grid.values()])), "values": {k: [str(x) for x in v] for k, v in grid.items()}}


def _throttled(emit, label: str):
    last = [0.0]

    def progress(done: int, total: int) -> None:
        now = time.perf_counter()
        if done == total or now - last[0] > 0.25:
            last[0] = now
            emit("progress", message=f"{label}: {done:,} / {total:,}", done=done, total=total, stage=label)

    return progress


def _daily(res, bars: pd.DataFrame, lo: int, hi: int) -> pd.Series:
    from guban.metrics import daily_equity

    w = bars.iloc[lo:hi]
    return daily_equity(res.equity, w["session_date"].reindex(res.equity.index) if "session_date" in w.columns else None)


def _charts(top: list[dict], d, metric_label: str) -> dict:
    from guban.charts import styled

    return {"equity": styled(_equity_chart)(top, d), "hists": styled(_hist_chart)(top), "scatter": styled(_scatter_chart)(top, metric_label)}


def _equity_chart(top: list[dict], d) -> str:
    from matplotlib.collections import LineCollection
    from matplotlib.lines import Line2D

    from guban.charts import COLORS, MONEY, figure, to_data_uri

    fig, ax = figure(9.6, 3.9)
    (a, b), (c, e) = d.windows["is"], d.windows["oos"]
    follows = c > b
    for key, color in (("is", COLORS["s1"]), ("oos", COLORS["s3"])):
        segs = []
        for t in reversed(top):
            s = t[f"{key}_daily"]
            if not len(s):
                continue
            y = s.to_numpy(float) + (t["is_daily"].iat[-1] if key == "oos" and follows and len(t["is_daily"]) else 0.0)
            x = pd.to_datetime([k.strftime("%Y-%m-%d") for k in s.index]).to_numpy().astype("datetime64[D]").astype(float)
            segs.append(np.c_[x, y])
        if segs:
            ax.add_collection(LineCollection(segs, colors=color, alpha=0.35, linewidths=0.8))
    for key, color in (("is", COLORS["s1"]), ("oos", COLORS["s3"])):
        s = top[0][f"{key}_daily"]
        if len(s):
            y = s.to_numpy(float) + (top[0]["is_daily"].iat[-1] if key == "oos" and follows and len(top[0]["is_daily"]) else 0.0)
            ax.plot(pd.to_datetime([k.strftime("%Y-%m-%d") for k in s.index]).to_numpy(), y, color=color, lw=2.0)
    ax.autoscale_view()
    import matplotlib.dates as mdates

    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.yaxis.set_major_formatter(MONEY)
    ax.legend(handles=[Line2D([], [], color=COLORS["s1"], label="In sample"), Line2D([], [], color=COLORS["s3"], label="Out of sample"),
                       Line2D([], [], color=COLORS["ink2"], lw=2, label="Rank 1 (bold)")], loc="upper left", ncols=3)
    ax.set_title(f"Cumulative net profit, top {len(top)} in-sample combinations" + ("" if follows else " (each window from 0)"))
    return to_data_uri(fig)


def _hist_chart(top: list[dict]) -> str:
    from guban.charts import COLORS, MONEY, figure, to_data_uri

    fig, (ax1, ax2) = figure(9.6, 3.0, cols=2)
    for ax, key, label, fmt in ((ax1, "sharpe_daily", "Sharpe (daily)", None), (ax2, "max_drawdown_intraday", "Max drawdown (USD)", MONEY)):
        vals = {w: np.array([t[w][key] for t in top], float) for w in ("is", "oos")}
        vals = {w: v[np.isfinite(v)] for w, v in vals.items()}
        both = np.concatenate(list(vals.values()))
        bins = np.histogram_bin_edges(both, bins=30) if len(both) else 30
        for w, color, name in (("is", COLORS["s1"], "In sample"), ("oos", COLORS["s3"], "Out of sample")):
            v = vals[w]
            if len(v):
                ax.hist(v, bins=bins, color=color, alpha=0.6, label=f"{name} (median {np.median(v):,.2f})".replace("-", "−"))
        if fmt:
            ax.xaxis.set_major_formatter(fmt)
        ax.set_xlabel(label)
        ax.set_ylabel("Combinations")
        ax.legend(loc="upper left", fontsize=7)
    return to_data_uri(fig)


def _scatter_chart(top: list[dict], metric_label: str) -> str:
    from guban.charts import COLORS, MONEY, figure, to_data_uri

    fig, ax = figure(4.8, 3.0)
    x = np.array([t["is"]["net_profit"] for t in top], float)
    y = np.array([t["oos"]["net_profit"] for t in top], float)
    ax.scatter(x, y, s=14, color=COLORS["s3"], alpha=0.8)
    ax.scatter(x[:1], y[:1], s=50, color=COLORS["s2"], zorder=3, label="Rank 1")
    ax.axhline(0, color=COLORS["axis"], lw=0.8)
    ax.xaxis.set_major_formatter(MONEY)
    ax.yaxis.set_major_formatter(MONEY)
    ax.set_xlabel("In-sample net profit")
    ax.set_ylabel("Out-of-sample net profit")
    ax.legend(loc="upper left", fontsize=7)
    ax.set_title(f"Ranked by {metric_label}", fontsize=9)
    return to_data_uri(fig)


@method("optimize.run")
def optimize_run(state: State, params: dict, emit) -> dict:
    from guban.cli import _costs
    from guban.config import get_contract
    from guban.engine import Costs
    from guban.metrics import backtest_metrics
    from guban.optimize import METRICS, DataSpec, flatten_metrics, optimize, window_runs

    t0 = time.perf_counter()
    emit("progress", message="Loading data…")
    d = load_data(params.get("data") or {}, emit)
    path, cls = _strategy_class(params.get("strategy") or {})
    grid = _grid(cls, params.get("grid"))
    fixed = coerce_inputs(cls, {k: v for k, v in (params.get("inputs") or {}).items() if k not in grid})
    metric = str(params.get("metric") or DEFAULTS["metric"])
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}")
    min_trades = int(params.get("min_trades") if params.get("min_trades") not in (None, "") else DEFAULTS["min_trades"])
    top_n = int(params.get("top_n") or DEFAULTS["top_n"])
    workers = int(params["workers"]) if params.get("workers") not in (None, "") else None
    fill_mode = str(params.get("fill_mode") or "magnifier")
    spec = get_contract(d.symbol)
    num = lambda k: None if params.get(k) in (None, "") else float(params[k])
    commission, slippage, costs_from = _costs(spec, num("commission"), num("slippage"))
    costs = Costs(commission=commission, slippage=slippage)
    (lo_is, hi_is), (lo_oos, hi_oos) = d.bounds("is"), d.bounds("oos")
    data = DataSpec(d.symbol, d.tf, d.session, d.tz, fill_mode=fill_mode, data_dir=str(DATA_DIR), data_from=d.first.isoformat(),
                    extra_columns=tuple(cls.extra_columns))
    common = dict(data=data, fixed=fixed, class_name=cls.__name__, workers=workers)
    opt = optimize(str(path), grid, spec, costs, metric=metric, min_trades=min_trades, confirm=bool(params.get("confirm")),
                   progress=_throttled(emit, "In sample"), window=(lo_is, hi_is), **common)
    t1 = time.perf_counter()
    ranked = opt.ranked()
    ranked = ranked[ranked["rank"].notna()].head(top_n)
    combos = [{k: (v.item() if isinstance(v, np.generic) else v) for k, v in row.items()} for row in ranked[list(grid)].to_dict("records")]
    runs = window_runs(str(path), [(c, lo, hi) for c in combos for lo, hi in ((lo_is, hi_is), (lo_oos, hi_oos))], spec, costs,
                       progress=_throttled(emit, "Top combinations, in and out of sample"), **common)
    t2 = time.perf_counter()
    emit("progress", message="Drawing charts…")
    top = []
    for k, c in enumerate(combos):
        r_is, r_oos = runs[2 * k], runs[2 * k + 1]
        top.append({"rank": k + 1, "inputs": c,
                    "is": flatten_metrics(backtest_metrics(r_is, d.bars.iloc[lo_is:hi_is])),
                    "oos": flatten_metrics(backtest_metrics(r_oos, d.bars.iloc[lo_oos:hi_oos])),
                    "is_daily": _daily(r_is, d.bars, lo_is, hi_is), "oos_daily": _daily(r_oos, d.bars, lo_oos, hi_oos)})
    images = _charts(top, d, METRICS[metric][0]) if top else {}
    t3 = time.perf_counter()
    columns = ["rank", *grid, *(f"IS {lab}" for _, lab in TABLE_METRICS), *(f"OOS {lab}" for _, lab in TABLE_METRICS)]
    rnd = lambda v: None if v is None or (isinstance(v, float) and not np.isfinite(v)) else round(float(v), 2)
    rows = [[t["rank"], *(t["inputs"][g] for g in grid), *(rnd(t["is"][k]) for k, _ in TABLE_METRICS), *(rnd(t["oos"][k]) for k, _ in TABLE_METRICS)]
            for t in top]
    state.optimization = {"opt": opt, "top": top, "grid": grid, "d": d, "cls": cls, "path": path, "fixed": fixed, "metric": metric,
                          "costs": (commission, slippage, costs_from), "fill_mode": fill_mode, "top_n": top_n}
    w = {k: [x.isoformat() for x in d.windows[k]] for k in ("is", "oos")}
    return {
        "combinations": len(opt.results),
        "ranked": int(opt.results["rank"].notna().sum()),
        "top": len(top),
        "workers": opt.workers,
        "windows": w,
        "costs": f"commission ${commission:,.2f}, slippage ${slippage:,.2f} per contract round trip ({costs_from}); {fill_mode} fills",
        "seconds": {"in_sample": round(t1 - t0, 1), "top": round(t2 - t1, 1), "charts": round(t3 - t2, 1)},
        "table": {"columns": columns, "rows": rows, "inputs": [{g: str(t["inputs"][g]) for g in grid} for t in top]},
        "charts": images,
    }


@method("optimize.save")
def optimize_save(state: State, params: dict, emit) -> dict:
    """results.csv (every in-sample run, ranked), oos.csv (the top N in and out of sample), optimize.json."""
    from guban.cli import run_dir
    from guban.metrics import jsonable

    o = state.optimization
    if o is None:
        raise ValueError("Nothing to save: run the optimization first")
    d = o["d"]
    out = Path(params["out"]) if params.get("out") else run_dir(o["path"], d.symbol, d.symbol, d.tf, d.session, "_opt_is_oos")
    results = o["opt"].write_csv(out / "results.csv")
    rows = [{"rank": t["rank"], **t["inputs"], **{f"is_{k}": v for k, v in t["is"].items()}, **{f"oos_{k}": v for k, v in t["oos"].items()}}
            for t in o["top"]]
    oos = out / "oos.csv"
    pd.DataFrame(rows).to_csv(oos, index=False, lineterminator="\n")
    commission, slippage, costs_from = o["costs"]
    meta = {"strategy": o["cls"].__name__, "file": o["path"].as_posix(), "symbol": d.symbol, "tf": d.tf, "session": d.session, "tz": d.tz,
            "data_from": d.first, "data_to": d.last, "in_sample": d.windows["is"], "out_of_sample": d.windows["oos"],
            "grid": o["grid"], "fixed": o["fixed"], "metric": o["metric"], "min_trades": o["opt"].min_trades, "top_n": o["top_n"],
            "fill_mode": o["fill_mode"], "commission": commission, "slippage": slippage, "costs": costs_from,
            "combinations": len(o["opt"].results), "workers": o["opt"].workers, "seconds": o["opt"].seconds,
            "best_in_sample": o["opt"].best, "written_by": "Guban Studio"}
    (out / "optimize.json").write_text(json.dumps(jsonable({"run": meta, "top": rows}), indent=2, default=str) + "\n", encoding="utf-8")
    return {"folder": str(out), "files": [str(results), str(oos), str(out / "optimize.json")]}
