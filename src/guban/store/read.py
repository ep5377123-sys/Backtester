"""Read the shared market-data store in Guban's conventions (Phase 15).

The store (layout: D:\\Market Data\\README.md) keeps one Parquet file per contract session, with bar OPEN times in UTC.
Guban's view: New York time, bars labeled by their CLOSE time (open + 1 minute), unadjusted prices, one contract per
session. The contract per session is the store's volume roll (quality.csv `front`, the same rule as
guban.data.importer.choose_contracts; except that a contract is never traded in its expiration session) or, for MC parity, the expiry roll (choose_contracts_by_expiry on the store's
minute volumes; CME equity index contracts only).
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from guban.data.importer import ROLLS, ImportSummary, choose_contracts_by_expiry, missing_weekdays, rolls_of, third_friday
from guban.data.ticks import trade_date as ticks_trade_date
from guban.data.parquet import write_parquet
from guban.store.build import read_quality

TZ = "America/New_York"
BAR_COLUMNS = ["open", "high", "low", "close", "volume", "bid_vol", "ask_vol"]
EXPIRY_CALENDARS = ("cme_equity",)  # third-Friday expirations (guban.data.importer.third_friday)


def store_root(root: Path | None = None) -> Path:
    """`root`, or config/store.toml root."""
    if root is not None:
        return Path(root)
    from guban.store.build import load_store_config

    return Path(load_store_config().root)


def quality(root: Path, symbol: str) -> pd.DataFrame:
    path = Path(root) / symbol / "quality.csv"
    if not path.exists():
        raise FileNotFoundError(f"{symbol}: {path} not found; build the store first (`guban store build --symbol {symbol}`)")
    return read_quality(path)


def front_sessions(root: Path, symbol: str) -> pd.DataFrame:
    """The store's front contract per session (index: session file date YYYYMMDD, ascending): its quality.csv rows."""
    q = quality(root, symbol)
    return q[q["front"].astype(bool)].set_index("session").sort_index()


def minute_path(root: Path, symbol: str, contract: str, session: str) -> Path:
    return Path(root) / symbol / "minute" / contract / f"{session}.parquet"


def to_close_time(ts_utc: pd.Series) -> pd.DatetimeIndex:
    """The store's bar open times (UTC) as Guban's bar close labels in New York time."""
    return pd.DatetimeIndex(pd.to_datetime(ts_utc, utc=True) + pd.Timedelta(minutes=1)).tz_convert(TZ)


def read_minute(root: Path, symbol: str, contract: str, session: str) -> pd.DataFrame:
    """One contract session's 1-minute bars, indexed by close time (tz-aware New York), columns BAR_COLUMNS."""
    path = minute_path(root, symbol, contract, session)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; the store's quality.csv lists it (`guban store verify --symbol {symbol}`)")
    df = pd.read_parquet(path)
    out = df[BAR_COLUMNS].set_axis(to_close_time(df["ts_utc"]))
    out.index.name = "time"
    return out


def last_trade_date(contract: str, calendar: str | None, is_open=lambda d: True) -> date | None:
    """A YYYYMM contract's last trading day: the third Friday (CME equity index), or the last Friday of the month
    (CME Bitcoin) -- the business day before when the exchange is closed that Friday (`is_open(date)`, e.g. Christmas
    2020). None for a calendar without a rule. Published in advance, so a roll may use it without looking ahead."""
    if calendar == "cme_equity":
        return third_friday(contract).date()
    if calendar != "cme_crypto":
        return None
    end = pd.Timestamp(int(contract[:4]), int(contract[4:6]), 1) + pd.offsets.MonthEnd(0)
    day = (end - pd.Timedelta(days=(end.weekday() - 4) % 7)).date()
    while not is_open(day):
        day = (pd.Timestamp(day) - pd.offsets.BDay(1)).date()
    return day


def _off_expiry(contracts: pd.Series, q: pd.DataFrame, calendar: str | None) -> tuple[pd.Series, list[str]]:
    """The volume roll with one change: a contract is never traded in its expiration session (BTC stops at 11:00 ET
    that day; the store's roll, decided on the previous session's volume, can still name it). The next contract with
    bars in that session takes it. Returns (contracts, sessions switched)."""
    has_bars = q[q["bars"].fillna(0) > 0]
    by_session = has_bars.groupby("session")["contract"].apply(sorted).to_dict()
    open_days = set(pd.to_datetime(has_bars["trade_date"]).dt.date)
    last_day = max(open_days) if open_days else None
    out, switched = contracts.copy(), []
    for session, contract in contracts.items():
        expiry = last_trade_date(contract, calendar, lambda d: d in open_days or d > last_day)
        if expiry is None or ticks_trade_date(session) != expiry:
            continue
        later = [c for c in by_session.get(session, []) if c > contract]
        if later:
            out[session] = later[0]
            switched.append(session)
    return out, switched


def store_contracts(root: Path, symbol: str) -> tuple[pd.Series, pd.DataFrame]:
    """(contract per session by the volume roll as `guban import` uses it, the symbol's quality.csv)."""
    q = quality(root, symbol)
    return _contracts(root, symbol, "volume", 2)[0], q


def _contracts(root: Path, symbol: str, roll: str, days_before: int) -> tuple[pd.Series, list[str]]:
    if roll == "volume":
        front = front_sessions(root, symbol)
        contracts = front.loc[front["bars"].fillna(0) > 0, "contract"].rename("contract")
        return _off_expiry(contracts, quality(root, symbol), _calendar(symbol))
    q = quality(root, symbol)
    volumes = q.pivot_table(index="session", columns="contract", values="minute_volume", aggfunc="sum", fill_value=0).sort_index()
    return choose_contracts_by_expiry(volumes, days_before), []


def _calendar(symbol: str) -> str | None:
    from guban.config import get_contract

    try:
        return get_contract(symbol).calendar
    except KeyError:
        return None


def import_from_store(
    symbol: str, out_dir: Path, root: Path | None = None, roll: str = "volume", days_before: int = 2
) -> ImportSummary:
    """Build `out_dir/<symbol>/1m.parquet` (Guban's 1-minute layout, as guban.data.importer.import_store writes it)
    from the store's sessions of `symbol`. roll: "volume" (the store's roll calendar) or "expiry"."""
    if roll not in ROLLS:
        raise ValueError(f"roll must be one of {ROLLS}, got {roll!r}")
    if roll == "expiry" and _calendar(symbol) not in EXPIRY_CALENDARS:
        raise ValueError(f"the expiry roll is the CME equity index rule (third Friday of the contract month); "
                         f"{symbol} does not expire that way: use --roll volume")  # fmt: skip
    root = store_root(root)
    contracts, switched = _contracts(root, symbol, roll, days_before)
    if contracts.empty:
        raise ValueError(f"{symbol}: no sessions with bars in {root / symbol}")
    frames = []
    for session, contract in contracts.items():
        df = read_minute(root, symbol, contract, session)
        frames.append(df.assign(contract=contract, session=pd.Timestamp(session)))
    data = pd.concat(frames)
    dupes = int(data.index.duplicated(keep="last").sum())  # sessions never overlap; kept for safety, as import_store does
    if dupes:
        data = data[~data.index.duplicated(keep="last")]
    data = data.sort_index()
    data["contract"] = data["contract"].astype("category")
    write_parquet(data, Path(out_dir) / symbol / "1m.parquet")

    front = front_sessions(root, symbol)
    thin = front.index[front["thin"].astype(bool)]
    return ImportSummary(
        symbol=symbol,
        first_bar=data.index[0],
        last_bar=data.index[-1],
        bars=len(data),
        sessions=len(contracts),
        rolls=rolls_of(contracts),
        missing_weekdays=missing_weekdays(contracts.index),
        duplicate_bars_merged=dupes,
        thin_sessions=sorted(set(thin) & set(contracts.index)),
        expiry_switches=switched,
        source=f"{root / symbol} ({roll} roll)",
    )
