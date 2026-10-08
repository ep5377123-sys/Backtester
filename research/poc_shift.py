"""NQ RTH POC-shift study (PLAN.md Phase 13).

Question: when day D-1's RTH POC is above D-2's, is D's RTH POC above D-1's more often than on an average day?
(And the mirror case: a down-shift followed by another down-shift.)

Primary test (pre-registered, 2026-10-05): tick-data POCs on clean days. A side is REAL only if
  1. P(D POC up | D-1 POC up vs D-2) beats the base rate P(D POC up) by >= 3 percentage points,
  2. a one-sided exact binomial test against the base rate gives p < 0.05, and
  3. the gap is positive in every calendar year (a first year with < 60 observations is pooled into the next).
Everything else is secondary: the minute-bar POC study over the full minute history, the 2x2 table, shift-size
buckets, value-area relationships, RTH close > open on D, and minute-vs-tick agreement.

Data: the shared market-data store (D:\\Market Data\\NQ, Phase 15; `--store Database/NQ-CME` reads the DeepCharts folder as
Phase 13 did). From the store, a day's ticks are used only when quality.csv `tick_ok` is True (user rule, 2026-10-07).

Days: RTH 09:30-16:00 ET (to the scheduled early close on half-days). Clean (tick) day: RTH tick volume within 2% of the
same contract's RTH minute volume and the last RTH trade within 2 minutes of the close. A triple (D-2, D-1, D) needs
three consecutive trading days that are all clean and on the same contract (a roll's calendar spread is not a POC
shift). A zero shift is "flat": a flat outcome is left out of the rates; a flat signal counts only in the base rate.

    uv run python research/poc_shift.py [--rebuild] [--out reports/poc_shift]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import get_contract
from guban.data.ticks import load_rth_profiles
from guban.research.days import minute_bars, trading_days
from guban.research.profile import from_bars, from_volume
from guban.research.report import write_study_report
from guban.research.stats import by_period, rate_test, verdict

SYMBOL = "NQ"
def _default_store() -> Path:
    from guban.store.read import store_root

    return store_root() / SYMBOL
VOLUME_TOL = 0.02  # clean tick day: |RTH tick volume / RTH minute volume - 1| <= this
END_SLACK = pd.Timedelta(minutes=2)  # the data must reach within this of the RTH close
MIN_CLEAN_DAYS = 500
MIN_GAP, ALPHA = 0.03, 0.05
MIN_PERIOD_OBS = 60  # a first calendar year with fewer observations is pooled into the next
BUCKETS = [0, 5, 20, 50, math.inf]  # |D-1 - D-2 POC shift| in points
AGREE_PTS = 2.0
GAP_FLAG_DAYS = 4


# --- days -------------------------------------------------------------------------------------------------------


def build_days(store: Path, cache_dir: Path, rebuild: bool) -> pd.DataFrame:
    tick = get_contract(SYMBOL).tick_size
    days = trading_days(store, SYMBOL)
    t0 = time.perf_counter()

    def progress(i, n):
        if i % 100 == 0 or i == n:
            print(f"  tick files {i}/{n} ({time.perf_counter() - t0:.0f} s)", file=sys.stderr, flush=True)

    tsum, vap = load_rth_profiles(store, days, tick, cache_dir, rebuild, progress)
    print(f"tick profiles: {len(tsum)} days with a tick file ({time.perf_counter() - t0:.1f} s)", file=sys.stderr)
    vap_by_day = {d: g for d, g in vap.groupby("session_date", sort=False)}

    rows = []
    for day, d in days.iterrows():
        r = {"date": day, "contract": d.contract, "session_file": d.session_file, "rth_end": d.rth_end, "early_close": d.early_close}
        if "tick_ok" in d:
            r["tick_ok"] = bool(d.tick_ok)
        mb = minute_bars(store, d)
        if len(mb) and mb["volume"].sum() > 0:
            p = from_bars(mb["low"].to_numpy(), mb["high"].to_numpy(), mb["volume"].to_numpy(), tick)
            val, vah = p.value_area()
            r |= {"minute_volume": int(mb["volume"].sum()), "minute_last": mb.index[-1], "minute_open": mb["open"].iloc[0],
                  "minute_close": mb["close"].iloc[-1], "minute_poc": p.poc(), "minute_val": val, "minute_vah": vah}  # fmt: skip
        if day in tsum.index:
            s = tsum.loc[day]
            r |= {"tick_trades": int(s.trades), "tick_volume": int(s.volume)}
            if s.trades:
                g = vap_by_day[day]
                p = from_volume(g["price_tick"].to_numpy(), g["volume"].to_numpy(), tick)
                val, vah = p.value_area()
                r |= {"tick_last": s.last_trade, "tick_open": s.open, "tick_close": s.close, "tick_poc": p.poc(), "tick_val": val, "tick_vah": vah}
        rows.append(r)
    df = pd.DataFrame(rows).set_index("date")
    for c in ("minute_volume", "tick_volume", "tick_trades"):
        if c not in df:
            df[c] = np.nan
    df["volume_ratio"] = df["tick_volume"] / df["minute_volume"]
    df["minute_reason"] = [_minute_reason(r) for r in df.itertuples()]
    df["tick_reason"] = [_tick_reason(r) for r in df.itertuples()]
    df["minute_clean"] = df["minute_reason"] == ""
    df["tick_clean"] = df["tick_reason"] == ""
    df["days_since_prev"] = df.index.to_series().diff().dt.days
    df["gap_flag"] = df["days_since_prev"] > GAP_FLAG_DAYS
    return df


def _minute_reason(r) -> str:
    if pd.isna(r.minute_volume):
        return "no minute data"
    if r.minute_last < r.rth_end - END_SLACK:
        return "minute data ends early"
    return ""


def _tick_reason(r) -> str:
    if getattr(r, "tick_ok", True) is False:
        return "store tick check failed (tick_ok)"
    if pd.isna(r.tick_trades):
        return "no tick file"
    if r.tick_trades == 0:
        return "no RTH ticks"
    if pd.isna(r.minute_volume):
        return "no minute data to check against"
    if r.tick_last < r.rth_end - END_SLACK:
        return "ticks end early"
    if abs(r.volume_ratio - 1) > VOLUME_TOL:
        return "tick volume != minute volume"
    return ""


# --- triples ----------------------------------------------------------------------------------------------------


def va_relation(val1, vah1, val2, vah2) -> str:
    """Where D-1's value area sits relative to D-2's."""
    if val1 > vah2:
        return "higher"
    if vah1 < val2:
        return "lower"
    if vah1 <= vah2 and val1 >= val2:
        return "inside"
    if vah1 > vah2 and val1 < val2:
        return "outside"
    return "overlapping higher" if vah1 > vah2 else "overlapping lower"


def build_triples(days: pd.DataFrame, src: str) -> tuple[pd.DataFrame, dict]:
    """Consecutive trading-day triples for POC source `src` ("tick" or "minute"), and drop counts."""
    clean = days[f"{src}_clean"].to_numpy()
    contract = days["contract"].astype(str).to_numpy()
    poc, val, vah = (days[f"{src}_{k}"].to_numpy(float) for k in ("poc", "val", "vah"))
    op, cl = days[f"{src}_open"].to_numpy(float), days[f"{src}_close"].to_numpy(float)
    rows, drops = [], {"unclean day": 0, "contract roll": 0}
    for i in range(2, len(days)):
        k = [i - 2, i - 1, i]
        if not clean[k].all():
            drops["unclean day"] += 1
            continue
        if len(set(contract[k])) > 1:
            drops["contract roll"] += 1
            continue
        rows.append({
            "date": days.index[i], "study": src, "contract": contract[i],
            "poc_d2": poc[i - 2], "poc_d1": poc[i - 1], "poc_d": poc[i],
            "signal_shift": poc[i - 1] - poc[i - 2], "outcome_shift": poc[i] - poc[i - 1],
            "va_relation": va_relation(val[i - 1], vah[i - 1], val[i - 2], vah[i - 2]),
            "open_d": op[i], "close_d": cl[i],
        })  # fmt: skip
    t = pd.DataFrame(rows)
    if len(t):
        t["signal"] = np.sign(t["signal_shift"]).astype(int)
        t["outcome"] = np.sign(t["outcome_shift"]).astype(int)
    return t, drops


def periods_for(dates: pd.Series) -> np.ndarray:
    years = dates.dt.year.to_numpy()
    uniq, counts = np.unique(years, return_counts=True)
    labels = {y: str(y) for y in uniq}
    if len(uniq) > 1 and counts[0] < MIN_PERIOD_OBS:
        labels[uniq[0]] = labels[uniq[1]] = f"{uniq[0]}-{uniq[1]}"
    return np.array([labels[y] for y in years])


# --- analysis ---------------------------------------------------------------------------------------------------


def analyze(t: pd.DataFrame) -> dict:
    obs = t[t["outcome"] != 0]
    per = periods_for(obs["date"])
    out = {"triples": len(t), "flat_outcomes": int((t["outcome"] == 0).sum()), "flat_signals": int((t["signal"] == 0).sum()),
           "observations": len(obs), "first": str(t["date"].min().date()), "last": str(t["date"].max().date())}  # fmt: skip
    for side, s in (("up", 1), ("down", -1)):
        hit, sig = (obs["outcome"] == s).to_numpy(), (obs["signal"] == s).to_numpy()
        test = rate_test(hit, sig, f"P(D POC {side} | D-1 shifted {side})")
        yearly = by_period(hit, sig, per)
        out[side] = {"test": test.to_dict(), "by_year": _records(yearly), "verdict": verdict(test, yearly, MIN_GAP, ALPHA).to_dict()}
        # shift-size buckets (secondary)
        m = obs["signal"] == s
        size = obs.loc[m, "signal_shift"].abs()
        b = pd.cut(size, BUCKETS, right=True, include_lowest=False)
        g = (obs.loc[m, "outcome"] == s).groupby(b, observed=False)
        out[side]["by_shift_size"] = [
            {"bucket": _bucket_label(iv), "n": int(n), "rate": float(r) if n else None}
            for iv, n, r in zip(g.size().index, g.size().to_numpy(), g.mean().to_numpy())
        ]
        # secondary outcome: RTH close vs open on D
        co = t[(t["close_d"] != t["open_d"])]
        hit2 = (np.sign(co["close_d"] - co["open_d"]) == s).to_numpy()
        out[side]["close_vs_open"] = rate_test(hit2, (co["signal"] == s).to_numpy(), f"P(D close {'>' if s > 0 else '<'} open | D-1 shifted {side})").to_dict()
    # 2x2 (+ flat signal row)
    ct = pd.crosstab(t["signal"].map({1: "up", -1: "down", 0: "flat"}), t["outcome"].map({1: "up", -1: "down", 0: "flat"}))
    out["crosstab"] = {str(r): {str(c): int(ct.loc[r, c]) for c in ct.columns} for r in ct.index}
    # value-area relationship of D-1 vs D-2 (secondary)
    va = []
    for rel, g in obs.groupby("va_relation"):
        va.append({"relation": rel, "n": len(g), "p_up": float((g["outcome"] > 0).mean()), "p_down": float((g["outcome"] < 0).mean())})
    out["by_va_relation"] = sorted(va, key=lambda r: _VA_ORDER.index(r["relation"]))
    return out


_VA_ORDER = ["higher", "overlapping higher", "inside", "outside", "overlapping lower", "lower"]


def _bucket_label(iv) -> str:
    return f"> {iv.left:g} pts" if math.isinf(iv.right) else f"{iv.left:g}–{iv.right:g} pts"


def _records(df: pd.DataFrame) -> list[dict]:
    ints = {"n", "hits", "base_n"}
    return [{"period": str(k), **{c: (int(v) if c in ints else float(v)) for c, v in row.items()}} for k, row in df.to_dict("index").items()]


def agreement(days: pd.DataFrame, tick_triples: pd.DataFrame) -> dict:
    both = days[days["tick_clean"] & days["minute_clean"]]
    diff = (both["minute_poc"] - both["tick_poc"]).abs()
    # minute POCs on the same three days as each tick triple
    pos = days.index.get_indexer(tick_triples["date"])
    mp = days["minute_poc"].to_numpy(float)
    msig = np.sign(mp[pos - 1] - mp[pos - 2])
    mout = np.sign(mp[pos] - mp[pos - 1])
    return {
        "days": len(both),
        "poc_within_pts": AGREE_PTS,
        "share_poc_within": float((diff <= AGREE_PTS).mean()),
        "median_abs_diff_pts": float(diff.median()),
        "p90_abs_diff_pts": float(diff.quantile(0.9)),
        "triples": len(tick_triples),
        "share_signal_direction_agrees": float((msig == tick_triples["signal"].to_numpy()).mean()),
        "share_outcome_direction_agrees": float((mout == tick_triples["outcome"].to_numpy()).mean()),
    }


# --- output -----------------------------------------------------------------------------------------------------


def pct(x, nd=1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{nd}f}%"


def pp(x) -> str:
    return f"{x * 100:+.1f} pp"


def print_summary(res: dict) -> None:
    tk = res["tick"]
    print(f"\nNQ RTH POC-shift study - tick POCs, {tk['first']} -> {tk['last']}: {tk['observations']} observations "
          f"({tk['triples']} triples, {tk['flat_outcomes']} flat outcomes)")  # fmt: skip
    for side in ("up", "down"):
        s = tk[side]
        t, v = s["test"], s["verdict"]
        print(f"\n  {side.upper()}: {t['label']} = {pct(t['rate'])} ({t['hits']}/{t['n']})  vs base {pct(t['base_rate'])} "
              f"({t['base_hits']}/{t['base_n']})  gap {pp(t['gap'])}  p = {t['p_value']:.4g}")  # fmt: skip
        for y in s["by_year"]:
            print(f"      {y['period']:>10}: {pct(y['rate'])} vs {pct(y['base_rate'])}  gap {pp(y['gap'])}  (n={y['n']})")
        print(f"    verdict: {'REAL' if v['real'] else 'NOT REAL'}  -  " + "; ".join(f"{'ok' if c['passed'] else 'FAIL'} {c['rule']} ({c['detail']})" for c in v["checks"]))
    a = res["agreement"]
    print(f"\n  minute vs tick POC: within {a['poc_within_pts']:g} pts on {pct(a['share_poc_within'])} of {a['days']} days "
          f"(median diff {a['median_abs_diff_pts']:.2f}); signal direction agrees {pct(a['share_signal_direction_agrees'])}, "
          f"outcome {pct(a['share_outcome_direction_agrees'])}")  # fmt: skip
    mn = res["minute"]
    print(f"  secondary, minute POCs {mn['first']} -> {mn['last']} ({mn['observations']} obs): "
          + "; ".join(f"{s} {pct(mn[s]['test']['rate'])} vs {pct(mn[s]['test']['base_rate'])} p={mn[s]['test']['p_value']:.3g} "
                      f"{'REAL' if mn[s]['verdict']['real'] else 'NOT REAL'}" for s in ("up", "down")))  # fmt: skip


def write_report(res: dict, path: Path) -> None:
    tk, mn = res["tick"], res["minute"]
    verdicts = []
    for side in ("up", "down"):
        t = tk[side]["test"]
        verdicts.append({
            "label": f"{side.capitalize()}-shift continues (tick, primary)", "real": tk[side]["verdict"]["real"],
            "headline": f"{pct(t['rate'])} after a {side}-shift vs {pct(t['base_rate'])} on all days ({pp(t['gap'])}, n = {t['n']})",
            "checks": tk[side]["verdict"]["checks"],
        })  # fmt: skip
    periods = [y["period"] for y in tk["up"]["by_year"]]
    charts = [
        {"id": "years", "title": "Gap over the base rate by year (tick, primary)", "yformat": "+.1f", "ref": MIN_GAP * 100,
         "caption": "Conditional rate minus that year's base rate, in percentage points. Rule 3 needs every bar above 0; the dashed line is rule 1's 3 pp.",
         "x": periods, "series": [{"name": f"{s.capitalize()} after {s}", "y": [round(y["gap"] * 100, 2) for y in tk[s]["by_year"]]} for s in ("up", "down")]},
        {"id": "sizes", "title": "Continuation rate by size of the D-1 shift (tick, secondary)", "yformat": ".1f",
         "caption": "P(D's POC moves the same way as D-1's) by |POC(D-1) − POC(D-2)|. Dashed line: 50%.", "ref": 50,
         "x": [b["bucket"] for b in tk["up"]["by_shift_size"]],
         "series": [{"name": f"{s.capitalize()} after {s}", "y": [None if b["rate"] is None else round(b["rate"] * 100, 2) for b in tk[s]["by_shift_size"]]} for s in ("up", "down")]},
    ]  # fmt: skip

    def test_rows(study, name):
        rows = {}
        for s in ("up", "down"):
            for key, lab in (("test", "POC"), ("close_vs_open", "close vs open")):
                t = study[s][key]
                rows[f"{name}: {t['label']}"] = [f"{t['hits']}/{t['n']}", pct(t["rate"]), pct(t["base_rate"]), pp(t["gap"]), f"{t['p_value']:.4g}"]
        return rows

    tests = pd.DataFrame.from_dict({**test_rows(tk, "Tick"), **test_rows(mn, "Minute")}, orient="index",
                                   columns=["hits / n", "rate", "base rate", "gap", "p (one-sided)"]).rename_axis("test")  # fmt: skip
    yearly = pd.DataFrame(
        [[f"{y['period']}", s, f"{y['hits']}/{y['n']}", pct(y["rate"]), pct(y["base_rate"]), pp(y["gap"]), f"{y['p_value']:.3g}"]
         for s in ("up", "down") for y in tk[s]["by_year"]],
        columns=["year", "side", "hits / n", "rate", "base rate", "gap", "p"]).set_index("year")  # fmt: skip
    ct = pd.DataFrame(tk["crosstab"]).T.fillna(0).astype(int).rename_axis("D-1 shift \\ D shift")
    va = pd.DataFrame([[r["relation"], r["n"], pct(r["p_up"]), pct(r["p_down"])] for r in tk["by_va_relation"]],
                      columns=["D-1 value area vs D-2", "n", "P(D POC up)", "P(D POC down)"]).set_index("D-1 value area vs D-2")  # fmt: skip
    a = res["agreement"]
    agree = pd.DataFrame.from_dict({
        "clean days compared": a["days"], f"POC within {a['poc_within_pts']:g} pts": pct(a["share_poc_within"]),
        "median |minute − tick| POC": f"{a['median_abs_diff_pts']:.2f} pts", "90th pct |minute − tick| POC": f"{a['p90_abs_diff_pts']:.2f} pts",
        "tick triples": a["triples"], "signal direction agrees": pct(a["share_signal_direction_agrees"]),
        "outcome direction agrees": pct(a["share_outcome_direction_agrees"]),
    }, orient="index", columns=["value"]).rename_axis("minute vs tick")  # fmt: skip
    data = res["data"]
    drops = pd.DataFrame.from_dict(
        {**{f"tick day: {k}": v for k, v in data["tick_day_reasons"].items()},
         **{f"tick triple dropped: {k}": v for k, v in data["tick_triple_drops"].items()},
         **{f"minute day: {k}": v for k, v in data["minute_day_reasons"].items()},
         **{f"minute triple dropped: {k}": v for k, v in data["minute_triple_drops"].items()}},
        orient="index", columns=["count"]).rename_axis("data")  # fmt: skip
    write_study_report(
        path,
        {"title": "NQ RTH POC-shift study",
         "subtitle": "Does a higher (lower) RTH POC on D-1 than on D-2 make a higher (lower) POC on D more likely than on an average day?",
         "details": {"Primary data": f"tick POCs, {tk['first']} → {tk['last']}", "Observations": f"{tk['observations']:,} (flat outcomes left out: {tk['flat_outcomes']})",
                     "Clean tick days": f"{data['tick_clean_days']:,} of {data['trading_days']:,}", "Session": "RTH 09:30–16:00 ET (early closes respected)",
                     "Rule": f"gap ≥ {MIN_GAP * 100:g} pp, p < {ALPHA:g} (one-sided exact binomial), gap > 0 every year"}},
        verdicts=verdicts, charts=charts,
        tables=[
            {"title": "Tests", "caption": "Primary: the Tick POC rows. Minute rows use 1-minute-bar POCs over the full minute history; close vs open uses RTH open and close on D. All but the Tick POC rows are secondary.", "frame": tests},
            {"title": "By year (tick, primary)", "caption": "Base rate taken within each year. A first year with fewer than 60 observations is pooled into the next.", "frame": yearly},
            {"title": "Signal × outcome counts (tick)", "caption": "Rows: D-1 POC vs D-2. Columns: D POC vs D-1.", "frame": ct},
            {"title": "By value-area relationship (tick, secondary)", "caption": "Where D-1's 70% value area sits relative to D-2's, and how often D's POC then moves up or down.", "frame": va},
            {"title": "Minute-bar POC vs tick POC", "caption": "On days clean in both. Minute POCs spread each 1-minute bar's volume evenly from its low to its high.", "frame": agree},
            {"title": "Data", "caption": "Why days and triples were left out.", "frame": drops},
        ],
        notes=[
            "A clean tick day: RTH tick volume within 2% of the same contract's RTH minute volume and the last trade within 2 minutes of the RTH close. Some tick sessions in the store stop at 09:29:59 or repeat every trade three times; they are excluded, not repaired. From the market-data store, only sessions whose quality.csv tick_ok is True are used.",
            "Triples spanning a contract roll are dropped: the calendar spread between contracts is not a POC shift.",
            "The p-value asks how likely a conditional rate at least this high would be if the signal carried no information (Binomial(n, base rate)). Tests on overlapping triples are not fully independent, so treat p-values near 0.05 with care.",
            "POC ties go to the price nearest the middle of the day's range. Value area: grown one tick at a time from the POC toward the side with more volume until it holds 70%.",
        ],
    )  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--store", type=Path, default=None, help="symbol folder: the market-data store's (default) or a DeepCharts one")
    ap.add_argument("--cache", type=Path, default=Path("data") / SYMBOL)
    ap.add_argument("--out", type=Path, default=Path("reports/poc_shift"))
    ap.add_argument("--rebuild", action="store_true", help="re-read every tick file instead of using the cache")
    args = ap.parse_args()

    days = build_days(args.store or _default_store(), args.cache, args.rebuild)
    n_clean = int(days["tick_clean"].sum())
    if n_clean < MIN_CLEAN_DAYS:
        reasons = days.loc[~days["tick_clean"], "tick_reason"].value_counts().to_dict()
        print(f"Error: only {n_clean} clean tick days (need {MIN_CLEAN_DAYS}); left out: {reasons}", file=sys.stderr)
        return 2
    tick_t, tick_drops = build_triples(days, "tick")
    minute_t, minute_drops = build_triples(days, "minute")
    res = {
        "question": "P(D RTH POC up | POC(D-1) > POC(D-2)) vs P(D RTH POC up), and the mirror for down",
        "rule": {"min_gap": MIN_GAP, "alpha": ALPHA, "test": "one-sided exact binomial vs base rate", "every_year_positive": True,
                 "min_period_obs": MIN_PERIOD_OBS},  # fmt: skip
        "data": {
            "trading_days": len(days), "tick_clean_days": n_clean, "minute_clean_days": int(days["minute_clean"].sum()),
            "tick_day_reasons": {k: int(v) for k, v in days.loc[~days["tick_clean"], "tick_reason"].value_counts().items()},
            "minute_day_reasons": {k: int(v) for k, v in days.loc[~days["minute_clean"], "minute_reason"].value_counts().items()},
            "tick_triple_drops": tick_drops, "minute_triple_drops": minute_drops,
            "volume_tolerance": VOLUME_TOL, "end_slack_min": END_SLACK.total_seconds() / 60,
        },  # fmt: skip
        "tick": analyze(tick_t),
        "minute": analyze(minute_t),
        "agreement": agreement(days, tick_t),
    }
    res["verdict"] = {s: res["tick"][s]["verdict"]["real"] for s in ("up", "down")}

    args.out.mkdir(parents=True, exist_ok=True)
    days.to_csv(args.out / "days.csv")
    pd.concat([tick_t, minute_t]).to_csv(args.out / "triples.csv", index=False)
    (args.out / "results.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    write_report(res, args.out / "report.html")
    print_summary(res)
    print(f"\nwrote {args.out}/days.csv, triples.csv, results.json, report.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
