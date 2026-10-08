"""The trading days of a per-contract store: which contract each day trades, its RTH window, its minute bars.

The contract per day is the import's volume roll (guban.data.importer.choose_contracts: the contract with the most
volume in the previous session), read straight from the store, so a study does not depend on how 1m.parquet was
imported. `store` is one symbol's folder: a DeepCharts folder (Database/NQ-CME) or the shared market-data store
(D:\\Market Data\\NQ, Phase 15), where the roll is guban.store.read's (quality.csv `front`) and each day also carries
`tick_ok` -- the store's tick check; tick consumers keep only days where it is True (an empty check counts as False). A day whose scheduled early close is at or before the RTH open (Good Friday 09:15, 2025-01-09 09:30) has no
RTH and is left out.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from guban.config import get_contract, get_early_closes, get_session
from guban.data.importer import _session_volumes, choose_contracts, read_session_file
from guban.data.ticks import is_market_data, trade_date


def trading_days(store: Path, symbol: str, session: str = "rth") -> pd.DataFrame:
    """One row per trading day with a session in `session` (index: naive trading date, ascending).

    Columns: session_file (the Minute/Tick file stem, the session's 18:00 ET start date), contract, rth_start,
    rth_end (naive Eastern wall clock; rth_end is the template end or the earlier scheduled close), early_close.
    """
    sess = get_session(session)
    if sess.overnight:
        raise ValueError(f"trading_days needs a day session such as rth, got {session!r} ({sess.start}-{sess.end})")
    early = get_early_closes(get_contract(symbol).calendar)
    tick_ok: dict[str, bool] = {}
    if is_market_data(store):
        from guban.store.read import store_contracts

        contracts, q = store_contracts(Path(store).parent, Path(store).name)
        tick_ok = {(s, c): bool(ok) for s, c, ok in zip(q["session"], q["contract"], q["tick_ok"].fillna(False))}
    else:
        contracts = choose_contracts(_session_volumes(Path(store)))
    rows = []
    for session_file, contract in contracts.items():
        d = trade_date(session_file)
        end_t = min(early.get(d, sess.end), sess.end)
        if end_t <= sess.start:
            continue
        rows.append({
            "date": pd.Timestamp(d), "session_file": session_file, "contract": contract,
            "rth_start": pd.Timestamp.combine(d, sess.start), "rth_end": pd.Timestamp.combine(d, end_t),
            "early_close": end_t < sess.end,
        } | ({"tick_ok": tick_ok.get((session_file, contract), False)} if tick_ok or is_market_data(store) else {}))  # fmt: skip
    return pd.DataFrame(rows).set_index("date").sort_index()


def minute_bars(store: Path, day: pd.Series) -> pd.DataFrame:
    """The 1-minute bars of `day` (a trading_days row) inside its RTH window: bar close time in (rth_start, rth_end],
    indexed by naive Eastern close time. Empty when the minute file is missing or empty."""
    if is_market_data(store):
        from guban.store.read import minute_path, read_minute

        root, sym = Path(store).parent, Path(store).name
        if not minute_path(root, sym, day.contract, day.session_file).exists():
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        bars = read_minute(root, sym, day.contract, day.session_file)
    else:
        path = Path(store) / day.contract / "Minute" / f"{day.session_file}.data"
        if not path.exists() or path.stat().st_size == 0:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        bars = read_session_file(path)
    bars.index = bars.index.tz_localize(None)
    return bars[(bars.index > day.rth_start) & (bars.index <= day.rth_end)]
