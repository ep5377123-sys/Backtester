"""Big-trade bubbles from the market-data store's ticks, as per-bar columns for strategies (Phase 18).

A bubble is one aggressive order as DeepCharts' Big Trades shows it (user, 2026-10-23): the tick records of one side
that share a millisecond timestamp, merged -- size = their summed size, price = their size-weighted average price.
Buy bubbles come from buy-aggressor records (`side` = +1, at the ask), sell bubbles from sell-aggressor records
(`side` = -1, at the bid); records between the quotes (0) and size-0 records (2026-05-26+ feed) add nothing.
A bubble belongs to the 1-minute bar labeled floor(time, 1 min) + 1 min (bars are labeled by close time).

Columns (guban.data.features adds them to the bars a strategy declares in `extra_columns`):
    big_buy_body   per 1-minute bar: the size of the largest buy bubble priced inside the bar's body
                   (min(open, close) <= price <= max(open, close)); 0 when there is none; NaN when the bar's
                   contract session has no usable ticks (no tick file, or quality.csv `tick_ok` not True).
    big_sell_body  the same for sell bubbles.
Bubbles under FLOOR contracts are not kept, so a column reads 0 for a body whose biggest bubble is smaller.

The cache (`data/<SYM>/big_trades.parquet`) holds every bubble >= FLOOR of the sessions read so far; its
fingerprint lists each tick file read (size, mtime). Store files are write-once, so only new sessions are read
when the bars grow; a changed file is read again.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from guban.data.parquet import read_fingerprint, write_parquet

TZ = "America/New_York"
FLOOR = 10  # smallest bubble kept in the cache (contracts)
CACHE_VERSION = 2  # 2: sell bubbles too (side column)
COLUMNS = ["session", "contract", "side", "time", "price", "size"]
SIDES = {"big_buy_body": 1, "big_sell_body": -1}


def bubbles_of(ticks: pd.DataFrame, floor: int = FLOOR) -> pd.DataFrame:
    """Bubbles of one tick file (columns ts_utc, price, size, side): side (+1 buy, -1 sell), time (bar close label,
    New York), price (size-weighted), size; only bubbles of at least `floor` contracts, by side then time."""
    t = ticks[(ticks["side"] != 0) & (ticks["size"] > 0)]
    size = t["size"].to_numpy(np.int64)
    g = pd.DataFrame({"side": t["side"].to_numpy(np.int8), "ts": t["ts_utc"].to_numpy(), "size": size,
                      "notional": t["price"].to_numpy(float) * size})  # fmt: skip
    g = g.groupby(["side", "ts"], sort=True).sum().reset_index()
    g = g[g["size"] >= floor]
    time = pd.DatetimeIndex(g["ts"])
    time = time.tz_localize("UTC") if time.tz is None else time
    label = time.floor("min") + pd.Timedelta(minutes=1)
    return pd.DataFrame({"side": g["side"].to_numpy(np.int8), "time": label.tz_convert(TZ),
                         "price": g["notional"].to_numpy() / g["size"].to_numpy(), "size": g["size"].to_numpy(np.int64)})  # fmt: skip


def usable_sessions(bars: pd.DataFrame, quality: pd.DataFrame) -> pd.DataFrame:
    """The (session file, contract) of each trading date the bars hold, with tick_ok from the store's quality.csv."""
    pairs = pd.DataFrame({"trade_date": pd.to_datetime(bars["session_date"]).dt.strftime("%Y%m%d"),
                          "contract": bars["contract"].astype(str)}).drop_duplicates()  # fmt: skip
    q = quality.assign(trade_date=quality["trade_date"].astype(str).str.replace("-", ""), contract=quality["contract"].astype(str))
    out = pairs.merge(q[["trade_date", "contract", "session", "tick_ok"]], on=["trade_date", "contract"], how="left")
    out["tick_ok"] = out["tick_ok"].fillna(False).astype(bool)
    return out


def load_bubbles(symbol: str, sessions: pd.DataFrame, root: Path, data_dir: Path) -> pd.DataFrame:
    """Bubbles (COLUMNS) of the tick_ok `sessions` (usable_sessions rows), from the cache where it holds them."""
    tick_dir = Path(root) / symbol / "tick"
    files = {}
    for s in sessions[sessions["tick_ok"]].itertuples():
        path = tick_dir / s.contract / f"{s.session}.parquet"
        if path.exists():
            st = path.stat()
            files[f"{s.contract}/{s.session}"] = [st.st_size, st.st_mtime_ns]
    cache = Path(data_dir) / symbol / "big_trades.parquet"
    fp = read_fingerprint(cache)
    cached, have = pd.DataFrame(columns=COLUMNS), {}
    if fp and fp.get("version") == CACHE_VERSION and fp.get("floor") == FLOOR:
        try:
            cached, have = pd.read_parquet(cache), fp["files"]
        except (OSError, ValueError):
            have = {}
    keep = {k for k, v in files.items() if have.get(k) == v}
    todo = sorted(set(files) - keep)
    if keep == set(have):
        frames = [cached] if keep else []
    else:
        frames = [cached[(cached["contract"].astype(str) + "/" + cached["session"].astype(str)).isin(keep)]] if keep else []
    for key in todo:
        contract, session = key.split("/")
        ticks = pd.read_parquet(tick_dir / contract / f"{session}.parquet", columns=["ts_utc", "price", "size", "side"])
        frames.append(bubbles_of(ticks).assign(session=session, contract=contract)[COLUMNS])
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    if todo or set(have) != set(keep):
        write_parquet(out, cache, {"version": CACHE_VERSION, "floor": FLOOR, "files": {k: files[k] for k in sorted(files)}}, index=False)
    return out


def bubble_columns(bars: pd.DataFrame, symbol: str, root: Path | None = None, data_dir: Path = Path("data")) -> pd.DataFrame:
    """big_buy_body and big_sell_body for 1-minute `bars` (load_bars output, any chart time zone), from the store's ticks."""
    from guban.store.read import quality, store_root

    root = store_root(root)
    sessions = usable_sessions(bars, quality(root, symbol))
    bubbles = load_bubbles(symbol, sessions, root, data_dir)
    ok = sessions.loc[sessions["tick_ok"], ["trade_date", "contract"]]
    side = bubbles["side"].to_numpy()
    return pd.DataFrame({name: body_bubbles(bars, bubbles[side == s], ok) for name, s in SIDES.items()}, index=bars.index)


def body_bubbles(bars: pd.DataFrame, bubbles: pd.DataFrame, ok: pd.DataFrame) -> pd.Series:
    """Per bar, the largest of `bubbles` (contract, time, price, size) at the bar's close label and contract priced
    inside its body; 0 without one; NaN for bars whose (trade_date YYYYMMDD, contract) is not in `ok`."""
    key = pd.DataFrame({"trade_date": pd.to_datetime(bars["session_date"]).dt.strftime("%Y%m%d").to_numpy(),
                        "contract": bars["contract"].astype(str).to_numpy()})  # fmt: skip
    usable = key.merge(ok.assign(_ok=True), on=["trade_date", "contract"], how="left")["_ok"].fillna(False).to_numpy(bool)
    vals = np.where(usable, 0.0, np.nan)
    if not bubbles.empty:
        o, c = bars["open"].to_numpy(float), bars["close"].to_numpy(float)
        lo, hi = np.minimum(o, c), np.maximum(o, c)
        pos = pd.Series(np.arange(len(bars)), index=bars.index.tz_convert(TZ))
        at = pos.reindex(pd.DatetimeIndex(bubbles["time"]).tz_convert(TZ)).to_numpy()
        hit = ~np.isnan(at)
        i = at[hit].astype(np.int64)
        price = bubbles["price"].to_numpy(float)[hit]
        same = (bubbles["contract"].astype(str).to_numpy()[hit] == key["contract"].to_numpy()[i]) & usable[i]
        inside = same & (price >= lo[i]) & (price <= hi[i])
        if inside.any():
            best = pd.Series(bubbles["size"].to_numpy(float)[hit][inside]).groupby(i[inside]).max()
            vals[best.index.to_numpy()] = best.to_numpy()
    return pd.Series(vals, index=bars.index)


def describe(bars: pd.DataFrame, column: pd.Series) -> str:
    """One line on how many trading dates have usable ticks."""
    days = pd.to_datetime(bars["session_date"])
    ok = column.notna().groupby(days.to_numpy()).any()
    return f"bubbles: {int(ok.sum())} of {len(ok)} trading dates have usable ticks ({len(ok) - int(ok.sum())} skipped)"
