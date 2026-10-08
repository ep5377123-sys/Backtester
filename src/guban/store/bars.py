"""One DeepCharts session (a contract's Minute and Tick file) -> the store's 1-minute bars and tick rows.

Store schema (also in the store's README.md):
- minute: ts_utc (bar OPEN, UTC), open, high, low, close, volume, ask_vol, bid_vol, contract, source ("tick"/"minute")
- tick:   ts_utc (ms, UTC), price, size, side (+1 at the ask, -1 at the bid, 0 between), seq, contract

Rules:
- Tick rows: every decoded record in file order, size-0 records included. seq is the raw cumulative-volume field,
  which stops tracking size from 2026-05-26 (see guban.data.ticks).
- Tick bars: records sorted by time, grouped by the minute they fall in (bar OPEN label): O first, H max, L min,
  C last over every record, volume = sum of size, ask_vol / bid_vol = size at the ask / bid; minutes with no size
  dropped. Size-0 records (2026-05-26+ feed) count for prices only: DeepCharts' own minute bars include their
  prices (with them, 69 of 74 sampled MNQ OHLC mismatches disappear) while size alone sums to the minute volume.
- Minute-record bars: minutes whose records hold no volume (placeholders) dropped; all records of a minute with
  volume merged in file order (O first, H max, L min, C last, volumes summed) -- the opening minute's zero-volume
  first record is its true open. DeepCharts minute field 6 is the sell side (bid_vol), as in guban.data.importer.
- Bar source (config/store.toml `bars`): "minute" (user, 2026-10-07: 1-minute bars by default, ticks only for
  strategies that need them) builds a session's bars from its minute records whenever they hold volume, and from
  its ticks only when they hold none. "tick" (the Phase 14 rule) builds them from the ticks when the tick file is
  whole, its summed size is within tolerance_pct of the minute volume, and at least min_minute_match of the
  session's minutes carry exactly the minute record's volume. In a tick session, a minute with a record but no
  trade keeps the record (source "minute"). Either way the tick check runs and `reason` names a tick problem.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from guban.data.importer import RECORD
from guban.data.ticks import tick_times

TZ = "America/New_York"
MINUTE_COLUMNS = ["ts_utc", "open", "high", "low", "close", "volume", "ask_vol", "bid_vol", "contract", "source"]
TICK_COLUMNS = ["ts_utc", "price", "size", "side", "seq", "contract"]
_AGG = dict(open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
            volume=("volume", "sum"), ask_vol=("ask_vol", "sum"), bid_vol=("bid_vol", "sum"))  # fmt: skip


def to_utc(dotnet: np.ndarray) -> pd.DatetimeIndex:
    """.NET ticks (Eastern wall clock) -> tz-aware UTC. A repeated fall-back hour (only BTC, which trades 24/7 since
    2026-05-29, crosses one) is resolved from the records' order."""
    naive = tick_times(dotnet)
    try:
        local = naive.tz_localize(TZ, ambiguous="raise", nonexistent="raise")
    except ValueError:
        local = naive.tz_localize(TZ, ambiguous="infer", nonexistent="raise")
    return local.tz_convert("UTC")


def tick_rows(raw: np.ndarray, contract: str) -> pd.DataFrame:
    """The tick file's records as store rows (TICK_COLUMNS), in file order."""
    price, bid, ask = raw["price"], raw["bid"], raw["ask"]
    side = np.select([(price == ask) & (price != bid), (price == bid) & (price != ask)], [1, -1], 0).astype(np.int8)
    return pd.DataFrame({
        "ts_utc": to_utc(raw["ticks"]).as_unit("ms"), "price": price.astype(np.float64), "size": raw["size"].astype(np.int32),
        "side": side, "seq": raw["cum_volume"].astype(np.int32), "contract": pd.Series([contract] * len(raw), dtype="str"),
    })  # fmt: skip


def _bars(df: pd.DataFrame, contract: str, source: str) -> pd.DataFrame:
    out = df.groupby("ts_utc", sort=True).agg(**_AGG).reset_index()
    for k in ("volume", "ask_vol", "bid_vol"):
        out[k] = out[k].astype(np.int64)
    out["contract"] = pd.Series([contract] * len(out), dtype="str")
    out["source"] = pd.Series([source] * len(out), dtype="str")
    return out[MINUTE_COLUMNS]


def minute_record_bars(raw: np.ndarray, contract: str) -> pd.DataFrame:
    """Bars from a minute file's records (MINUTE_COLUMNS, source "minute"), sorted by time. A minute whose records
    hold no volume is a placeholder and is dropped; a minute with volume merges ALL its records: the session's
    opening minute is stored as four records (open with volume 0, high, low, close), and the zero-volume one is the
    true open (it equals the first tick)."""
    df = pd.DataFrame({
        "ts_utc": to_utc(raw["ticks"]), "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
        "volume": raw["volume"].astype(np.int64), "ask_vol": raw["ask_vol"].astype(np.int64), "bid_vol": raw["bid_vol"].astype(np.int64),
    })  # fmt: skip
    live = df.groupby("ts_utc")["volume"].transform("sum") > 0
    return _bars(df[live], contract, "minute")


def tick_bars(ticks: pd.DataFrame) -> pd.DataFrame:
    """Bars from tick rows (MINUTE_COLUMNS, source "tick"), labeled by their minute's open: prices of every record
    (size-0 ones too: DeepCharts' own minute bars include them), volume from size; minutes with no size dropped."""
    live = ticks.sort_values("ts_utc", kind="stable")
    size = live["size"].to_numpy(np.int64)
    side = live["side"].to_numpy()
    df = pd.DataFrame({
        "ts_utc": live["ts_utc"].dt.floor("min").dt.as_unit("ns"), "open": live["price"], "high": live["price"], "low": live["price"],
        "close": live["price"], "volume": size, "ask_vol": np.where(side == 1, size, 0), "bid_vol": np.where(side == -1, size, 0),
    })  # fmt: skip
    contract = str(ticks["contract"].iat[0]) if len(ticks) else ""
    bars = _bars(df, contract, "tick")
    return bars[bars["volume"] > 0].reset_index(drop=True)


@dataclass
class SessionResult:
    bars: pd.DataFrame  # MINUTE_COLUMNS
    ticks: pd.DataFrame | None  # TICK_COLUMNS, None when there is no tick file
    quality: dict


def match_counts(minute: pd.DataFrame, bars: pd.DataFrame) -> tuple[int, int, int, int]:
    """Compare bars with minute-record bars: (minutes with equal volume, minutes in either; minutes with equal OHLC,
    minutes in both)."""
    m, t = minute.set_index("ts_utc"), bars.set_index("ts_utc")
    union = m.index.union(t.index)
    vol_eq = m["volume"].reindex(union, fill_value=0).to_numpy() == t["volume"].reindex(union, fill_value=0).to_numpy()
    both = m.index.intersection(t.index)
    cols = ["open", "high", "low", "close"]
    ohlc_eq = (m.loc[both, cols].to_numpy() == t.loc[both, cols].to_numpy()).all(axis=1)
    return int(vol_eq.sum()), len(union), int(ohlc_eq.sum()), len(both)


def _match(minute: pd.DataFrame, bars: pd.DataFrame) -> tuple[float, float]:
    v, n, o, b = match_counts(minute, bars)
    return (v / n if n else 1.0), (o / b if b else 1.0)


def session_bars(
    minute_raw: np.ndarray | None,
    tick_raw: np.ndarray | None,
    contract: str,
    tolerance_pct: float,
    min_minute_match: float,
    bars: str = "tick",
) -> SessionResult:
    """Bars and tick rows of one contract session, and its quality row (see the module rules).

    bars="minute" (the store's rule since 2026-10-07): the minute records build the bars whenever they hold volume;
    ticks only when they do not. The tick check still runs, and `reason` names a tick problem. bars="tick": the
    Phase 14 rule (valid ticks build the bars)."""
    if bars not in ("minute", "tick"):
        raise ValueError(f"bars must be 'minute' or 'tick', got {bars!r} (config/store.toml bars)")
    if minute_raw is None:
        minute_raw = np.empty(0, RECORD)
    minute = minute_record_bars(minute_raw, contract)
    minute_volume = int(minute_raw["volume"].sum(dtype=np.int64))
    q = {"minute_volume": minute_volume, "tick_volume": None, "diff_pct": None, "minute_match": None, "ohlc_match": None,
         "bar_source": "minute", "reason": "", "minute_bars_in_tick_session": 0}  # fmt: skip
    if tick_raw is None:
        q["reason"] = "no tick file"
        return SessionResult(minute, None, q | {"bars": len(minute)})
    ticks = tick_rows(tick_raw, contract)
    tb = tick_bars(ticks)
    tick_volume = int(tb["volume"].sum())
    vol_match, ohlc_match = _match(minute, tb)
    q |= {"tick_volume": tick_volume, "minute_match": round(vol_match, 6), "ohlc_match": round(ohlc_match, 6)}
    if minute_volume:
        q["diff_pct"] = round((tick_volume / minute_volume - 1) * 100, 4)
    use_ticks = False
    if tick_volume == 0:
        q["reason"] = "no trades in the tick file"
    elif minute_volume == 0:
        q["reason"], use_ticks = "no minute volume", True
    elif abs(q["diff_pct"]) > tolerance_pct:
        q["reason"] = f"tick volume {q['diff_pct']:+.1f}% vs minute"
    elif vol_match < min_minute_match:
        q["reason"] = f"only {vol_match:.1%} of minutes match the minute volume"
    else:
        use_ticks = True
    if bars == "minute" and minute_volume:
        return SessionResult(minute, ticks, q | {"bars": len(minute)})
    if use_ticks:
        keep = minute[~minute["ts_utc"].isin(tb["ts_utc"])]
        bars = pd.concat([tb, keep], ignore_index=True).sort_values("ts_utc", kind="stable").reset_index(drop=True)
        q |= {"bar_source": "tick", "minute_bars_in_tick_session": len(keep)}
        return SessionResult(bars, ticks, q | {"bars": len(bars)})
    return SessionResult(minute, ticks, q | {"bars": len(minute)})
