import os
from datetime import time

import numpy as np
import pandas as pd
import pytest

import guban.data.resample as rs
from guban.config import Session
from guban.data.resample import cache_path, load_bars, parse_timeframe, resample

TZ = "America/New_York"


def one_minute(start: str, end: str, seed: int = 0) -> pd.DataFrame:
    """Synthetic 1-min bars labeled by close time from `start` to `end` inclusive (ET wall clock)."""
    idx = pd.date_range(start, end, freq="1min", tz=TZ)
    rng = np.random.default_rng(seed)
    close = 15000 + np.cumsum(rng.integers(-8, 9, len(idx))) * 0.25
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + rng.integers(0, 5, len(idx)) * 0.25
    low = np.minimum(open_, close) - rng.integers(0, 5, len(idx)) * 0.25
    vol = rng.integers(1, 100, len(idx))
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol, "bid_vol": vol // 2, "ask_vol": vol - vol // 2},
        index=pd.DatetimeIndex(idx, name="time"),
    )


def hand_bar(m: pd.DataFrame, first: str, last: str) -> dict:
    """Aggregate the 1-min bars with close times first..last (inclusive) by hand."""
    x = m.loc[first:last]
    return dict(open=x.open.iloc[0], high=x.high.max(), low=x.low.min(), close=x.close.iloc[-1], volume=x.volume.sum())


def bar(df: pd.DataFrame, i: int) -> dict:
    r = df.iloc[i]
    return dict(open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume)


def test_parse_timeframe():
    assert parse_timeframe("5m") == 5
    assert parse_timeframe("1h") == 60
    assert parse_timeframe("15min") == 15
    assert parse_timeframe("1d") is None
    with pytest.raises(ValueError):
        parse_timeframe("0m")
    with pytest.raises(ValueError):
        parse_timeframe("5s")


def test_rth_5min_matches_hand_built_bars():
    m = one_minute("2024-03-05 09:00", "2024-03-05 16:30")  # includes pre- and post-RTH bars
    b = resample(m, "5m", "rth")
    assert len(b) == 78
    assert b.index[0] == pd.Timestamp("2024-03-05 09:35", tz=TZ)
    assert b.index[-1] == pd.Timestamp("2024-03-05 16:00", tz=TZ)
    assert bar(b, 0) == hand_bar(m, "2024-03-05 09:31", "2024-03-05 09:35")
    assert bar(b, 1) == hand_bar(m, "2024-03-05 09:36", "2024-03-05 09:40")
    assert bar(b, -1) == hand_bar(m, "2024-03-05 15:56", "2024-03-05 16:00")
    assert (b.session_date == pd.Timestamp("2024-03-05")).all()
    # every in-session 1-min bar is used exactly once
    assert b.volume.sum() == m.between_time("09:31", "16:00").volume.sum()


@pytest.mark.parametrize(
    "before, after",
    [("2024-03-08", "2024-03-11"), ("2024-11-01", "2024-11-04")],  # spring-forward and fall-back weekends
    ids=["spring", "fall"],
)
def test_dst_week_keeps_rth_open_at_0930(before, after):
    m = pd.concat([one_minute(f"{d} 09:00", f"{d} 16:30", seed=i) for i, d in enumerate([before, after])])
    b = resample(m, "5m", "rth")
    for d in (before, after):
        day = b[b.session_date == pd.Timestamp(d)]
        assert len(day) == 78
        assert day.index[0].strftime("%H:%M") == "09:35"
        assert day.index[-1].strftime("%H:%M") == "16:00"
        assert bar(day, 0) == hand_bar(m, f"{d} 09:31", f"{d} 09:35")
    offsets = {b[b.session_date == pd.Timestamp(d)].index[0].utcoffset() for d in (before, after)}
    assert len(offsets) == 2  # the UTC offset changed across the weekend, the wall-clock open did not


def test_eth_overnight_session_belongs_to_next_trading_day():
    m = one_minute("2024-03-10 18:01", "2024-03-11 17:00")  # Sunday evening (after DST switch) -> Monday
    b = resample(m, "60m", "eth")
    assert len(b) == 23
    assert b.index[0] == pd.Timestamp("2024-03-10 19:00", tz=TZ)
    assert b.index[-1] == pd.Timestamp("2024-03-11 17:00", tz=TZ)
    assert (b.session_date == pd.Timestamp("2024-03-11")).all()
    assert bar(b, 0) == hand_bar(m, "2024-03-10 18:01", "2024-03-10 19:00")
    assert bar(b, 6) == hand_bar(m, "2024-03-11 00:01", "2024-03-11 01:00")  # bin that crosses midnight


def test_gaps_are_not_filled():
    m = one_minute("2024-03-05 09:31", "2024-03-05 10:00")
    m = m.drop(m.loc["2024-03-05 09:36":"2024-03-05 09:40"].index)  # a whole 5-min bin missing
    m = m.drop(m.loc["2024-03-05 09:43":"2024-03-05 09:44"].index)  # a partially missing bin
    b = resample(m, "5m", "rth")
    assert list(b.index.strftime("%H:%M")) == ["09:35", "09:45", "09:50", "09:55", "10:00"]
    assert bar(b, 1) == hand_bar(m, "2024-03-05 09:41", "2024-03-05 09:45")


def test_daily_bars_one_per_session_labeled_at_close():
    m = pd.concat([one_minute(f"{d} 09:00", f"{d} 16:30", seed=i) for i, d in enumerate(["2024-03-05", "2024-03-06"])])
    d = resample(m, "1d", "rth")
    assert list(d.index) == [pd.Timestamp("2024-03-05 16:00", tz=TZ), pd.Timestamp("2024-03-06 16:00", tz=TZ)]
    assert bar(d, 0) == hand_bar(m, "2024-03-05 09:31", "2024-03-05 16:00")
    assert bar(d, 1) == hand_bar(m, "2024-03-06 09:31", "2024-03-06 16:00")


def test_timeframe_not_dividing_session_fails_loudly():
    m = one_minute("2024-03-05 09:31", "2024-03-05 16:00")
    with pytest.raises(ValueError, match="does not divide"):
        resample(m, "7m", "rth")
    b = resample(m, "7m", "rth", allow_partial=True)
    assert b.index[-1] == pd.Timestamp("2024-03-05 16:00", tz=TZ)  # 390 = 55*7 + 5: last bar is short
    assert bar(b, -1) == hand_bar(m, "2024-03-05 15:56", "2024-03-05 16:00")


def test_contract_column_is_carried():
    m = one_minute("2024-03-05 09:31", "2024-03-05 09:40")
    m["contract"] = pd.Categorical(["202403"] * 7 + ["202406"] * 3)
    b = resample(m, "5m", "rth")
    assert list(b.contract.astype(str)) == ["202403", "202406"]


def test_load_bars_caches_and_rebuilds_when_source_changes(tmp_path):
    src = tmp_path / "NQ" / "1m.parquet"
    src.parent.mkdir()
    one_minute("2024-03-05 09:00", "2024-03-05 16:30").to_parquet(src)
    first = load_bars("NQ", "5m", "rth", tmp_path)
    cached = cache_path("NQ", "5m", "rth", tmp_path)
    assert cached.name == "5m_rth.parquet" and cached.exists()
    pd.testing.assert_frame_equal(load_bars("NQ", "5m", "rth", tmp_path), first)

    one_minute("2024-03-06 09:00", "2024-03-06 16:30").to_parquet(src)
    later = cached.stat().st_mtime + 10
    os.utime(src, (later, later))
    rebuilt = load_bars("NQ", "5m", "rth", tmp_path)
    assert (rebuilt.session_date == pd.Timestamp("2024-03-06")).all()


def _source(tmp_path, start="2024-03-05 09:00", end="2024-03-05 16:30"):
    src = tmp_path / "NQ" / "1m.parquet"
    src.parent.mkdir(exist_ok=True)
    one_minute(start, end).to_parquet(src)
    return src


def test_cache_rebuilds_when_the_session_definition_changes(tmp_path, monkeypatch):
    # FIX.md #8: editing config/sessions.toml used to leave the old bars in the cache
    _source(tmp_path)
    assert load_bars("NQ", "5m", "rth", tmp_path).index[0] == pd.Timestamp("2024-03-05 09:35", tz=TZ)
    monkeypatch.setattr(rs, "get_session", lambda key: Session("rth", "RTH", time(10, 0), time(16, 0)))
    assert load_bars("NQ", "5m", "rth", tmp_path).index[0] == pd.Timestamp("2024-03-05 10:05", tz=TZ)


def test_cache_rebuilds_on_a_new_cache_version_or_a_same_mtime_source_change(tmp_path, monkeypatch):
    src = _source(tmp_path)
    first = load_bars("NQ", "5m", "rth", tmp_path)
    cached = cache_path("NQ", "5m", "rth", tmp_path)
    old_version = rs.read_fingerprint(cached)["version"]
    monkeypatch.setattr(rs, "CACHE_VERSION", old_version + 1)
    pd.testing.assert_frame_equal(load_bars("NQ", "5m", "rth", tmp_path), first)
    assert rs.read_fingerprint(cached)["version"] == old_version + 1  # rebuilt and re-stamped
    # a rewritten source with its old modification time (a copy that kept timestamps) is still noticed by its size
    st = src.stat()
    one_minute("2024-03-06 09:00", "2024-03-06 12:00").to_parquet(src)
    os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert (load_bars("NQ", "5m", "rth", tmp_path).session_date == pd.Timestamp("2024-03-06")).all()


def test_corrupt_or_foreign_cache_is_rebuilt_and_writes_are_atomic(tmp_path):
    _source(tmp_path)
    cached = cache_path("NQ", "5m", "rth", tmp_path)
    cached.write_bytes(b"not parquet")  # e.g. a write cut off by a crash under the old direct-write code
    bars = load_bars("NQ", "5m", "rth", tmp_path)
    assert len(bars) == 78 and rs.read_fingerprint(cached) is not None
    pd.DataFrame({"x": [1]}).to_parquet(cached)  # a Parquet file without a fingerprint
    assert len(load_bars("NQ", "5m", "rth", tmp_path)) == 78
    assert not list(cached.parent.glob("*.tmp"))  # no temporary files left behind


def test_session_close_is_the_template_end_or_the_scheduled_early_close():
    from guban.config import get_session
    from guban.data.resample import session_closes

    days = pd.Series(pd.to_datetime(["2024-12-23", "2024-12-24", "2024-12-24", "2024-12-26"]))
    early = {pd.Timestamp("2024-12-24").date(): time(13, 15)}
    eth = session_closes(days, get_session("eth"), early)
    assert list(eth.dt.strftime("%Y-%m-%d %H:%M")) == ["2024-12-23 17:00", "2024-12-24 13:15", "2024-12-24 13:15", "2024-12-26 17:00"]
    assert str(eth.dt.tz) == TZ
    rth = session_closes(days, get_session("rth"), early | {pd.Timestamp("2024-12-26").date(): time(18, 0)})
    assert list(rth.dt.strftime("%H:%M")) == ["16:00", "13:15", "13:15", "16:00"]  # a later "early" close never extends


def test_load_bars_adds_session_close_from_the_symbols_calendar_in_chart_time(tmp_path):
    _source(tmp_path, "2024-12-24 09:00", "2024-12-24 13:15")
    b = load_bars("NQ", "5m", "rth", tmp_path, tz="America/Chicago")
    assert (b["session_close"] == pd.Timestamp("2024-12-24 12:15", tz="America/Chicago")).all()
    assert str(b["session_close"].dt.tz) == "America/Chicago"
    assert "session_close" not in pd.read_parquet(cache_path("NQ", "5m", "rth", tmp_path)).columns  # not cached
