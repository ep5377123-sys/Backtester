"""NQ RTH range-band study (PLAN.md Phase 21).

Question: how well can NQ's RTH range, and the distance its high and low reach from the open, be forecast before the
open? And does price reverse at the forecast band edges more often than at any other level?

Forecasts (target: ln of the RTH log range ln(H/L); everything in log terms so 2019 and 2026 price levels compare):
  ATR14       mean of the last 14 RTH ranges, scaled by its training bias
  HAR         1-day, 5-day and 22-day average ranges (heterogeneous autoregression)
  VX          30-day constant-maturity VX futures close from the bar before the open (guban.research.vx)
  HAR+VX+ON   HAR + VX + the pre-RTH overnight range (18:00 -> 09:30 ET), all known at 09:30
Refit on the first session of every month on all earlier sessions (expanding, >= 250), so every forecast is out of
sample. Bands = forecast x the training window's residual quantiles; excursion levels q50 / q80 / q95 = training
quantiles of (high - open) / forecast and (open - low) / forecast, pooled.

Checks (descriptive): OOS R² and band coverage per model; textbook touch rates 2(1 - Phi(k)) at k sigma (sigma = mean
range / 1.596) vs observed; the reflection-principle extension formula P(new high >= H_t + d) = 2(1 - Phi((ln(H_t/P_t)
+ d) / (sigma sqrt(s(t))))) from 10:00 to 15:00 ET, s(t) = the training days' average share of RTH variance still to
come, as a reliability table.

Primary test (pre-registered, 2026-10-28), on the model with the lowest OOS MSE: from the first touch of a level
open +/- q x forecast, does price come back through it by 0.25 x forecast (fade) before going on by as much (extend)?
Observations: levels q = 0.1 ... 2.0 plus the band edges q50 and q80, one side at a time; resolved races only. Per side
and band edge, the edge's fade rate vs every race on that side is REAL only if
  1. the gap is >= 3 percentage points,
  2. a one-sided exact binomial test against the base rate gives p < 0.05, and
  3. the gap is positive in every calendar year.

    uv run python research/range_bands.py [--vx data/VIX/.../*.ohlcv-1d.csv] [--out reports/range_bands]
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from guban.data.resample import load_bars
from guban.research.ranges import (
    EXTEND, FADE, OUTCOME_NAMES, RANGE_PER_SIGMA, monthly_refits, norm_sf, ols, race, remaining_share, touch_prob,
    variance_shares,
)  # fmt: skip
from guban.research.report import write_study_report
from guban.research.stats import by_period, rate_test, verdict
from guban.research.vx import load_vx, vx_before

SYMBOL, TZ = "NQ", "America/New_York"
VX_CSV = Path("data/VIX/XCBF-20261026-KCNSUYPJQB/xcbf-pitch-20181104-20261024.ohlcv-1d.csv")
OPEN_MIN, SLOTS, MIN_BARS = 9 * 60 + 30, 390, 385  # RTH 09:30-16:00 ET, 1-minute slots
MIN_TRAIN = 250
MODELS = {  # name: (offset column or None, regressors); a constant is always fitted
    "ATR14": ("latr14", []),
    "HAR": (None, ["lr1", "lr5", "lr22"]),
    "VX": (None, ["lvx"]),
    "HAR+VX+ON": (None, ["lr1", "lr5", "lr22", "lvx", "lon"]),
}
BANDS = {0.50: (0.25, 0.75), 0.80: (0.10, 0.90), 0.95: (0.025, 0.975)}  # central range bands
EXCURSION_Q = (0.50, 0.80, 0.95)
TEXTBOOK_K = (0.5, 1.0, 1.5, 2.0)
CHECKPOINTS = ("10:00", "10:30", "11:00", "12:00", "13:00", "14:00", "15:00")
EXT_D = (0.0, 0.1, 0.25, 0.5)  # extension beyond the current high/low, x forecast
RELIABILITY_EDGES = [0, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 1.0000001]
GRID = tuple(round(q, 1) for q in np.arange(0.1, 2.01, 0.1))
EDGES = ("q50", "q80")
RACE_Y = 0.25
MIN_GAP, ALPHA = 0.03, 0.05
MIN_PERIOD_OBS = 60


# --- days -------------------------------------------------------------------------------------------------------


def build_days(vx_csv: Path) -> tuple[pd.DataFrame, dict, dict]:
    """Day table (one row per full RTH session), per-slot arrays (log high / low / close, (days, 390)) and drop counts."""
    rth = load_bars(SYMBOL, "1m", "rth", tz=TZ)
    t = rth.index
    slot = (t.hour * 60 + t.minute - OPEN_MIN - 1).to_numpy()
    sd = rth["session_date"].to_numpy()
    n = rth.groupby("session_date").size()
    close_hm = rth.groupby("session_date")["session_close"].first().map(lambda x: x.hour * 60 + x.minute)
    early, short = close_hm < 16 * 60, (n < MIN_BARS) & (close_hm >= 16 * 60)
    keep = n.index[~early & ~short]
    drops = {"early close": int(early.sum()), f"fewer than {MIN_BARS} bars": int(short.sum())}

    m = np.isin(sd, keep.to_numpy()) & (slot >= 0) & (slot < SLOTS)
    day_i = pd.Index(keep).get_indexer(sd[m])
    arr = {}
    for k in ("open", "high", "low", "close"):
        a = np.full((len(keep), SLOTS), np.nan)
        a[day_i, slot[m]] = np.log(rth[k].to_numpy(float)[m])
        arr[k] = a
    arr["close_ff"] = pd.DataFrame(arr["close"]).ffill(axis=1).to_numpy()
    first = np.argmax(~np.isnan(arr["open"]), axis=1)
    lo = arr["open"][np.arange(len(keep)), first]
    ret = np.diff(np.column_stack([lo, arr["close_ff"]]), axis=1)  # slot 0: close - open of the first bar
    ret[np.isnan(arr["close"])] = np.nan
    arr["ret"] = ret

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        lh, ll = np.nanmax(arr["high"], axis=1), np.nanmin(arr["low"], axis=1)
    last = SLOTS - 1 - np.argmax(~np.isnan(arr["close"][:, ::-1]), axis=1)
    days = pd.DataFrame({
        "open": np.exp(lo), "high": np.exp(lh), "low": np.exp(ll), "close": np.exp(arr["close"][np.arange(len(keep)), last]),
        "bars": n.loc[keep].to_numpy(), "contract": rth.groupby("session_date")["contract"].first().loc[keep].to_numpy(),
        "rng": lh - ll, "up": lh - lo, "dn": lo - ll,
    }, index=pd.DatetimeIndex(keep, name="date"))  # fmt: skip

    # overnight: the same session's ETH bars up to 09:30
    eth = load_bars(SYMBOL, "1m", "eth", tz=TZ)
    naive = eth.index.tz_localize(None)
    pre = eth[naive <= eth["session_date"] + pd.Timedelta(minutes=OPEN_MIN)]
    on = pre.groupby("session_date").agg(on_high=("high", "max"), on_low=("low", "min"))
    days = days.join(on)
    days["on_rng"] = np.log(days["on_high"] / days["on_low"])

    vx = load_vx(vx_csv)
    days["vx"] = vx_before(vx["vx"], days.index).to_numpy()

    r = days["rng"]
    days["r1"], days["r5"], days["r22"], days["atr14"] = (r.rolling(w).mean().shift(1) for w in (1, 5, 22, 14))
    for k in ("r1", "r5", "r22", "atr14"):
        days["l" + k] = np.log(days[k])
    days["lvx"], days["lon"], days["y"] = np.log(days["vx"]), np.log(days["on_rng"].where(days["on_rng"] > 0)), np.log(r)
    return days, arr, drops


def feature_matrix(days: pd.DataFrame, model: str) -> tuple[np.ndarray, np.ndarray]:
    off, regs = MODELS[model]
    x = np.column_stack([np.ones(len(days)), *(days[c].to_numpy(float) for c in regs)])
    o = days[off].to_numpy(float) if off else np.zeros(len(days))
    return x, o


# --- walk-forward forecasts -------------------------------------------------------------------------------------


def walk_forward(days: pd.DataFrame, arr: dict) -> tuple[dict[str, pd.DataFrame], np.ndarray, np.ndarray]:
    """Per model, OOS forecast rows (pred = ln forecast, smear, band residual quantiles, excursion quantiles, refit);
    plus the OOS row positions and their remaining-variance curves (len(oos), 390)."""
    cols = ["lr1", "lr5", "lr22", "latr14", "lvx", "lon", "y"]
    usable = np.isfinite(days[cols].to_numpy(float)).all(axis=1)
    y = days["y"].to_numpy(float)
    up, dn = days["up"].to_numpy(float), days["dn"].to_numpy(float)
    mats = {m: feature_matrix(days, m) for m in MODELS}
    out = {m: [] for m in MODELS}
    pos, rem = [], []
    for start, train, test in monthly_refits(days.index, usable, MIN_TRAIN):
        ti = np.flatnonzero(test)
        rem_m = remaining_share(variance_shares(arr["ret"][train]))
        pos.extend(ti)
        rem.extend([rem_m] * len(ti))
        for m, (x, o) in mats.items():
            beta = ols(x[train], y[train] - o[train])
            fit = x[train] @ beta + o[train]
            res = y[train] - fit
            f_tr = np.exp(fit)
            exc = np.concatenate([up[train] / f_tr, dn[train] / f_tr])
            row = {"refit": start, "smear": float(np.mean(np.exp(res)))}
            row |= {f"res_{p:g}": float(np.quantile(res, p)) for lo_hi in BANDS.values() for p in lo_hi}
            row |= {f"q{int(q * 100)}": float(np.quantile(exc, q)) for q in EXCURSION_Q}
            pred = x[test] @ beta + o[test]
            out[m].extend({"pos": int(i), "date": days.index[i], "pred": float(p)} | row for i, p in zip(ti, pred))
    frames = {m: pd.DataFrame(v).set_index("date") for m, v in out.items()}
    return frames, np.array(pos), np.array(rem)


def final_fit(days: pd.DataFrame) -> dict:
    """Coefficients on every usable session, and the forecast for the session after the last one where the inputs exist
    (the overnight range of that session is not known yet)."""
    cols = ["lr1", "lr5", "lr22", "latr14", "lvx", "lon", "y"]
    usable = np.isfinite(days[cols].to_numpy(float)).all(axis=1)
    y = days["y"].to_numpy(float)
    r = days["rng"]
    nxt = {"lr1": math.log(r.iloc[-1]), "lr5": math.log(r.iloc[-5:].mean()), "lr22": math.log(r.iloc[-22:].mean()),
           "latr14": math.log(r.iloc[-14:].mean()), "lvx": math.log(days["vx_last"].iloc[-1]), "lon": np.nan}  # fmt: skip
    res = {}
    for m, (off, regs) in MODELS.items():
        x, o = feature_matrix(days, m)
        beta = ols(x[usable], y[usable] - o[usable])
        resid = y[usable] - (x[usable] @ beta + o[usable])
        f = np.exp(x[usable] @ beta + o[usable])
        exc = np.concatenate([days["up"].to_numpy()[usable] / f, days["dn"].to_numpy()[usable] / f])
        coef = dict(zip(["const", *regs], map(float, beta)))
        if off:
            coef[off] = 1.0
        p = beta[0] + sum(b * nxt[c] for b, c in zip(beta[1:], regs)) + (nxt[off] if off else 0.0)
        res[m] = {"coef": coef, "next_pred": float(p) if np.isfinite(p) else None,
                  "res_q": {f"{q:g}": float(np.quantile(resid, q)) for lo_hi in BANDS.values() for q in lo_hi},
                  "exc_q": {f"q{int(q * 100)}": float(np.quantile(exc, q)) for q in EXCURSION_Q},
                  "smear": float(np.mean(np.exp(resid)))}  # fmt: skip
    return res


# --- checks -----------------------------------------------------------------------------------------------------


def model_metrics(days: pd.DataFrame, fc: pd.DataFrame) -> dict:
    d = days.loc[fc.index]
    y, p = d["y"].to_numpy(), fc["pred"].to_numpy()
    e = y - p
    f = np.exp(p)
    out = {"n": len(fc), "mse": float(np.mean(e**2)), "r2": float(1 - np.sum(e**2) / np.sum((y - y.mean()) ** 2)),
           "corr2": float(np.corrcoef(y, p)[0, 1] ** 2), "mae_pct": float(np.mean(np.abs(np.exp(e) - 1)))}  # fmt: skip
    for nom, (lo, hi) in BANDS.items():
        out[f"cover_{nom:g}"] = float(np.mean((e >= fc[f"res_{lo:g}"]) & (e <= fc[f"res_{hi:g}"])))
    for q in EXCURSION_Q:
        k = f"q{int(q * 100)}"
        out[f"touch_up_{k}"] = float(np.mean(d["up"].to_numpy() >= fc[k].to_numpy() * f))
        out[f"touch_dn_{k}"] = float(np.mean(d["dn"].to_numpy() >= fc[k].to_numpy() * f))
    return out


def coverage_by_year(days: pd.DataFrame, fc: pd.DataFrame) -> pd.DataFrame:
    e = days.loc[fc.index, "y"] - fc["pred"]
    rows = {}
    for yr, idx in fc.groupby(fc.index.year).groups.items():
        r = {"n": len(idx)}
        for nom, (lo, hi) in BANDS.items():
            r[f"{nom:g}"] = float(np.mean((e[idx] >= fc.loc[idx, f"res_{lo:g}"]) & (e[idx] <= fc.loc[idx, f"res_{hi:g}"])))
        rows[yr] = r
    return pd.DataFrame.from_dict(rows, orient="index")


def textbook(days: pd.DataFrame, fc: pd.DataFrame) -> list[dict]:
    d = days.loc[fc.index]
    sigma = np.exp(fc["pred"]) * fc["smear"] / RANGE_PER_SIGMA
    return [{"k": k, "theory": float(2 * norm_sf(k)), "up": float(np.mean(d["up"] >= k * sigma)), "dn": float(np.mean(d["dn"] >= k * sigma))}
            for k in TEXTBOOK_K]  # fmt: skip


def extension(arr: dict, fc: pd.DataFrame, rem: np.ndarray) -> pd.DataFrame:
    """One row per OOS day x checkpoint x side x d: the reflection formula's probability and what happened."""
    pos = fc["pos"].to_numpy()
    f = np.exp(fc["pred"].to_numpy())
    sigma = f * fc["smear"].to_numpy() / RANGE_PER_SIGMA
    hi, lo, cl = arr["high"][pos], arr["low"][pos], arr["close_ff"][pos]
    rows = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for cp in CHECKPOINTS:
            h, mi = map(int, cp.split(":"))
            s = h * 60 + mi - OPEN_MIN - 1  # the bar closing at the checkpoint
            hs, ls, pt = np.nanmax(hi[:, : s + 1], axis=1), np.nanmin(lo[:, : s + 1], axis=1), cl[:, s]
            hf, lf = np.nanmax(hi[:, s + 1 :], axis=1), np.nanmin(lo[:, s + 1 :], axis=1)
            sig = sigma * np.sqrt(rem[:, s])
            for dm in EXT_D:
                d = dm * f
                for side, dist, obs in (("up", hs - pt + d, hf > hs + d), ("down", pt - ls + d, lf < ls - d)):
                    ok = np.isfinite(dist) & np.isfinite(hf if side == "up" else lf)
                    rows.append(pd.DataFrame({"date": fc.index[ok], "checkpoint": cp, "side": side, "d": dm,
                                              "pred": touch_prob(dist[ok], sig[ok]), "obs": obs[ok]}))  # fmt: skip
    return pd.concat(rows, ignore_index=True)


def reliability(ext: pd.DataFrame) -> pd.DataFrame:
    b = pd.cut(ext["pred"], RELIABILITY_EDGES, right=False)
    g = ext.groupby(b, observed=False)
    out = pd.DataFrame({"n": g.size(), "pred": g["pred"].mean(), "obs": g["obs"].mean()})
    out.index = [f"{iv.left:.0%}–{min(iv.right, 1):.0%}" for iv in out.index]
    return out


# --- races ------------------------------------------------------------------------------------------------------


def races(days: pd.DataFrame, arr: dict, fc: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for date, r in fc.iterrows():
        i = int(r["pos"])
        ok = ~np.isnan(arr["high"][i])
        slots = np.flatnonzero(ok)
        lh, ll = arr["high"][i, ok], arr["low"][i, ok]
        lo, f = math.log(days["open"].iat[i]), math.exp(r["pred"])
        levels = [("grid", q) for q in GRID] + [(e, float(r[e])) for e in EDGES]
        for kind, q in levels:
            for side in (1, -1):
                code, ti, ri = race(lh, ll, lo + side * q * f, RACE_Y * f, side)
                rows.append((date, "up" if side > 0 else "down", kind, q, code,
                             int(slots[ti]) if ti >= 0 else -1, int(slots[ri]) if ri >= 0 else -1))  # fmt: skip
    t = pd.DataFrame(rows, columns=["date", "side", "kind", "q", "code", "touch_slot", "resolve_slot"])
    t["outcome"] = t["code"].map(OUTCOME_NAMES)
    return t


def periods_for(dates: pd.Series) -> np.ndarray:
    years = pd.DatetimeIndex(dates).year.to_numpy()
    uniq, counts = np.unique(years, return_counts=True)
    labels = {y: str(y) for y in uniq}
    if len(uniq) > 1 and counts[0] < MIN_PERIOD_OBS:
        labels[uniq[0]] = labels[uniq[1]] = f"{uniq[0]}-{uniq[1]}"
    return np.array([labels[y] for y in years])


def race_tests(t: pd.DataFrame) -> dict:
    out = {}
    for side in ("up", "down"):
        for edge in EDGES:
            obs = t[(t["side"] == side) & t["code"].isin([FADE, EXTEND]) & t["kind"].isin(["grid", edge])]
            hit, sig = (obs["code"] == FADE).to_numpy(), (obs["kind"] == edge).to_numpy()
            label = f"P(fade | {'upper' if side == 'up' else 'lower'} {edge} edge)"
            test = rate_test(hit, sig, label)
            yearly = by_period(hit, sig, periods_for(obs["date"]))
            out[f"{side}_{edge}"] = {"side": side, "edge": edge, "test": test.to_dict(), "by_year": _records(yearly),
                                     "verdict": verdict(test, yearly, MIN_GAP, ALPHA).to_dict()}  # fmt: skip
    return out


def fade_by_q(t: pd.DataFrame) -> dict:
    g = t[(t["kind"] == "grid") & t["code"].isin([FADE, EXTEND])]
    return {side: {f"{q:.1f}": float((s["code"] == FADE).mean()) for q, s in g[g["side"] == side].groupby("q")} for side in ("up", "down")}


def race_counts(t: pd.DataFrame) -> dict:
    return {k: {o: int(v) for o, v in g["outcome"].value_counts().items()} for k, g in t.groupby("kind")}


def _records(df: pd.DataFrame) -> list[dict]:
    ints = {"n", "hits", "base_n"}
    return [{"period": str(k), **{c: (int(v) if c in ints else float(v)) for c, v in row.items()}} for k, row in df.to_dict("index").items()]


# --- output -----------------------------------------------------------------------------------------------------


def pct(x, nd=1) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x * 100:.{nd}f}%"


def pp(x) -> str:
    return f"{x * 100:+.1f} pp"


def pts(log_dist: float, price: float) -> str:
    return f"{price * (math.exp(log_dist) - 1):,.0f}"


def print_summary(res: dict) -> None:
    print(f"\nNQ RTH range bands - OOS {res['oos']['first']} -> {res['oos']['last']} ({res['oos']['days']} sessions)")
    for m, s in res["models"].items():
        print(f"  {m:10} R2 {s['r2']:.3f}  MAE {pct(s['mae_pct'])}  cover 50/80/95: {pct(s['cover_0.5'])} / {pct(s['cover_0.8'])} / {pct(s['cover_0.95'])}"
              f"  high>=q80 {pct(s['touch_up_q80'])}, low<=q80 {pct(s['touch_dn_q80'])}")  # fmt: skip
    print(f"  best (lowest OOS MSE): {res['best']}")
    print("  textbook touch rates (k sigma, theory vs up / down): "
          + "; ".join(f"{t['k']:g}: {pct(t['theory'])} vs {pct(t['up'])} / {pct(t['dn'])}" for t in res["textbook"]))  # fmt: skip
    print(f"  reflection formula: mean |pred - obs| over buckets {pct(res['extension']['ece'])} ({res['extension']['n']:,} predictions)")
    for k, v in res["tests"].items():
        t = v["test"]
        print(f"  {t['label']}: {pct(t['rate'])} ({t['hits']}/{t['n']}) vs {pct(t['base_rate'])}  gap {pp(t['gap'])}  p = {t['p_value']:.4g}"
              f"  -> {'REAL' if v['verdict']['real'] else 'NOT REAL'}")  # fmt: skip


def write_report(res: dict, rel: pd.DataFrame, cov_year: pd.DataFrame, path: Path) -> None:
    best = res["best"]
    verdicts = []
    for k, v in res["tests"].items():
        t = v["test"]
        verdicts.append({"label": f"{'Upper' if v['side'] == 'up' else 'Lower'} {v['edge']} edge: fade",
                         "real": v["verdict"]["real"],
                         "headline": f"{pct(t['rate'])} fade at the edge vs {pct(t['base_rate'])} at every level ({pp(t['gap'])}, n = {t['n']})",
                         "checks": v["verdict"]["checks"]})  # fmt: skip
    models = list(res["models"])
    years = [str(y) for y in cov_year.index]
    fq = res["fade_by_q"]
    charts = [
        {"id": "r2", "title": "Forecast accuracy (out of sample)", "yformat": ".2f",
         "caption": "R² of ln(RTH range): the share of the day-to-day variation in range the forecast explains. Walk-forward, refit monthly.",
         "x": models, "series": [{"name": "OOS R²", "y": [round(res["models"][m]["r2"], 4) for m in models]}]},
        {"id": "cover", "title": f"Range-band coverage minus nominal, by year ({best})", "yformat": "+.1f", "ref": 0,
         "caption": "Share of sessions whose range fell inside the 50 / 80 / 95% band, minus 50 / 80 / 95, in percentage points. 0 = calibrated.",
         "x": years, "series": [{"name": f"{int(nom * 100)}% band", "y": [round((cov_year.loc[int(y), f"{nom:g}"] - nom) * 100, 2) for y in years]} for nom in BANDS]},
        {"id": "reliability", "title": "Reflection formula: predicted vs observed", "yformat": ".0f",
         "caption": "P(a new high / low at least d beyond the current one before the close), bucketed by the formula's prediction. "
                    "Bars side by side = calibrated. All checkpoints 10:00–15:00 ET, both sides, d = 0 / 0.1 / 0.25 / 0.5 × forecast.",
         "x": list(rel.index), "series": [{"name": "predicted %", "y": [None if pd.isna(v) else round(v * 100, 2) for v in rel["pred"]]},
                                          {"name": "observed %", "y": [None if pd.isna(v) else round(v * 100, 2) for v in rel["obs"]]}]},
        {"id": "fade_q", "title": "Fade rate by level", "yformat": ".0f", "ref": 50,
         "caption": f"From the first touch of open ± q × forecast: P(back {RACE_Y:g} × forecast before on {RACE_Y:g} × forecast). Resolved races. Dashed: 50%, a driftless random walk's rate.",
         "x": list(fq["up"]), "series": [{"name": "upper level", "y": [round(v * 100, 2) for v in fq["up"].values()]},
                                         {"name": "lower level", "y": [round(v * 100, 2) for v in fq["down"].values()]}]},
        {"id": "gap_years", "title": "Band-edge fade gap over the base rate, by year", "yformat": "+.1f", "ref": MIN_GAP * 100,
         "caption": "Edge fade rate minus that year's base (all levels, same side), in pp. Rule 3 needs every bar above 0; the dashed line is rule 1's 3 pp.",
         "x": [y["period"] for y in next(iter(res["tests"].values()))["by_year"]],
         "series": [{"name": f"{'upper' if v['side'] == 'up' else 'lower'} {v['edge']}", "y": [round(y["gap"] * 100, 2) for y in v["by_year"]]}
                    for v in res["tests"].values()]},
    ]  # fmt: skip

    mt = pd.DataFrame(
        [[m, f"{s['r2']:.3f}", f"{s['corr2']:.3f}", pct(s["mae_pct"]), pct(s["cover_0.5"]), pct(s["cover_0.8"]), pct(s["cover_0.95"]),
          " / ".join(pct(s[f"touch_up_q{q}"]) for q in (50, 80, 95)), " / ".join(pct(s[f"touch_dn_q{q}"]) for q in (50, 80, 95))]
         for m, s in res["models"].items()],
        columns=["model", "R²", "corr²", "MAE of range", "50% band", "80% band", "95% band", "high ≥ q50 / q80 / q95", "low ≤ q50 / q80 / q95"],
    ).set_index("model")  # fmt: skip
    tb = pd.DataFrame([[f"{t['k']:g} σ", pct(t["theory"]), pct(t["up"]), pct(t["dn"])] for t in res["textbook"]],
                      columns=["level from open", "random walk 2(1 − Φ(k))", "high reached", "low reached"]).set_index("level from open")  # fmt: skip
    cp = pd.DataFrame([[r["checkpoint"], r["d"], r["n"], pct(r["pred"]), pct(r["obs"]), pp(r["obs"] - r["pred"])] for r in res["extension"]["by_checkpoint"]],
                      columns=["checkpoint", "d × forecast", "n", "predicted", "observed", "observed − predicted"]).set_index("checkpoint")  # fmt: skip
    rl = pd.DataFrame([[i, int(r.n), pct(r.pred), pct(r.obs)] for i, r in rel.iterrows()], columns=["predicted bucket", "n", "mean predicted", "observed"]).set_index("predicted bucket")
    tests = pd.DataFrame([[v["test"]["label"], f"{v['test']['hits']}/{v['test']['n']}", pct(v["test"]["rate"]), pct(v["test"]["base_rate"]), pp(v["test"]["gap"]),
                           f"{v['test']['p_value']:.4g}"] for v in res["tests"].values()],
                         columns=["test", "fades / n", "rate", "base rate", "gap", "p (one-sided)"]).set_index("test")  # fmt: skip
    yearly = pd.DataFrame([[y["period"], f"{v['side']} {v['edge']}", f"{y['hits']}/{y['n']}", pct(y["rate"]), pct(y["base_rate"]), pp(y["gap"])]
                           for v in res["tests"].values() for y in v["by_year"]],
                          columns=["year", "edge", "fades / n", "rate", "base rate", "gap"]).set_index("year")  # fmt: skip
    nx = res["next_session"]
    nxt = pd.DataFrame([[m, *r] for m, r in nx["rows"].items()],
                       columns=["model", "range (pts)", "80% band (pts)", "high/low from open q50 (pts)", "q80 (pts)"]).set_index("model")  # fmt: skip
    coef = pd.DataFrame([[m, ", ".join(f"{k} {v:+.3f}" for k, v in s["coef"].items())] for m, s in res["final_fit"].items()],
                        columns=["model", "ln(range) = …"]).set_index("model")  # fmt: skip
    rc = res["race_counts"]
    data = pd.DataFrame.from_dict(
        {**{f"session dropped: {k}": v for k, v in res["data"]["dropped"].items()},
         "full RTH sessions": res["data"]["sessions"], "sessions with every input (after warm-up)": res["data"]["usable"],
         "OOS sessions (walk-forward)": res["oos"]["days"],
         **{f"races ({k}): {o}": n for k, c in rc.items() for o, n in c.items()}},
        orient="index", columns=["count"]).rename_axis("data")  # fmt: skip

    write_study_report(
        path,
        {"title": "NQ RTH range-band study",
         "subtitle": "How well can the RTH range and its extremes be forecast before the open, and does price reverse at the band edges?",
         "details": {"OOS": f"{res['oos']['first']} → {res['oos']['last']} ({res['oos']['days']:,} sessions)", "Best model": best,
                     "Session": "RTH 09:30–16:00 ET, full sessions", "Volatility input": "VX futures, 30-day constant maturity",
                     "Rule": f"gap ≥ {MIN_GAP * 100:g} pp, p < {ALPHA:g} (one-sided exact binomial), gap > 0 every year"}},
        verdicts=verdicts, charts=charts,
        tables=[
            {"title": "Models (out of sample)", "caption": "Bands from each training window's residual quantiles; q50 / q80 / q95 = training quantiles of the excursion from the open ÷ forecast, so a calibrated model reaches them on 50 / 20 / 5% of sessions per side.", "frame": mt},
            {"title": f"Textbook touch rates ({best})", "caption": "σ = the forecast's mean range ÷ 1.596 (a random walk's expected range is 1.596 σ). The random walk says a level k σ from the open is reached with probability 2(1 − Φ(k)).", "frame": tb},
            {"title": f"Reflection formula by checkpoint ({best})", "caption": "P(new high ≥ current high + d, or new low ≤ current low − d, before 16:00), both sides pooled. σ for the time left = σ × √(share of RTH variance still to come).", "frame": cp},
            {"title": "Reflection formula reliability", "caption": "Every checkpoint, side and d, bucketed by the predicted probability.", "frame": rl},
            {"title": "Band-edge fade tests (primary)", "caption": f"Observations: resolved races from levels q = 0.1 … 2.0 and the edge, on one side. Fade = back {RACE_Y:g} × forecast through the level before {RACE_Y:g} × forecast beyond it.", "frame": tests},
            {"title": "By year", "caption": "Base rate taken within each year.", "frame": yearly},
            {"title": f"Next session after {nx['after']}", "caption": f"Final fit on every session; points at the last close {nx['price']:,.2f}. HAR+VX+ON needs the overnight range, known at 09:30 — use its coefficients below.", "frame": nxt},
            {"title": "Coefficients (final fit, every session)", "caption": "ln of the RTH log range. lr1 / lr5 / lr22 = ln of the last 1 / 5 / 22-session average range, latr14 = ln 14-session average (ATR14's offset), lvx = ln VX30, lon = ln overnight range.", "frame": coef},
            {"title": "Data", "caption": "Sessions and races left out, and race outcomes.", "frame": data},
        ],
        notes=[
            "Every forecast is out of sample: refit on the first session of each month using only earlier sessions (expanding window, at least 250). The excursion quantiles use the training window's in-sample fits, a slight optimism.",
            "VX: Databento CFE VX futures daily bars (UTC days); the 30-day constant-maturity close of the last bar dated before the session (it closes 00:00 UTC, before 09:30 ET). VX futures price forward S&P 500 volatility, not spot VIX and not Nasdaq volatility; the log regressions absorb the level difference.",
            "Races: the touching bar counts only toward an extension (its other extreme may come first); a later bar reaching both thresholds is ambiguous; neither by the close is unresolved; both are dropped. Races on one day are correlated (one path, many levels), so p-values are optimistic.",
            "A band edge reached later in the day has less time left; time of day is not matched between the edge and the base races.",
        ],
    )  # fmt: skip


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vx", type=Path, default=VX_CSV, help="Databento CFE VX ohlcv-1d CSV")
    ap.add_argument("--out", type=Path, default=Path("reports/range_bands"))
    args = ap.parse_args()
    if not args.vx.exists():
        print(f"Error: VX file not found: {args.vx} (pass --vx)", file=sys.stderr)
        return 2

    days, arr, dropped = build_days(args.vx)
    fcs, _, rem = walk_forward(days, arr)
    metrics = {m: model_metrics(days, fc) for m, fc in fcs.items()}
    best = min(metrics, key=lambda m: metrics[m]["mse"])
    fc = fcs[best]

    ext = extension(arr, fc, rem)
    rel = reliability(ext)
    by_cp = [{"checkpoint": cp, "d": d, "n": len(g), "pred": float(g["pred"].mean()), "obs": float(g["obs"].mean())}
             for (cp, d), g in ext.groupby(["checkpoint", "d"], sort=True)]  # fmt: skip
    ece = float(np.nansum(rel["n"] * (rel["pred"] - rel["obs"]).abs()) / rel["n"].sum())

    t = races(days, arr, fc)
    tests = race_tests(t)

    vx = load_vx(args.vx)
    days["vx_last"] = vx_before(vx["vx"], days.index + pd.Timedelta(days=1)).to_numpy()  # last VX bar dated <= the session
    ff = final_fit(days)
    price = float(days["close"].iloc[-1])
    rows = {}
    for m, s in ff.items():
        p = s["next_pred"]
        if p is None:
            rows[m] = ["needs the overnight range", "", "", ""]
            continue
        lo, hi = p + s["res_q"]["0.1"], p + s["res_q"]["0.9"]
        f = math.exp(p)
        rows[m] = [pts(f, price), f"{pts(math.exp(lo), price)} – {pts(math.exp(hi), price)}", pts(s["exc_q"]["q50"] * f, price), pts(s["exc_q"]["q80"] * f, price)]

    usable = int(np.isfinite(days[["lr1", "lr5", "lr22", "latr14", "lvx", "lon", "y"]].to_numpy(float)).all(axis=1).sum())
    res = {
        "question": "Forecast NQ's RTH range and high/low excursions from the open; does price fade at the forecast band edges?",
        "rule": {"min_gap": MIN_GAP, "alpha": ALPHA, "test": "one-sided exact binomial vs base rate", "every_year_positive": True, "race_y": RACE_Y},
        "data": {"sessions": len(days), "usable": usable, "dropped": dropped, "vx_file": str(args.vx)},
        "oos": {"first": str(fc.index.min().date()), "last": str(fc.index.max().date()), "days": len(fc)},
        "models": metrics, "best": best, "coverage_by_year": cov_year_dict(coverage_by_year(days, fc)),
        "textbook": textbook(days, fc),
        "extension": {"n": len(ext), "ece": ece, "by_checkpoint": by_cp,
                      "reliability": [{"bucket": i, "n": int(r.n), "pred": None if pd.isna(r.pred) else float(r.pred), "obs": None if pd.isna(r.obs) else float(r.obs)}
                                      for i, r in rel.iterrows()]},  # fmt: skip
        "tests": tests, "fade_by_q": fade_by_q(t), "race_counts": race_counts(t),
        "final_fit": ff, "next_session": {"after": str(days.index[-1].date()), "price": price, "rows": rows},
    }
    res["verdict"] = {k: v["verdict"]["real"] for k, v in tests.items()}

    args.out.mkdir(parents=True, exist_ok=True)
    keep = ["open", "high", "low", "close", "bars", "contract", "rng", "up", "dn", "on_rng", "vx"]
    out_days = days[keep].join(pd.concat({m: np.exp(f["pred"]) for m, f in fcs.items()}, axis=1).add_prefix("forecast_"))
    out_days.join(fc[["q50", "q80", "q95"]].add_prefix(f"{best}_")).to_csv(args.out / "days.csv")
    t.to_csv(args.out / "races.csv", index=False)
    (args.out / "results.json").write_text(json.dumps(res, indent=2, default=str), encoding="utf-8")
    write_report(res, rel, coverage_by_year(days, fc), args.out / "report.html")
    print_summary(res)
    print(f"\nwrote {args.out}/days.csv, races.csv, results.json, report.html")
    return 0


def cov_year_dict(df: pd.DataFrame) -> dict:
    return {str(y): {k: (int(v) if k == "n" else float(v)) for k, v in r.items()} for y, r in df.to_dict("index").items()}


if __name__ == "__main__":
    sys.exit(main())
