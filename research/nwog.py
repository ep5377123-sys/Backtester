"""New Week Opening Gaps: size of every Sunday-open gap since 2019 and how long it took to fill (2026-11-02)

Rules:
  gap      the first trade of the Sunday 18:00 ET session vs the last trade before the weekend (Friday 17:00 ET, or the
           early close / Thursday close on holiday weeks), both in the contract that is front on Sunday, so a weekend
           roll adds no contract spread. A Sunday with no session (Christmas / New Year closures) has no row.
  filled   the first later 1-minute bar, the Sunday open's own bar included, that trades the prior close: low <= level
           for an up gap, high >= level for a down gap. Fill times are 1-minute bar starts.
  rolls    a gap still open at a roll moves with the contract: its level shifts by the spread (new - old close) at the
           last minute both contracts traded in the old contract's last front session. `Rolls Crossed` counts them.
  days     `Days To Fill` = calendar days from the Sunday open to the fill bar; `Trading Days To Fill` counts sessions,
           1 = filled by Monday's 17:00 ET close. Both are blank for a gap still open at the end of the data.
  notes    `Data Note` marks a gap that was still open across a session with almost no minute data (below
           MIN_SESSION_BARS, e.g. 2020-11-29 stops at 19:00): a fill inside the hole can't be seen, so it may be earlier.

    uv run python research/nwog.py [--symbol NQ] [--start 2019-01-01] [--out reports/nwog]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from guban.store.read import quality, read_minute, store_root

ONE_MINUTE = pd.Timedelta(minutes=1)
TIME_FORMAT = "%Y-%m-%d %H:%M"
MIN_SESSION_BARS = 300


def roll_spread(root: Path, symbol: str, old: str, new: str, session: str) -> float:
    """new - old close at the last minute of `session` that both contracts traded."""
    both = read_minute(root, symbol, old, session)[["close"]].join(
        read_minute(root, symbol, new, session)[["close"]], how="inner", lsuffix="_old", rsuffix="_new"
    )
    if both.empty:
        raise ValueError(f"{symbol} {old}/{new} share no minute in session {session}; cannot measure the roll spread")
    return float(both["close_new"].iloc[-1] - both["close_old"].iloc[-1])


def contract_offsets(root: Path, symbol: str, front: pd.DataFrame) -> pd.Series:
    """Per front session: the amount that puts its prices on the last contract's scale (back adjustment)."""
    offsets = np.zeros(len(front))
    for i in range(len(front) - 1, 0, -1):
        old, new = front["contract"].iloc[i - 1], front["contract"].iloc[i]
        spread = roll_spread(root, symbol, old, new, front["session"].iloc[i - 1]) if old != new else 0.0
        offsets[i - 1] = offsets[i] + spread
    return pd.Series(offsets, index=front.index)


def nwog_table(root: Path, symbol: str, start: str) -> pd.DataFrame:
    """One row per Sunday session on or after `start`, oldest first."""
    q = quality(root, symbol)
    front = q[q["front"]].sort_values("session").reset_index(drop=True)
    dates = pd.to_datetime(front["session"])
    first = max(int(np.searchsorted(dates, pd.Timestamp(start))) - 1, 0)
    front, dates = front.iloc[first:].reset_index(drop=True), dates.iloc[first:].reset_index(drop=True)
    offsets = contract_offsets(root, symbol, front)

    frames, session_starts, n = [], [], 0
    for row in front.itertuples():
        bars = read_minute(root, symbol, row.contract, row.session)[["open", "high", "low"]]
        frames.append(bars)
        session_starts.append(n)
        n += len(bars)
    times = pd.DatetimeIndex(pd.concat(frames).index)
    high = np.concatenate([f["high"].to_numpy() + offsets[i] for i, f in enumerate(frames)])
    low = np.concatenate([f["low"].to_numpy() + offsets[i] for i, f in enumerate(frames)])
    session_of_bar = np.repeat(np.arange(len(frames)), [len(f) for f in frames])

    rows = []
    for i in dates.index[(dates.dt.dayofweek == 6) & (dates >= pd.Timestamp(start))]:
        contract = front["contract"].iloc[i]
        prior = read_minute(root, symbol, contract, front["session"].iloc[i - 1])
        if prior.empty:
            raise ValueError(f"{symbol} {contract} has no bars in {front['session'].iloc[i - 1]}, the session before a Sunday")
        level, opened = float(prior["close"].iloc[-1]), float(frames[i]["open"].iloc[0])
        gap = opened - level
        start_bar = session_starts[i]
        open_time = times[start_bar] - ONE_MINUTE
        row = {
            "Week Of (Sunday)": dates.iloc[i].strftime("%Y-%m-%d"),
            "Contract": contract,
            "Prior Close Time (ET)": prior.index[-1].strftime(TIME_FORMAT),
            "Prior Close": level,
            "Sunday Open Time (ET)": open_time.strftime(TIME_FORMAT),
            "Sunday Open": opened,
            "Direction": "Up" if gap > 0 else "Down" if gap < 0 else "None",
            "Gap Points": abs(gap),
            "Gap %": round(abs(gap) / level * 100, 3),
            "Status": "No gap" if gap == 0 else "Unfilled",
            "Fill Time (ET)": "",
            "Fill Price": np.nan,
            "Days To Fill": np.nan,
            "Trading Days To Fill": np.nan,
            "Rolls Crossed": 0,
            "Data Note": "",
        }
        if gap != 0:
            target = level + offsets[i]
            hits = low[start_bar:] <= target if gap > 0 else high[start_bar:] >= target
            if hits.any():
                bar = start_bar + int(hits.argmax())
                fill_session = int(session_of_bar[bar])
                fill_time = times[bar] - ONE_MINUTE
                row |= {
                    "Status": "Filled",
                    "Fill Time (ET)": fill_time.strftime(TIME_FORMAT),
                    "Fill Price": target - offsets[fill_session],
                    "Days To Fill": round((fill_time - open_time) / pd.Timedelta(days=1), 2),
                    "Trading Days To Fill": fill_session - i + 1,
                    "Rolls Crossed": int(front["contract"].iloc[i:fill_session + 1].nunique()) - 1,
                }
            else:
                row["Rolls Crossed"] = int(front["contract"].iloc[i:].nunique()) - 1
            end = fill_session if row["Status"] == "Filled" else len(front) - 1
            holes = front.iloc[i:end + 1].query("bars < @MIN_SESSION_BARS")["trade_date"]
            if len(holes):
                row["Data Note"] = f"Minute data missing on {', '.join(holes)}; a fill then would not be seen"
        rows.append(row)
    table = pd.DataFrame(rows)
    return table.astype({"Trading Days To Fill": "Int64"})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbol", default="NQ")
    ap.add_argument("--start", default="2019-01-01")
    ap.add_argument("--out", type=Path, default=Path("reports/nwog"))
    args = ap.parse_args()

    table = nwog_table(store_root(), args.symbol, args.start)
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"{args.symbol}_new_week_opening_gaps.csv"
    table.to_csv(path, index=False)

    gaps = table[table["Status"] != "No gap"]
    days = gaps["Days To Fill"].fillna(np.inf)
    print(f"{len(table)} Sundays {table['Week Of (Sunday)'].iloc[0]} .. {table['Week Of (Sunday)'].iloc[-1]}; "
          f"{len(gaps)} gaps, {(gaps['Status'] == 'Filled').sum()} filled, {(gaps['Status'] == 'Unfilled').sum()} unfilled, "
          f"{(table['Status'] == 'No gap').sum()} no gap; {(gaps['Direction'] == 'Up').sum()} up / {(gaps['Direction'] == 'Down').sum()} down")
    print(f"gap points: median {gaps['Gap Points'].median():.2f}, mean {gaps['Gap Points'].mean():.2f}, max {gaps['Gap Points'].max():.2f}; "
          f"gap %: median {gaps['Gap %'].median():.3f}")
    print(f"days to fill: median {days.median():.2f}, 75th pct {days.quantile(.75):.2f}, 90th pct {days.quantile(.9):.2f}, max filled {gaps['Days To Fill'].max():.2f}")
    print("filled within: " + ", ".join(f"{d}d {(days <= d).mean():.1%}" for d in (0.25, 1, 2, 5, 7, 14, 30)))
    print(path)


if __name__ == "__main__":
    main()
