"""CFE VX futures daily bars (Databento XCBF.PITCH ohlcv-1d CSV) → a 30-day constant-maturity VX series (Phase 21).

The CSV holds outrights (`VX/H4`) and spreads (`VX/H4:1:S - VX/J4:1:B`), often two rows per outright per day (two
instrument ids on one symbol, prices within a few cents); the larger-volume row is kept. The expiry rule ignores
holidays: 5 of 104 contracts (H9, H2, M4, H5, K6) last traded a day before it, which moves days-to-expiry by one. Bars are UTC calendar days (`ts_event` midnight UTC, no Saturday bars), so the bar dated D-1 closes at
00:00 UTC on D, before an RTH open: `vx_before(series, day)` takes the last bar dated before `day`.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import numpy as np
import pandas as pd

MONTHS = "FGHJKMNQUVXZ"
_OUTRIGHT = re.compile(r"^VX/([FGHJKMNQUVXZ])(\d)$")
TARGET_DAYS = 30


def contract_year(digit: int) -> int:
    """VX year digit → year, for data from 2018 to 2027."""
    return 2010 + digit if digit >= 8 else 2020 + digit


def expiry(year: int, month: int) -> dt.date:
    """Final settlement date of the VX monthly contract for (year, month): the Wednesday 30 days before the third
    Friday of the following calendar month (CFE rule; holiday adjustments ignored)."""
    y, m = (year + 1, 1) if month == 12 else (year, month + 1)
    first = dt.date(y, m, 1)
    third_friday = first + dt.timedelta(days=(4 - first.weekday()) % 7 + 14)
    return third_friday - dt.timedelta(days=30)


def read_outrights(path: str | Path) -> pd.DataFrame:
    """Monthly outright bars: date (naive UTC date), symbol, expiry, open, high, low, close, volume."""
    df = pd.read_csv(path, usecols=["ts_event", "open", "high", "low", "close", "volume", "symbol"])
    m = df["symbol"].str.extract(_OUTRIGHT)
    df = df[m[0].notna()].copy()
    m = m[m[0].notna()]
    df["date"] = pd.to_datetime(df["ts_event"].str[:10])
    df["expiry"] = pd.to_datetime([expiry(contract_year(int(d)), MONTHS.index(c) + 1) for c, d in zip(m[0], m[1])])
    df = df.sort_values("volume").drop_duplicates(["date", "symbol"], keep="last")
    return df.drop(columns="ts_event").sort_values(["date", "expiry"]).reset_index(drop=True)


def constant_maturity(bars: pd.DataFrame, target_days: int = TARGET_DAYS) -> pd.DataFrame:
    """Per bar date: vx (target-day constant-maturity close), vx1 (front close), t1/t2 (days to the two nearest
    expiries after the date). Linear in days between F1 and F2 when t1 < target <= t2; F1 alone when t1 >= target or
    F2 has no bar that day."""
    live = bars[bars["expiry"] > bars["date"]]
    rows = []
    for date, g in live.groupby("date", sort=True):
        g = g.sort_values("expiry")
        t = (g["expiry"] - date).dt.days.to_numpy()
        f = g["close"].to_numpy(float)
        t1, f1 = t[0], f[0]
        if t1 >= target_days or len(g) < 2:
            vx, t2 = f1, (t[1] if len(g) > 1 else np.nan)
        else:
            t2, f2 = t[1], f[1]
            w = (t2 - target_days) / (t2 - t1)
            vx = w * f1 + (1 - w) * f2
        rows.append({"date": date, "vx": vx, "vx1": f1, "t1": t1, "t2": t2})
    return pd.DataFrame(rows).set_index("date")


def load_vx(path: str | Path, target_days: int = TARGET_DAYS) -> pd.DataFrame:
    return constant_maturity(read_outrights(path), target_days)


def vx_before(series: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """For each trading day, the value of the last bar dated strictly before it (NaN when none)."""
    s = series.sort_index()
    pos = s.index.searchsorted(days, side="left") - 1
    out = np.where(pos >= 0, s.to_numpy()[np.clip(pos, 0, None)], np.nan)
    return pd.Series(out, index=days, name=series.name)


def expiry_mismatches(bars: pd.DataFrame, data_end: pd.Timestamp | None = None) -> pd.DataFrame:
    """Contracts whose last bar date differs from the rule's expiry (contracts still trading at data_end skipped)."""
    last = bars.groupby("symbol").agg(last=("date", "max"), expiry=("expiry", "first"))
    end = data_end if data_end is not None else bars["date"].max()
    last = last[last["expiry"] <= end]
    return last[last["last"] != last["expiry"]]
