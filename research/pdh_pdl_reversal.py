"""Does price reverse from the previous day's high or low? NQ 15-minute sweep-and-reject study (Phase 26, 2026-10-29).

Rules (user, 2026-10-29). The previous day is the futures daily bar, the 18:00-17:00 ET session; its high and low
(PDH / PDL) are the levels for the next session, and every 15-minute candle of that session can signal:
  bearish   high > PDH, close < open, close < PDH   (sweeps the high, closes red back below it)
  bullish   low < PDL,  close > open, close > PDL   (sweeps the low, closes green back above it)
Every qualifying candle counts, not only the first of the session. From the signal candle's close, on 1-minute bars of
the rest of the session: success = price moves `target` points (50) from the close in the reversal direction before it
takes out the signal candle's wick (its high for bearish, its low for bullish); failure = the wick goes first.
  ambiguous    one 1-minute bar reaches both: counted as a failure (1-minute is the finest bar data) and reported
  unresolved   neither by the session's close (17:00 ET or an early close): left out of the rates and reported
Risk = |close - wick| points, so a signal scores +target or -risk; breakeven success rate = risk / (risk + target).
Sessions whose contract differs from the previous session's (rolls) are dropped: the levels would be the old contract's.
Control: every other red (green) 15-minute candle of the same sessions, run through the same race with its own wick.

Verdict per side (pre-registered): REAL only if the session-clustered bootstrap 95% CI of the expectancy (points per
signal, before costs) is above 0, the expectancy beats the control candles', and it is positive in every year.

    uv run python research/pdh_pdl_reversal.py [--symbol NQ] [--target 50] [--out reports/pdh_pdl_reversal]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import get_contract
from guban.data.resample import load_bars
from guban.research.report import write_study_report

SUCCESS, FAILURE, AMBIGUOUS, UNRESOLVED = "success", "failure", "ambiguous", "unresolved"
BOOT = 2000
SEED = 42


def daily_levels(bars: pd.DataFrame) -> pd.DataFrame:
    """Per session_date: pdh / pdl (the previous session's high / low) and roll (the contract changed since then).
    The first session has NaN levels."""
    g = bars.groupby("session_date", sort=True)
    day = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "contract": g["contract"].first()})
    return pd.DataFrame({
        "pdh": day["high"].shift(1),
        "pdl": day["low"].shift(1),
        "roll": day["contract"].ne(day["contract"].shift(1)) & day["contract"].shift(1).notna(),
    })


def signals(candles: pd.DataFrame, levels: pd.DataFrame) -> pd.DataFrame:
    """The 15-minute candles with their session's levels and a `side` column: "bearish", "bullish", or "" (none).
    Candles of roll sessions and of sessions without levels are dropped."""
    c = candles.join(levels, on="session_date")
    c = c[c["pdh"].notna() & ~c["roll"].astype(bool)]
    bear = (c["high"] > c["pdh"]) & (c["close"] < c["open"]) & (c["close"] < c["pdh"])
    bull = (c["low"] < c["pdl"]) & (c["close"] > c["open"]) & (c["close"] > c["pdl"])
    return c.assign(side=np.select([bear, bull], ["bearish", "bullish"], ""))


def race(high: np.ndarray, low: np.ndarray, side: int, entry: float, wick: float, target: float) -> tuple[str, int, float]:
    """Race on the 1-minute bars after the signal (high/low arrays, in order, to the session's close).

    side +1 = bullish (target above, wick below), -1 = bearish. Returns (outcome, bars to the resolving bar (1 = the
    first bar) or 0 when unresolved, max favorable move in points up to the resolving bar or the session's close).
    """
    if side < 0:
        hit_t, hit_w, fav = low <= entry - target, high > wick, entry - low
    else:
        hit_t, hit_w, fav = high >= entry + target, low < wick, high - entry
    it = int(np.argmax(hit_t)) if hit_t.any() else len(high)
    iw = int(np.argmax(hit_w)) if hit_w.any() else len(high)
    end = min(it, iw)
    mfe = float(fav[: end + 1].max()) if len(high) else 0.0
    if end == len(high):
        return UNRESOLVED, 0, max(mfe, 0.0)
    outcome = AMBIGUOUS if it == iw else SUCCESS if it < iw else FAILURE
    return outcome, end + 1, max(mfe, 0.0)


def run_races(c: pd.DataFrame, minutes: pd.DataFrame, target: float) -> pd.DataFrame:
    """Race every red/green candle of `c` (signals and control); adds risk_pts, outcome, minutes_to_resolution,
    max_favorable_pts, signal (bool)."""
    c = c[(c["close"] != c["open"])].copy()
    bull = (c["side"] == "bullish") | ((c["side"] == "") & (c["close"] > c["open"]))
    c["dir"] = np.where(bull, 1, -1)
    c["wick"] = np.where(bull, c["low"], c["high"])
    c["risk_pts"] = (c["close"] - c["wick"]).abs()
    c["signal"] = c["side"] != ""
    out, mins, mfe = [], [], []  # c is in time order, so groupby(sort=False) walks its rows in order
    by_session = {d: g for d, g in minutes.groupby("session_date", sort=False)}
    for d, g in c.groupby("session_date", sort=False):
        m = by_session.get(d)
        times = m.index.values if m is not None else np.array([], "datetime64[ns]")
        hi = m["high"].to_numpy() if m is not None else np.array([])
        lo = m["low"].to_numpy() if m is not None else np.array([])
        starts = np.searchsorted(times, g.index.values, side="right")
        for s, dr, e, w in zip(starts, g["dir"].to_numpy(), g["close"].to_numpy(), g["wick"].to_numpy()):
            o, n, f = race(hi[s:], lo[s:], int(dr), float(e), float(w), target)
            out.append(o), mins.append(n), mfe.append(f)
    c["outcome"], c["minutes_to_resolution"], c["max_favorable_pts"] = out, mins, mfe
    return c


def score(df: pd.DataFrame, target: float) -> pd.Series:
    """Points per resolved row: +target for a success, -risk otherwise (ambiguous counts as a failure)."""
    return pd.Series(np.where(df["outcome"] == SUCCESS, target, -df["risk_pts"]), index=df.index)


def bootstrap_ci(pts: pd.Series, sessions: pd.Series, reps: int = BOOT, seed: int = SEED) -> tuple[float, float]:
    """95% CI of the mean of `pts`, resampling whole sessions with replacement (signals in a session overlap)."""
    if pts.empty:
        return float("nan"), float("nan")
    g = pd.DataFrame({"p": pts.to_numpy(), "s": sessions.to_numpy()}).groupby("s")["p"].agg(["sum", "count"])
    s, n = g["sum"].to_numpy(), g["count"].to_numpy()
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(g), size=(reps, len(g)))
    means = s[idx].sum(1) / n[idx].sum(1)
    return float(np.quantile(means, 0.025)), float(np.quantile(means, 0.975))


def summary(df: pd.DataFrame, target: float, cost_pts: float, ci: bool = False) -> dict:
    res = df[df["outcome"] != UNRESOLVED]
    pts = score(res, target)
    out = {
        "signals": len(df), "success": int((df["outcome"] == SUCCESS).sum()),
        "failure": int((df["outcome"] == FAILURE).sum()), "ambiguous": int((df["outcome"] == AMBIGUOUS).sum()),
        "unresolved": int((df["outcome"] == UNRESOLVED).sum()),
        "success_rate": float((res["outcome"] == SUCCESS).mean()) if len(res) else float("nan"),
        "breakeven_rate": float((res["risk_pts"] / (res["risk_pts"] + target)).mean()) if len(res) else float("nan"),
        "median_risk_pts": float(res["risk_pts"].median()) if len(res) else float("nan"),
        "expectancy_pts": float(pts.mean()) if len(res) else float("nan"),
        "expectancy_net_pts": float(pts.mean() - cost_pts) if len(res) else float("nan"),
    }
    if ci:
        out["ci_low"], out["ci_high"] = bootstrap_ci(pts, res["session_date"])
    return out


def frame(rows: dict, index_name: str) -> pd.DataFrame:
    t = pd.DataFrame.from_dict(rows, orient="index")
    f = pd.DataFrame(index=t.index.rename(index_name))
    f["signals"] = t["signals"].map("{:,}".format)
    f["success / failure"] = [f"{s:,} / {fl + a:,}" for s, fl, a in zip(t["success"], t["failure"], t["ambiguous"])]
    f["ambiguous"] = t["ambiguous"].map("{:,}".format)
    f["unresolved"] = t["unresolved"].map("{:,}".format)
    f["success rate"] = t["success_rate"].map("{:.1%}".format)
    f["breakeven"] = t["breakeven_rate"].map("{:.1%}".format)
    f["median risk"] = t["median_risk_pts"].map("{:.2f}".format)
    f["exp. pts"] = t["expectancy_pts"].map("{:+.2f}".format)
    f["exp. net pts"] = t["expectancy_net_pts"].map("{:+.2f}".format)
    if "ci_low" in t:
        f["95% CI (pts)"] = [f"{lo:+.2f} … {hi:+.2f}" for lo, hi in zip(t["ci_low"], t["ci_high"])]
    return f


def study(symbol: str, target: float, out: Path) -> dict:
    spec = get_contract(symbol)
    cost_pts = (spec.commission + spec.slippage) / spec.point_value
    minutes = load_bars(symbol, "1m", "eth")
    candles = load_bars(symbol, "15m", "eth")
    levels = daily_levels(minutes)
    c = signals(candles, levels)
    rolls = int(levels["roll"].sum())
    r = run_races(c, minutes, target)
    r["year"] = r.index.year
    r["hour"] = (r.index - pd.Timedelta(minutes=15)).hour  # the candle's opening hour, ET
    sig, ctl = r[r["signal"]], r[~r["signal"]]

    sides = {"bearish": "Bearish at PDH", "bullish": "Bullish at PDL"}
    overview = {sides[s]: summary(sig[sig["side"] == s], target, cost_pts, ci=True) for s in sides}
    overview["Both"] = summary(sig, target, cost_pts, ci=True)
    for s, ctl_dir in (("bearish", -1), ("bullish", 1)):
        overview[f"Control: other {'red' if ctl_dir < 0 else 'green'} candles"] = summary(ctl[ctl["dir"] == ctl_dir], target, cost_pts)
    firsts = sig[~sig.duplicated(["session_date", "side"])]
    first = {sides[s]: summary(firsts[firsts["side"] == s], target, cost_pts, ci=True) for s in sides}

    years = sorted(r["year"].unique())
    by_year = {s: {y: summary(sig[(sig["side"] == s) & (sig["year"] == y)], target, cost_pts) for y in years} for s in sides}
    terc = {}
    for s, d in sides.items():
        x = sig[sig["side"] == s]
        edges = x["risk_pts"].quantile([0, 1 / 3, 2 / 3, 1]).to_numpy()
        lab = pd.cut(x["risk_pts"], np.unique(edges), include_lowest=True)
        cx = ctl[(ctl["dir"] == (-1 if s == "bearish" else 1))]
        lab_c = pd.cut(cx["risk_pts"], np.unique(edges), include_lowest=True)
        for iv, g in x.groupby(lab, observed=True):
            terc[f"{d}, risk {iv.left:.2f}–{iv.right:.2f}"] = summary(g, target, cost_pts)
            terc[f"  control, risk {iv.left:.2f}–{iv.right:.2f}"] = summary(cx[lab_c == iv], target, cost_pts)
    hours = sorted(sig["hour"].unique(), key=lambda h: (h - 18) % 24)
    by_hour = {f"{h:02d}:00": summary(sig[sig["hour"] == h], target, cost_pts) for h in hours}

    verdicts = []
    for s, d in sides.items():
        o = overview[d]
        ctl_exp = overview[f"Control: other {'red' if s == 'bearish' else 'green'} candles"]["expectancy_pts"]
        neg = [str(y) for y, v in by_year[s].items() if not v["expectancy_pts"] > 0]
        checks = [
            {"rule": "bootstrap 95% CI of expectancy > 0", "passed": bool(o["ci_low"] > 0),
             "detail": f"{o['ci_low']:+.2f} … {o['ci_high']:+.2f} pts"},
            {"rule": "beats the control candles", "passed": bool(o["expectancy_pts"] > ctl_exp),
             "detail": f"{o['expectancy_pts']:+.2f} vs {ctl_exp:+.2f} pts"},
            {"rule": "expectancy > 0 every year", "passed": not neg,
             "detail": "all positive" if not neg else "not positive in " + ", ".join(neg)},
        ]
        verdicts.append({
            "label": d, "real": all(c["passed"] for c in checks),
            "headline": f"{o['success_rate']:.1%} success vs {o['breakeven_rate']:.1%} breakeven; {o['expectancy_pts']:+.2f} pts/signal ({o['expectancy_net_pts']:+.2f} net)",
            "checks": checks,
        })

    out.mkdir(parents=True, exist_ok=True)
    cols = ["side", "session_date", "pdh", "pdl", "open", "high", "low", "close", "risk_pts", "outcome",
            "minutes_to_resolution", "max_favorable_pts"]
    sig[cols].rename_axis("candle_close").to_csv(out / "signals.csv")
    span = f"{r.index.min():%Y-%m-%d} → {r.index.max():%Y-%m-%d}"
    charts = [
        {"id": "year_exp", "title": "Expectancy by year", "yformat": "+.1f", "ref": 0,
         "caption": f"Points per resolved signal before costs: +{target:g} for a success, −risk for a failure.",
         "x": years, "series": [{"name": sides[s], "y": [round(by_year[s][y]["expectancy_pts"], 3) for y in years]} for s in sides]},
        {"id": "year_rate", "title": "Success rate vs breakeven by year", "yformat": ".0%",
         "caption": "Success rate of resolved signals and the average breakeven rate risk / (risk + target), both sides together.",
         "x": years, "series": [
             {"name": "success rate", "y": [round(summary(sig[sig["year"] == y], target, cost_pts)["success_rate"], 4) for y in years]},
             {"name": "breakeven", "y": [round(summary(sig[sig["year"] == y], target, cost_pts)["breakeven_rate"], 4) for y in years]}]},
        {"id": "hour_exp", "title": "Expectancy by hour (ET)", "yformat": "+.1f", "ref": 0,
         "caption": "Both sides, by the signal candle's opening hour, session order from 18:00.",
         "x": list(by_hour), "series": [{"name": "exp. pts", "y": [round(v["expectancy_pts"], 3) for v in by_hour.values()]}]},
    ]
    tables = [
        {"title": "Overview", "caption": "Every qualifying candle. Rates and expectancy over resolved signals (ambiguous = failure). Net = after "
         f"{spec.symbol} default costs (${spec.commission + spec.slippage:.2f} RT = {cost_pts:.3f} pts). CI resamples whole sessions.",
         "frame": frame(overview, "")},
        {"title": "First signal of the session per side", "caption": "Reference view: only the first qualifying candle of each session and side.",
         "frame": frame(first, "")},
        {"title": "By risk size", "caption": "Risk thirds of each side's signals; control rows = the other same-colour candles with risk in the same range.",
         "frame": frame(terc, "")},
        *({"title": f"{sides[s]} by year", "caption": "", "frame": frame(by_year[s], "year")} for s in sides),
        {"title": "By hour (ET)", "caption": "Both sides, by the signal candle's opening hour.", "frame": frame(by_hour, "hour")},
    ]
    notes = [
        f"Data: {symbol} 1-minute ETH bars (18:00–17:00 ET), {span}; 15-minute candles from the same bars. Unadjusted continuous contract.",
        f"Roll sessions dropped: {rolls} (their previous day is the old contract).",
        "Ambiguous = one 1-minute bar reaches both the target and the wick; the order inside the minute is unknown, so it counts as a failure.",
        "Unresolved = neither reached by the session's close; left out of the rates and expectancy.",
        "Signals in the same session overlap (every qualifying candle counts), so the CI resamples whole sessions.",
    ]
    write_study_report(
        out / "report.html",
        {"title": f"{symbol} previous-day high/low reversal",
         "subtitle": f"15-minute candles that sweep the previous day's high (low) and close back inside: does price run {target:g} points before the wick is taken out?",
         "details": {"symbol": symbol, "span": span, "target": f"{target:g} pts", "signals": f"{len(sig):,}",
                     "control candles": f"{len(ctl):,}"}},
        verdicts=verdicts, charts=charts, tables=tables, notes=notes,
    )
    result = {"symbol": symbol, "target": target, "span": span, "rolls_dropped": rolls, "overview": overview,
              "first_per_session": first, "verdicts": verdicts}
    (out / "study.json").write_text(json.dumps(result, indent=2, default=float), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--target", type=float, default=50.0, help="points from the signal close")
    ap.add_argument("--out", type=Path, default=Path("reports/pdh_pdl_reversal"))
    a = ap.parse_args()
    res = study(a.symbol, a.target, a.out)
    for name, o in res["overview"].items():
        print(f"{name:32s} n={o['signals']:6,} success {o['success_rate']:.1%} breakeven {o['breakeven_rate']:.1%} "
              f"exp {o['expectancy_pts']:+.2f} pts (net {o['expectancy_net_pts']:+.2f}) unresolved {o['unresolved']:,}")
    for v in res["verdicts"]:
        print(f"{v['label']}: {'REAL' if v['real'] else 'NOT REAL'}")
    print(f"wrote {a.out / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
