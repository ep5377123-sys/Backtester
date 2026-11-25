"""How often does an open RTH gap fill between 9:30 and 11:30 ET -- by gap size, days since it opened, and ATR(20)? (2026-11-01)

Rules (user, 2026-11-01; plan approved the same day):
  gap         today's 9:30 bar open vs the previous trading day's last RTH close (16:00, or the early close); zero gaps skipped
  open gap    a gap stays open until any later 1-minute bar (ETH included) trades its level (the prior close): low <= level
              for an up gap, high >= level for a down gap. A fill overnight retires it before the next morning.
  observation every gap still open at a session's 9:30 bar: age 0 = the gap that opened today, 1 = yesterday's, ...
              (age counts sessions with a 9:30 bar). filled = a bar starting 9:30 .. --end trades the level.
  ATR(20)     simple mean of the previous 20 RTH true ranges (prior RTH close included), known before 9:30.
  rolls       the data is an unadjusted continuous contract, so a roll's first session makes no gap and every gap still
              open is censored there (dropped, neither filled nor unfilled): their levels belong to the old contract.
Fill rates carry Wilson 95% CIs; logistic regressions (statsmodels) test size, age and ATR together. Several open gaps
on one morning share that morning's price path, so the all-gaps regression clusters its errors by date.

    uv run --with statsmodels python research/gap_fill.py
    uv run --with statsmodels python research/gap_fill.py --end 16:00 --out reports/gap_fill_1600
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.api as sm

from guban.data.resample import load_bars
from guban.research.report import write_study_report

Z = 1.959963984540054
PTS_BINS = [0, 10, 25, 50, 100, np.inf]
ATR_BINS = [0, 0.05, 0.1, 0.2, 0.4, np.inf]
AGE_BINS = [-1, 0, 1, 2, 5, 10, np.inf]
AGE_LABELS = ["0 (today)", "1", "2", "3–5", "6–10", "11+"]
THIRDS = ["low", "mid", "high"]


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * np.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return float(c - h), float(c + h)


def bin_labels(edges: list[float], fmt: str) -> list[str]:
    return [f"{fmt.format(a)}–{fmt.format(b)}" if np.isfinite(b) else f"{fmt.format(a)}+" for a, b in zip(edges, edges[1:])]


def daily(bars: pd.DataFrame) -> pd.DataFrame:
    """One row per session with RTH bars: 9:30 open (NaN without a bar starting at 9:30), RTH high/low/close, contract,
    previous RTH close, roll flag, ATR(20) known before the session's RTH."""
    start = bars.index - pd.Timedelta(minutes=1)
    tod = start.hour * 60 + start.minute
    rth = (tod >= 570) & (tod < 960) & (bars.index <= bars["session_close"])
    r = bars[rth]
    g = r.groupby("session_date")
    d = pd.DataFrame({"high": g["high"].max(), "low": g["low"].min(), "close": g["close"].last()})
    is930 = tod[rth] == 570
    d["open930"] = r["open"][is930].groupby(r["session_date"][is930]).first()
    contract = bars.groupby("session_date")["contract"]
    if (contract.nunique() > 1).any():
        raise ValueError("a session mixes contracts; roll detection assumes one contract per session")
    d["contract"] = contract.first()
    d["prev_close"] = d["close"].shift(1)
    d["roll"] = d["contract"] != d["contract"].shift(1)
    d.loc[d.index[0], "roll"] = True
    tr = np.maximum(d["high"], d["prev_close"]) - np.minimum(d["low"], d["prev_close"])
    tr = tr.where(~d["roll"], d["high"] - d["low"])
    d["atr"] = tr.rolling(20).mean().shift(1)
    return d


def scan(bars: pd.DataFrame, d: pd.DataFrame, end_t: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Walk every session's bars in order. Returns (observations, gaps)."""
    h1, m1 = (int(v) for v in end_t.split(":"))
    end_min = h1 * 60 + m1
    start = bars.index - pd.Timedelta(minutes=1)
    tod = np.asarray(start.hour * 60 + start.minute)
    hi, lo = bars["high"].to_numpy(), bars["low"].to_numpy()
    sess = bars["session_date"].to_numpy()
    bounds = np.flatnonzero(np.r_[True, sess[1:] != sess[:-1], True])
    gaps: list[dict] = []
    open_ids: list[int] = []
    obs: list[dict] = []
    n930 = 0

    def first_hit(gid: int, a: int, b: int) -> int:
        """Index (relative to a) of the first bar in [a, b) that trades gap gid's level, or -1."""
        gp = gaps[gid]
        hit = lo[a:b] <= gp["level"] if gp["side"] == "up" else hi[a:b] >= gp["level"]
        return int(np.argmax(hit)) if hit.any() else -1

    for a, b in zip(bounds[:-1], bounds[1:]):
        sd = sess[a]
        row = d.loc[sd] if sd in d.index else None
        if row is not None and row["roll"]:
            for gid in open_ids:
                gaps[gid]["fate"] = "censored (roll)"
            open_ids = []
        t = tod[a:b]
        i930 = np.flatnonzero(t == 570)
        has930 = row is not None and len(i930) and not np.isnan(row["open930"])
        cut = a + int(i930[0]) if has930 else b
        still = []
        for gid in open_ids:
            k = first_hit(gid, a, cut)
            if k >= 0:
                gaps[gid]["fate"] = "filled overnight"
                gaps[gid]["filled_date"] = sd
            else:
                still.append(gid)
        open_ids = still
        if not has930:
            for gid in open_ids:
                k = first_hit(gid, cut, b)
                if k >= 0:
                    gaps[gid]["fate"] = "filled later in session"
                    gaps[gid]["filled_date"] = sd
            open_ids = [g for g in open_ids if "filled_date" not in gaps[g]]
            continue
        n930 += 1
        o = float(row["open930"])
        if not row["roll"] and not np.isnan(row["prev_close"]) and o != row["prev_close"]:
            gaps.append({"id": len(gaps), "date": sd, "side": "up" if o > row["prev_close"] else "down",
                         "level": float(row["prev_close"]), "open930": o, "size": abs(o - row["prev_close"]),
                         "atr": float(row["atr"]), "born": n930, "fate": "open at data end"})
            open_ids.append(len(gaps) - 1)
        win_end = cut + int(np.searchsorted(t[cut - a:], end_min))
        still = []
        for gid in open_ids:
            gp = gaps[gid]
            k = first_hit(gid, cut, b)
            filled = 0 <= k < win_end - cut
            obs.append({
                "date": sd, "year": pd.Timestamp(sd).year, "gap_id": gid, "gap_date": gp["date"],
                "age": n930 - gp["born"], "side": gp["side"], "gap_pts": gp["size"],
                "dist_pts": abs(o - gp["level"]), "atr": float(row["atr"]), "atr_pct": float(row["atr"]) / o,
                "filled": filled, "fill_minute": k + 1 if filled else np.nan,
            })
            if k >= 0:
                gp["fate"] = f"filled by {end_t}" if filled else "filled later in session"
                gp["filled_date"] = sd
                gp["fill_age"] = n930 - gp["born"]
            else:
                still.append(gid)
        open_ids = still
    o = pd.DataFrame(obs)
    o["gap_atr"] = o["gap_pts"] / o["atr"]
    o["dist_atr"] = o["dist_pts"] / o["atr"]
    return o, pd.DataFrame(gaps)


def rates(df: pd.DataFrame, by: str, order: list | None = None) -> pd.DataFrame:
    rows = {}
    keys = order if order is not None else sorted(df[by].dropna().unique())
    for key in keys:
        s = df[df[by] == key]
        n, k = len(s), int(s["filled"].sum())
        lo, hi = wilson(k, n)
        rows[key] = {"n": n, "filled": k, "rate": k / n if n else np.nan, "ci_low": lo, "ci_high": hi,
                     "med_minute": float(s.loc[s["filled"], "fill_minute"].median()) if k else np.nan,
                     "med_dist": float(s["dist_pts"].median()) if n else np.nan}
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis(by)


def fmt(t: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=t.index.astype(str).rename(t.index.name))
    f["gaps"] = t["n"].map("{:,}".format)
    f["filled"] = t["filled"].map("{:,}".format)
    f["fill rate"] = t["rate"].map("{:.1%}".format)
    f["95% CI"] = [f"{a:.1%} … {b:.1%}" for a, b in zip(t["ci_low"], t["ci_high"])]
    f["median fill minute"] = t["med_minute"].map("{:.0f}".format)
    f["median distance (pts)"] = t["med_dist"].map("{:.1f}".format)
    return f.replace("nan", "–").replace("nan%", "–").replace("nan% … nan%", "–")


def pivot(df: pd.DataFrame, rows: str, cols: str, row_order: list, col_order: list) -> pd.DataFrame:
    p = pd.DataFrame(index=pd.Index([str(r) for r in row_order], name=rows))
    for c in col_order:
        cells = []
        for r in row_order:
            s = df[(df[rows] == r) & (df[cols] == c)]
            cells.append(f"{s['filled'].mean():.0%} ({len(s)})" if len(s) else "–")
        p[str(c)] = cells
    return p


def logit(df: pd.DataFrame, X: pd.DataFrame, cluster: bool) -> pd.DataFrame:
    X = sm.add_constant(X.astype(float), has_constant="add")
    y = df["filled"].astype(float)
    kw = {"cov_type": "cluster", "cov_kwds": {"groups": pd.factorize(df["date"])[0]}} if cluster else {}
    m = sm.Logit(y, X).fit(disp=0, maxiter=200, **kw)
    ci = m.conf_int()
    t = pd.DataFrame({"coef": m.params, "odds ratio": np.exp(m.params), "OR 95% CI low": np.exp(ci[0]),
                      "OR 95% CI high": np.exp(ci[1]), "p": m.pvalues})
    t.attrs["n"], t.attrs["pseudo_r2"] = int(m.nobs), float(m.prsquared)
    return t.rename_axis("term")


def fmt_logit(t: pd.DataFrame) -> pd.DataFrame:
    f = pd.DataFrame(index=t.index)
    f["coef"] = t["coef"].map("{:+.3f}".format)
    f["odds ratio"] = t["odds ratio"].map("{:.2f}".format)
    f["OR 95% CI"] = [f"{a:.2f} … {b:.2f}" for a, b in zip(t["OR 95% CI low"], t["OR 95% CI high"])]
    f["p"] = t["p"].map(lambda p: f"{p:.2g}" if p >= 1e-4 else "<0.0001")
    return f


def study(symbol: str, end_t: str, out: Path) -> dict:
    bars = load_bars(symbol, "1m", "eth")
    d = daily(bars)
    o, gaps = scan(bars, d, end_t)
    o = o[o["atr"].notna()].copy()
    o["pts_bucket"] = pd.cut(o["dist_pts"], PTS_BINS, labels=bin_labels(PTS_BINS, "{:g}"), right=False)
    o["atr_bucket"] = pd.cut(o["dist_atr"], ATR_BINS, labels=bin_labels(ATR_BINS, "{:g}"), right=False)
    o["age_bucket"] = pd.cut(o["age"], AGE_BINS, labels=AGE_LABELS)
    days0 = o[o["age"] == 0]
    q = days0["atr_pct"].quantile([1 / 3, 2 / 3]).to_numpy()
    o["atr_third"] = pd.cut(o["atr_pct"], [-np.inf, *q, np.inf], labels=THIRDS)
    qr = days0["atr"].quantile([1 / 3, 2 / 3]).to_numpy()
    o["atr_raw_third"] = pd.cut(o["atr"], [-np.inf, *qr, np.inf], labels=THIRDS)
    new = o[o["age"] == 0]
    old = o[o["age"] > 0]
    pts_l, atr_l = bin_labels(PTS_BINS, "{:g}"), bin_labels(ATR_BINS, "{:g}")

    t_pts, t_atr = rates(new, "pts_bucket", pts_l), rates(new, "atr_bucket", atr_l)
    t_age = rates(o, "age_bucket", AGE_LABELS)
    t_third, t_raw = rates(new, "atr_third", THIRDS), rates(new, "atr_raw_third", THIRDS)
    t_side, t_year = rates(new, "side", ["up", "down"]), rates(new, "year")
    t_old_atr = rates(old, "atr_bucket", atr_l)

    m_new = logit(new, pd.DataFrame({"log(gap ÷ ATR)": np.log(new["gap_atr"]),
                                     "ATR third: mid": new["atr_third"] == "mid", "ATR third: high": new["atr_third"] == "high",
                                     "up gap": new["side"] == "up"}), cluster=False)
    m_pts = logit(new, pd.DataFrame({"log(gap pts)": np.log(new["gap_pts"]), "log(ATR pts)": np.log(new["atr"]),
                                     "up gap": new["side"] == "up"}), cluster=False)
    Xall = pd.DataFrame({"log(distance ÷ ATR)": np.log(o["dist_atr"])})
    for lab in AGE_LABELS[1:]:
        Xall[f"age {lab}"] = o["age_bucket"] == lab
    Xall["ATR third: mid"], Xall["ATR third: high"] = o["atr_third"] == "mid", o["atr_third"] == "high"
    Xall["up gap"] = o["side"] == "up"
    m_all = logit(o, Xall, cluster=True)

    fates = gaps["fate"].value_counts().rename_axis("fate").to_frame("gaps")
    fates["share"] = (fates["gaps"] / len(gaps)).map("{:.1%}".format)
    fa = gaps["fill_age"].dropna() if "fill_age" in gaps else pd.Series(dtype=float)

    span = f"{pd.Timestamp(o['date'].min()):%Y-%m-%d} → {pd.Timestamp(o['date'].max()):%Y-%m-%d}"
    charts = [
        {"id": "size", "title": "Today's gap: fill rate by size ÷ ATR(20), per ATR third", "yformat": ".0%",
         "caption": f"Age-0 gaps; fill = a bar from 9:30 to {end_t} trades the prior RTH close.",
         "x": atr_l, "series": [{"name": f"ATR {th}", "y": [None if np.isnan(v) else round(v, 4) for v in
                                 rates(new[new["atr_third"] == th], "atr_bucket", atr_l)["rate"]]} for th in THIRDS]
         + [{"name": "all", "y": [None if np.isnan(v) else round(v, 4) for v in t_atr["rate"]]}]},
        {"id": "pts", "title": "Today's gap: fill rate by size in points", "yformat": ".0%", "caption": "",
         "x": pts_l, "series": [{"name": "fill rate", "y": [None if np.isnan(v) else round(v, 4) for v in t_pts["rate"]]}]},
        {"id": "age", "title": "Fill rate by days since the gap opened", "yformat": ".0%",
         "caption": "Every gap still open at 9:30; older gaps are usually far from price (median distance in the table).",
         "x": AGE_LABELS, "series": [{"name": "fill rate", "y": [None if np.isnan(v) else round(v, 4) for v in t_age["rate"]]}]},
    ]

    def logit_caption(t: pd.DataFrame, what: str) -> str:
        return f"{what} n = {t.attrs['n']:,}, McFadden pseudo-R² = {t.attrs['pseudo_r2']:.3f}. Odds ratio > 1 = fills more often."

    tables = [
        {"title": "Today's gap by size (points)", "caption": "Age-0 gaps.", "frame": fmt(t_pts)},
        {"title": "Today's gap by size ÷ ATR(20)", "caption": "", "frame": fmt(t_atr)},
        {"title": "Today's gap by ATR(20) third (ATR ÷ price)",
         "caption": f"Thirds of ATR(20) as a share of the 9:30 price over all days: cuts {q[0]:.2%} / {q[1]:.2%}.",
         "frame": fmt(t_third)},
        {"title": "Today's gap: size ÷ ATR × ATR third", "caption": "Cell = fill rate (gaps). Does volatility matter at the same relative size?",
         "frame": pivot(new, "atr_bucket", "atr_third", atr_l, THIRDS)},
        {"title": "Today's gap: size (points) × ATR third", "caption": "Does volatility matter at the same point size?",
         "frame": pivot(new, "pts_bucket", "atr_third", pts_l, THIRDS)},
        {"title": "Today's gap by raw ATR(20) third (points)",
         "caption": f"Cuts {qr[0]:.0f} / {qr[1]:.0f} pts; mostly sorts by year as NQ's price rose.", "frame": fmt(t_raw)},
        {"title": "All open gaps by days since they opened", "caption": "Distance = |9:30 open − gap level| that morning.",
         "frame": fmt(t_age)},
        {"title": "Older gaps (age ≥ 1) by distance ÷ ATR(20)", "caption": "", "frame": fmt(t_old_atr)},
        {"title": "All open gaps: distance ÷ ATR × days since opened", "caption": "Cell = fill rate (gaps).",
         "frame": pivot(o, "atr_bucket", "age_bucket", atr_l, AGE_LABELS)},
        {"title": "Today's gap: up vs down", "caption": "", "frame": fmt(t_side)},
        {"title": "Today's gap by year", "caption": "", "frame": fmt(t_year)},
        {"title": "Logit: today's gap — relative size, ATR third, side", "caption": logit_caption(m_new, "Age-0 gaps,"),
         "frame": fmt_logit(m_new)},
        {"title": "Logit: today's gap — points and ATR level separately",
         "caption": logit_caption(m_pts, "Age-0 gaps,") + " log(ATR) answers: at the same point size, does higher ATR change the fill odds?",
         "frame": fmt_logit(m_pts)},
        {"title": "Logit: all open gaps — distance, age, ATR third, side",
         "caption": logit_caption(m_all, "All observations,") + " Errors clustered by date. Age terms are vs age 0 at the same distance.",
         "frame": fmt_logit(m_all)},
        {"title": "What became of every gap", "caption": f"Over its whole life. Gaps that filled: median age at fill "
         f"{fa.median():.0f} days, 90th pct {fa.quantile(0.9):.0f}.", "frame": fates},
    ]
    notes = [
        f"Data: {symbol} 1-minute ETH bars, unadjusted continuous contract, ET; {len(new):,} sessions with a gap and an ATR, {span}.",
        f"Gap = 9:30 bar open vs the previous session's last RTH bar close; fill window 9:30 → {end_t} ET (the 9:30 bar counts).",
        "A gap stays open until any later bar trades its level, overnight included; an overnight fill retires it before the next morning.",
        "Roll sessions make no gap and censor every open gap (levels are in the old contract's prices), so gap ages are capped by the quarterly roll.",
        "ATR(20) = simple mean of the previous 20 RTH true ranges (high/low vs prior RTH close), known before 9:30.",
        "Several gaps open on one morning share its price path: the all-gaps rows are not independent (the regression clusters by date).",
    ]
    write_study_report(
        out / "report.html",
        {"title": f"{symbol} RTH gap fill, 9:30–{end_t} ET",
         "subtitle": "How often does an open gap to the prior RTH close fill in the morning — by size, age and ATR(20)?",
         "details": {"symbol": symbol, "span": span, "window": f"09:30 → {end_t} ET", "gaps": f"{len(gaps):,}",
                     "observations": f"{len(o):,}"}},
        charts=charts, tables=tables, notes=notes,
    )
    o.to_csv(out / "events.csv", index=False)
    gaps.to_csv(out / "gaps.csv", index=False)
    res = {"symbol": symbol, "end": end_t, "span": span, "atr_pct_cuts": q.tolist(), "atr_raw_cuts": qr.tolist(),
           "tables": {k: v.set_axis(v.index.astype(str)).reset_index().to_dict("records") for k, v in
                      {"size_pts": t_pts, "size_atr": t_atr, "age": t_age, "atr_third": t_third, "atr_raw_third": t_raw,
                       "side": t_side, "year": t_year, "old_by_atr": t_old_atr}.items()},
           "logit": {k: v.reset_index().to_dict("records") for k, v in {"new": m_new, "points_atr": m_pts, "all": m_all}.items()},
           "fates": gaps["fate"].value_counts().to_dict()}
    (out / "study.json").write_text(json.dumps(res, indent=2, default=float), encoding="utf-8")
    res["frames"] = {"pts": t_pts, "atr": t_atr, "age": t_age, "third": t_third, "side": t_side, "year": t_year,
                     "m_new": m_new, "m_pts": m_pts, "m_all": m_all, "fates": fates}
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--end", dest="end_t", default="11:30", help="fill window end, HH:MM ET")
    ap.add_argument("--out", type=Path, default=Path("reports/gap_fill"))
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    res = study(a.symbol, a.end_t, a.out)
    f = res["frames"]
    pd.set_option("display.width", 200)
    print(f"{res['symbol']} RTH gap fill 9:30->{a.end_t} ET, {res['span']}")
    for k in ("pts", "atr", "third", "age", "side"):
        print(f[k][["n", "filled", "rate", "ci_low", "ci_high", "med_minute", "med_dist"]].round(3).to_string(), "\n")
    for k in ("m_new", "m_pts", "m_all"):
        print(k, f"n={f[k].attrs['n']}\n", f[k].round(4).to_string(), "\n")
    print(f["fates"].to_string())
    print(f"wrote {a.out / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
