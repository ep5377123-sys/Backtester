from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from guban.data.importer import RECORD, _DOTNET_EPOCH_TICKS, choose_contracts, import_store, read_session_file


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
            ("2023-11-20 18:00", 101.0, 101.0, 101.0, 101.0, 10),  # duplicate open print
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


def test_choose_contracts_rolls_on_volume_and_never_back():
    vol = pd.DataFrame(
        {"202312": [100, 90, 40, 60], "202403": [10, 50, 80, 55]},
        index=["20231211", "20231212", "20231213", "20231214"],
    )
    assert list(choose_contracts(vol)) == ["202312", "202312", "202403", "202403"]


def test_import_store_builds_continuous_series(tmp_path):
    src = tmp_path / "NQ-CME"
    _write(src / "202312/Minute/20231211.data", [("2023-12-11 18:00", 1, 1, 1, 1, 100)])
    _write(src / "202403/Minute/20231211.data", [("2023-12-11 18:00", 2, 2, 2, 2, 5)])
    _write(src / "202312/Minute/20231212.data", [("2023-12-12 18:00", 1, 1, 1, 1, 5)])
    _write(src / "202403/Minute/20231212.data", [("2023-12-12 18:00", 2, 2, 2, 2, 100)])
    summary = import_store(src, "NQ", tmp_path / "data")
    out = pd.read_parquet(tmp_path / "data/NQ/1m.parquet")
    assert list(out["contract"].astype(str)) == ["202312", "202403"]
    assert list(out["close"]) == [1, 2]
    assert summary.rolls == [("20231211", "202312"), ("20231212", "202403")]
