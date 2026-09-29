"""Import the binary per-contract minute store into one continuous Parquet series.

Source layout (decoded 2026-09-28 from Database/NQ-CME):
    <SYMBOL>-<EXCH>/<YYYYMM contract>/Minute/<YYYYMMDD session>.data
Each file is one Globex session (18:00 ET -> 16:59 ET next day), a flat array of 60-byte records:
    int64  .NET ticks, Eastern wall-clock time, bar OPEN time
    f8 x4  high, low, open, close        (note: not OHLC order)
    i4 x5  bid volume, ask volume, volume, volume (duplicate), unused
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

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


def read_session_file(path: Path) -> pd.DataFrame:
    """Read one session file into a DataFrame indexed by tz-aware bar CLOSE time (MC convention)."""
    raw = np.fromfile(path, dtype=RECORD)
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
            vol = int(np.fromfile(f, dtype=RECORD)["volume"].sum(dtype="int64"))
            records.append((f.stem, contract_dir.name, vol))
    df = pd.DataFrame(records, columns=["session", "contract", "volume"])
    return df.pivot(index="session", columns="contract", values="volume").fillna(0).sort_index()


def choose_contracts(volumes: pd.DataFrame) -> pd.Series:
    """Pick the contract per session: highest volume, never rolling back to an earlier contract."""
    chosen: dict[str, str] = {}
    current: str | None = None
    for session, row in volumes.iterrows():
        row = row[row > 0]
        if row.empty:
            continue
        best = row.idxmax()
        if current is None or best > current:  # contract names are YYYYMM, so string order == time order
            current = best
        if row.get(current, 0) == 0:  # current contract has no data this session: move to the best later one
            later = row[row.index > current]
            if later.empty:
                continue
            current = later.idxmax()
        chosen[session] = current
    return pd.Series(chosen, name="contract")


def import_store(source: Path, symbol: str, out_dir: Path) -> ImportSummary:
    """Build an unadjusted continuous 1-minute series from a per-contract store and write Parquet."""
    source = Path(source)
    if not source.is_dir():
        raise FileNotFoundError(f"Not a directory: {source}")
    volumes = _session_volumes(source)
    if volumes.empty:
        raise ValueError(f"No non-empty Minute/*.data files under {source}")
    contracts = choose_contracts(volumes)

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

    out = Path(out_dir) / symbol
    out.mkdir(parents=True, exist_ok=True)
    data.to_parquet(out / "1m.parquet")

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
