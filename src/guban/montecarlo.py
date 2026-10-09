"""Trade Monte Carlo for `guban run` (Phases 11, 16): how much of a backtest's result is the luck of its trade sequence.

Three methods run on the run's own trade list (net P&L after costs, one value per trade), side by side:
- resample: each run draws n trades from the list with replacement, so net profit and drawdown both vary.
- reorder: each run shuffles the same n trades, so net profit is the backtest's in every run; only the path varies.
- day: each run draws D whole trading days (all trades of one exit session, in their own order) with replacement,
  where D is the number of sessions in which the backtest closed a trade (days without trades are not drawn). Trades of one day stay together, so losers that cluster on a
  day cluster in the simulation too (a block bootstrap). Runs only when trade days are given and there are >= 2.

Max drawdown per run is close-to-close, exactly guban.metrics.compute_metrics' definition: the deepest drop of
cumulative net P&L below its running peak, from 0 (always <= 0). It is trade by trade for every method, the day
method included (a day's internal dip counts). A trade list has no intraday path, so the run's intraday drawdown
cannot be simulated.

Also, from the same trades (the user's notebook "Strategy EV & Monte Carlo Analysis"):
- ev: expected value per trade in USD and in R with a bootstrap CI and P(EV <= 0), for all trades, long, short and,
  given --mc-oos-from, in/out of sample. R = net / the trade's own risk to its initial stop (Phase 17, `risks`);
  --mc-risk instead: net / (contracts x that risk per contract); neither: the average losing trade per contract.
  Trades without a stop are left out of the R figures only (their R draws are skipped, the USD draws unchanged).
- horizons: net profit and max drawdown depth percentiles after the first 10/20/40/100 trades (resample) or trading
  days (day) of each run.
- sample paths: the first SAMPLE_PATHS runs' equity curves of each method.

Deterministic: resample draws from default_rng(seed), reorder from seed + 1, day from seed + 2, the EV bootstrap from
seed + 3, so adding a method never changes another's draws. Runs are drawn in batches to bound memory; numpy's
integers() and permuted() give the same draws whatever the batch size, so the batch size does not change the results
(tests/test_montecarlo.py pins this).
"""

from __future__ import annotations

import math
import numbers
import time
import warnings
from dataclasses import dataclass, field

import numpy as np

METHODS = ("resample", "reorder", "day")
PERCENTILES = (5, 25, 50, 75, 95)
DEPTH_PERCENTILES = (50, 75, 90, 95, 99)  # max drawdown depth (positive USD): deeper at higher percentiles
HORIZONS = (10, 20, 40, 100)  # trades (resample) or trading days (day)
HORIZON_METHODS = ("resample", "day")
UNITS = {"resample": "trade", "reorder": "trade", "day": "day"}
SAMPLE_PATHS = 100
CI = 0.95
MIN_TRADES = 2
MAX_SIMS = 1_000_000
BAND_POINTS = 400  # equity bands are kept at this many points at most
BATCH_CELLS = 2_000_000  # runs per batch = this // trades (at least 1)


def check_settings(sims: int, seed: int, dd_limit: float | None = None, risk: float | None = None, oos_from: str | None = None) -> None:
    """Raise ValueError naming the flag when a Monte Carlo setting is out of range (sims 0 means off)."""
    if isinstance(sims, bool) or not isinstance(sims, numbers.Integral) or not 0 <= sims <= MAX_SIMS:
        raise ValueError(f"--mc-sims must be a whole number from 0 (off) to {MAX_SIMS:,}, got {sims!r}")
    if isinstance(seed, bool) or not isinstance(seed, numbers.Integral) or seed < 0:
        raise ValueError(f"--mc-seed must be a whole number >= 0, got {seed!r}")
    if dd_limit is not None and not _positive(dd_limit):
        raise ValueError(f"--mc-dd-limit must be a drawdown in USD > 0 (e.g. 2500), got {dd_limit!r}")
    if risk is not None and not _positive(risk):
        raise ValueError(f"--mc-risk must be the risk per contract in USD > 0 (e.g. 400), got {risk!r}")
    if oos_from is not None:
        _oos_timestamp(oos_from)


def _positive(v) -> bool:
    return not isinstance(v, bool) and isinstance(v, numbers.Real) and math.isfinite(v) and v > 0


def _oos_timestamp(oos_from):
    import pandas as pd

    try:
        ts = pd.Timestamp(oos_from)
    except (ValueError, TypeError):
        ts = pd.NaT
    if ts is pd.NaT:
        raise ValueError(f"--mc-oos-from must be a date YYYY-MM-DD, got {oos_from!r}")
    return ts


@dataclass
class MethodResult:
    net: np.ndarray  # net profit per run
    max_dd: np.ndarray  # close-to-close max drawdown per run (<= 0)
    bands: np.ndarray  # (len(PERCENTILES), len(x)): percentiles of cumulative net after x[j] trades (or days)
    x: np.ndarray = None  # trade counts (day: trading-day counts) the bands and paths are sampled at
    backtest: np.ndarray = None  # the backtest's own cumulative net at x
    paths: np.ndarray = None  # (min(sims, SAMPLE_PATHS), len(x)): the first runs' cumulative net at x
    horizon_net: dict = field(default_factory=dict)  # {n: net after the run's first n trades/days}
    horizon_dd: dict = field(default_factory=dict)  # {n: max drawdown within the run's first n trades/days}


@dataclass
class MonteCarloResult:
    sims: int
    seed: int
    dd_limit: float | None
    trades: int
    net: float  # the backtest's net profit
    max_dd: float  # the backtest's close-to-close max drawdown
    x: np.ndarray  # trade counts the trade methods' bands are sampled at (0 .. trades)
    path: np.ndarray  # the backtest's cumulative net at x
    methods: dict[str, MethodResult]
    seconds: float
    ev: dict = field(default_factory=dict)
    skipped: dict = field(default_factory=dict)  # {method: why it did not run}
    days: int | None = None  # trading days, when trade days were given

    def method_summary(self, name: str) -> dict:
        m = self.methods[name]
        out = {
            "net_profit": _pcts(m.net),
            "max_drawdown": _pcts(m.max_dd),
            "max_drawdown_depth": _pcts(-m.max_dd, DEPTH_PERCENTILES),
            "p_dd_worse_than_backtest": float(np.mean(m.max_dd < self.max_dd - 1e-9)),
        }
        if name != "reorder":
            out["p_net_le_0"] = float(np.mean(m.net <= 0))
        if self.dd_limit is not None:
            out["p_dd_reaches_limit"] = float(np.mean(m.max_dd <= -self.dd_limit))
        return out

    @property
    def horizons(self) -> dict:
        """{method: [{"n", "net_profit", "max_drawdown_depth", "p_net_lt_0"}]}; n counts trades (resample) or trading
        days (day). Horizons longer than the run are left out."""
        out = {}
        for name in HORIZON_METHODS:
            if name in self.methods:
                m = self.methods[name]
                out[name] = [
                    {"n": n, "net_profit": _pcts(m.horizon_net[n]), "max_drawdown_depth": _pcts(-m.horizon_dd[n], DEPTH_PERCENTILES),
                     "p_net_lt_0": float(np.mean(m.horizon_net[n] < 0))}
                    for n in sorted(m.horizon_net)
                ]  # fmt: skip
        return out

    def summary(self) -> dict:
        """The numbers for metrics.json (no timing, so the same inputs give identical JSON)."""
        out = {
            "sims": self.sims,
            "seed": self.seed,
            "unit": "trade",
            "dd_basis": "close_to_close",
            "dd_limit": self.dd_limit,
            "trades": self.trades,
            "backtest": {"net_profit": self.net, "max_drawdown": self.max_dd, **({"days": self.days} if self.days else {})},
            **{name: self.method_summary(name) for name in METHODS if name in self.methods},
            "ev": self.ev,
            "horizons": self.horizons,
        }
        if self.skipped:
            out["skipped"] = dict(self.skipped)
        return out


def run_monte_carlo(
    net, sims: int = 10_000, seed: int = 42, dd_limit: float | None = None, *, days=None, contracts=None, sides=None,
    risk: float | None = None, times=None, oos_from=None, ci: float = CI, risks=None,
) -> MonteCarloResult:  # fmt: skip
    """Resample, reorder and (given `days`) day-resample `net` (per-trade net P&L, in trade order) `sims` times each,
    and bootstrap its EV. Per trade, all optional: `days` = trading-day label (exit session), `contracts`, `sides`
    ("long"/"short"), `times` (exit time, for the --mc-oos-from split), `risks` (USD at risk to the initial stop, NaN
    without one). `risk` = USD per contract for R, overriding `risks` (fallback: the average losing trade per
    contract; without contracts, R needs `risk` or `risks` and counts 1 contract per trade)."""
    check_settings(sims, seed, dd_limit, risk, oos_from)
    if sims == 0:
        raise ValueError("--mc-sims 0 means off; nothing to simulate")
    if not 0 < ci < 1:
        raise ValueError(f"the EV confidence level must be between 0 and 1, got {ci!r}")
    net = np.asarray(net, dtype=float)
    if net.ndim != 1 or len(net) < MIN_TRADES:
        raise ValueError(f"the Monte Carlo needs at least {MIN_TRADES} trades, got {net.size}")
    if not np.isfinite(net).all():
        raise ValueError("trade net P&L has NaN or infinite values")
    n = len(net)
    for name, v in (("days", days), ("contracts", contracts), ("sides", sides), ("times", times), ("risks", risks)):
        if v is not None and len(v) != n:
            raise ValueError(f"{name} has {len(v)} values for {n} trades")
    t0 = time.perf_counter()
    x = _sample_points(n)
    path, max_dd = _paths(net[None, :])
    batch = max(1, BATCH_CELLS // n)
    methods = {
        "resample": _simulate(lambda rng, b: _trade_draw(net[rng.integers(0, n, (b, n))]), seed, sims, batch, x, path[0, x], HORIZONS),
        "reorder": _simulate(lambda rng, b: _trade_draw(rng.permuted(np.broadcast_to(net, (b, n)), axis=1)), seed + 1, sims, batch, x, path[0, x], ()),
    }  # fmt: skip
    skipped, n_days = {}, None
    if days is not None:
        stats = _day_stats(net, days)
        n_days = len(stats["sum"])
        if n_days < 2:
            skipped["day"] = f"the day method needs at least 2 trading days; this run has {n_days} trading day"
        else:
            xd = _sample_points(n_days)
            day_path = np.r_[0.0, np.cumsum(stats["sum"])]
            methods["day"] = _simulate(lambda rng, b: _day_draw(rng.integers(0, n_days, (b, n_days)), stats), seed + 2, sims, batch, xd, day_path[xd], HORIZONS)  # fmt: skip
    ev = _ev(net, contracts, sides, risk, times, oos_from, sims, seed + 3, ci, risks)
    return MonteCarloResult(
        sims=sims, seed=seed, dd_limit=None if dd_limit is None else float(dd_limit), trades=n,
        net=float(net.sum()), max_dd=float(max_dd[0]), x=x, path=path[0, x], methods=methods,
        seconds=time.perf_counter() - t0, ev=ev, skipped=skipped, days=n_days,
    )  # fmt: skip


def _sample_points(n: int) -> np.ndarray:
    return np.unique(np.rint(np.linspace(0, n, min(n + 1, BAND_POINTS))).astype(np.int64))


def _simulate(draw, seed: int, sims: int, batch: int, x: np.ndarray, backtest: np.ndarray, horizons) -> MethodResult:
    """Run `sims` runs of `draw(rng, b) -> (cum, under)` in batches: cum = cumulative net per step from 0, under =
    each step's worst drop below the running peak (both (b, steps + 1))."""
    rng = np.random.default_rng(seed)
    nets, dds = np.empty(sims), np.empty(sims)
    sampled = np.empty((sims, len(x)), dtype=np.float32)
    paths = np.empty((min(sims, SAMPLE_PATHS), len(x)))
    steps = int(x[-1])
    hz = [h for h in horizons if h <= steps]
    h_net, h_dd = {h: np.empty(sims) for h in hz}, {h: np.empty(sims) for h in hz}
    for s in range(0, sims, batch):
        b = min(batch, sims - s)
        cum, under = draw(rng, b)
        nets[s:s + b], dds[s:s + b] = cum[:, -1], under.min(axis=1)
        sampled[s:s + b] = cum[:, x]
        if s < len(paths):
            paths[s:s + b] = cum[: len(paths) - s, x]
        for h in hz:
            h_net[h][s:s + b], h_dd[h][s:s + b] = cum[:, h], under[:, : h + 1].min(axis=1)
    bands = np.percentile(sampled, PERCENTILES, axis=0).astype(float)
    return MethodResult(nets, dds, bands, x, backtest, paths, h_net, h_dd)


def _trade_draw(draw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    cum = np.zeros((draw.shape[0], draw.shape[1] + 1))
    np.cumsum(draw, axis=1, out=cum[:, 1:])
    return cum, cum - np.maximum.accumulate(cum, axis=1)


def _paths(draw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(cumulative net with a leading 0 per row, close-to-close max drawdown per row)."""
    cum, under = _trade_draw(draw)
    return cum, under.min(axis=1)


def _day_stats(net: np.ndarray, days) -> dict:
    """Per trading day (sorted labels), from its trades in trade order with prefix sums P (P0 = 0): sum, min P, max P,
    and its own drawdown min(P - running max P)."""
    _, inv = np.unique(np.asarray(days), return_inverse=True)
    order = np.argsort(inv, kind="stable")
    groups = np.split(net[order], np.cumsum(np.bincount(inv))[:-1])
    out = {k: np.empty(len(groups)) for k in ("sum", "min", "max", "dd")}
    for i, g in enumerate(groups):
        p = np.r_[0.0, np.cumsum(g)]
        out["sum"][i], out["min"][i], out["max"][i] = p[-1], p.min(), p.max()
        out["dd"][i] = (p - np.maximum.accumulate(p)).min()
    return out


def _day_draw(d: np.ndarray, stats: dict) -> tuple[np.ndarray, np.ndarray]:
    """Trade-level path of whole drawn days without expanding them: inside day j (starting at cum S, peak so far Q),
    the deepest point below the peak is min(S - Q + min P, the day's own drawdown); the peak after it is
    max(Q, S + max P)."""
    b, n_days = d.shape
    cum = np.zeros((b, n_days + 1))
    np.cumsum(stats["sum"][d], axis=1, out=cum[:, 1:])
    start = cum[:, :-1]
    peak = np.zeros((b, n_days + 1))
    np.maximum.accumulate(start + stats["max"][d], axis=1, out=peak[:, 1:])  # >= 0: max P >= 0 and day 0 starts at 0
    under = np.zeros((b, n_days + 1))
    under[:, 1:] = np.minimum(start - peak[:, :-1] + stats["min"][d], stats["dd"][d])
    return cum, under


def _ev(net, contracts, sides, risk, times, oos_from, sims: int, seed: int, ci: float, risks=None) -> dict:
    """Bootstrap EV per trade (USD and R) with a `ci` interval and P(EV <= 0) per trade group."""
    n = len(net)
    if contracts is not None:
        contracts = np.asarray(contracts, dtype=float)
        if not (np.isfinite(contracts).all() and (contracts > 0).all()):
            raise ValueError("contracts must be > 0 for every trade")
    per_trade = None
    if risks is not None:
        per_trade = np.asarray(risks, dtype=float)
        if (per_trade <= 0).any():
            raise ValueError("risks must be > 0 USD (NaN for a trade without a stop)")
        if not np.isfinite(per_trade).any():
            per_trade = None
    r_trades = n
    if risk is not None:
        risk_pc, source = float(risk), "--mc-risk"
    elif per_trade is not None:
        known = np.isfinite(per_trade)
        r_trades = int(known.sum())
        risk_pc = float(np.mean(per_trade[known] / (contracts[known] if contracts is not None else 1.0)))
        source = "per-trade stop" + ("" if r_trades == n else f" ({n - r_trades:,} of {n:,} trades without a stop are not in R)")
    elif contracts is not None and (net < 0).any():
        risk_pc, source = float(np.mean(-net[net < 0] / contracts[net < 0])), "average losing trade per contract"
    else:
        risk_pc, source = None, "no contracts given" if contracts is None else "no losing trades"
    if risk_pc is None:
        r = None
    elif per_trade is not None and risk is None:
        r = net / per_trade  # NaN without a stop
    else:
        r = net / ((contracts if contracts is not None else 1.0) * risk_pc)
    masks = {"all": np.ones(n, dtype=bool)}
    if sides is not None:
        s = np.char.lower(np.asarray(sides).astype(str))
        masks |= {"long": s == "long", "short": s == "short"}
    if oos_from is not None:
        if times is None:
            raise ValueError("--mc-oos-from needs the trades' exit times")
        import pandas as pd

        t = pd.DatetimeIndex(times)
        ts = _oos_timestamp(oos_from)
        if t.tz is not None and ts.tz is None:
            ts = ts.tz_localize(t.tz)
        oos = np.asarray(t >= ts)
        masks |= {"in_sample": ~oos, "out_of_sample": oos}
    rng = np.random.default_rng(seed)
    lo_q, hi_q = 100 * (1 - ci) / 2, 100 * (1 + ci) / 2
    groups, skipped = {}, {}
    for name, mask in masks.items():
        k = int(mask.sum())
        if k < MIN_TRADES:
            skipped[name] = f"{k} trade{'s' if k != 1 else ''}"
            continue
        vals = net[mask]
        rv = None if r is None else r[mask]
        gaps = rv is not None and bool(np.isnan(rv).any())
        if gaps and np.isfinite(rv).sum() < MIN_TRADES:
            rv = None
        boot, rboot = np.empty(sims), np.empty(sims)
        batch = max(1, BATCH_CELLS // k)
        for s in range(0, sims, batch):
            idx = rng.integers(0, k, (min(batch, sims - s), k))
            boot[s:s + len(idx)] = vals[idx].mean(axis=1)
            if rv is not None:
                if gaps:  # trades without a stop drop out of R; a draw of only such trades has no R mean
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", RuntimeWarning)
                        rboot[s:s + len(idx)] = np.nanmean(rv[idx], axis=1)
                else:
                    rboot[s:s + len(idx)] = rv[idx].mean(axis=1)
        groups[name] = {
            "trades": k,
            "win_rate": float(np.mean(vals > 0)),
            "usd": _interval(vals.mean(), boot, lo_q, hi_q),
            "r": None if rv is None else _interval(float(np.nanmean(rv)), rboot[np.isfinite(rboot)], lo_q, hi_q),
            "p_ev_le_0": float(np.mean(boot <= 0)),
        }
    out = {"ci": ci, "risk_per_contract": risk_pc, "risk_source": source, "r_trades": r_trades if risk_pc is not None else 0, "oos_from": None if oos_from is None else str(oos_from), "groups": groups}
    if skipped:
        out["skipped"] = skipped
    return out


def _interval(mean: float, boot: np.ndarray, lo_q: float, hi_q: float) -> dict:
    lo, hi = np.percentile(boot, [lo_q, hi_q])
    return {"mean": float(mean), "lo": float(lo), "hi": float(hi)}


def _pcts(a: np.ndarray, percentiles=PERCENTILES) -> dict:
    return {f"p{p}": float(v) for p, v in zip(percentiles, np.percentile(a, percentiles))}
