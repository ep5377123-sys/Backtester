"""Build N-minute and daily bars from the 1-minute store, aligned to a session template.

Conventions (MultiCharts):
- Every bar is labeled by its CLOSE time, tz-aware America/New_York.
- N-minute bins are counted from the session open, so with RTH 09:30 the first 5-minute bar is 09:35.
- Only 1-minute bars inside the session are used. Minutes with no data are not filled; a bin with no
  1-minute bars produces no bar.
- Daily bars are one per session, labeled at the session close time on the session's trading date.

Bin arithmetic is done in Eastern wall-clock minutes. Sessions never span the 02:00 Sunday DST switch
(Globex is closed then), so wall-clock and elapsed minutes agree inside a session.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import Session, get_session

TZ = "America/New_York"
_NS_PER_MIN = 60_000_000_000
_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "bid_vol": "sum", "ask_vol": "sum"}


def parse_timeframe(tf: str) -> int | None:
    """'5m' -> 5, '1h' -> 60, '1d'/'D' -> None (daily). Raises ValueError on anything else."""
    s = tf.strip().lower()
    if s in ("d", "1d", "daily"):
        return None
    m = re.fullmatch(r"(\d+)\s*(m|min|h)", s)
    if not m or int(m.group(1)) <= 0:
        raise ValueError(f"Bad timeframe {tf!r}: use e.g. 1m, 5m, 15m, 1h, 1d")
    return int(m.group(1)) * (60 if m.group(2) == "h" else 1)


def _wall_minutes(index: pd.DatetimeIndex) -> np.ndarray:
    """Wall-clock minutes since 1970-01-01 (index may be any datetime resolution)."""
    return index.tz_localize(None).as_unit("ns").asi8 // _NS_PER_MIN


def _session_minutes(index: pd.DatetimeIndex, session: Session) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each 1-min bar: (in-session mask, session open as wall-clock minute number, trading-day number)."""
    minute = _wall_minutes(index)
    day, tod = np.divmod(minute, 1440)
    s, e = session.start_min, session.end_min
    if session.overnight:
        after_open = tod > s
        in_sess = after_open | (tod <= e)
        trade_day = day + after_open  # bars after the evening open belong to the next day's session
        open_min = (trade_day - 1) * 1440 + s
    else:
        in_sess = (tod > s) & (tod <= e)
        trade_day = day
        open_min = day * 1440 + s
    return in_sess, open_min, trade_day


def _localize_minutes(minutes: np.ndarray) -> pd.DatetimeIndex:
    naive = pd.DatetimeIndex(minutes * _NS_PER_MIN, dtype="datetime64[ns]")
    return naive.tz_localize(TZ, ambiguous="raise", nonexistent="raise")


def resample(bars: pd.DataFrame, timeframe: str, session: str | Session = "rth", allow_partial: bool = False) -> pd.DataFrame:
    """Resample 1-minute bars (close-time labeled, tz-aware ET) to `timeframe` inside `session`.

    Output columns: open, high, low, close, volume, bid_vol, ask_vol, contract, session_date.
    session_date is the trading date (naive midnight) the bar belongs to.
    """
    sess = get_session(session) if isinstance(session, str) else session
    n = parse_timeframe(timeframe)
    if n is not None and sess.length_min % n and not allow_partial:
        raise ValueError(
            f"{timeframe} does not divide the {sess.key} session ({sess.length_min} min); "
            "pass allow_partial=True to end each session with a shorter bar"
        )
    if str(bars.index.tz) != TZ:
        raise ValueError(f"1-minute bars must be indexed in {TZ}, got {bars.index.tz}")

    in_sess, open_min, trade_day = _session_minutes(bars.index, sess)
    src = bars[in_sess]
    open_min, trade_day = open_min[in_sess], trade_day[in_sess]

    if n is None:  # daily: one bar per session, labeled at the session close
        label_min = trade_day * 1440 + sess.end_min
    else:
        offset = _wall_minutes(src.index) - open_min  # 1 .. session length
        bin_end = np.minimum(-(-offset // n) * n, sess.length_min)  # ceil to N, capped at session close
        label_min = open_min + bin_end

    agg = {k: v for k, v in _AGG.items() if k in src.columns}
    if "contract" in src.columns:
        agg["contract"] = "last"
    out = src.groupby(label_min, sort=True).agg(agg)
    # A bin never spans two sessions, so every row of a bin shares one trading day.
    days = pd.Series(trade_day, index=label_min).groupby(level=0, sort=True).first().to_numpy()
    out["session_date"] = (days * 1440 * _NS_PER_MIN).astype("datetime64[ns]")
    if "contract" in out.columns and isinstance(bars["contract"].dtype, pd.CategoricalDtype):
        out["contract"] = out["contract"].astype(bars["contract"].dtype)
    out.index = _localize_minutes(out.index.to_numpy())
    out.index.name = "time"
    return out


def cache_path(symbol: str, timeframe: str, session: str, data_dir: Path = Path("data")) -> Path:
    n = parse_timeframe(timeframe)
    tf = "1d" if n is None else f"{n}m"
    return Path(data_dir) / symbol / f"{tf}_{session.lower()}.parquet"


def load_bars(
    symbol: str,
    timeframe: str = "1m",
    session: str = "rth",
    data_dir: Path = Path("data"),
    rebuild: bool = False,
    tz: str | None = None,
) -> pd.DataFrame:
    """Bars for `symbol` at `timeframe`/`session`, from the Parquet cache when it is newer than 1m.parquet.

    tz: chart time zone for the returned index (e.g. "America/Chicago" for an MC chart in CME exchange time).
    Sessions and bins are always built in Eastern time; this only relabels the same instants.
    """
    source = Path(data_dir) / symbol / "1m.parquet"
    if not source.exists():
        raise FileNotFoundError(f"{source} not found; run `guban import` first")
    cached = cache_path(symbol, timeframe, session, data_dir)
    if not rebuild and cached.exists() and cached.stat().st_mtime >= source.stat().st_mtime:
        bars = pd.read_parquet(cached)
    else:
        bars = resample(pd.read_parquet(source), timeframe, session)
        bars.to_parquet(cached)
    return bars.tz_convert(tz) if tz else bars


def splice_history(bars: pd.DataFrame, history: pd.DataFrame) -> pd.DataFrame:
    """Prepend the rows of `history` that come before `bars`' first bar, e.g. NQ bars ahead of an MNQ series
    that starts later (same underlying). Lets indicators with long memory see the same history as MC did."""
    if str(history.index.tz) != str(bars.index.tz):
        history = history.tz_convert(bars.index.tz)
    out = pd.concat([history[history.index < bars.index[0]], bars])
    if "contract" in out.columns:
        out["contract"] = out["contract"].astype(str)
    return out
