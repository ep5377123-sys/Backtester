"""Parameter grid optimizer (R8, S4).

Grid spec, one per input: name=start:stop[:step] (inclusive, step 1 when omitted), name=a,b,c, or name=value.
Ranges are stepped in decimal, so 0.4:1.0:0.1 gives exactly 0.4, 0.5, ... 1.0. Values take the type of the
input's default: an int input rejects 0.5, a float input turns 1 into 1.0 (numbers must be finite), a bool input
takes true / false only, and a str input takes each token as written (quotes optional; no ranges). Grids and fixed
inputs passed to optimize() / walk_forward() directly are checked against the defaults the same way.

Every combination is run in a process pool sized to the CPU cores. Each worker imports the strategy file and
loads the bars once (pool initializer), then runs its share of combinations. Results come back in grid order
whatever the completion order, so the results table is deterministic.

Ranking: by one metric (default net profit), best first, over the runs with at least `min_trades` trades; ties
keep grid order. Runs below the filter stay in the table unranked.
"""

from __future__ import annotations

import ast
import inspect
import itertools
import math
import numbers
import os
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import ContractSpec
from guban.engine import BacktestResult, Costs, Strategy, run_backtest
from guban.engine.strategy import load_strategy_class
from guban.metrics import backtest_metrics, empty_metrics, window_metrics

MAX_COMBINATIONS = 5000

# Per-run columns of the results table: (key, label, format, higher is better). Keys are guban.metrics keys
# from the "all" trade stats or the "account" figures. Drawdowns and losses are <= 0, so higher is better.
RESULT_METRICS = [
    ("net_profit", "Net profit", "usd", True),
    ("profit_factor", "Profit factor", "ratio", True),
    ("total_trades", "Trades", "int", True),
    ("percent_profitable", "Percent profitable", "pct", True),
    ("avg_trade", "Avg trade", "usd", True),
    ("max_drawdown_intraday", "Max drawdown", "usd", True),
    ("max_drawdown_close_to_close", "Max close-to-close drawdown", "usd", True),
    ("return_on_account", "Return on account", "pct", True),
    ("sharpe_daily", "Sharpe (daily)", "ratio", True),
    ("max_consecutive_losers", "Max consecutive losers", "int", False),
    ("largest_losing_trade", "Largest losing trade", "usd", True),
]
METRICS = {key: (label, kind, hib) for key, label, kind, hib in RESULT_METRICS}


class TooManyCombinations(ValueError):
    """The grid is larger than MAX_COMBINATIONS and the caller did not confirm it."""


# --- grid spec -----------------------------------------------------------------------------------


def parse_grid(specs: list[str], defaults: dict) -> dict[str, list]:
    """Parse grid specs ("name=0.4:1.0:0.1", "name=1:4", "name=a,b,c", "name=v") into {name: [values]}."""
    grid: dict[str, list] = {}
    for spec in specs:
        name, sep, raw = spec.partition("=")
        name, raw = name.strip(), raw.strip()
        if not sep or not raw or name not in defaults:
            raise ValueError(f"grid {spec!r}: use name=start:stop[:step], name=a,b,c or name=value with one of {sorted(defaults)}")
        if name in grid:
            raise ValueError(f"grid {spec!r}: {name} is given twice")
        numeric = _is_number(defaults[name])
        values = _range(raw, spec) if ":" in raw and numeric else raw.split(",")
        values = [_coerce(v, defaults[name], spec) for v in values]
        if len({repr(v) for v in values}) != len(values):
            raise ValueError(f"grid {spec!r}: repeated values {values}")
        grid[name] = values
    return grid


def _range(raw: str, spec: str) -> list[Decimal]:
    parts = [p.strip() for p in raw.split(":")]
    if len(parts) not in (2, 3):
        raise ValueError(f"grid {spec!r}: a range is start:stop or start:stop:step")
    try:
        start, stop, step = (Decimal(p) for p in (*parts, "1")[:3])
    except InvalidOperation:
        raise ValueError(f"grid {spec!r}: range bounds and step must be numbers") from None
    if step <= 0 or stop < start:
        raise ValueError(f"grid {spec!r}: need stop >= start and step > 0")
    return [start + k * step for k in range(int((stop - start) / step) + 1)]


def _literal(raw: str):
    raw = raw.strip()
    try:
        return ast.literal_eval(raw)
    except (ValueError, SyntaxError):
        return raw


def _is_number(v) -> bool:
    return isinstance(v, numbers.Real) and not isinstance(v, (bool, np.bool_))


def _coerce(v: str | Decimal, default, spec: str, what: str = "grid"):
    """One grid value (a raw token, or a Decimal from a range) -> the type of the input's default."""
    if isinstance(v, Decimal) and not _is_number(default):
        raise ValueError(f"{what} {spec!r}: a range needs a numeric input (default {default!r})")
    if isinstance(default, (bool, np.bool_)):
        token = v.strip().lower()
        if token not in ("true", "false"):
            raise ValueError(f"{what} {spec!r}: {v.strip()!r} is not true or false (the default is {default!r})")
        return token == "true"
    if isinstance(default, str):
        token = v.strip()
        return token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"" else token
    if not _is_number(default):
        return _literal(v)
    raw_v = v.strip() if isinstance(v, str) else v
    if isinstance(v, str):
        try:
            v = Decimal(v.strip())
        except InvalidOperation:
            raise ValueError(f"{what} {spec!r}: {v.strip()!r} is not a number") from None
    if not v.is_finite():
        raise ValueError(f"{what} {spec!r}: {v} is not a finite number")
    v = int(v) if v == v.to_integral_value() else float(v)
    if isinstance(default, float):
        try:
            v = float(v)
        except OverflowError:
            v = math.inf
        if not math.isfinite(v):
            raise ValueError(f"{what} {spec!r}: {raw_v} is not a finite number")
        return v
    if isinstance(v, float):
        if not v.is_integer():
            raise ValueError(f"{what} {spec!r}: {v!r} is not a whole number (the default is {default!r})")
        v = int(v)
    return v


def combinations(grid: dict[str, list]) -> list[dict]:
    """Every combination, in grid order (the last input varies fastest)."""
    return [dict(zip(grid, values)) for values in itertools.product(*grid.values())]


# --- data and workers ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DataSpec:
    """Where a run's bars come from; load() is what `guban run` does: strategy bars, the 1-minute sub-bars for
    magnifier mode, and --history-from bars spliced in ahead of the symbol's first bar."""

    symbol: str
    tf: str = "5m"
    session: str = "rth"
    tz: str = "America/New_York"
    history_from: str | None = None
    fill_mode: str = "magnifier"
    data_dir: str = "data"

    def load(self) -> tuple[pd.DataFrame, pd.DataFrame | None]:
        from guban.data.resample import load_bars, splice_history

        d = Path(self.data_dir)
        bars = load_bars(self.symbol, self.tf, self.session, d, tz=self.tz)
        sub = load_bars(self.symbol, "1m", self.session, d, tz=self.tz) if self.fill_mode == "magnifier" else None
        if self.history_from:
            bars = splice_history(bars, load_bars(self.history_from, self.tf, self.session, d, tz=self.tz))
            if sub is not None:
                sub = splice_history(sub, load_bars(self.history_from, "1m", self.session, d, tz=self.tz))
        return bars, sub


@dataclass
class _Job:
    strategy_file: str
    class_name: str
    spec: ContractSpec
    costs: Costs
    fill_mode: str | None
    fixed: dict
    data: DataSpec | None  # workers load bars from here, or get `frames` passed in
    windows: tuple[tuple[int, int], ...] | None = None  # walk-forward: score these [lo, hi) bar ranges, not the whole run


_WORKER: dict = {}


def _init_worker(job: _Job, frames: tuple[pd.DataFrame, pd.DataFrame | None] | None) -> None:
    bars, sub = frames if frames is not None else job.data.load()
    _WORKER.update(job=job, cls=load_strategy_class(job.strategy_file, job.class_name), bars=bars, sub=sub)


def _backtest(params: dict, stop: int | None = None, **window):
    """One run with `params` over the worker's bars (the first `stop` of them), with run_backtest's `window` options."""
    job, bars = _WORKER["job"], _WORKER["bars"]
    try:
        strategy = _WORKER["cls"](**job.fixed, **params)
        bars = bars if stop is None else bars.iloc[:stop]
        return run_backtest(strategy, bars, job.spec, job.costs, sub_bars=_WORKER["sub"], fill_mode=job.fill_mode, **window)
    except Exception as e:
        raise RuntimeError(f"run with inputs {params} failed: {e}") from e


def _run_one(params: dict) -> dict | list[dict]:
    """Metrics of one run: of the whole run, or (walk-forward) one set per trading window [lo, hi) in job.windows,
    each scored as its own run: flat at lo, flattened at hi (guban.metrics.window_metrics). Windows that start on the
    same bar share one run."""
    job, bars = _WORKER["job"], _WORKER["bars"]
    if job.windows is None:
        return flatten_metrics(backtest_metrics(_backtest(params), bars))
    ends: dict[int, set[int]] = {}
    for lo, hi in job.windows:
        if hi > lo:
            ends.setdefault(lo, set()).add(hi)
    scored = {}
    for lo, his in ends.items():
        res = _backtest(params, max(his), trade_from=lo, window_ends=his)
        scored.update({(lo, hi): flatten_metrics(window_metrics(res, bars, lo, hi)) for hi in his})
    empty = flatten_metrics(empty_metrics())
    return [scored.get(w, empty) for w in job.windows]


def _run_window(item: tuple[dict, int, int]) -> BacktestResult:
    """The trading window [lo, hi) of a run with `params` (walk-forward stitching): BacktestResult.window(lo, hi)."""
    params, lo, hi = item
    res = _backtest(params, hi, trade_from=lo, window_ends={hi})
    out = res.window(lo, hi)
    out.strategy = None  # workers send results back by pickling; the strategy instance is not needed
    return out


@contextmanager
def _pool(job: _Job, frames, workers: int):
    """Yield run(fn, items, progress=None) -> list of fn(item) in item order, computed by `workers` processes that
    each loaded the strategy and the bars once (workers=1: in this process)."""

    def collect(results, total, progress):
        out = []
        for r in results:
            out.append(r)
            if progress:
                progress(len(out), total)
        return out

    if workers == 1:
        _init_worker(job, frames)
        try:
            yield lambda fn, items, progress=None: collect(map(fn, items), len(items), progress)
        finally:
            _WORKER.clear()
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker, initargs=(job, frames)) as ex:
            yield lambda fn, items, progress=None: collect(
                ex.map(fn, items, chunksize=max(1, len(items) // (workers * 4))), len(items), progress
            )


def flatten_metrics(m: dict) -> dict:
    """guban.metrics.compute_metrics output -> {key: value} for RESULT_METRICS (None -> NaN; inf is kept)."""
    out = {}
    for key, _, _, _ in RESULT_METRICS:
        v = m["account"][key] if key in m["account"] else m["all"][key]
        out[key] = math.nan if v is None else v
    return out


# --- optimize ------------------------------------------------------------------------------------


@dataclass
class OptimizeResult:
    grid: dict[str, list]
    results: pd.DataFrame  # one row per combination in grid order: combo, inputs, RESULT_METRICS, rank
    metric: str
    min_trades: int
    workers: int
    seconds: float
    fixed: dict = field(default_factory=dict)

    def ranked(self) -> pd.DataFrame:
        """Ranked runs first (best first), then the runs the min-trades filter left out, in grid order."""
        r = self.results
        return pd.concat([r[r["rank"].notna()].sort_values("rank"), r[r["rank"].isna()]])

    @property
    def best(self) -> dict | None:
        """Inputs of the rank-1 run (None when no run passed the filter)."""
        top = self.results[self.results["rank"] == 1]
        return None if top.empty else {k: _plain(top[k].iat[0]) for k in self.grid}

    def write_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.ranked().to_csv(path, index=False, lineterminator="\n")
        return path


def _plain(v):
    return v.item() if isinstance(v, np.generic) else v


def rank_results(results: pd.DataFrame, metric: str, min_trades: int = 0) -> pd.Series:
    """1 = best by `metric` among runs with >= min_trades trades and a defined metric; NA for the rest."""
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; use one of {list(METRICS)}")
    ok = (results["total_trades"] >= min_trades) & results[metric].notna()
    order = results[ok].sort_values(metric, ascending=not METRICS[metric][2], kind="mergesort")
    rank = pd.Series(pd.NA, index=results.index, dtype="Int64", name="rank")
    rank[order.index] = np.arange(1, len(order) + 1)
    return rank


def optimize(
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
    metric: str = "net_profit",
    min_trades: int = 0,
    workers: int | None = None,
    confirm: bool = False,
    progress: Callable[[int, int], None] | None = None,
) -> OptimizeResult:
    """Backtest every combination of `grid` and rank the runs by `metric`.

    strategy: a Strategy subclass defined at the top level of a file, or the path of a strategy file
    (`class_name` picks the class when the file defines several). Workers re-import it from its file.
    Data: `data` (each worker loads it; fill mode from data.fill_mode unless given) or `bars` / `sub_bars` frames
    (sent to each worker once). fixed: input overrides held constant across the grid.
    workers: process count (default: CPU cores, capped at the number of combinations); 1 runs in this process.
    confirm: required when the grid has more than MAX_COMBINATIONS combinations.
    """
    job, frames, combos = _setup(strategy, grid, spec, costs, data, bars, sub_bars, fill_mode, fixed, class_name, metric, confirm,
                                 min_trades, workers)  # fmt: skip
    workers = pool_size(workers, len(combos))
    t0 = time.perf_counter()
    with _pool(job, frames, workers) as run:
        rows = run(_run_one, combos, progress)
    seconds = time.perf_counter() - t0

    results = pd.concat([pd.DataFrame(combos), pd.DataFrame(rows)], axis=1)
    results.insert(0, "combo", np.arange(1, len(results) + 1))
    results["rank"] = rank_results(results, metric, min_trades)
    return OptimizeResult(grid, results, metric, min_trades, workers, seconds, job.fixed)


def pool_size(workers: int | None, jobs: int) -> int:
    """Worker processes: `workers` (default: CPU cores), capped at the number of jobs, at least 1."""
    return max(1, min(workers or os.cpu_count() or 1, jobs))


def _check_value(what: str, name: str, v, default) -> None:
    """A grid / fixed value passed directly must fit its input's default type (as parse_grid would make it)."""
    if isinstance(default, (bool, np.bool_)):
        ok = isinstance(v, (bool, np.bool_))
    elif isinstance(default, numbers.Integral):
        ok = isinstance(v, numbers.Integral) and not isinstance(v, (bool, np.bool_))
    elif _is_number(default):
        ok = _is_number(v) and math.isfinite(v)
    elif isinstance(default, str):
        ok = isinstance(v, str)
    else:
        ok = True
    if not ok:
        raise ValueError(f"{what} input {name}={v!r} does not fit its default {default!r} ({type(default).__name__})")


def _check_counts(min_trades, workers) -> None:
    if isinstance(min_trades, bool) or not isinstance(min_trades, numbers.Integral) or min_trades < 0:
        raise ValueError(f"min_trades must be a whole number >= 0, got {min_trades!r}")
    if workers is not None and (isinstance(workers, bool) or not isinstance(workers, numbers.Integral) or workers < 1):
        raise ValueError(f"workers must be a whole number >= 1 (or omitted for all CPU cores), got {workers!r}")


def _setup(strategy, grid, spec, costs, data, bars, sub_bars, fill_mode, fixed, class_name, metric, confirm, min_trades=0, workers=None):
    """Check a grid run's arguments; return (job, frames for the workers or None, combinations)."""
    _check_counts(min_trades, workers)
    if isinstance(strategy, (str, Path)):
        file, cls = str(strategy), load_strategy_class(strategy, class_name)
    else:
        cls = strategy
        if cls.__qualname__ != cls.__name__:
            raise ValueError(f"{cls.__qualname__}: the optimizer re-imports strategies from their file, so define it at a file's top level")
        file = inspect.getfile(cls)
    fixed = dict(fixed or {})
    for what, names in (("grid", grid), ("fixed", fixed)):
        unknown = set(names) - set(cls.inputs)
        if unknown:
            raise ValueError(f"{cls.__name__}: unknown {what} inputs {sorted(unknown)}; known: {sorted(cls.inputs)}")
    both = set(grid) & set(fixed)
    if both:
        raise ValueError(f"inputs {sorted(both)} are both fixed and in the grid")
    if not grid or any(not v for v in grid.values()):
        raise ValueError("the grid needs at least one input with at least one value")
    for name, values in grid.items():
        if isinstance(values, (str, bytes)) or not hasattr(values, "__iter__"):
            raise ValueError(f"grid input {name}: give a list of values, got {values!r}")
        for v in values:
            _check_value("grid", name, v, cls.inputs[name])
    for name, v in fixed.items():
        _check_value("fixed", name, v, cls.inputs[name])
    if metric not in METRICS:
        raise ValueError(f"unknown metric {metric!r}; use one of {list(METRICS)}")
    combos = combinations(grid)
    if len(combos) > MAX_COMBINATIONS and not confirm:
        raise TooManyCombinations(f"the grid has {len(combos):,} combinations (> {MAX_COMBINATIONS:,}); confirm to run it")
    if (data is None) == (bars is None):
        raise ValueError("give either data= or bars=")
    if data is not None:
        fill_mode = fill_mode or data.fill_mode
    job = _Job(file, cls.__name__, spec, costs, fill_mode, fixed, data)
    return job, None if data is not None else (bars, sub_bars), combos


# --- heatmap data --------------------------------------------------------------------------------


def varying(grid: dict[str, list]) -> list[str]:
    """Grid inputs with more than one value."""
    return [k for k, v in grid.items() if len(v) > 1]


def heatmap(opt: OptimizeResult, x: str, y: str | None, metric: str) -> dict:
    """Best `metric` per (y, x) cell among ranked-eligible runs (min-trades filter), over every other input.

    Returns {"x": values, "y": values or None, "z": [[...]] (rows = y; NaN = no eligible run), "trades": [[...]],
    "combo": [[...]] (the combo number behind each cell)}. With y=None it is a single row over x.
    """
    label, kind, hib = METRICS[metric]
    r = opt.results
    ok = (r["total_trades"] >= opt.min_trades) & r[metric].notna()
    xs = opt.grid[x]
    ys = opt.grid[y] if y else [None]
    z = np.full((len(ys), len(xs)), np.nan)
    trades = np.full_like(z, np.nan)
    combo = np.zeros(z.shape, dtype=int)
    for (iy, yv), (ix, xv) in itertools.product(enumerate(ys), enumerate(xs)):
        cell = ok & (r[x] == xv) & ((r[y] == yv) if y else True)
        if not cell.any():
            continue
        vals = r.loc[cell, metric]
        k = vals.idxmax() if hib else vals.idxmin()  # first of equal values = earliest in grid order
        z[iy, ix], trades[iy, ix], combo[iy, ix] = r.at[k, metric], r.at[k, "total_trades"], r.at[k, "combo"]
    return {"x": xs, "y": None if y is None else ys, "z": z, "trades": trades, "combo": combo}
