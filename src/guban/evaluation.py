"""Model evaluation (user, 2026-10-08): does a backtest meet the user's bar for a tradable model?

Criteria (CRITERIA): Sharpe > 1, profit factor > 1.2, max drawdown <= 25 R, Sortino > 1. Each run is judged twice:

- As run: the backtest's own figures (guban.metrics: daily Sharpe / Sortino, profit factor). The drawdown is the
  close-to-close max drawdown in R, where 1 R = the average USD risk to the initial stop per trade (`--mc-risk` x the
  average contracts per trade when given; no stops: n/a).
- Equal risk: every trade rescaled to risk exactly 1 R (its r_multiple), the curve a strategy sized to a fixed risk
  per trade would have (e.g. on micros). Daily Sharpe / Sortino over the same sessions (untraded = 0), profit factor
  and drawdown of the R curve, plus a resample Monte Carlo of the R multiples (seed + 4, see guban.montecarlo).

Breakdowns (both USD and R): per exit year, per stop-width tercile (USD risk per contract) and per exit signal.
Every max_dd_r here is a depth (>= 0), like the criterion it is judged against.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from guban.metrics import downside_deviation

CRITERIA = [  # (key, label, op, threshold)
    ("sharpe", "Sharpe (daily, annualized)", ">", 1.0),
    ("profit_factor", "Profit factor", ">", 1.2),
    ("max_dd_r", "Max drawdown (R)", "<=", 25.0),
    ("sortino", "Sortino (daily, annualized)", ">", 1.0),
]
DD_PERCENTILES = (50, 95, 99)
WIDTH_LABELS = ("Narrow", "Mid", "Wide")


def _passes(value, op: str, threshold: float) -> bool | None:
    if value is None or not math.isfinite(value):
        return None if value is None else (value > threshold if op == ">" else False)
    return value > threshold if op == ">" else value <= threshold


def _judge(values: dict) -> dict:
    checks = {key: _passes(values.get(key), op, thr) for key, _, op, thr in CRITERIA}
    return {**values, "pass": checks, "passed": sum(1 for v in checks.values() if v), "of": len(CRITERIA)}


def _pf(x: np.ndarray):
    gp, gl = x[x > 0].sum(), x[x < 0].sum()
    return float(gp / -gl) if gl else (math.inf if gp else None)


def _max_dd(x: np.ndarray) -> float:
    c = np.r_[0.0, np.cumsum(x)]
    return float((c - np.maximum.accumulate(c)).min())


def _daily_ratios(x: pd.Series, days, trading_days: int) -> tuple:
    d = x.groupby(np.asarray(days)).sum().to_numpy(float)
    d = np.r_[d, np.zeros(max(trading_days - len(d), 0))]
    sd, down = (float(d.std(ddof=1)) if len(d) > 1 else 0.0), downside_deviation(d)
    mean = d.mean() if len(d) else 0.0
    return (float(mean / sd * math.sqrt(252)) if sd > 0 else None, float(mean / down * math.sqrt(252)) if down > 0 else None)


def _groups(df: pd.DataFrame, key) -> list[dict]:
    rows = []
    for name, g in df.groupby(key, observed=True, sort=True):
        r = g["r"].dropna().to_numpy(float)
        rows.append({
            "group": str(name), "trades": len(g), "net": float(g["net"].sum()), "profit_factor": _pf(g["net"].to_numpy(float)),
            "win_rate": float((g["net"] > 0).mean()), "avg_r": float(r.mean()) if len(r) else None,
            "sum_r": float(r.sum()) if len(r) else None, "max_dd_r": -_max_dd(r) if len(r) else None,
        })  # fmt: skip
    return rows


def evaluate(trades: pd.DataFrame, metrics: dict, days, sims: int = 10_000, seed: int = 42, risk: float | None = None) -> dict | None:
    """Evaluation of a run's engine trades (net_pnl, r_multiple, risk_usd, qty, exit_time, exit_name).

    metrics: compute_metrics' result for the same run. days: each trade's trading day (exit session).
    sims/seed: the R Monte Carlo (0 sims: none). risk: `--mc-risk` (USD per contract) for the as-run R.
    """
    if not len(trades):
        return None
    acc, a = metrics["account"], metrics["all"]
    net = trades["net_pnl"].to_numpy(float)
    r = trades["r_multiple"].to_numpy(float) if "r_multiple" in trades else np.full(len(trades), np.nan)
    stop = np.isfinite(r)
    risk_usd = trades["risk_usd"].to_numpy(float) if "risk_usd" in trades else np.full(len(trades), np.nan)
    if risk is not None:
        r_usd, r_source = float(risk * trades["qty"].mean()), f"--mc-risk {risk:g} x avg contracts"
    elif stop.any():
        r_usd, r_source = float(np.nanmean(risk_usd[stop])), "average risk to the initial stop per trade"
    else:
        r_usd, r_source = None, "no trade has a stop"
    dd = acc["max_drawdown_close_to_close"]
    out = {
        "criteria": [{"key": k, "label": lbl, "op": op, "threshold": thr} for k, lbl, op, thr in CRITERIA],
        "r_usd": r_usd,
        "r_source": r_source,
        "as_run": _judge({
            "sharpe": acc["sharpe_daily"], "sortino": acc.get("sortino_daily"), "profit_factor": a["profit_factor"],
            "max_dd_usd": dd, "max_dd_intraday_usd": acc["max_drawdown_intraday"],
            "max_dd_r": -dd / r_usd if r_usd else None,
        }),  # fmt: skip
        "trades_with_stop": int(stop.sum()),
        "equal_risk": None,
    }
    df = pd.DataFrame({"net": net, "r": np.where(stop, r, np.nan), "year": pd.DatetimeIndex(trades["exit_time"]).year,
                       "exit": trades["exit_name"].astype(str).to_numpy()})  # fmt: skip
    out["by_year"] = _groups(df, "year")
    out["by_exit"] = _groups(df, "exit")
    per_contract = risk_usd / trades["qty"].to_numpy(float)
    out["by_stop_width"], out["stop_width_usd"] = [], []
    try:  # too few distinct stop widths for three bins: no breakdown
        width = pd.qcut(per_contract[stop], len(WIDTH_LABELS), labels=WIDTH_LABELS)
    except ValueError:
        width = None
    if width is not None:
        out["by_stop_width"] = _groups(df[stop].assign(width=width), "width")
        out["stop_width_usd"] = [float(e) for e in np.percentile(per_contract[stop], np.linspace(0, 100, len(WIDTH_LABELS) + 1))]
    if not stop.any():
        return out

    rs = r[stop]
    sharpe, sortino = _daily_ratios(pd.Series(rs), np.asarray(days)[stop], acc["trading_days"])
    eq = _judge({"sharpe": sharpe, "sortino": sortino, "profit_factor": _pf(rs), "max_dd_r": -_max_dd(rs),
                 "net_r": float(rs.sum()), "avg_r": float(rs.mean())})  # fmt: skip
    if sims:
        rng = np.random.default_rng(seed + 4)
        paths = rng.choice(rs, size=(sims, len(rs)))
        c = np.cumsum(paths, axis=1)
        mdd = -(c - np.maximum.accumulate(np.maximum(c, 0.0), axis=1)).min(axis=1)  # depth >= 0, from a 0 start
        limit = next(thr for k, _, _, thr in CRITERIA if k == "max_dd_r")
        eq["monte_carlo"] = {
            "sims": sims, "seed": seed + 4,
            "max_dd_r": {f"p{p}": float(np.percentile(mdd, p)) for p in DD_PERCENTILES},
            "p_dd_over_limit": float((mdd > limit).mean()),
            "net_r": {f"p{p}": float(np.percentile(c[:, -1], p)) for p in (5, 50, 95)},
            "p_net_le_0": float((c[:, -1] <= 0).mean()),
        }  # fmt: skip
    out["equal_risk"] = eq
    return out


def summary_line(ev: dict) -> str:
    """One stdout line: '1/4 as run (Sharpe 0.65 FAIL, ...); 3/4 at equal risk (...)'."""

    def part(v: dict) -> str:
        bits = []
        for k, lbl, _, _ in CRITERIA:
            x, ok = v.get(k), v["pass"][k]
            val = "n/a" if x is None else (f"{x:.1f}R" if k == "max_dd_r" else f"{x:.2f}")
            bits.append(f"{lbl.split(' (')[0]} {val} {'pass' if ok else 'FAIL'}")
        return f"{v['passed']}/{v['of']} (" + ", ".join(bits) + ")"

    s = f"as run {part(ev['as_run'])}"
    if ev["equal_risk"]:
        s += f"; equal risk {part(ev['equal_risk'])}"
    return s
