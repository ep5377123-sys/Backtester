"""Import the binary per-contract minute store into one continuous Parquet series.

Source layout (decoded 2026-09-28 from Database/NQ-CME):
    <SYMBOL>-<EXCH>/<YYYYMM contract>/Minute/<YYYYMMDD session>.data
Each file is one Globex session (18:00 ET -> 16:59 ET next day), a flat array of 60-byte records:
    int64  .NET ticks, Eastern wall-clock time, bar OPEN time
    f8 x4  high, low, open, close        (note: not OHLC order)
    i4 x5  bid volume, ask volume, volume, volume (duplicate), unused

Every file is checked as it is read (a CorruptFileError names the file and the record): a whole number of
records, finite prices with low <= open, close <= high, non-negative volume, and bar times inside the session's
two calendar days. Records out of time order are sorted, not rejected (two real NQ files have one swapped minute).

Roll rules for the continuous series:
- "volume" (default): each session trades the contract that had the highest volume in the previous session
  (known at this session's open; the first session takes the front month), never rolling back. Rolls land one
  session after the next contract first out-trades the current one, around the Monday of expiration week.
- "expiry": hold each contract until `days_before` days before its expiration (3rd Friday of the contract
  month, CME equity index futures), then switch at that trading day's session open. days_before=2 (roll on
  the Wednesday of expiration week) reproduces MC's continuous contract in the Vault Break MNQ export.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from guban.data.parquet import write_parquet

TZ = "America/New_York"

RECORD = np.dtype(
    [
        ("ticks", "<i8"),
        ("high", "<f8"),
        ("low", "<f8"),
        ("open", "<f8"),
        ("close", "<f8"),
        ("bid_vol", "<i4"),
        ("ask_vol", "<i4"),
        ("volume", "<i4"),
        ("volume_dup", "<i4"),
        ("unused", "<i4"),
    ]
)
assert RECORD.itemsize == 60

# .NET ticks (100 ns since 0001-01-01) -> Unix epoch nanoseconds
_DOTNET_EPOCH_TICKS = 621_355_968_000_000_000


class CorruptFileError(ValueError):
    """A .data file is truncated or holds impossible records."""


def read_records(path: Path) -> np.ndarray:
    """A session file's records, after checking them (see the module docstring)."""
    path = Path(path)
    size = path.stat().st_size
    if size % RECORD.itemsize:
        raise CorruptFileError(
            f"{path}: {size:,} bytes is not a whole number of {RECORD.itemsize}-byte records "
            f"({size % RECORD.itemsize} stray bytes after record {size // RECORD.itemsize}); the file is truncated or not a minute file"
        )
    raw = np.fromfile(path, dtype=RECORD)
    if raw.size:
        _check_records(raw, path)
    return raw


def _check_records(raw: np.ndarray, path: Path) -> None:
    o, h, l, c = (raw[k].astype(float) for k in ("open", "high", "low", "close"))  # noqa: E741
    eps = 1e-9
    try:
        day = pd.Timestamp(path.stem)
    except ValueError:
        day = None
    ns = (raw["ticks"] - _DOTNET_EPOCH_TICKS) * 100
    checks = [
        (~(np.isfinite(o) & np.isfinite(h) & np.isfinite(l) & np.isfinite(c)), "a missing (NaN) or infinite price"),
        ((l > h + eps) | (o < l - eps) | (o > h + eps) | (c < l - eps) | (c > h + eps), "open/close outside low..high (or low > high)"),
        ((raw["volume"] < 0) | (raw["bid_vol"] < 0) | (raw["ask_vol"] < 0), "a negative volume"),
    ]
    if day is not None:  # a session starts at 18:00 on its file date and ends the next afternoon
        lo, hi = day.value, (day + pd.Timedelta(days=2)).value
        checks.append(((ns < lo) | (ns >= hi), f"a time outside {day:%Y-%m-%d} .. +2 days"))
    for bad, why in checks:
        if bad.any():
            k = int(np.flatnonzero(bad)[0])
            t = pd.Timestamp(int(ns[k]))
            raise CorruptFileError(
                f"{path}: {int(bad.sum())} record(s) have {why}; first is record {k} (byte offset {k * RECORD.itemsize}): "
                f"time={t} open={o[k]!r} high={h[k]!r} low={l[k]!r} close={c[k]!r} volume={int(raw['volume'][k])}"
            )


def read_session_file(path: Path) -> pd.DataFrame:
    """Read one session file into a DataFrame indexed by tz-aware bar CLOSE time (MC convention)."""
    raw = read_records(path)
    if raw.size == 0:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "bid_vol", "ask_vol"])
    open_time = pd.to_datetime((raw["ticks"] - _DOTNET_EPOCH_TICKS) * 100, unit="ns")
    df = pd.DataFrame(
        {
            "open": raw["open"],
            "high": raw["high"],
            "low": raw["low"],
            "close": raw["close"],
            "volume": raw["volume"].astype("int64"),
            "bid_vol": raw["bid_vol"].astype("int64"),
            "ask_vol": raw["ask_vol"].astype("int64"),
        },
        index=open_time + pd.Timedelta(minutes=1),
    )
    # Several records can share a timestamp (opening prints at 18:00): merge them into one bar.
    df = df.groupby(level=0, sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
        bid_vol=("bid_vol", "sum"),
        ask_vol=("ask_vol", "sum"),
    )
    df.index = df.index.tz_localize(TZ, ambiguous="raise", nonexistent="raise")
    df.index.name = "time"
    return df


@dataclass
class ImportSummary:
    symbol: str
    first_bar: pd.Timestamp
    last_bar: pd.Timestamp
    bars: int
    sessions: int
    rolls: list[tuple[str, str]]  # (session date, contract rolled into)
    missing_weekdays: list[str]
    duplicate_bars_merged: int

    def __str__(self) -> str:
        lines = [
            f"{self.symbol}: {self.bars:,} one-minute bars in {self.sessions} sessions",
            f"  first bar: {self.first_bar}   last bar: {self.last_bar}",
            f"  duplicate records merged: {self.duplicate_bars_merged:,}",
            f"  contract rolls ({len(self.rolls)}): " + ", ".join(f"{d}->{c}" for d, c in self.rolls),
            f"  weekday sessions with no data ({len(self.missing_weekdays)}): "
            + (", ".join(self.missing_weekdays[:20]) + (" ..." if len(self.missing_weekdays) > 20 else "")),
        ]
        return "\n".join(lines)


def _session_volumes(source: Path) -> pd.DataFrame:
    """Volume per (session date, contract), from file contents. Rows: session date; columns: contract."""
    records = []
    for contract_dir in sorted(p for p in source.iterdir() if p.is_dir()):
        for f in sorted((contract_dir / "Minute").glob("*.data")):
            if f.stat().st_size == 0:
                continue
            vol = int(read_records(f)["volume"].sum(dtype="int64"))
            records.append((f.stem, contract_dir.name, vol))
    df = pd.DataFrame(records, columns=["session", "contract", "volume"])
    return df.pivot(index="session", columns="contract", values="volume").fillna(0).sort_index()


def choose_contracts(volumes: pd.DataFrame) -> pd.Series:
    """Pick the contract per session from what is known at its open: the contract with the highest volume in the
    previous session with data (the front month for the first one), never rolling back to an earlier contract.
    A session's own volume is not used: it is not known until the session ends."""
    chosen: dict[str, str] = {}
    current: str | None = None
    prev: pd.Series | None = None  # volumes of the previous session with data
    for session, row in volumes.iterrows():
        row = row[row > 0]
        if row.empty:
            continue
        if current is None:
            current = min(row.index)  # front month; contract names are YYYYMM, so string order == time order
        elif prev is not None and prev.idxmax() > current:
            current = prev.idxmax()
        if current not in row.index:  # current contract has no data this session: move to the next listed one
            later = sorted(c for c in row.index if c > current)
            if not later:
                prev = row
                continue
            current = later[0]
        chosen[session] = current
        prev = row
    return pd.Series(chosen, name="contract")


def third_friday(contract: str) -> pd.Timestamp:
    """Expiration date of a YYYYMM equity index contract: the third Friday of its month."""
    first = pd.Timestamp(int(contract[:4]), int(contract[4:6]), 1)
    return first + pd.Timedelta(days=(4 - first.weekday()) % 7 + 14)


def choose_contracts_by_expiry(volumes: pd.DataFrame, days_before: int = 2) -> pd.Series:
    """Pick the contract per session by calendar: the earliest contract whose roll day (expiration minus
    `days_before` days) is after the session's trading day, never rolling back. A session file is named by
    its 18:00 ET start, so its trading day is the next calendar day."""
    chosen: dict[str, str] = {}
    current: str | None = None
    for session, row in volumes.iterrows():
        row = row[row > 0]
        if row.empty:
            continue
        trading_day = pd.Timestamp(session) + pd.Timedelta(days=1)
        live = [c for c in sorted(row.index) if trading_day < third_friday(c) - pd.Timedelta(days=days_before)]
        if not live:
            continue
        best = live[0]
        if current is None or best > current:
            current = best
        if row.get(current, 0) == 0:
            later = [c for c in live if c > current]
            if not later:
                continue
            current = later[0]
        chosen[session] = current
    return pd.Series(chosen, name="contract")


ROLLS = ("volume", "expiry")


def import_store(source: Path, symbol: str, out_dir: Path, roll: str = "volume", days_before: int = 2) -> ImportSummary:
    """Build an unadjusted continuous 1-minute series from a per-contract store and write Parquet.

    roll: "volume" or "expiry" (see the module docstring); days_before applies to "expiry".
    """
    source = Path(source)
    if not source.is_dir():
        raise FileNotFoundError(f"Not a directory: {source}")
    if roll not in ROLLS:
        raise ValueError(f"roll must be one of {ROLLS}, got {roll!r}")
    volumes = _session_volumes(source)
    if volumes.empty:
        raise ValueError(f"No non-empty Minute/*.data files under {source}")
    contracts = choose_contracts(volumes) if roll == "volume" else choose_contracts_by_expiry(volumes, days_before)

    frames = []
    raw_count = 0
    for session, contract in contracts.items():
        path = source / contract / "Minute" / f"{session}.data"
        raw_count += path.stat().st_size // RECORD.itemsize
        df = read_session_file(path)
        df["contract"] = contract
        df["session"] = pd.Timestamp(session)
        frames.append(df)
    data = pd.concat(frames)
    if not data.index.is_unique:
        data = data[~data.index.duplicated(keep="last")]
    data = data.sort_index()
    data["contract"] = data["contract"].astype("category")

    write_parquet(data, Path(out_dir) / symbol / "1m.parquet")  # atomic: never a partial file under the final name

    rolls = [(s, c) for s, c, prev in zip(contracts.index, contracts, contracts.shift()) if c != prev]
    # Sessions are named by their 18:00 start date; Sun..Thu starts cover Mon..Fri trading days.
    starts = pd.to_datetime(contracts.index)
    expected = pd.bdate_range(starts.min(), starts.max()) - pd.Timedelta(days=1)  # Mon..Fri -> Sun..Thu
    expected = expected[(expected >= starts.min()) & (expected <= starts.max())]
    missing = sorted(set(expected.strftime("%Y%m%d")) - set(contracts.index))
    return ImportSummary(
        symbol=symbol,
        first_bar=data.index[0],
        last_bar=data.index[-1],
        bars=len(data),
        sessions=len(contracts),
        rolls=rolls,
        missing_weekdays=missing,
        duplicate_bars_merged=raw_count - len(data),
    )


def load_bars(symbol: str, data_dir: Path = Path("data")) -> pd.DataFrame:
    return pd.read_parquet(Path(data_dir) / symbol / "1m.parquet")
