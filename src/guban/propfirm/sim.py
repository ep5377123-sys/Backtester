"""Prop-firm evaluation Monte Carlo (R10 / S6): a literal port of "25k Prop Firm Accurate Rules.ipynb", cells 4-11.

Pipeline (notebook cell in brackets):
- [4] Each trade pays `commission + slippage` per contract on top of its profit_usd; R = net / (contracts x risk).
  A bootstrap gives a confidence interval for the mean trade.
- [5] Daily pool: net P&L summed by entry date over every weekday (Mon-Fri) from the first to the last trade date,
  0 on weekdays without trades. Trades dated on a weekend are not in the pool (the notebook drops them the same way).
- [6-7] Each simulated evaluation draws one pool day per trading day, with replacement, times `size`:
  equity += day; drawdown = end-of-day high - equity; fail at drawdown >= max_drawdown, then (optionally) on a
  day losing >= daily_loss_limit; the high is raised after those checks (EOD trailing); pass when equity >=
  profit_target (and at least min_trading_days); fail on time after max_trading_days.
- [8] Degradation: shrink every pool day by pnl_shrink, then turn int(|win_rate_adj| x pool days) winning days
  (chosen at random) into the mean losing day.
- [9] Size sweep: the same simulation at several sizes, each from the same seed (common random numbers).
- [10] Equity paths for the chart. [11] Summary: expected attempts, cumulative pass probability, verdict.

Random draws are made in exactly the notebook's order with numpy's default_rng, so the same pool, seed and
settings give the notebook's numbers exactly.
"""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import CONFIG_DIR

PASS, FAIL_DD, FAIL_DAILY, FAIL_TIME = 0, 1, 2, 3
OUTCOMES = {PASS: "pass", FAIL_DD: "fail_dd", FAIL_DAILY: "fail_daily", FAIL_TIME: "fail_time"}

# Degradation, sweep and equity paths each start their own generator at seed + offset (notebook cells 8, 9, 10).
DEGRADE_SEED, SWEEP_SEED, PATHS_SEED = 1000, 2000, 5000

VERDICTS = [(0.70, "STRONG", "High probability, strategy well-suited."), (0.50, "SOLID", "Good odds, minor tweaks may help."),
            (0.35, "MODERATE", "Coin-flip territory, consider tighter risk."), (0.20, "RISKY", "More likely to fail, reduce exposure."),
            (0.0, "POOR", "Very low chance, major changes needed.")]  # fmt: skip


@dataclass(frozen=True)
class Rules:
    name: str
    account_size: float
    profit_target: float
    max_drawdown: float
    eod_trailing: bool = True
    daily_loss_limit: float = 0.0
    min_trading_days: int = 0
    max_trading_days: int = 21


@dataclass(frozen=True)
class Scenario:
    label: str
    win_rate_adj: float = 0.0  # e.g. -0.05: turn 5% of the pool's days (winning ones) into the mean losing day
    pnl_shrink: float = 0.0  # e.g. 0.15: every day's P&L x 0.85


@dataclass(frozen=True)
class Setup:
    """Everything one run needs: the firm's rules plus the notebook's cost and simulation settings."""

    key: str
    rules: Rules
    commission: float = 2.25
    slippage: float = 5.0
    risk_per_contract: float = 1000.0
    sims: int = 20_000
    sweep_sims: int = 5_000
    seed: int = 42
    size: float = 1.0
    sweep: tuple[float, ...] = ()
    paths: int = 300
    bootstrap_sims: int = 10_000
    scenarios: tuple[Scenario, ...] = ()
    notes: tuple[str, ...] = ()

    def with_overrides(self, **kw) -> Setup:
        """Copy with the given settings changed; None values are ignored (CLI flags left unset)."""
        return replace(self, **{k: v for k, v in kw.items() if v is not None})


def rules_path(name: str | Path, config_dir: Path | None = None) -> Path:
    """A rules file path, or a name in config/propfirm/ (with or without .toml)."""
    p = Path(name)
    if p.suffix.lower() == ".toml" or p.exists():
        return p
    return Path(config_dir or CONFIG_DIR) / "propfirm" / f"{name}.toml"


def load_setup(name: str | Path, config_dir: Path | None = None) -> Setup:
    path = rules_path(name, config_dir)
    if not path.exists():
        known = sorted(q.stem for q in path.parent.glob("*.toml")) if path.parent.exists() else []
        raise FileNotFoundError(f"No prop-firm rules {str(name)!r} ({path}); known: {', '.join(known) or 'none'}")
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    r = raw["rules"]
    rules = Rules(
        name=str(r["name"]), account_size=float(r.get("account_size", 0)), profit_target=float(r["profit_target"]),
        max_drawdown=float(r["max_drawdown"]), eod_trailing=bool(r.get("eod_trailing", True)),
        daily_loss_limit=float(r.get("daily_loss_limit", 0)), min_trading_days=int(r.get("min_trading_days", 0)),
        max_trading_days=int(r.get("max_trading_days", 21)),
    )  # fmt: skip
    c, s = raw.get("costs", {}), raw.get("simulation", {})
    scen = tuple(Scenario(str(x["label"]), float(x.get("win_rate_adj", 0)), float(x.get("pnl_shrink", 0))) for x in raw.get("scenarios", []))
    return Setup(
        key=path.stem, rules=rules,
        commission=float(c.get("commission", 0)), slippage=float(c.get("slippage", 0)), risk_per_contract=float(c.get("risk_per_contract", 1000)),
        sims=int(s.get("sims", 20_000)), sweep_sims=int(s.get("sweep_sims", 5_000)), seed=int(s.get("seed", 42)),
        size=float(s.get("size", 1.0)), sweep=tuple(float(v) for v in s.get("sweep", [])), paths=int(s.get("paths", 300)),
        bootstrap_sims=int(s.get("bootstrap_sims", 10_000)), scenarios=scen, notes=tuple(raw.get("notes", [])),
    )  # fmt: skip


# --- trades and the daily pool (cells 3-5) ----------------------------------------------------------


def load_trades(path: str | Path) -> pd.DataFrame:
    """Trades from an MC export (.csv / .xlsx) or a guban trades.csv: trade_id, contracts, profit_usd, direction,
    datetime (entry, naive chart time) and date (entry date), in file order."""
    from guban.parity import load_mc_trades

    mc = load_mc_trades(path, tz=None)
    if mc.empty:
        raise ValueError(f"{Path(path).name}: no trades")
    df = pd.DataFrame({
        "trade_id": mc["trade_no"], "contracts": mc["qty"].astype(float), "profit_usd": mc["net_pnl"].astype(float),
        "direction": mc["direction"].str.upper(), "datetime": mc["entry_time"],
    })  # fmt: skip
    df["date"] = df["datetime"].dt.normalize()
    return df


def add_costs(trades: pd.DataFrame, setup: Setup) -> pd.DataFrame:
    df = trades.copy()
    df["costs_usd"] = df["contracts"] * (setup.commission + setup.slippage)
    df["profit_net_usd"] = df["profit_usd"] - df["costs_usd"]
    df["initial_risk_usd"] = df["contracts"] * float(setup.risk_per_contract)
    df["r_mult"] = df["profit_net_usd"] / df["initial_risk_usd"]
    return df


def bootstrap_ci(x, sims: int = 10_000, ci: float = 0.95, seed: int = 42) -> tuple[np.ndarray, float, float]:
    x = pd.Series(x).dropna().to_numpy()
    rng = np.random.default_rng(seed)
    boot = rng.choice(x, size=(sims, len(x)), replace=True).mean(axis=1)
    a = (1 - ci) / 2
    return boot, float(np.quantile(boot, a)), float(np.quantile(boot, 1 - a))


def trade_stats(df: pd.DataFrame, setup: Setup) -> dict:
    """Cell 4 figures for trades with costs (add_costs)."""
    net = df["profit_net_usd"]
    b_usd, lo, hi = bootstrap_ci(net, setup.bootstrap_sims, seed=setup.seed)
    b_r, rlo, rhi = bootstrap_ci(df["r_mult"], setup.bootstrap_sims, seed=setup.seed)
    return {
        "trades": len(df), "first": df["datetime"].min(), "last": df["datetime"].max(),
        "win_rate_gross": float((df["profit_usd"] > 0).mean()), "avg_trade_gross": float(df["profit_usd"].mean()),
        "win_rate_net": float((net > 0).mean()), "avg_trade_net": float(net.mean()), "ev_r": float(df["r_mult"].mean()),
        "avg_winner": _mean(net[net > 0]), "avg_loser": _mean(net[net < 0]),
        "ev_usd_ci": (lo, hi), "ev_r_ci": (rlo, rhi),
        "p_ev_r_le_0": float(np.mean(b_r <= 0)), "p_ev_usd_le_0": float(np.mean(b_usd <= 0)),
        "costs_total": float(df["costs_usd"].sum()), "contracts": _contracts(df),
    }  # fmt: skip


def _mean(s: pd.Series) -> float | None:
    return float(s.mean()) if len(s) else None


def _contracts(df: pd.DataFrame) -> float | None:
    """The file's trade size when every trade has the same one, else None."""
    c = df["contracts"].unique()
    return float(c[0]) if len(c) == 1 else None


def daily_pool(df: pd.DataFrame) -> pd.DataFrame:
    """Cell 5: one row per weekday from the first to the last trade date (0 on days without trades)."""
    actual = (
        df.groupby("date", as_index=False)
        .agg(n_trades=("trade_id", "count"), gross_pnl_usd=("profit_usd", "sum"), net_pnl_usd=("profit_net_usd", "sum"), net_r=("r_mult", "sum"))
        .sort_values("date")
        .reset_index(drop=True)
    )
    days = pd.DataFrame({"date": pd.date_range(start=df["date"].min(), end=df["date"].max(), freq="B")}).merge(actual, on="date", how="left")
    days["n_trades"] = days["n_trades"].fillna(0).astype(int)
    for col in ("gross_pnl_usd", "net_pnl_usd", "net_r"):
        days[col] = days[col].fillna(0.0)
    days["is_trade_day"] = days["n_trades"] > 0
    return days


# --- simulation (cells 6-10) ------------------------------------------------------------------------


def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = successes / n
    denom = 1 + z**2 / n
    center = (p + z**2 / (2 * n)) / denom
    margin = (z / denom) * np.sqrt(p * (1 - p) / n + z**2 / (4 * n**2))
    return float(max(0, center - margin)), float(min(1, center + margin))


def degrade(net: np.ndarray, rng: np.random.Generator, win_rate_adj: float = 0.0, pnl_shrink: float = 0.0) -> np.ndarray:
    """Cell 6 apply_day_degradation on the pool's daily net P&L (draws from rng only when win_rate_adj < 0)."""
    pool = np.array(net, dtype=float)
    if pnl_shrink > 0:
        pool *= 1.0 - pnl_shrink
    if win_rate_adj < 0:
        win_idx = np.flatnonzero(pool > 0)
        loss_vals = pool[pool < 0]
        if len(win_idx) > 0:
            n_flip = min(int(abs(win_rate_adj) * len(pool)), len(win_idx))
            flip_idx = rng.choice(win_idx, size=n_flip, replace=False)
            pool[flip_idx] = np.mean(loss_vals) if len(loss_vals) > 0 else -abs(pool.mean())
    return pool


@dataclass
class SimResult:
    """One simulate() run; per-simulation arrays are in simulation order."""

    n_sims: int
    size: float
    outcome: np.ndarray  # PASS / FAIL_DD / FAIL_DAILY / FAIL_TIME
    days: np.ndarray  # simulated days until the outcome
    max_dd: np.ndarray  # worst end-of-day drawdown seen
    final_equity: np.ndarray

    def count(self, code: int) -> int:
        return int((self.outcome == code).sum())

    @property
    def passed(self) -> int:
        return self.count(PASS)

    @property
    def pass_rate(self) -> float:
        return self.passed / self.n_sims

    @property
    def pass_rate_ci(self) -> tuple[float, float]:
        return wilson_ci(self.passed, self.n_sims)

    @property
    def days_to_pass(self) -> np.ndarray:
        return self.days[self.outcome == PASS]

    def rate(self, code: int) -> float:
        return self.count(code) / self.n_sims

    def summary(self) -> dict:
        dtp = self.days_to_pass
        eq_pass = self.final_equity[self.outcome == PASS]
        return {
            "size": self.size, "n_sims": self.n_sims,
            "passed": self.passed, "fail_dd": self.count(FAIL_DD), "fail_daily": self.count(FAIL_DAILY), "fail_time": self.count(FAIL_TIME),
            "pass_rate": self.pass_rate, "pass_rate_ci": self.pass_rate_ci,
            "avg_days_to_pass": float(np.mean(dtp)) if len(dtp) else None,
            "median_days_to_pass": float(np.median(dtp)) if len(dtp) else None,
            "avg_max_dd": float(np.mean(self.max_dd)), "p50_max_dd": float(np.median(self.max_dd)),
            "p90_max_dd": float(np.percentile(self.max_dd, 90)), "p95_max_dd": float(np.percentile(self.max_dd, 95)),
            "worst_max_dd": float(np.max(self.max_dd)),
            "avg_final_equity": float(np.mean(self.final_equity)),
            "avg_equity_at_pass": float(np.mean(eq_pass)) if len(eq_pass) else None,
        }  # fmt: skip


def simulate(net: np.ndarray, rules: Rules, n_sims: int, size: float, rng: np.random.Generator, win_rate_adj: float = 0.0, pnl_shrink: float = 0.0) -> SimResult:
    """Cell 6 simulate_apex: n_sims evaluations drawn from the daily pool `net` (daily net P&L), scaled by size."""
    vals = (degrade(net, rng, win_rate_adj, pnl_shrink) * size).tolist()
    n = len(vals)
    if n == 0:
        raise ValueError("the daily pool is empty")
    draw = rng.integers
    eod, target, limit, dll = rules.eod_trailing, rules.profit_target, rules.max_drawdown, rules.daily_loss_limit
    min_days, max_days = rules.min_trading_days, rules.max_trading_days
    outcome = np.full(n_sims, FAIL_TIME, np.int8)
    days = np.zeros(n_sims, np.int32)
    max_dd = np.zeros(n_sims)
    final = np.zeros(n_sims)
    for i in range(n_sims):
        equity = hwm = sim_dd = 0.0
        code, day = FAIL_TIME, 0
        for day in range(1, max_days + 1):
            pnl = vals[draw(n)]
            equity += pnl
            if eod:
                dd = hwm - equity
            else:
                dd = hwm - equity if equity <= hwm else 0.0
                if equity > hwm:
                    hwm = equity
            if dd > sim_dd:
                sim_dd = dd
            if dd >= limit:
                code = FAIL_DD
                break
            if dll > 0 and pnl <= -dll:
                code = FAIL_DAILY
                break
            if eod and equity > hwm:
                hwm = equity
            if equity >= target and day >= min_days:
                code = PASS
                break
        outcome[i], days[i], max_dd[i], final[i] = code, day, sim_dd, equity
    return SimResult(n_sims, size, outcome, days, max_dd, final)


def simulate_paths(net: np.ndarray, rules: Rules, n_paths: int, size: float, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Cell 10 simulate_paths: equity after each day; NaN after a drawdown failure, flat after a pass.

    As in the notebook, the paths check only the drawdown and the target (no daily loss limit or minimum days).
    """
    vals = (np.asarray(net, float) * size).tolist()
    n, max_days = len(vals), rules.max_trading_days
    paths = np.full((n_paths, max_days), np.nan)
    outcomes = np.full(n_paths, FAIL_TIME, np.int8)
    for i in range(n_paths):
        equity = hwm = 0.0
        for d in range(max_days):
            equity += vals[rng.integers(n)]
            paths[i, d] = equity
            if rules.eod_trailing:
                dd = hwm - equity
            else:
                dd = hwm - equity if equity <= hwm else 0.0
                if equity > hwm:
                    hwm = equity
            if dd >= rules.max_drawdown:
                outcomes[i] = FAIL_DD
                break
            if rules.eod_trailing and equity > hwm:
                hwm = equity
            if equity >= rules.profit_target:
                outcomes[i] = PASS
                paths[i, d + 1 :] = equity
                break
    return paths, outcomes


def verdict(pass_rate: float) -> tuple[str, str]:
    for floor, tag, text in VERDICTS:
        if pass_rate >= floor:
            return tag, text
    return VERDICTS[-1][1:]


# --- full run ---------------------------------------------------------------------------------------


@dataclass
class PropFirmResult:
    setup: Setup
    trades: pd.DataFrame  # with costs and R-multiples
    stats: dict
    daily: pd.DataFrame
    main: SimResult
    scenarios: list[tuple[Scenario, SimResult]]
    sweep: list[SimResult]
    paths: np.ndarray
    path_outcomes: np.ndarray
    weekend_trades: int = 0
    weekend_days: int = 0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def best_size(self) -> SimResult | None:
        """Cell 9: the highest pass rate among sweep sizes that passed at least once (first one on ties)."""
        best, best_pr = None, 0.0
        for r in self.sweep:
            if r.passed and r.pass_rate > best_pr:
                best, best_pr = r, r.pass_rate
        return best

    def summary(self) -> dict:
        """Cell 11 figures for the main run."""
        m = self.main.summary()
        pr = m["pass_rate"]
        fails = {"DD": m["fail_dd"], "Daily Limit": m["fail_daily"], "Time Cap": m["fail_time"]}
        primary = max(fails, key=fails.get)
        tag, text = verdict(pr)
        return {
            "expected_attempts": 1 / pr if pr > 0 else None,
            "cumulative": {n: 1 - (1 - pr) ** n for n in (1, 2, 3, 4)},
            "primary_failure": primary if fails[primary] > 0 else None,
            "primary_failure_rate": fails[primary] / m["n_sims"],
            "verdict": tag, "verdict_text": text,
        }  # fmt: skip

    def sweep_table(self) -> pd.DataFrame:
        c = self.stats["contracts"]
        rows = []
        for r in self.sweep:
            s = r.summary()
            rows.append({
                "size": r.size, "contracts": None if c is None else round(r.size * c, 6), "sims": r.n_sims,
                "pass_rate": s["pass_rate"], "ci_low": s["pass_rate_ci"][0], "ci_high": s["pass_rate_ci"][1],
                "fail_dd_rate": r.rate(FAIL_DD), "fail_daily_rate": r.rate(FAIL_DAILY), "fail_time_rate": r.rate(FAIL_TIME),
                "avg_max_dd": s["avg_max_dd"], "avg_days_to_pass": s["avg_days_to_pass"],
            })  # fmt: skip
        return pd.DataFrame(rows)

    def scenario_table(self) -> pd.DataFrame:
        rows = []
        for sc, r in self.scenarios:
            s = r.summary()
            rows.append({
                "scenario": sc.label, "win_rate_adj": sc.win_rate_adj, "pnl_shrink": sc.pnl_shrink, "size": r.size, "sims": r.n_sims,
                "pass_rate": s["pass_rate"], "ci_low": s["pass_rate_ci"][0], "ci_high": s["pass_rate_ci"][1],
                "fail_dd_rate": r.rate(FAIL_DD), "fail_daily_rate": r.rate(FAIL_DAILY), "fail_time_rate": r.rate(FAIL_TIME),
                "avg_max_dd": s["avg_max_dd"], "avg_days_to_pass": s["avg_days_to_pass"],
            })  # fmt: skip
        return pd.DataFrame(rows)

    def to_json(self) -> dict:
        st = self.setup
        d = self.daily
        best = self.best_size
        return {
            "rules": {"key": st.key, **vars(st.rules)},
            "settings": {k: getattr(st, k) for k in ("commission", "slippage", "risk_per_contract", "sims", "sweep_sims", "seed", "size", "sweep", "paths", "bootstrap_sims")},
            "scenarios": [vars(s) for s in st.scenarios],
            "trades": self.stats,
            "daily_pool": {"first": d["date"].min(), "last": d["date"].max(), "weekdays": len(d), "trade_days": int(d["is_trade_day"].sum()),
                           "avg_trades_per_weekday": float(d["n_trades"].mean()), "weekend_trades_dropped": self.weekend_trades},
            "main": self.main.summary(),
            "summary": self.summary(),
            "degradation": self.scenario_table().to_dict("records"),
            "sweep": self.sweep_table().to_dict("records"),
            "best_size": None if best is None else {"size": best.size, "pass_rate": best.pass_rate},
            "notes": self.notes,
            "seconds": round(self.seconds, 2),
        }  # fmt: skip


def run_propfirm(trades: pd.DataFrame, setup: Setup, progress=None) -> PropFirmResult:
    """Run the whole notebook on a trade list (load_trades): stats, daily pool, main run, degradation, sweep, paths."""
    t0 = time.perf_counter()
    say = progress or (lambda msg: None)
    if setup.sims < 1 or setup.sweep_sims < 1:
        raise ValueError("sims and sweep_sims must be at least 1")
    if setup.rules.max_trading_days < 1:
        raise ValueError("max_trading_days must be at least 1")
    df = add_costs(trades, setup)
    stats = trade_stats(df, setup)
    daily = daily_pool(df)
    weekend = df["date"].dt.dayofweek >= 5
    net = daily["net_pnl_usd"].to_numpy()
    rules = setup.rules

    say(f"main run: {setup.sims:,} simulations at size {setup.size:g}")
    main = simulate(net, rules, setup.sims, setup.size, np.random.default_rng(setup.seed))
    scen = []
    if setup.scenarios:
        say(f"degradation: {len(setup.scenarios)} scenarios x {setup.sweep_sims:,} simulations")
    for sc in setup.scenarios:
        scen.append((sc, simulate(net, rules, setup.sweep_sims, setup.size, np.random.default_rng(setup.seed + DEGRADE_SEED), sc.win_rate_adj, sc.pnl_shrink)))
    sweep = []
    if setup.sweep:
        say(f"size sweep: {len(setup.sweep)} sizes x {setup.sweep_sims:,} simulations")
    for size in setup.sweep:
        sweep.append(simulate(net, rules, setup.sweep_sims, size, np.random.default_rng(setup.seed + SWEEP_SEED)))
    paths, outcomes = simulate_paths(net, rules, setup.paths, setup.size, np.random.default_rng(setup.seed + PATHS_SEED))

    notes = list(setup.notes)
    if setup.commission or setup.slippage:
        notes.append(f"Each trade pays ${setup.commission + setup.slippage:,.2f} per contract (commission ${setup.commission:g} + slippage ${setup.slippage:g}) "
                     "on top of its profit_usd, which is already net of the backtest's own costs. Use --commission 0 --slippage 0 if the backtest "
                     "already charged realistic costs.")  # fmt: skip
    c = stats["contracts"]
    notes.append("Size multiplies the trade file's daily P&L: 1.0 is the size the file traded"
                 + (f" ({c:g} contracts per trade)." if c is not None else " (trade sizes vary in this file).")
                 + " The notebook labels size 1.0 as 1 NQ (10 MNQ), which is only true for a 1-NQ trade file.")  # fmt: skip
    notes.append(f"Each simulated trading day is a random weekday of the pool, including the {int((~daily['is_trade_day']).sum()):,} of "
                 f"{len(daily):,} weekdays with no trades, so the {rules.max_trading_days}-day deadline counts weekdays, not days traded.")  # fmt: skip
    if weekend.any():
        notes.append(f"{int(weekend.sum()):,} trade(s) on {df.loc[weekend, 'date'].nunique():,} weekend date(s) are not in the daily pool "
                     "(the notebook builds the pool from weekdays only).")  # fmt: skip
    return PropFirmResult(setup, df, stats, daily, main, scen, sweep, paths, outcomes, int(weekend.sum()),
                          int(df.loc[weekend, "date"].nunique()), time.perf_counter() - t0, notes)  # fmt: skip
