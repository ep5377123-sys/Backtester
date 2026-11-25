"""BTC's weekend direction vs NQ's Sunday-night open (2026-11-02)

Rules:
  NQ gap    Sunday 18:00 ET first trade vs the last trade before the weekend, both in the contract front on Sunday
            (same gap as research/nwog.py).
  BTC move  CME BTC's price at NQ's pre-weekend close (last print within MAX_ANCHOR_STALE) vs its first print of the Sunday
            session, in the contract front on Sunday. CME BTC had no weekend session before 2026-05-29, so this first
            print is what prices in the weekend's spot move; a Sunday whose first print comes over MAX_OPEN_LATE after the
            open has no row in the tests, since by then it can have seen NQ.
  real      from 2026-05-29 BTC trades through the weekend: `btc_preopen_pct` is the move to the last print before
            Sunday 18:00, known before NQ opens. Those weeks check the first-print proxy; too few to test alone.
  after     NQ's move from the close of the minute in which BTC first printed (the first moment the signal is known) to
            15 min later, 60 min later and Monday 09:30. Before costs. Prices are read only within MAX_PRICE_STALE.
  tests     sign agreement against what independent signs would give, Pearson and Spearman r, all with a two-sided
            permutation p (SIMS, seed 42); 95% bootstrap CI on Pearson r. Weeks with a zero move have no sign.

    uv run python research/btc_weekend_nq_open.py [--start 2020-01-01] [--out reports/btc_weekend_nq_open]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from guban.research.stats import by_period, rate_test, verdict
from guban.store.read import quality, read_minute, store_root

MAX_ANCHOR_STALE = pd.Timedelta(minutes=60)
MAX_OPEN_LATE = pd.Timedelta(minutes=15)
MAX_PRICE_STALE = pd.Timedelta(minutes=5)
MAX_PREOPEN_STALE = pd.Timedelta(hours=6)
ONE_MINUTE = pd.Timedelta(minutes=1)
SIMS = 20_000
SEED = 42
SIZE_BUCKETS = (0.0, 1.0, 2.0, 3.0, 5.0)
AFTER = {"nq_after_15m": pd.Timedelta(minutes=15), "nq_after_60m": pd.Timedelta(minutes=60)}


def price_at(bars: pd.DataFrame, t: pd.Timestamp, stale: pd.Timedelta = MAX_PRICE_STALE) -> float:
    """The close of the last bar labeled at or before t; NaN if there is none within `stale`."""
    bars = bars[bars.index <= t]
    if bars.empty or t - bars.index[-1] > stale:
        return np.nan
    return float(bars["close"].iloc[-1])


def signs_agree(a, b) -> np.ndarray:
    """Per week: do the signs match? Only meaningful where both are non-zero; see `tests`."""
    return np.sign(a) == np.sign(b)


def tests(btc, nq, sims: int = SIMS, seed: int = SEED) -> dict:
    """Agreement and correlation of two aligned move series, weeks with a zero or missing move dropped."""
    btc, nq = np.asarray(btc, float), np.asarray(nq, float)
    keep = np.isfinite(btc) & np.isfinite(nq) & (btc != 0) & (nq != 0)
    btc, nq = btc[keep], nq[keep]
    n = len(btc)
    if n < 3:
        return {"n": n}
    rng = np.random.default_rng(seed)
    agree = signs_agree(btc, nq).mean()
    up_b, up_n = (btc > 0).mean(), (nq > 0).mean()
    expected = up_b * up_n + (1 - up_b) * (1 - up_n)
    rank_b, rank_n = pd.Series(btc).rank().to_numpy(), pd.Series(nq).rank().to_numpy()
    pearson = np.corrcoef(btc, nq)[0, 1]
    spearman = np.corrcoef(rank_b, rank_n)[0, 1]
    hits = np.zeros(3)
    for _ in range(sims):
        order = rng.permutation(n)
        hits += [
            abs(signs_agree(btc, nq[order]).mean() - expected) >= abs(agree - expected),
            abs(np.corrcoef(btc, nq[order])[0, 1]) >= abs(pearson),
            abs(np.corrcoef(rank_b, rank_n[order])[0, 1]) >= abs(spearman),
        ]
    boots = [np.corrcoef(btc[i], nq[i])[0, 1] for i in rng.integers(0, n, (2000, n))]
    p_agree, p_pearson, p_spearman = (hits + 1) / (sims + 1)
    return {
        "n": n,
        "agree": float(agree),
        "expected": float(expected),
        "p_agree": float(p_agree),
        "pearson": float(pearson),
        "pearson_lo": float(np.nanquantile(boots, 0.025)),
        "pearson_hi": float(np.nanquantile(boots, 0.975)),
        "p_pearson": float(p_pearson),
        "spearman": float(spearman),
        "p_spearman": float(p_spearman),
        "beta": float(np.polyfit(btc, nq, 1)[0]),
    }


def weekend_table(root: Path, start: str) -> pd.DataFrame:
    """One row per Sunday session on or after `start` that both NQ and BTC have, oldest first."""
    nq_front = quality(root, "NQ")
    nq_front = nq_front[nq_front["front"]].sort_values("session").reset_index(drop=True)
    btc_all = quality(root, "BTC")
    btc_files = set(zip(btc_all["contract"], btc_all["session"]))
    btc_contract = btc_all[btc_all["front"]].set_index("session")["contract"]
    dates = pd.to_datetime(nq_front["session"])

    rows = []
    for i in dates.index[(dates.dt.dayofweek == 6) & (dates >= pd.Timestamp(start))]:
        session = nq_front["session"].iloc[i]
        if i == 0 or session not in btc_contract.index:
            continue
        contract, prior_session = nq_front["contract"].iloc[i], nq_front["session"].iloc[i - 1]
        bars = read_minute(root, "NQ", contract, session)
        prior = read_minute(root, "NQ", contract, prior_session)
        btc_c = btc_contract[session]
        btc_bars = read_minute(root, "BTC", btc_c, session)
        if bars.empty or prior.empty or btc_bars.empty:
            continue
        nq_prior, nq_open = float(prior["close"].iloc[-1]), float(bars["open"].iloc[0])
        close_time, open_time = prior.index[-1], bars.index[0] - ONE_MINUTE
        row = {
            "week": dates.iloc[i].strftime("%Y-%m-%d"),
            "nq_contract": contract,
            "btc_contract": btc_c,
            "nq_prior_close_time": close_time,
            "nq_open_time": open_time,
            "nq_prior_close": nq_prior,
            "nq_open": nq_open,
            "nq_gap_pct": (nq_open / nq_prior - 1) * 100,
            "nq_gap_pts": nq_open - nq_prior,
            "btc_first_print_time": btc_bars.index[0] - ONE_MINUTE,
            "btc_first_print": float(btc_bars["open"].iloc[0]),
            "btc_late_min": (btc_bars.index[0] - ONE_MINUTE - open_time) / ONE_MINUTE,
            "btc_anchor_time": pd.NaT,
            "btc_anchor_stale_min": np.nan,
            "btc_preopen_pct": np.nan,
            "btc_preopen_stale_min": np.nan,
        }

        anchor = np.nan
        if (btc_c, prior_session) in btc_files:
            anchor_bars = read_minute(root, "BTC", btc_c, prior_session)
            anchor_bars = anchor_bars[anchor_bars.index <= close_time]
            if not anchor_bars.empty:
                row["btc_anchor_time"] = anchor_bars.index[-1]
                row["btc_anchor_stale_min"] = (close_time - anchor_bars.index[-1]) / ONE_MINUTE
                if close_time - anchor_bars.index[-1] <= MAX_ANCHOR_STALE:
                    anchor = float(anchor_bars["close"].iloc[-1])
        row["btc_anchor"] = anchor
        row["btc_pct"] = (row["btc_first_print"] / anchor - 1) * 100

        saturday = (dates.iloc[i] - pd.Timedelta(days=1)).strftime("%Y%m%d")
        if (btc_c, saturday) in btc_files:
            before = read_minute(root, "BTC", btc_c, saturday)
            before = before[before.index <= open_time]
            if not before.empty and open_time - before.index[-1] <= MAX_PREOPEN_STALE:
                row["btc_preopen_pct"] = (float(before["close"].iloc[-1]) / anchor - 1) * 100
                row["btc_preopen_stale_min"] = (open_time - before.index[-1]) / ONE_MINUTE

        known = btc_bars.index[0]
        entry = price_at(bars, known)
        for name, wait in AFTER.items():
            row[name] = (price_at(bars, known + wait) - entry) if known + wait <= bars.index[-1] else np.nan
        monday = dates.iloc[i].normalize().tz_localize("America/New_York") + pd.Timedelta(days=1, hours=9, minutes=30)
        row["nq_after_0930"] = (price_at(bars, monday) - entry) if monday <= bars.index[-1] else np.nan
        rows.append(row)
    return pd.DataFrame(rows)


def line(label: str, t: dict) -> str:
    if t["n"] < 3:
        return f"{label:<30} n={t['n']}"
    return (f"{label:<30} n={t['n']:>3}  agree {t['agree']:.1%} vs {t['expected']:.1%} (p {t['p_agree']:.3f})  "
            f"r {t['pearson']:+.3f} [{t['pearson_lo']:+.2f},{t['pearson_hi']:+.2f}] (p {t['p_pearson']:.4f})  "
            f"rank r {t['spearman']:+.3f} (p {t['p_spearman']:.4f})  beta {t['beta']:+.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2020-01-01")
    ap.add_argument("--out", type=Path, default=Path("reports/btc_weekend_nq_open"))
    args = ap.parse_args()

    table = weekend_table(store_root(), args.start)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "weekends.csv"
    table.to_csv(path, index=False)

    on_time = table[(table["btc_late_min"] <= MAX_OPEN_LATE / ONE_MINUTE) & table["btc_anchor"].notna()].copy()
    on_time = on_time[(on_time["btc_pct"] != 0) & (on_time["nq_gap_pct"] != 0)]
    print(f"{len(table)} Sundays {table['week'].iloc[0]} .. {table['week'].iloc[-1]}; {len(on_time)} usable "
          f"({(table['btc_late_min'] > MAX_OPEN_LATE / ONE_MINUTE).sum()} BTC first print late, "
          f"{table['btc_anchor'].isna().sum()} no BTC price at NQ's close, "
          f"{(table['btc_pct'] == 0).sum()} / {(table['nq_gap_pct'] == 0).sum()} zero BTC / NQ moves)")
    print(f"BTC anchor staleness: median {on_time['btc_anchor_stale_min'].median():.0f} min, max {on_time['btc_anchor_stale_min'].max():.0f}; "
          f"up weeks: BTC {(on_time['btc_pct'] > 0).mean():.1%}, NQ gap {(on_time['nq_gap_pct'] > 0).mean():.1%}")

    btc, gap = on_time["btc_pct"], on_time["nq_gap_pct"]
    print("\nNQ gap direction vs BTC weekend move")
    print(line("all", tests(btc, gap)))
    print(line("first print in first minute", tests(btc[on_time["btc_late_min"] == 0], gap[on_time["btc_late_min"] == 0])))
    calm = abs(btc) <= btc.abs().quantile(0.95)
    print(line("without largest 5% BTC moves", tests(btc[calm], gap[calm])))

    print("\nby |BTC move| (agreement of signs)")
    for lo, hi in zip(SIZE_BUCKETS, SIZE_BUCKETS[1:] + (np.inf,)):
        m = (btc.abs() >= lo) & (btc.abs() < hi)
        print(line(f"{lo:g}% to {hi:g}%", tests(btc[m], gap[m], sims=5000)))

    years = pd.to_datetime(on_time["week"]).dt.year.to_numpy()
    print("\nby year")
    for y in sorted(set(years)):
        print(line(str(y), tests(btc[years == y], gap[years == y], sims=5000)))

    up, nq_up = (btc > 0).to_numpy(), (gap > 0).to_numpy()
    for label, hit, signal in (("NQ gaps up | BTC up", nq_up, up), ("NQ gaps down | BTC down", ~nq_up, ~up)):
        t = rate_test(hit, signal, label)
        v = verdict(t, by_period(hit, signal, years))
        print(f"\n{label}: {t.rate:.1%} of {t.n} vs base {t.base_rate:.1%} (gap {t.gap * 100:+.1f} pp, p {t.p_value:.2g}) -> "
              f"{'REAL' if v.real else 'NOT REAL'}: " + "; ".join(f"{r} [{'ok' if ok else 'no'}: {d}]" for r, ok, d in v.checks))

    real = on_time[on_time["btc_preopen_pct"].notna()]
    print(f"\nreal weekends (BTC trading 24/7, last print before 18:00 known before NQ opens; median {real['btc_preopen_stale_min'].median():.0f} min stale)")
    print(line("pre-open BTC vs first print", tests(real["btc_preopen_pct"], real["btc_pct"], sims=5000)))
    print(line("pre-open BTC vs NQ gap", tests(real["btc_preopen_pct"], real["nq_gap_pct"], sims=5000)))

    print("\nNQ after the open, points from the minute BTC's print is known, in BTC's direction (before costs)")
    for name, label in (("nq_after_15m", "+15 min"), ("nq_after_60m", "+60 min"), ("nq_after_0930", "to Mon 09:30")):
        print(line(label, tests(btc, on_time[name])))
        with_btc = (np.sign(btc) * on_time[name]).dropna()
        tstat = with_btc.mean() / with_btc.std() * np.sqrt(len(with_btc))
        print(f"{'':<30} mean with BTC {with_btc.mean():+.2f} pts, t {tstat:+.2f}, win {(with_btc > 0).mean():.1%}")
    print(path)


if __name__ == "__main__":
    main()
