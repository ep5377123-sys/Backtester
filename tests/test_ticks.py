"""Tick store reader (Phase 13): record layout, checks, RTH window, volume at price, cache."""

import numpy as np
import pandas as pd
import pytest

from guban.data.importer import CorruptFileError
from guban.data.ticks import (
    TICK_RECORD,
    build_rth_profiles,
    dotnet_ticks,
    load_rth_profiles,
    read_ticks,
    tick_times,
    trade_date,
    volume_at_price,
    window_trades,
)


def write_ticks(path, trades):
    """trades: [(naive ET time str, price, size)]; cumulative volume filled in like the real files."""
    raw = np.zeros(len(trades), TICK_RECORD)
    cum = 0
    for i, (t, p, s) in enumerate(trades):
        cum += s
        raw[i] = (dotnet_ticks(pd.Timestamp(t)), p, p - 0.25, p, s, s, cum, 0, 0, 0, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw.tofile(path)
    return path


TRADES = [
    ("2025-08-04 18:00:00", 100.00, 5),  # overnight: outside RTH
    ("2025-08-05 09:29:59.999", 100.25, 7),  # one tick before the open
    ("2025-08-05 09:30:00", 100.50, 2),  # first RTH trade
    ("2025-08-05 10:00:00", 100.75, 3),
    ("2025-08-05 12:00:00", 100.50, 4),
    ("2025-08-05 15:59:59.999", 101.00, 1),  # last RTH trade
    ("2025-08-05 16:00:00", 102.00, 9),  # the close itself is outside [start, end)
]


def test_record_is_57_bytes_and_round_trips(tmp_path):
    assert TICK_RECORD.itemsize == 57
    raw = read_ticks(write_ticks(tmp_path / "20250804.data", TRADES))
    assert len(raw) == len(TRADES)
    assert str(tick_times(raw["ticks"][[2]])[0]) == "2025-08-05 09:30:00"
    assert (np.diff(raw["cum_volume"]) == raw["size"][1:]).all()


def test_rth_window_is_start_inclusive_end_exclusive(tmp_path):
    raw = read_ticks(write_ticks(tmp_path / "20250804.data", TRADES))
    tr = window_trades(raw, pd.Timestamp("2025-08-05 09:30"), pd.Timestamp("2025-08-05 16:00"))
    assert tr["price"].tolist() == [100.50, 100.75, 100.50, 101.00]
    assert int(tr["size"].sum()) == 10


def test_volume_at_price_snaps_to_ticks():
    base, vol = volume_at_price(np.array([100.5, 100.75, 100.5000000001, 101.0]), np.array([2, 3, 4, 1]), 0.25)
    assert base == 402
    assert vol.tolist() == [6, 3, 1]


@pytest.mark.parametrize(
    "mutate, match",
    [
        (lambda r: r.__setitem__("price", np.nan), "price"),
        (lambda r: r.__setitem__("size", -1), "negative size"),
        (lambda r: r.__setitem__("ticks", dotnet_ticks(pd.Timestamp("2025-08-10"))), "time outside"),
    ],
)
def test_bad_records_raise(tmp_path, mutate, match):
    path = write_ticks(tmp_path / "20250804.data", TRADES)
    raw = np.fromfile(path, TICK_RECORD)
    rec = raw[3]
    mutate(rec)
    raw[3] = rec
    raw.tofile(path)
    with pytest.raises(CorruptFileError, match=match):
        read_ticks(path)


def test_size_zero_records_add_no_volume(tmp_path):
    # the feed since 2026-05-26 has size-0 records; they are kept and add nothing
    path = write_ticks(tmp_path / "20250804.data", TRADES[:3] + [("2025-08-05 09:31:00", 100.25, 0)] + TRADES[3:])
    raw = read_ticks(path)
    tr = window_trades(raw, pd.Timestamp("2025-08-05 09:30"), pd.Timestamp("2025-08-05 16:00"))
    assert int(tr["size"].sum()) == 10 and len(tr) == 5


def test_truncated_file_raises(tmp_path):
    path = write_ticks(tmp_path / "20250804.data", TRADES)
    path.write_bytes(path.read_bytes()[:-3])
    with pytest.raises(CorruptFileError, match="whole number"):
        read_ticks(path)


def test_trade_date_is_the_next_calendar_day():
    assert str(trade_date("20250804")) == "2025-08-05"
    assert str(trade_date("20250803")) == "2025-08-04"  # Sunday evening -> Monday


def _days(store):
    write_ticks(store / "202509" / "Tick" / "20250804.data", TRADES)
    write_ticks(store / "202509" / "Tick" / "20250805.data", [("2025-08-05 18:00", 100.0, 1), ("2025-08-06 09:29", 100.0, 1)])
    (store / "202509" / "Tick" / "20250806.data").write_bytes(b"")
    idx = pd.to_datetime(["2025-08-05", "2025-08-06", "2025-08-07", "2025-08-08"])
    return pd.DataFrame({
        "session_file": ["20250804", "20250805", "20250806", "20250807"], "contract": "202509",
        "rth_start": [d + pd.Timedelta("9h30min") for d in idx], "rth_end": [d + pd.Timedelta("16h") for d in idx],
    }, index=idx)  # fmt: skip


def test_build_rth_profiles(tmp_path):
    summary, vap = build_rth_profiles(tmp_path, _days(tmp_path), 0.25)
    assert list(summary.index.strftime("%Y-%m-%d")) == ["2025-08-05", "2025-08-06", "2025-08-07"]  # 08-08 has no file
    d = summary.loc["2025-08-05"]
    assert (d.trades, d.volume, d.open, d.high, d.low, d.close) == (4, 10, 100.5, 101.0, 100.5, 101.0)
    assert str(d.last_trade) == "2025-08-05 15:59:59.999000"
    assert summary.loc["2025-08-06"].trades == 0  # ends 09:29: no RTH
    assert summary.loc["2025-08-07"].trades == 0  # empty file
    v = vap[vap.session_date == "2025-08-05"].set_index("price_tick")["volume"].to_dict()
    assert v == {402: 6, 403: 3, 404: 1}


def test_cache_is_reused_and_rebuilt_when_a_file_changes(tmp_path):
    days = _days(tmp_path / "store")
    s1, v1 = load_rth_profiles(tmp_path / "store", days, 0.25, tmp_path / "cache")
    calls = []
    s2, _ = load_rth_profiles(tmp_path / "store", days, 0.25, tmp_path / "cache", progress=lambda i, n: calls.append(i))
    assert not calls  # served from the cache
    pd.testing.assert_frame_equal(s1, s2)
    write_ticks(tmp_path / "store" / "202509" / "Tick" / "20250804.data", TRADES[:4])
    s3, _ = load_rth_profiles(tmp_path / "store", days, 0.25, tmp_path / "cache", progress=lambda i, n: calls.append(i))
    assert calls and s3.loc["2025-08-05"].volume == 5


def write_store_ticks(path, trades, contract="202509"):
    """The market-data store's tick layout (D:\Market Data\<SYM>\tick\<contract>\<session>.parquet): UTC ms times."""
    ts = pd.DatetimeIndex([pd.Timestamp(t) for t, _, _ in trades]).tz_localize("America/New_York").tz_convert("UTC").as_unit("ms")
    df = pd.DataFrame({"ts_utc": ts, "price": [p for _, p, _ in trades], "size": np.array([s for _, _, s in trades], np.int32),
                       "side": np.int8(1), "seq": np.int32(0), "contract": contract})  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)
    return path


def test_build_rth_profiles_reads_the_market_data_store_the_same_way(tmp_path):
    binary = build_rth_profiles(tmp_path / "bin", _days(tmp_path / "bin"), 0.25)
    sym = tmp_path / "Market Data" / "NQ"
    (sym).mkdir(parents=True)
    (sym / "quality.csv").write_text("session\n", encoding="utf-8")  # marks the store layout
    write_store_ticks(sym / "tick" / "202509" / "20250804.parquet", TRADES)
    write_store_ticks(sym / "tick" / "202509" / "20250805.parquet", [("2025-08-05 18:00", 100.0, 1), ("2025-08-06 09:29", 100.0, 1)])
    write_store_ticks(sym / "tick" / "202509" / "20250806.parquet", [])
    days = _days(tmp_path / "bin")
    store = build_rth_profiles(sym, days, 0.25)
    pd.testing.assert_frame_equal(store[0], binary[0], check_dtype=False)
    pd.testing.assert_frame_equal(store[1], binary[1], check_dtype=False)
    s1, _ = load_rth_profiles(sym, days, 0.25, tmp_path / "cache")
    write_store_ticks(sym / "tick" / "202509" / "20250804.parquet", TRADES[:4])
    s2, _ = load_rth_profiles(sym, days, 0.25, tmp_path / "cache")
    assert (s1.loc["2025-08-05"].volume, s2.loc["2025-08-05"].volume) == (10, 5)  # the cache sees the store file change
