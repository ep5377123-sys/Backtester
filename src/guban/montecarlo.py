"""Trade Monte Carlo for `guban run` (Phase 11): how much of a backtest's result is the luck of its trade sequence.

Two methods run on the run's own trade list (net P&L after costs, one value per trade), side by side:
- resample: each run draws n trades from the list with replacement, so net profit and drawdown both vary.
- reorder: each run shuffles the same n trades, so net profit is the backtest's in every run; only the path varies.

Max drawdown per run is close-to-close, exactly guban.metrics.compute_metrics' definition: the deepest drop of
cumulative net P&L below its running peak, from 0 (always <= 0). A trade list has no intraday path, so the run's
intraday drawdown cannot be simulated. Trades are drawn one at a time: trades that move together (same day) are
treated as independent.

Deterministic: resample draws from default_rng(seed), reorder from default_rng(seed + 1). Runs are drawn in batches
to bound memory; numpy's integers() and permuted() give the same draws whatever the batch size, so the batch size
does not change the results (tests/test_montecarlo.py pins this).
"""

from __future__ import annotations

import math
import numbers
import time
from dataclasses import dataclass

import numpy as np

METHODS = ("resample", "reorder")
PERCENTILES = (5, 25, 50, 75, 95)
MIN_TRADES = 2
MAX_SIMS = 1_000_000
BAND_POINTS = 400  # equity bands are kept at this many trade counts at most
BINS = 60
BATCH_CELLS = 2_000_000  # runs per batch = this // trades (at least 1)


def check_settings(sims: int, seed: int, dd_limit: float | None = None) -> None:
    """Raise ValueError naming the flag when a Monte Carlo setting is out of range (sims 0 means off)."""
    if isinstance(sims, bool) or not isinstance(sims, numbers.Integral) or not 0 <= sims <= MAX_SIMS:
        raise ValueError(f"--mc-sims must be a whole number from 0 (off) to {MAX_SIMS:,}, got {sims!r}")
    if isinstance(seed, bool) or not isinstance(seed, numbers.Integral) or seed < 0:
        raise ValueError(f"--mc-seed must be a whole number >= 0, got {seed!r}")
    if dd_limit is not None and (isinstance(dd_limit, bool) or not isinstance(dd_limit, numbers.Real) or not math.isfinite(dd_limit) or dd_limit <= 0):
        raise ValueError(f"--mc-dd-limit must be a drawdown in USD > 0 (e.g. 2500), got {dd_limit!r}")


@dataclass
class MethodResult:
    net: np.ndarray  # net profit per run
    max_dd: np.ndarray  # close-to-close max drawdown per run (<= 0)
    bands: np.ndarray  # (len(PERCENTILES), len(x)): percentiles of cumulative net after x[j] trades


@dataclass
class MonteCarloResult:
    sims: int
    seed: int
    dd_limit: float | None
    trades: int
    net: float  # the backtest's net profit
    max_dd: float  # the backtest's close-to-close max drawdown
    x: np.ndarray  # trade counts the bands are sampled at (0 .. trades)
    path: np.ndarray  # the backtest's cumulative net at x
    methods: dict[str, MethodResult]
    seconds: float

    def method_summary(self, name: str) -> dict:
        m = self.methods[name]
        out = {
            "net_profit": _pcts(m.net),
            "max_drawdown": _pcts(m.max_dd),
            "p_dd_worse_than_backtest": float(np.mean(m.max_dd < self.max_dd - 1e-9)),
        }
        if name == "resample":
            out["p_net_le_0"] = float(np.mean(m.net <= 0))
        if self.dd_limit is not None:
            out["p_dd_reaches_limit"] = float(np.mean(m.max_dd <= -self.dd_limit))
        return out

    def summary(self) -> dict:
        """The numbers for metrics.json (no timing, so the same inputs give identical JSON)."""
        return {
            "sims": self.sims,
            "seed": self.seed,
            "unit": "trade",
            "dd_basis": "close_to_close",
            "dd_limit": self.dd_limit,
            "trades": self.trades,
            "backtest": {"net_profit": self.net, "max_drawdown": self.max_dd},
            **{name: self.method_summary(name) for name in METHODS},
        }

    def report_data(self) -> dict:
        """Compact chart data for report.html: sampled bands and binned histograms."""
        out = {"x": self.x.tolist(), "path": _round(self.path), "net": round(self.net, 2), "dd": round(self.max_dd, 2), "limit": self.dd_limit}
        for name, m in self.methods.items():
            out[name] = {"bands": [_round(b) for b in m.bands], "dd_hist": _hist(m.max_dd)}
            if name == "resample":
                out[name]["net_hist"] = _hist(m.net)
        return out


def run_monte_carlo(net, sims: int = 10_000, seed: int = 42, dd_limit: float | None = None) -> MonteCarloResult:
    """Resample and reorder `net` (per-trade net P&L, in trade order) `sims` times each."""
    check_settings(sims, seed, dd_limit)
    if sims == 0:
        raise ValueError("--mc-sims 0 means off; nothing to simulate")
    net = np.asarray(net, dtype=float)
    if net.ndim != 1 or len(net) < MIN_TRADES:
        raise ValueError(f"the Monte Carlo needs at least {MIN_TRADES} trades, got {net.size}")
    if not np.isfinite(net).all():
        raise ValueError("trade net P&L has NaN or infinite values")
    t0 = time.perf_counter()
    n = len(net)
    x = np.unique(np.rint(np.linspace(0, n, min(n + 1, BAND_POINTS))).astype(np.int64))
    path, max_dd = _paths(net[None, :])
    rngs = {"resample": np.random.default_rng(seed), "reorder": np.random.default_rng(seed + 1)}
    batch = max(1, BATCH_CELLS // n)
    methods = {}
    for name in METHODS:
        rng = rngs[name]
        nets, dds = np.empty(sims), np.empty(sims)
        sampled = np.empty((sims, len(x)), dtype=np.float32)
        for s in range(0, sims, batch):
            b = min(batch, sims - s)
            draw = net[rng.integers(0, n, (b, n))] if name == "resample" else rng.permuted(np.broadcast_to(net, (b, n)), axis=1)
            cum, dd = _paths(draw)
            nets[s:s + b], dds[s:s + b] = cum[:, -1], dd
            sampled[s:s + b] = cum[:, x]
        methods[name] = MethodResult(nets, dds, np.percentile(sampled, PERCENTILES, axis=0).astype(float))
    return MonteCarloResult(
        sims=sims, seed=seed, dd_limit=None if dd_limit is None else float(dd_limit), trades=n,
        net=float(net.sum()), max_dd=float(max_dd[0]), x=x, path=path[0, x], methods=methods,
        seconds=time.perf_counter() - t0,
    )  # fmt: skip


def _paths(draw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(cumulative net with a leading 0 per row, close-to-close max drawdown per row)."""
    cum = np.zeros((draw.shape[0], draw.shape[1] + 1))
    np.cumsum(draw, axis=1, out=cum[:, 1:])
    dd = (cum - np.maximum.accumulate(cum, axis=1)).min(axis=1)
    return cum, dd


def _pcts(a: np.ndarray) -> dict:
    return {f"p{p}": float(v) for p, v in zip(PERCENTILES, np.percentile(a, PERCENTILES))}


def _round(a) -> list:
    return [round(float(v), 2) for v in a]


def _hist(a: np.ndarray) -> dict:
    counts, edges = np.histogram(a, bins=BINS)
    return {"edges": _round(edges), "counts": counts.tolist()}
