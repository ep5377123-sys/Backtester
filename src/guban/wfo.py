"""Walk-forward analysis (R9, S5).

Windows are built on session dates, in whole months, end exclusive. Rolling: in-sample window k covers
[start + k*step, start + k*step + is); anchored: [start, start + k*step + is). Its out-of-sample window follows
it directly: [in-sample end, in-sample end + oos), cut off at the end of the data (then marked partial). Windows
continue while an out-of-sample window has at least one day of data. step >= oos, so no out-of-sample day is in
two windows, and no out-of-sample window overlaps its own in-sample window.

Each in-sample window picks the input combination ranked first by the metric, among runs with at least
`min_trades` trades entered in that window (guban.optimize.rank_results; ties keep grid order). Those inputs
trade the next out-of-sample window; a window with no eligible run sits out (flat, no trades).

Every run starts at the first bar of the data, so long-memory indicators are warm in every window. A backtest is
causal (bars after bar k never change what happened up to bar k), so a run through a window's end is the start
of a run over all the data: each grid combination is backtested once over all the data, and every window is
scored from that run (guban.metrics.window_metrics: trades by entry bar, equity rebased at the window start).
The stitched out-of-sample result is, per window, the chosen inputs' trades entered in the window, and their
equity over the window's bars, rebased to continue the curve. A trade still open at a window's end counts whole
in the window it was entered in; when the next window trades other inputs, the equity curve keeps only its part up
to the boundary (and picks up the next inputs' carried-in trade, if any). Those trades are counted in
`boundary_trades`; an intraday strategy has none.

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
from guban.engine.backtest import TRADE_COLUMNS, BacktestResult
from guban.metrics import compute_metrics, session_keys
from guban.optimize import DataSpec, _pool, _run_full, _run_one, _setup, pool_size, rank_results

YEAR_DAYS = 365.25


@dataclass(frozen=True)
class Window:
    no: int
    is_start: pd.Timestamp  # session dates; ends are exclusive
    is_end: pd.Timestamp
    oos_start: pd.Timestamp
    oos_end: pd.Timestamp  # cut off at the day after the last session
    partial: bool = False  # the out-of-sample window was cut off by the end of the data

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
) -> list[Window]:
    """In-sample / out-of-sample windows for data whose sessions run from `first` to `last` (session dates).

    start: the first in-sample day (default: the first day of the first whole month of data). step defaults to oos.
    """
    step = oos_months if step_months is None else step_months
    if min(is_months, oos_months, step) < 1:
        raise ValueError("window lengths and the step must be at least 1 month")
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
    while True:
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
    return windows


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
    results: pd.DataFrame  # one row per (window, combo): inputs, is_<metric>..., is_rank, oos_<metric>..., oos_rank
    summary: pd.DataFrame  # one row per window: dates, chosen combo and inputs, IS/OOS profit, WFE, OOS rank
    trades: pd.DataFrame  # stitched out-of-sample trades (engine columns; bar numbers index `bars`)
    equity: pd.Series  # stitched out-of-sample equity over the out-of-sample bars
    equity_low: pd.Series
    metrics: dict  # guban.metrics.compute_metrics of the stitched result
    pooled: dict  # windows that traded: {"is_net_per_year", "oos_net_per_year", "wfe"} (None when undefined)
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
        return BacktestResult(self.trades, self.equity, None, None, self.fill_mode, 0, self.equity_low, self.spec, self.costs)

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
    metric: str = "net_profit",
    min_trades: int = 0,
    workers: int | None = None,
    confirm: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> WalkForwardResult:
    """Walk-forward analysis of `grid`: optimize each in-sample window, trade the winner out of sample, stitch.

    Arguments as guban.optimize.optimize, plus the window layout (see make_windows). min_trades applies to the
    trades entered in each in-sample window. progress(done, total) reports the grid runs.
    """
    job, frames, combos = _setup(strategy, grid, spec, costs, data, bars, sub_bars, fill_mode, fixed, class_name, metric, confirm)
    t0 = time.perf_counter()
    frame = bars if bars is not None else data.load()[0]  # also builds missing bar caches before the workers read them
    sess = session_keys(frame.index, frame["session_date"] if "session_date" in frame.columns else None)
    if not sess.is_monotonic_increasing:
        raise ValueError("bars' session dates must not decrease")
    windows = make_windows(sess[0], sess[-1], is_months, oos_months, step_months, anchored, start)
    is_r = [(int(sess.searchsorted(w.is_start)), int(sess.searchsorted(w.is_end))) for w in windows]
    oos_r = [(int(sess.searchsorted(w.oos_start)), int(sess.searchsorted(w.oos_end))) for w in windows]
    job = dataclasses.replace(job, windows=tuple(is_r + oos_r))
    workers = pool_size(workers, len(combos))

    with _pool(job, frames, workers) as run:
        rows = run(_run_one, combos, progress)
        results, chosen = _rank_windows(windows, combos, rows, metric, min_trades)
        picked = sorted({c for c in chosen if c is not None})
        full = dict(zip(picked, run(_run_full, [combos[c - 1] for c in picked])))

    trades, equity, equity_low, boundary = _stitch(frame, oos_r, chosen, full)
    sessions = frame["session_date"].reindex(equity.index) if "session_date" in frame.columns else None
    metrics = compute_metrics(trades, equity, equity_low, sessions)
    summary, pooled = _summary(windows, grid, combos, results, chosen, metric)
    return WalkForwardResult(
        windows, grid, metric, min_trades, anchored, is_months, oos_months, oos_months if step_months is None else step_months,
        results, summary, trades, equity, equity_low, metrics, pooled, boundary,
        frame, spec, costs, job.fill_mode or ("magnifier" if sub_bars is not None else "ohlc"),
        workers, time.perf_counter() - t0, job.fixed,
    )  # fmt: skip


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


def _stitch(frame, oos_r, chosen, full) -> tuple[pd.DataFrame, pd.Series, pd.Series, int]:
    eq_parts, low_parts, tr_parts, pos = [], [], [], []
    offset, boundary = 0.0, 0
    for k, ((lo, hi), c) in enumerate(zip(oos_r, chosen)):
        pos.append(np.arange(lo, hi))
        if c is None:
            eq_parts.append(np.full(hi - lo, offset))
            low_parts.append(np.full(hi - lo, offset))
            continue
        tr, eq, low = full[c]
        base = eq[lo - 1] if lo > 0 else 0.0
        eq_parts.append(eq[lo:hi] - base + offset)
        low_parts.append(low[lo:hi] - base + offset)
        if hi > lo:
            offset = float(eq_parts[-1][-1])
        mine = tr[(tr["entry_bar"] >= lo) & (tr["entry_bar"] < hi)]
        tr_parts.append(mine)
        # trades open across a boundary where the curve switches inputs (or skips days)
        if k + 1 == len(chosen) or chosen[k + 1] != c or oos_r[k + 1][0] != hi:
            boundary += int((mine["exit_bar"] >= hi).sum())
        if k == 0 or chosen[k - 1] != c or oos_r[k - 1][1] != lo:
            boundary += int(((tr["entry_bar"] < lo) & (tr["exit_bar"] >= lo)).sum())
    idx = frame.index[np.concatenate(pos)]
    tr_parts = [t for t in tr_parts if len(t)]
    trades = pd.concat(tr_parts, ignore_index=True) if tr_parts else pd.DataFrame(columns=TRADE_COLUMNS)
    trades["trade_no"] = np.arange(1, len(trades) + 1)
    trades["cum_net_pnl"] = trades["net_pnl"].astype(float).cumsum()
    equity = pd.Series(np.concatenate(eq_parts), index=idx, name="equity")
    equity_low = pd.Series(np.concatenate(low_parts), index=idx, name="equity_low")
    return trades, equity, equity_low, boundary


def _summary(windows, grid, combos, results, chosen, metric) -> tuple[pd.DataFrame, dict]:
    rows = []
    sums = [0.0, 0.0, 0.0, 0.0]  # IS net, IS years, OOS net, OOS years (windows that traded)
    for w, c in zip(windows, chosen):
        t = results[results["window"] == w.no]
        row = {
            "window": w.no,
            "is_from": w.is_start, "is_to": w.is_end - pd.Timedelta(days=1),
            "oos_from": w.oos_start, "oos_to": w.oos_end - pd.Timedelta(days=1),
            "partial": w.partial,
            "eligible": int(t["is_rank"].notna().sum()),
            "combo": c,
        }  # fmt: skip
        r = t[t["combo"] == c].iloc[0] if c is not None else None
        row.update({k: (combos[c - 1][k] if c is not None else None) for k in grid})
        if r is None:
            row.update(is_metric=None, is_net_profit=None, is_trades=None, is_net_per_year=None, oos_net_profit=None,
                       oos_trades=None, oos_net_per_year=None, wfe=None, oos_rank=None)  # fmt: skip
        else:
            is_net, oos_net = float(r["is_net_profit"]), float(r["oos_net_profit"])
            is_py, oos_py = is_net / w.is_years, oos_net / w.oos_years
            row.update(
                is_metric=_num(r[f"is_{metric}"]), is_net_profit=is_net, is_trades=int(r["is_total_trades"]), is_net_per_year=is_py,
                oos_net_profit=oos_net, oos_trades=int(r["oos_total_trades"]), oos_net_per_year=oos_py,
                wfe=oos_py / is_py if is_py > 0 else None, oos_rank=None if pd.isna(r["oos_rank"]) else int(r["oos_rank"]),
            )  # fmt: skip
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
