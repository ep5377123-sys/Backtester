"""After price displaces X points from a session open, how often does it trade back to that open? (2026-11-01)

Rules (user, 2026-11-01). Anchor = the open of the 1-minute bar that starts at `--open` (ET); the window runs from it to
`--end` (ET; an end at or before the open means the next day). For each displacement threshold X on the grid:
  displaced   the first window bar whose high >= open + X (up) or low <= open - X (down); the side hit first counts
              (one bar reaching both sides: the one whose X-level is nearer that bar's open, a coin flip in practice)
  reverted    a LATER window bar trades back to the open (low <= open after an up move, high >= open after a down move)
The displacement bar itself is not checked for the return: the order inside one minute is unknown, so the rate is
conservative. Sessions without a bar starting exactly at the anchor are skipped. Descriptive study: no verdict.
Per threshold: displaced days, revert rate (Wilson 95% CI), minutes from displacement to the return, the furthest
the move ran from the open before returning, and where the non-reverting days stood at the window's end.

    uv run python research/open_revert.py --open 09:30 --end 11:30 --grid 10:200:10
    uv run python research/open_revert.py --open 20:00 --end 00:00 --grid 5:100:5
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from guban.data.resample import load_bars
from guban.research.report import write_study_report

Z = 1.959963984540054


def wilson(k: int, n: int) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * np.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return float(c - h), float(c + h)


def parse_grid(s: str) -> list[float]:
    lo, hi, step = (float(v) for v in s.split(":"))
    return [round(v, 6) for v in np.arange(lo, hi + step / 2, step)]


def windows(minutes: pd.DataFrame, open_t: str, end_t: str) -> list[tuple[pd.Timestamp, np.ndarray, np.ndarray, np.ndarray, float]]:
    """Per session: (anchor bar start, high, low, close arrays of the window's bars, anchor open)."""
    h0, m0 = (int(v) for v in open_t.split(":"))
    h1, m1 = (int(v) for v in end_t.split(":"))
    dur = pd.Timedelta(minutes=((h1 * 60 + m1) - (h0 * 60 + m0)) % 1440 or 1440)
    start = minutes.index - pd.Timedelta(minutes=1)
    is_anchor = (start.hour == h0) & (start.minute == m0)
    out = []
    starts = start.values
    for i in np.flatnonzero(is_anchor):
        t0 = start[i]
        j = np.searchsorted(starts, (t0 + dur).asm8, side="left")
        w = minutes.iloc[i:j]
        out.append((t0, w["high"].to_numpy(), w["low"].to_numpy(), w["close"].to_numpy(), float(w["open"].iloc[0])))
    return out


def events(wins: list, grid: list[float]) -> pd.DataFrame:
    """One row per (session, threshold) that was displaced."""
    rows = []
    for t0, hi, lo, cl, o in wins:
        up_ext, dn_ext = hi - o, o - lo
        for x in grid:
            up, dn = up_ext >= x, dn_ext >= x
            iu = int(np.argmax(up)) if up.any() else len(hi)
            idn = int(np.argmax(dn)) if dn.any() else len(hi)
            if iu == len(hi) and idn == len(hi):
                continue
            side = 1 if iu < idn else -1 if idn < iu else (1 if hi[iu] - o <= o - lo[iu] else -1)
            i = iu if side > 0 else idn
            back = (lo[i + 1:] <= o) if side > 0 else (hi[i + 1:] >= o)
            rev = bool(back.any())
            k = i + 1 + int(np.argmax(back)) if rev else len(hi)
            ext = up_ext if side > 0 else dn_ext
            rows.append({
                "anchor": t0, "year": t0.year, "threshold": x, "side": "up" if side > 0 else "down",
                "reverted": rev, "disp_minute": i + 1,
                "minutes_to_revert": (k - i) if rev else np.nan,
                "max_ext_before_revert": float(ext[: k + 1].max()) if rev else np.nan,
                "end_dist": float(side * (cl[-1] - o)) if not rev else np.nan,
                "window_max_ext": float(ext.max()),
            })
    return pd.DataFrame(rows)


def summarize(e: pd.DataFrame, sessions: int) -> dict:
    n, k = len(e), int(e["reverted"].sum()) if len(e) else 0
    lo, hi = wilson(k, n)
    r = e[e["reverted"]] if n else e
    nr = e[~e["reverted"]] if n else e
    return {
        "displaced": n, "displaced_pct": n / sessions if sessions else float("nan"), "reverted": k,
        "rate": k / n if n else float("nan"), "ci_low": lo, "ci_high": hi,
        "med_minutes": float(r["minutes_to_revert"].median()) if len(r) else float("nan"),
        "med_ext": float(r["max_ext_before_revert"].median()) if len(r) else float("nan"),
        "p80_ext": float(r["max_ext_before_revert"].quantile(0.8)) if len(r) else float("nan"),
        "med_end_dist": float(nr["end_dist"].median()) if len(nr) else float("nan"),
    }


def fmt(rows: dict, index_name: str) -> pd.DataFrame:
    t = pd.DataFrame.from_dict(rows, orient="index")
    f = pd.DataFrame(index=t.index.rename(index_name))
    f["displaced days"] = [f"{n:,} ({p:.0%})" for n, p in zip(t["displaced"], t["displaced_pct"])]
    f["reverted"] = t["reverted"].map("{:,}".format)
    f["revert rate"] = t["rate"].map("{:.1%}".format)
    f["95% CI"] = [f"{a:.1%} … {b:.1%}" for a, b in zip(t["ci_low"], t["ci_high"])]
    f["median min to revert"] = t["med_minutes"].map("{:.0f}".format)
    f["median furthest ext. (reverts)"] = t["med_ext"].map("{:.1f}".format)
    f["80th pct ext. (reverts)"] = t["p80_ext"].map("{:.1f}".format)
    f["median end dist. (no revert)"] = t["med_end_dist"].map("{:+.1f}".format)
    return f.replace("nan", "–").replace("+nan", "–")


def label(x: float) -> str:
    return f"{x:g} pts"


def study(symbol: str, open_t: str, end_t: str, grid: list[float], out: Path) -> dict:
    minutes = load_bars(symbol, "1m", "eth")
    wins = windows(minutes, open_t, end_t)
    sessions = len(wins)
    e = events(wins, grid)
    years = sorted({w[0].year for w in wins})
    per_year_sessions = pd.Series([w[0].year for w in wins]).value_counts()

    both = {label(x): summarize(e[e["threshold"] == x], sessions) for x in grid}
    up = {label(x): summarize(e[(e["threshold"] == x) & (e["side"] == "up")], sessions) for x in grid}
    down = {label(x): summarize(e[(e["threshold"] == x) & (e["side"] == "down")], sessions) for x in grid}

    pivot = pd.DataFrame(index=pd.Index([label(x) for x in grid], name="threshold"))
    for y in years:
        col = []
        for x in grid:
            s = e[(e["threshold"] == x) & (e["year"] == y)]
            col.append(f"{s['reverted'].mean():.0%} ({len(s)})" if len(s) else "–")
        pivot[str(y)] = col

    span = f"{wins[0][0]:%Y-%m-%d} → {wins[-1][0]:%Y-%m-%d}"
    xs = [label(x) for x in grid]
    charts = [
        {"id": "rate", "title": "Revert rate by displacement", "yformat": ".0%",
         "caption": "Share of displaced days that traded back to the open later in the window.",
         "x": xs, "series": [{"name": "up moves", "y": [round(up[k]["rate"], 4) if up[k]["displaced"] else None for k in xs]},
                             {"name": "down moves", "y": [round(down[k]["rate"], 4) if down[k]["displaced"] else None for k in xs]},
                             {"name": "both", "y": [round(both[k]["rate"], 4) if both[k]["displaced"] else None for k in xs]}]},
        {"id": "count", "title": "Days displaced at least X points", "yformat": ",.0f",
         "caption": f"Out of {sessions:,} sessions with an anchor bar.",
         "x": xs, "series": [{"name": "displaced", "y": [both[k]["displaced"] for k in xs]},
                             {"name": "reverted", "y": [both[k]["reverted"] for k in xs]}]},
        {"id": "ext", "title": "How far it ran before coming back (reverting days)", "yformat": ",.0f",
         "caption": "Furthest distance from the open before the return; points.",
         "x": xs, "series": [{"name": "median", "y": [None if np.isnan(both[k]["med_ext"]) else round(both[k]["med_ext"], 1) for k in xs]},
                             {"name": "80th percentile", "y": [None if np.isnan(both[k]["p80_ext"]) else round(both[k]["p80_ext"], 1) for k in xs]}]},
    ]
    tables = [
        {"title": "Both directions", "caption": "Displaced days = sessions that reached X in either direction (share of all sessions).",
         "frame": fmt(both, "threshold")},
        {"title": "Up moves (open is below)", "caption": "", "frame": fmt(up, "threshold")},
        {"title": "Down moves (open is above)", "caption": "", "frame": fmt(down, "threshold")},
        {"title": "Revert rate by year", "caption": "Both directions; cell = revert rate (displaced days). NQ's price roughly tripled "
         "over the span, so a fixed point threshold is a smaller move in later years.", "frame": pivot},
    ]
    notes = [
        f"Data: {symbol} 1-minute ETH bars, unadjusted continuous contract, ET; {sessions:,} sessions with a bar starting at {open_t}, {span}.",
        f"Window: {open_t} → {end_t} ET. A session's early close cuts the window short.",
        "The displacement bar is not checked for the return (order inside a minute unknown), so the revert rate is a lower bound.",
        "Furthest extension and minutes-to-revert are over reverting days; end distance is the close of the window's last bar "
        "vs the open, signed in the direction of the move, over non-reverting days.",
        "Rates at successive thresholds share days (a 50-point day is also a 40-point day), so they are not independent.",
    ]
    title = f"{symbol} return to the {open_t} open ({open_t}–{end_t} ET)"
    write_study_report(
        out / "report.html",
        {"title": title,
         "subtitle": f"After price moves X points from the {open_t} open, how often does it trade back to it before {end_t}?",
         "details": {"symbol": symbol, "span": span, "window": f"{open_t} → {end_t} ET", "sessions": f"{sessions:,}"}},
        charts=charts, tables=tables, notes=notes,
    )
    out.mkdir(parents=True, exist_ok=True)
    e.to_csv(out / "events.csv", index=False)
    result = {"symbol": symbol, "open": open_t, "end": end_t, "span": span, "sessions": sessions,
              "per_year_sessions": {int(k): int(v) for k, v in per_year_sessions.items()},
              "both": both, "up": up, "down": down}
    (out / "study.json").write_text(json.dumps(result, indent=2, default=float), encoding="utf-8")
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--open", dest="open_t", default="09:30", help="anchor bar start, HH:MM ET")
    ap.add_argument("--end", dest="end_t", default="11:30", help="window end, HH:MM ET (<= open = next day)")
    ap.add_argument("--grid", default="10:200:10", help="lo:hi:step points")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()
    out = a.out or Path(f"reports/open_revert_{a.open_t.replace(':', '')}_{a.end_t.replace(':', '')}")
    res = study(a.symbol, a.open_t, a.end_t, parse_grid(a.grid), out)
    print(f"{res['symbol']} {a.open_t}->{a.end_t} ET, {res['sessions']:,} sessions, {res['span']}")
    print(f"{'X':>7} {'disp':>6} {'rate':>6}  {'up':>12} {'down':>12} {'med min':>8} {'med ext':>8}")
    for k, b in res["both"].items():
        u, d = res["up"][k], res["down"][k]
        print(f"{k:>7} {b['displaced']:6,} {b['rate']:6.1%}  {u['rate']:6.1%} ({u['displaced']:4}) "
              f"{d['rate']:6.1%} ({d['displaced']:4}) {b['med_minutes']:8.0f} {b['med_ext']:8.1f}")
    print(f"wrote {out / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
