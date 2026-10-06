"""Read the binary per-contract tick store and build RTH volume-at-price per trading day (Phase 13).

Source layout (decoded 2026-10-05 from Database/NQ-CME):
    <SYMBOL>-<EXCH>/<YYYYMM contract>/Tick/<YYYYMMDD session>.data
Each file is one Globex session (18:00 ET on its file date -> ~16:59 ET the next day), a flat array of 57-byte trades:
    int64  .NET ticks, Eastern wall-clock trade time (same clock as the Minute files)
    f8 x3  trade price, bid, ask
    i4 x6  size, size (duplicate), cumulative session volume, 0, 0, 0
    u1     flag (1)
From 2026-05-26 11:23 ET the feed changed: the last two i4 fields carry data, the flag is mostly 0, and records with
size 0 appear (5-25% of a session). The summed `size` still matches the minute volume within ~1%, so size is the
volume and a size-0 record adds none; the cumulative-volume field no longer tracks size and is not used.

Known defects, found by comparing each session with the same contract's Minute file: some sessions stop at 09:29:59
(no RTH), a few hold every trade three times, and some (Jan 2024) carry ~1.2x the minute volume. Most sessions match
the minute volume exactly. The reader does not repair these; callers keep a day only when its RTH tick volume agrees
with the minute volume (guban.research.days).

The cache (`data/<SYM>/vap_rth_tick.parquet` + `rth_tick_days.parquet`) is fingerprinted by the tick files read
(size, mtime), the RTH session template, the early-close calendar and CACHE_VERSION.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from guban.data.importer import CorruptFileError
from guban.data.parquet import read_fingerprint, write_parquet

TICK_RECORD = np.dtype(
    [
        ("ticks", "<i8"),
        ("price", "<f8"),
        ("bid", "<f8"),
        ("ask", "<f8"),
        ("size", "<i4"),
        ("size_dup", "<i4"),
        ("cum_volume", "<i4"),
        ("unused1", "<i4"),
        ("unused2", "<i4"),
        ("unused3", "<i4"),
        ("flag", "u1"),
    ]
)
assert TICK_RECORD.itemsize == 57

# .NET ticks (100 ns since 0001-01-01) -> Unix epoch nanoseconds
_DOTNET_EPOCH_TICKS = 621_355_968_000_000_000
# Bump when the cached columns or the RTH aggregation change: the tick cache is rebuilt.
CACHE_VERSION = 1


def dotnet_ticks(wall: pd.Timestamp) -> int:
    """A naive Eastern wall-clock time as .NET ticks (the files' time unit)."""
    return pd.Timestamp(wall).as_unit("ns").value // 100 + _DOTNET_EPOCH_TICKS


def tick_times(ticks: np.ndarray) -> pd.DatetimeIndex:
    """.NET ticks -> naive Eastern wall-clock times."""
    return pd.to_datetime((np.asarray(ticks, dtype=np.int64) - _DOTNET_EPOCH_TICKS) * 100, unit="ns")


def read_ticks(path: Path) -> np.ndarray:
    """A session file's trades, after checking them: a whole number of records, finite prices, size >= 0, and trade
    times inside the session's two calendar days. Raises CorruptFileError naming the file and the first bad record."""
    path = Path(path)
    size = path.stat().st_size
    if size % TICK_RECORD.itemsize:
        raise CorruptFileError(
            f"{path}: {size:,} bytes is not a whole number of {TICK_RECORD.itemsize}-byte records "
            f"({size % TICK_RECORD.itemsize} stray bytes after record {size // TICK_RECORD.itemsize}); the file is truncated or not a tick file"
        )
    raw = np.fromfile(path, dtype=TICK_RECORD)
    if raw.size:
        _check(raw, path)
    return raw


def _check(raw: np.ndarray, path: Path) -> None:
    checks = [
        (~np.isfinite(raw["price"]) | (raw["price"] <= 0), "a missing, infinite or non-positive price"),
        (raw["size"] < 0, "a negative size"),
    ]
    try:
        day = pd.Timestamp(path.stem)
    except ValueError:
        day = None
    if day is not None:
        lo, hi = dotnet_ticks(day), dotnet_ticks(day + pd.Timedelta(days=2))
        checks.append(((raw["ticks"] < lo) | (raw["ticks"] >= hi), f"a time outside {day:%Y-%m-%d} .. +2 days"))
    for bad, why in checks:
        if bad.any():
            k = int(np.flatnonzero(bad)[0])
            r = raw[k]
            raise CorruptFileError(
                f"{path}: {int(bad.sum())} record(s) have {why}; first is record {k} (byte offset {k * TICK_RECORD.itemsize}): "
                f"time={tick_times([r['ticks']])[0]} price={float(r['price'])!r} size={int(r['size'])}"
            )


def window_trades(raw: np.ndarray, start: pd.Timestamp, end: pd.Timestamp) -> np.ndarray:
    """Trades with start <= time < end (naive Eastern wall clock), in time order."""
    t = raw["ticks"]
    out = raw[(t >= dotnet_ticks(start)) & (t < dotnet_ticks(end))]
    if out.size > 1 and (np.diff(out["ticks"]) < 0).any():
        out = out[np.argsort(out["ticks"], kind="stable")]
    return out


def volume_at_price(prices: np.ndarray, sizes: np.ndarray, tick_size: float) -> tuple[int, np.ndarray]:
    """(lowest price in ticks, traded volume per tick from there up). Prices are snapped to the tick grid."""
    idx = np.rint(np.asarray(prices, float) / tick_size).astype(np.int64)
    base = int(idx.min())
    return base, np.bincount(idx - base, weights=np.asarray(sizes, np.int64)).astype(np.int64)


DAY_COLUMNS = ["contract", "session_file", "trades", "volume", "first_trade", "last_trade", "open", "high", "low", "close"]


def build_rth_profiles(
    store: Path, days: pd.DataFrame, tick_size: float, progress=None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """RTH trades of each day in `days` (index: trade date; columns session_file, contract, rth_start, rth_end), read
    from `store/<contract>/Tick/<session_file>.data`.

    Returns (summary, vap). summary: one row per day with a tick file (DAY_COLUMNS; trades = 0 when the file has no
    RTH trades). vap: long table (session_date, price_tick, volume) of the days with RTH trades.
    """
    rows, vaps = [], []
    for n, (day, d) in enumerate(days.iterrows()):
        path = Path(store) / d.contract / "Tick" / f"{d.session_file}.data"
        if not path.exists():
            continue
        raw = read_ticks(path) if path.stat().st_size else np.empty(0, TICK_RECORD)
        tr = window_trades(raw, d.rth_start, d.rth_end) if raw.size else raw
        row = {"session_date": day, "contract": d.contract, "session_file": d.session_file, "trades": int(tr.size),
               "volume": int(tr["size"].sum(dtype=np.int64)) if tr.size else 0}  # fmt: skip
        if tr.size:
            times = tick_times(tr["ticks"][[0, -1]])
            p = tr["price"]
            row |= {"first_trade": times[0], "last_trade": times[1], "open": p[0], "high": p.max(), "low": p.min(), "close": p[-1]}
            base, vol = volume_at_price(p, tr["size"], tick_size)
            nz = np.flatnonzero(vol)
            vaps.append(pd.DataFrame({"session_date": day, "price_tick": (base + nz).astype(np.int32), "volume": vol[nz]}))
        rows.append(row)
        if progress:
            progress(n + 1, len(days))
    summary = pd.DataFrame(rows, columns=["session_date", *DAY_COLUMNS]).set_index("session_date")
    vap = pd.concat(vaps, ignore_index=True) if vaps else pd.DataFrame(columns=["session_date", "price_tick", "volume"])
    return summary, vap


def _fingerprint(store: Path, days: pd.DataFrame, tick_size: float) -> dict:
    files = []
    for day, d in days.iterrows():
        path = Path(store) / d.contract / "Tick" / f"{d.session_file}.data"
        if path.exists():
            st = path.stat()
            files.append([str(day.date()), d.contract, d.session_file, st.st_size, st.st_mtime_ns, str(d.rth_start), str(d.rth_end)])
    digest = hashlib.sha256(json.dumps(files).encode()).hexdigest()
    return {"version": CACHE_VERSION, "tick_size": tick_size, "files": len(files), "digest": digest}


def load_rth_profiles(
    store: Path, days: pd.DataFrame, tick_size: float, cache_dir: Path, rebuild: bool = False, progress=None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """build_rth_profiles, from `cache_dir/{rth_tick_days,vap_rth_tick}.parquet` when the cache is current."""
    fp = _fingerprint(store, days, tick_size)
    days_path, vap_path = Path(cache_dir) / "rth_tick_days.parquet", Path(cache_dir) / "vap_rth_tick.parquet"
    if not rebuild and read_fingerprint(days_path) == fp and read_fingerprint(vap_path) == fp:
        try:
            return pd.read_parquet(days_path), pd.read_parquet(vap_path)
        except (OSError, ValueError):
            pass
    summary, vap = build_rth_profiles(store, days, tick_size, progress)
    write_parquet(vap, vap_path, fp)
    write_parquet(summary, days_path, fp)
    return summary, vap


def trade_date(session_file: str) -> date:
    """A session file is named by its 18:00 ET start; its trading date is the next calendar day."""
    return (pd.Timestamp(session_file) + pd.Timedelta(days=1)).date()
