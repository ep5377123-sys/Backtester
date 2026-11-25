from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

import pytest

from guban.data.importer import (
    RECORD,
    CorruptFileError,
    _DOTNET_EPOCH_TICKS,
    choose_contracts,
    choose_contracts_by_expiry,
    import_store,
    read_session_file,
    third_friday,
)


def _ticks(ts: str) -> int:
    return int(pd.Timestamp(ts).value // 100) + _DOTNET_EPOCH_TICKS


def _write(path: Path, bars: list[tuple[str, float, float, float, float, int]]) -> None:
    """bars: (open_time, open, high, low, close, volume)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.zeros(len(bars), dtype=RECORD)
    for i, (t, o, h, l, c, v) in enumerate(bars):
        arr[i] = (_ticks(t), h, l, o, c, v // 2, v - v // 2, v, v, 0)
    arr.tofile(path)


def test_read_session_file_decodes_field_order_and_close_time_labels(tmp_path):
    f = tmp_path / "20231120.data"
    _write(
        f,
        [
            ("2023-11-20 18:00", 100.0, 100.0, 100.0, 100.0, 0),
            ("2023-11-20 18:00", 101.0, 101.0, 101.0, 101.0, 10),
            ("2023-11-20 18:01", 101.0, 103.0, 99.0, 102.0, 20),
        ],
    )
    df = read_session_file(f)
    assert list(df.index.strftime("%Y-%m-%d %H:%M")) == ["2023-11-20 18:01", "2023-11-20 18:02"]
    assert str(df.index.tz) == "America/New_York"
    first = df.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (100.0, 101.0, 100.0, 101.0, 10)
    second = df.iloc[1]
    assert (second.open, second.high, second.low, second.close) == (101.0, 103.0, 99.0, 102.0)


def test_choose_contracts_rolls_on_the_previous_sessions_volume_and_never_back():
    vol = pd.DataFrame(
        {"202312": [100, 90, 40, 60, 70], "202403": [10, 50, 80, 55, 30]},
        index=["20231211", "20231212", "20231213", "20231214", "20231215"],
    )
    assert list(choose_contracts(vol)) == ["202312", "202312", "202312", "202403", "202403"]


def test_choose_contracts_never_uses_the_sessions_own_volume():
    vol = pd.DataFrame({"202312": [5, 5], "202403": [100, 100]}, index=["20231211", "20231212"])
    assert list(choose_contracts(vol)) == ["202312", "202403"]
    for k in range(len(vol)):
        bumped = vol.copy()
        bumped.iloc[k] = [1000, 1]
        assert choose_contracts(bumped).iloc[k] == choose_contracts(vol).iloc[k]


def test_import_store_builds_continuous_series(tmp_path):
    src = tmp_path / "NQ-CME"
    _write(src / "202312/Minute/20231211.data", [("2023-12-11 18:00", 1, 1, 1, 1, 100)])
    _write(src / "202403/Minute/20231211.data", [("2023-12-11 18:00", 2, 2, 2, 2, 5)])
    _write(src / "202312/Minute/20231212.data", [("2023-12-12 18:00", 1, 1, 1, 1, 5)])
    _write(src / "202403/Minute/20231212.data", [("2023-12-12 18:00", 2, 2, 2, 2, 100)])
    _write(src / "202312/Minute/20231213.data", [("2023-12-13 18:00", 1, 1, 1, 1, 5)])
    _write(src / "202403/Minute/20231213.data", [("2023-12-13 18:00", 2, 2, 2, 2, 100)])
    summary = import_store(src, "NQ", tmp_path / "data")
    out = pd.read_parquet(tmp_path / "data/NQ/1m.parquet")
    assert list(out["contract"].astype(str)) == ["202312", "202312", "202403"]
    assert list(out["close"]) == [1, 1, 2]
    assert summary.rolls == [("20231211", "202312"), ("20231213", "202403")]


def test_third_friday():
    assert [str(third_friday(c).date()) for c in ("202506", "202512", "202603", "202609")] == [
        "2025-06-20", "2025-12-19", "2026-03-20", "2026-09-18",
    ]


def test_choose_contracts_by_expiry_rolls_on_wednesday_of_expiration_week():
    sessions = ["20250612", "20250615", "20250616", "20250617", "20250618"]
    vol = pd.DataFrame({"202506": [100, 90, 40, 30, 20], "202509": [10, 95, 80, 90, 95]}, index=sessions)
    chosen = choose_contracts_by_expiry(vol, days_before=2)
    assert list(chosen) == ["202506", "202506", "202506", "202509", "202509"]
    assert list(choose_contracts(vol)) == ["202506", "202506", "202509", "202509", "202509"]


def test_import_store_expiry_roll(tmp_path):
    src = tmp_path / "MNQ-CME"
    for session, t in (("20250615", "2025-06-15 18:00"), ("20250617", "2025-06-17 18:00")):
        _write(src / f"202506/Minute/{session}.data", [(t, 1, 1, 1, 1, 5)])
        _write(src / f"202509/Minute/{session}.data", [(t, 2, 2, 2, 2, 100)])
    summary = import_store(src, "MNQ", tmp_path / "data", roll="expiry")
    out = pd.read_parquet(tmp_path / "data/MNQ/1m.parquet")
    assert list(out["contract"].astype(str)) == ["202506", "202509"]
    assert summary.rolls == [("20250615", "202506"), ("20250617", "202509")]


def test_truncated_file_is_rejected_with_its_name(tmp_path):
    f = tmp_path / "20231120.data"
    _write(f, [("2023-11-20 18:00", 100.0, 101.0, 99.0, 100.5, 10), ("2023-11-20 18:01", 100.5, 101.0, 100.0, 100.0, 5)])
    f.write_bytes(f.read_bytes()[:-7])
    with pytest.raises(CorruptFileError, match=r"20231120\.data: 113 bytes is not a whole number of 60-byte records"):
        read_session_file(f)


@pytest.mark.parametrize(
    "bar, why",
    [
        (("2023-11-20 18:01", float("nan"), 101.0, 99.0, 100.0, 5), "NaN"),
        (("2023-11-20 18:01", 100.0, 99.0, 101.0, 100.0, 5), "low > high"),
        (("2023-11-20 18:01", 100.0, 101.0, 99.5, 102.0, 5), "outside low..high"),
        (("2023-11-20 18:01", 100.0, 101.0, 99.0, 100.0, -3), "negative volume"),
        (("2023-11-25 18:01", 100.0, 101.0, 99.0, 100.0, 5), "time outside 2023-11-20"),
    ],
)
def test_impossible_records_are_rejected_with_the_record_offset(tmp_path, bar, why):
    f = tmp_path / "20231120.data"
    _write(f, [("2023-11-20 18:00", 100.0, 101.0, 99.0, 100.0, 10), bar])
    with pytest.raises(CorruptFileError, match=rf"{why}.*record 1 \(byte offset 60\)"):
        read_session_file(f)


def test_records_out_of_order_are_sorted_not_rejected(tmp_path):
    f = tmp_path / "20231120.data"
    _write(f, [("2023-11-20 18:01", 101.0, 101.0, 101.0, 101.0, 1), ("2023-11-20 18:00", 100.0, 100.0, 100.0, 100.0, 1)])
    assert read_session_file(f)["close"].tolist() == [100.0, 101.0]
