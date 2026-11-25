"""Walk-forward analysis (R9, S5).

Windows are built on session dates, in whole months, end exclusive. Rolling: in-sample window k covers
[start + k*step, start + k*step + is); anchored: [start, start + k*step + is). Its out-of-sample window follows
it directly: [in-sample end, in-sample end + oos), cut off at the end of the data (then marked partial). Windows
continue while an out-of-sample window has at least one day of data. step >= oos, so no out-of-sample day is in
two windows, and no out-of-sample window overlaps its own in-sample window. `folds` caps the number of windows: exactly
that many, all whole (never a partial last window), else "not enough data"; fold_start() places them so the last one
ends at the end of the data.

Each in-sample window picks the input combination ranked first by the metric, among runs with at least
`min_trades` trades in that window (guban.optimize.rank_results; ties keep grid order). Those inputs trade the
next out-of-sample window; a window with no eligible run sits out (flat, no trades).

Every window, in sample or out of sample, is scored as its own trading window [lo, hi): the strategy runs from
the first bar of the data (so long-memory indicators are warm) but no order fills before the window's first bar,
so it starts flat, and a position still open after the window's last bar is closed at that bar's close as a
market fill named "Window End" (run_backtest's trade_from / flatten_at_end; guban.metrics.window_metrics). No
price after a window's end ever reaches its score, and no position opened before a window is carried into it.
Windows that start on the same bar share one run (the engine is causal: BacktestResult.window).

The stitched out-of-sample result chains the chosen inputs' trading windows: their trades, and their equity
continued from the previous window's final equity. Every window starts and ends flat, so the stitched final
equity equals the sum of the stitched trades' net profit (checked). Trades closed by a window's end are counted
in `boundary_trades`; a strategy that is flat at every session's end has none, while an always-in-the-market
strategy has one per window.

Walk-forward efficiency = annualized out-of-sample net profit / annualized in-sample net profit of the chosen
inputs (net per 365.25 calendar days of the window). Overall: pooled over the windows that traded,
(sum OOS net / sum OOS years) / (sum IS net / sum IS years). Undefined when in-sample profit is <= 0.
"""

from __future__ import annotations

import dataclasses
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import ContractSpec
from guban.engine import Costs, Strategy
from guban.engine.backtest import TRADE_COLUMNS, WINDOW_END, BacktestResult
from guban.metrics import compute_metrics, session_keys
from guban.optimize import DataSpec, _pool, _run_one, _run_window, _setup, pool_size, rank_results

YEAR_DAYS = 365.25


@dataclass(frozen=True)
class Window:
    no: int
    is_start: pd.Timestamp
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp
    partial: bool = False

    @property
    def is_years(self) -> float:
        return (self.is_end - self.is_start).days / YEAR_DAYS

    @property
    def oos_years(self) -> float:
        return (self.oos_end - self.oos_start).days / YEAR_DAYS


def make_windows(
    first: pd.Timestamp,
    last: pd.Timestamp,
    is_months: int = 12,
    oos_months: int = 3,
    step_months: int | None = None,
    anchored: bool = False,
    start: str | pd.Timestamp | None = None,
    folds: int | None = None,
) -> list[Window]:
    """In-sample / out-of-sample windows for data whose sessions run from `first` to `last` (session dates).

    start: the first in-sample day (default: the first day of the first whole month of data). step defaults to oos.
    folds: exactly this many windows, none partial (default: as many as the data holds, the last one maybe partial).
    """
    step = oos_months if step_months is None else step_months
    if min(is_months, oos_months, step) < 1:
        raise ValueError("window lengths and the step must be at least 1 month")
    if folds is not None and folds < 1:
        raise ValueError(f"folds must be at least 1, got {folds}")
    if step < oos_months:
        raise ValueError(f"a step of {step} months is shorter than the {oos_months}-month out-of-sample window, so windows would share out-of-sample days")
    first, last = pd.Timestamp(first).normalize(), pd.Timestamp(last).normalize()
    if start is None:
        start = first if first.day == 1 else first + pd.offsets.MonthBegin(1)
    start = pd.Timestamp(start).normalize()
    if start < first:
        raise ValueError(f"start {start:%Y-%m-%d} is before the first session in the data ({first:%Y-%m-%d})")
    end = last + pd.Timedelta(days=1)
    windows = []
    while folds is None or len(windows) < folds:
        k = len(windows)
        is_start = start if anchored else start + pd.DateOffset(months=k * step)
        is_end = start + pd.DateOffset(months=k * step + is_months)
        if is_end >= end:
            break
        oos_end = is_end + pd.DateOffset(months=oos_months)
        windows.append(Window(k + 1, is_start, is_end, is_end, min(oos_end, end), oos_end > end))
    if not windows:
        raise ValueError(
            f"not enough data: sessions {first:%Y-%m-%d} to {last:%Y-%m-%d} leave no out-of-sample days after "
            f"{is_months} in-sample months from {start:%Y-%m-%d}"
        )
    if folds is not None and (len(windows) < folds or windows[-1].partial):
        need = is_months + step * (folds - 1) + oos_months
        raise ValueError(
            f"not enough data: {folds} folds of {is_months} + {oos_months} months (step {step}) need {need} months from "
            f"{start:%Y-%m-%d}, to {start + pd.DateOffset(months=need):%Y-%m-%d}; the last session is {last:%Y-%m-%d}"
        )
    return windows


def fold_start(last: str | pd.Timestamp, is_months: int, oos_months: int, step_months: int | None, folds: int) -> pd.Timestamp:
    """The first in-sample day that makes `folds` windows end exactly at the day after the session `last` (rolling or
    anchored: either way the last out-of-sample window ends is + step * (folds - 1) + oos months after the start)."""
    step = oos_months if step_months is None else step_months
    return pd.Timestamp(last).normalize() + pd.Timedelta(days=1) - pd.DateOffset(months=is_months + step * (folds - 1) + oos_months)


@dataclass
class WalkForwardResult:
    windows: list[Window]
    grid: dict[str, list]
    metric: str
    min_trades: int
    anchored: bool
    is_months: int
    oos_months: int
    step_months: int
    results: pd.DataFrame
    summary: pd.DataFrame
    trades: pd.DataFrame
    equity: pd.Series
    equity_low: pd.Series
    equity_high: pd.Series
    bar_drawdown: pd.Series
    metrics: dict
    pooled: dict
    boundary_trades: int
    bars: pd.DataFrame
    spec: ContractSpec
    costs: Costs
    fill_mode: str
    workers: int
    seconds: float
    fixed: dict = field(default_factory=dict)

    @property
    def wfe(self) -> float | None:
        """Overall walk-forward efficiency (None when the in-sample profit is <= 0)."""
        return self.pooled["wfe"]

    @property
    def combinations(self) -> int:
        return int((self.results["window"] == 1).sum())

    def oos_result(self) -> BacktestResult:
        """The stitched out-of-sample result as a BacktestResult (for trades.csv and the report)."""
        return BacktestResult(
            self.trades, self.equity, None, None, self.fill_mode, 0, self.equity_low, self.spec, self.costs,
            equity_high=self.equity_high, bar_drawdown=self.bar_drawdown,
        )

    def write_csvs(self, out: str | Path) -> list[Path]:
        """windows.csv (the summary) and results.csv (every window x combination)."""
        out = Path(out)
        out.mkdir(parents=True, exist_ok=True)
        paths = [out / "windows.csv", out / "results.csv"]
        self.summary.to_csv(paths[0], index=False, lineterminator="\n", date_format="%Y-%m-%d")
        self.results.to_csv(paths[1], index=False, lineterminator="\n")
        return paths

    def summary_records(self) -> list[dict]:
        """summary rows as plain Python values (missing -> None), for JSON."""
        recs = self.summary.astype(object).to_dict("records")
        return [{k: None if _missing(v) else v for k, v in r.items()} for r in recs]


def _missing(v) -> bool:
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def walk_forward(
    strategy: type[Strategy] | str | Path,
    grid: dict[str, list],
    spec: ContractSpec,
    costs: Costs = Costs(),
    *,
    data: DataSpec | None = None,
    bars: pd.DataFrame | None = None,
    sub_bars: pd.DataFrame | None = None,
    fill_mode: str | None = None,
    fixed: dict | None = None,
    class_name: str | None = None,
    is_months: int = 12,
    oos_months: int = 3,
    step_months: int | None = None,
    anchored: bool = False,
    start: str | pd.Timestamp | None = None,
    folds: int | None = None,
    metric: str = "net_profit",
    min_trades: int = 0,
    workers: int | None = None,
    confirm: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> WalkForwardResult:
    """Walk-forward analysis of `grid`: optimize each in-sample window, trade the winner out of sample, stitch.

    Arguments as guban.optimize.optimize, plus the window layout (see make_windows). min_trades applies to the
    trades in each in-sample window. progress(done, total) reports the grid runs.
    """
    job, frames, combos = _setup(strategy, grid, spec, costs, data, bars, sub_bars, fill_mode, fixed, class_name, metric, confirm,
                                 min_trades, workers)
    t0 = time.perf_counter()
    frame = bars if bars is not None else data.load()[0]
    sess = session_keys(frame.index, frame["session_date"] if "session_date" in frame.columns else None)
    if not sess.is_monotonic_increasing:
        raise ValueError("bars' session dates must not decrease")
    windows = make_windows(sess[0], sess[-1], is_months, oos_months, step_months, anchored, start, folds)
    is_r = [(int(sess.searchsorted(w.is_start)), int(sess.searchsorted(w.is_end))) for w in windows]
    oos_r = [(int(sess.searchsorted(w.oos_start)), int(sess.searchsorted(w.oos_end))) for w in windows]
    job = dataclasses.replace(job, windows=tuple(is_r + oos_r))
    workers = pool_size(workers, len(combos))

    with _pool(job, frames, workers) as run:
        rows = run(_run_one, combos, progress)
        results, chosen = _rank_windows(windows, combos, rows, metric, min_trades)
        picked = [(k, c) for k, c in enumerate(chosen) if c is not None and oos_r[k][1] > oos_r[k][0]]
        runs = run(_run_window, [(combos[c - 1], *oos_r[k]) for k, c in picked])
    stitched = _stitch(frame, oos_r, dict(zip((k for k, _ in picked), runs)))
    trades, equity, equity_low, equity_high, bar_dd, boundary = stitched
    sessions = frame["session_date"].reindex(equity.index) if "session_date" in frame.columns else None
    metrics = compute_metrics(trades, equity, equity_low, sessions, equity_high=equity_high, bar_drawdown=bar_dd)
    summary, pooled = _summary(windows, grid, combos, results, chosen, metric)
    return WalkForwardResult(
        windows, grid, metric, min_trades, anchored, is_months, oos_months, oos_months if step_months is None else step_months,
        results, summary, trades, equity, equity_low, equity_high, bar_dd, metrics, pooled, boundary,
        frame, spec, costs, job.fill_mode or ("magnifier" if sub_bars is not None else "ohlc"),
        workers, time.perf_counter() - t0, job.fixed,
    )


def _rank_windows(windows, combos, rows, metric, min_trades) -> tuple[pd.DataFrame, list[int | None]]:
    n = len(windows)
    inputs = pd.DataFrame(combos)
    tables, chosen = [], []
    for k, w in enumerate(windows):
        ins = pd.DataFrame([r[k] for r in rows])
        oos = pd.DataFrame([r[n + k] for r in rows])
        t = pd.concat([inputs, ins.add_prefix("is_"), oos.add_prefix("oos_")], axis=1)
        t.insert(0, "combo", np.arange(1, len(t) + 1))
        t.insert(0, "window", w.no)
        t["is_rank"] = rank_results(ins, metric, min_trades)
        t["oos_rank"] = rank_results(oos, metric, 0)
        top = t.loc[t["is_rank"] == 1, "combo"]
        chosen.append(int(top.iat[0]) if len(top) else None)
        tables.append(t)
    return pd.concat(tables, ignore_index=True), chosen


def _stitch(frame, oos_r, runs: dict[int, BacktestResult]):
    """Chain the out-of-sample windows (runs[k] = window k's result; a window without one sits out, flat) into
    (trades, equity, equity_low, equity_high, bar_drawdown, boundary_trades)."""
    parts: dict[str, list] = {"eq": [], "low": [], "high": [], "dd": []}
    tr_parts, pos = [], []
    offset = 0.0
    for k, (lo, hi) in enumerate(oos_r):
        pos.append(np.arange(lo, hi))
        r = runs.get(k)
        if r is None:
            for key in ("eq", "low", "high"):
                parts[key].append(np.full(hi - lo, offset))
            parts["dd"].append(np.zeros(hi - lo))
            continue
        net = r.net_profit
        if not math.isclose(float(r.equity.iat[-1]), net, rel_tol=1e-9, abs_tol=1e-6):
            raise RuntimeError(f"out-of-sample window {k + 1}: final equity {r.equity.iat[-1]} != its trades' net profit {net}")
        parts["eq"].append(r.equity.to_numpy(float) + offset)
        parts["low"].append(r.equity_low.to_numpy(float) + offset)
        parts["high"].append(r.equity_high.to_numpy(float) + offset)
        parts["dd"].append(r.bar_drawdown.to_numpy(float))
        tr_parts.append(r.trades)
        offset += net
    idx = frame.index[np.concatenate(pos)]
    tr_parts = [t for t in tr_parts if len(t)]
    trades = pd.concat(tr_parts, ignore_index=True) if tr_parts else pd.DataFrame(columns=TRADE_COLUMNS)
    trades["trade_no"] = np.arange(1, len(trades) + 1)
    trades["cum_net_pnl"] = trades["net_pnl"].astype(float).cumsum()
    series = [pd.Series(np.concatenate(parts[key]), index=idx, name=name)
              for key, name in (("eq", "equity"), ("low", "equity_low"), ("high", "equity_high"), ("dd", "bar_drawdown"))]
    total = float(trades["net_pnl"].sum()) if len(trades) else 0.0
    final = float(series[0].iat[-1]) if len(idx) else 0.0
    if not math.isclose(final, total, rel_tol=1e-9, abs_tol=1e-6):
        raise RuntimeError(f"stitched out-of-sample equity {final} does not reconcile with its trades' net profit {total}")
    boundary = int((trades["exit_name"] == WINDOW_END).sum()) if len(trades) else 0
    return trades, *series, boundary


def _summary(windows, grid, combos, results, chosen, metric) -> tuple[pd.DataFrame, dict]:
    rows = []
    sums = [0.0, 0.0, 0.0, 0.0]
    for w, c in zip(windows, chosen):
        t = results[results["window"] == w.no]
        row = {
            "window": w.no,
            "is_from": w.is_start, "is_to": w.is_end - pd.Timedelta(days=1),
            "oos_from": w.oos_start, "oos_to": w.oos_end - pd.Timedelta(days=1),
            "partial": w.partial,
            "eligible": int(t["is_rank"].notna().sum()),
            "combo": c,
        }
        r = t[t["combo"] == c].iloc[0] if c is not None else None
        row.update({k: (combos[c - 1][k] if c is not None else None) for k in grid})
        if r is None:
            row.update(is_metric=None, is_net_profit=None, is_trades=None, is_net_per_year=None, oos_net_profit=None,
                       oos_trades=None, oos_net_per_year=None, wfe=None, oos_rank=None)
        else:
            is_net, oos_net = float(r["is_net_profit"]), float(r["oos_net_profit"])
            is_py, oos_py = is_net / w.is_years, oos_net / w.oos_years
            row.update(
                is_metric=_num(r[f"is_{metric}"]), is_net_profit=is_net, is_trades=int(r["is_total_trades"]), is_net_per_year=is_py,
                oos_net_profit=oos_net, oos_trades=int(r["oos_total_trades"]), oos_net_per_year=oos_py,
                wfe=oos_py / is_py if is_py > 0 else None, oos_rank=None if pd.isna(r["oos_rank"]) else int(r["oos_rank"]),
            )
            sums = [sums[0] + is_net, sums[1] + w.is_years, sums[2] + oos_net, sums[3] + w.oos_years]
        rows.append(row)
    summary = pd.DataFrame(rows)
    for col in ("combo", "is_trades", "oos_trades", "oos_rank"):
        summary[col] = summary[col].astype("Int64")
    is_py = sums[0] / sums[1] if sums[1] else None
    oos_py = sums[2] / sums[3] if sums[3] else None
    wfe = oos_py / is_py if is_py and is_py > 0 else None
    return summary, {"is_net_per_year": is_py, "oos_net_per_year": oos_py, "wfe": wfe}


def _num(v):
    v = v.item() if isinstance(v, np.generic) else v
    return None if isinstance(v, float) and math.isnan(v) else v
