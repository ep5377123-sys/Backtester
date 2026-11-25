"""Guban reads the shared market-data store (Phase 15): import, bar times, rolls, ticks."""

from pathlib import Path

import pandas as pd
import pytest

from guban.store.build import QUALITY_COLUMNS
from guban.store.read import front_sessions, import_from_store, read_minute

NY = "America/New_York"


def _session(root: Path, sym: str, contract: str, session: str, bars: list[tuple[str, float, int]]) -> None:
    """bars: (open time in New York, price, volume): one minute bar each, written as the store does (ts_utc = open, UTC)."""
    ts = pd.DatetimeIndex([pd.Timestamp(t) for t, _, _ in bars]).tz_localize(NY).tz_convert("UTC")
    df = pd.DataFrame({
        "ts_utc": ts, "open": [p for _, p, _ in bars], "high": [p + 1 for _, p, _ in bars], "low": [p - 1 for _, p, _ in bars],
        "close": [p for _, p, _ in bars], "volume": [v for _, _, v in bars], "ask_vol": [v // 2 for _, _, v in bars],
        "bid_vol": [v - v // 2 for _, _, v in bars], "contract": contract, "source": "minute",
    })
    path = root / sym / "minute" / contract / f"{session}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def _quality(root: Path, sym: str, rows: list[tuple[str, str, int, bool, bool]]) -> None:
    """rows: (session, contract, minute volume, front, thin)."""
    q = pd.DataFrame([{"session": s, "trade_date": (pd.Timestamp(s) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), "contract": c,
                       "minute_volume": v, "bars": 1, "bar_source": "minute", "front": f, "thin": t} for s, c, v, f, t in rows])
    q.reindex(columns=QUALITY_COLUMNS).to_csv(root / sym / "quality.csv", index=False)


@pytest.fixture
def store(tmp_path):
    root = tmp_path / "Market Data"
    for s, t in (("20231211", "2023-12-11 18:00"), ("20231212", "2023-12-12 18:00"), ("20231213", "2023-12-13 18:00")):
        _session(root, "NQ", "202312", s, [(t, 1.0, 5)])
        _session(root, "NQ", "202403", s, [(t, 2.0, 100)])
    _quality(root, "NQ", [("20231211", "202312", 5, True, False), ("20231211", "202403", 100, False, False),
                          ("20231212", "202312", 5, True, True), ("20231212", "202403", 100, False, False),
                          ("20231213", "202312", 5, False, False), ("20231213", "202403", 100, True, False)])
    return root


def test_read_minute_labels_bars_by_close_time_in_new_york(store):
    bars = read_minute(store, "NQ", "202312", "20231211")
    assert str(bars.index.tz) == NY
    assert list(bars.index.strftime("%Y-%m-%d %H:%M")) == ["2023-12-11 18:01"]
    assert list(bars.columns) == ["open", "high", "low", "close", "volume", "bid_vol", "ask_vol"]


def test_front_sessions_come_from_quality_csv(store):
    assert dict(front_sessions(store, "NQ")["contract"]) == {"20231211": "202312", "20231212": "202312", "20231213": "202403"}


def test_import_from_store_follows_the_store_roll_calendar(store, tmp_path):
    summary = import_from_store("NQ", tmp_path / "data", root=store)
    out = pd.read_parquet(tmp_path / "data/NQ/1m.parquet")
    assert list(out["contract"].astype(str)) == ["202312", "202312", "202403"]
    assert list(out["close"]) == [1.0, 1.0, 2.0]
    assert list(out["session"]) == [pd.Timestamp("2023-12-11"), pd.Timestamp("2023-12-12"), pd.Timestamp("2023-12-13")]
    assert str(out.index.tz) == NY and out.index.is_monotonic_increasing
    assert summary.rolls == [("20231211", "202312"), ("20231213", "202403")]
    assert summary.thin_sessions == ["20231212"]
    assert "thin front sessions (1): 20231212" in str(summary)


def test_import_from_store_expiry_roll(tmp_path):
    root = tmp_path / "Market Data"
    for s, t in (("20250615", "2025-06-15 18:00"), ("20250617", "2025-06-17 18:00")):
        _session(root, "MNQ", "202506", s, [(t, 1.0, 5)])
        _session(root, "MNQ", "202509", s, [(t, 2.0, 100)])
    _quality(root, "MNQ", [("20250615", "202506", 5, False, False), ("20250615", "202509", 100, True, False),
                           ("20250617", "202506", 5, False, False), ("20250617", "202509", 100, True, False)])
    summary = import_from_store("MNQ", tmp_path / "data", root=root, roll="expiry")
    out = pd.read_parquet(tmp_path / "data/MNQ/1m.parquet")
    assert list(out["contract"].astype(str)) == ["202506", "202509"]
    assert summary.rolls == [("20250615", "202506"), ("20250617", "202509")]


def test_expiry_roll_is_refused_for_btc(store, tmp_path):
    with pytest.raises(ValueError, match="expiry roll.*third Friday.*BTC"):
        import_from_store("BTC", tmp_path / "data", root=store, roll="expiry")


def test_missing_symbol_names_the_build_command(store, tmp_path):
    with pytest.raises(FileNotFoundError, match=r"ES.*quality\.csv.*guban store build --symbol ES"):
        import_from_store("ES", tmp_path / "data", root=store)


def test_missing_front_file_is_an_error_naming_it(store, tmp_path):
    (store / "NQ/minute/202403/20231213.parquet").unlink()
    with pytest.raises(FileNotFoundError, match=r"202403[\\/]20231213\.parquet"):
        import_from_store("NQ", tmp_path / "data", root=store)


def test_last_trade_date_rules():
    from guban.store.read import last_trade_date

    assert last_trade_date("202509", "cme_equity") == pd.Timestamp("2025-09-19").date()
    assert last_trade_date("202002", "cme_crypto") == pd.Timestamp("2020-02-28").date()
    closed = {pd.Timestamp("2020-12-25").date()}
    assert last_trade_date("202012", "cme_crypto", lambda d: d not in closed) == pd.Timestamp("2020-12-24").date()


def test_volume_roll_never_trades_a_contract_in_its_expiration_session(tmp_path):
    root = tmp_path / "Market Data"
    sessions = (("20200226", "2020-02-26 18:00"), ("20200227", "2020-02-27 18:00"), ("20200301", "2020-03-01 18:00"))
    for s, t in sessions:
        _session(root, "BTC", "202003", s, [(t, 2.0, 50)])
    for s, t in sessions[:2]:
        _session(root, "BTC", "202002", s, [(t, 1.0, 100)])
    _quality(root, "BTC", [("20200226", "202002", 100, True, False), ("20200226", "202003", 50, False, False),
                           ("20200227", "202002", 100, True, True), ("20200227", "202003", 50, False, False),
                           ("20200301", "202003", 50, True, False)])
    summary = import_from_store("BTC", tmp_path / "data", root=root)
    out = pd.read_parquet(tmp_path / "data/BTC/1m.parquet")
    assert list(out["contract"].astype(str)) == ["202002", "202003", "202003"]
    assert summary.rolls == [("20200226", "202002"), ("20200227", "202003")]
    assert summary.expiry_switches == ["20200227"]
