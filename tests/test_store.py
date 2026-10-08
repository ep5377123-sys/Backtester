"""Shared market-data store (Phase 14): tick-built bars, minute fallback, the validity rule, write-once builds."""

import hashlib
import os
import stat
from dataclasses import replace
from datetime import time

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from guban.cli import app
from guban.data.importer import RECORD
from guban.data.ticks import TICK_RECORD, dotnet_ticks
from guban.store import StoreConfig, build_store, verify_store
from guban.store.bars import MINUTE_COLUMNS, TICK_COLUMNS, minute_record_bars, session_bars, tick_bars, tick_rows, to_utc

ET = "America/New_York"
NOW = pd.Timestamp("2025-08-20 12:00", tz=ET)


def minute_raw(rows):
    """rows: [(naive ET bar-open time, open, high, low, close, volume, bid_vol, ask_vol)] -> RECORD array."""
    raw = np.zeros(len(rows), RECORD)
    for i, (t, o, h, l, c, v, b, a) in enumerate(rows):  # noqa: E741
        raw[i] = (dotnet_ticks(pd.Timestamp(t)), h, l, o, c, b, a, v, v, 0)
    return raw


def tick_raw(rows, tick=0.25):
    """rows: [(naive ET time, price, size, side)]; side +1 prints at the ask, -1 at the bid, 0 between."""
    raw = np.zeros(len(rows), TICK_RECORD)
    cum = 0
    for i, (t, p, s, side) in enumerate(rows):
        cum += s
        bid, ask = {1: (p - tick, p), -1: (p, p + tick), 0: (p - tick, p + tick)}[side]
        raw[i] = (dotnet_ticks(pd.Timestamp(t)), p, bid, ask, s, s, cum, 0, 0, 0, 1)
    return raw


# One session (file 20250804 = Mon 18:00 ET): three minutes of trades.
TICKS = [
    ("2025-08-04 18:00:00.100", 100.00, 2, 1),
    ("2025-08-04 18:00:30", 100.50, 1, -1),
    ("2025-08-04 18:00:59.999", 99.75, 3, 1),
    ("2025-08-04 18:01:10", 100.25, 4, -1),
    ("2025-08-04 18:01:20", 100.25, 0, 0),  # size-0 record (2026 feed): no volume, no price
    ("2025-08-04 18:02:00", 101.00, 5, 0),
]
MINUTES = [  # the matching minute records: two records at 18:00 (merged), a zero-volume placeholder
    ("2025-08-04 18:00", 100.00, 100.50, 100.00, 100.50, 3, 1, 2),
    ("2025-08-04 18:00", 100.50, 100.50, 99.75, 99.75, 3, 0, 3),
    ("2025-08-04 18:01", 100.25, 100.25, 100.25, 100.25, 4, 4, 0),
    ("2025-08-04 18:02", 101.00, 101.00, 101.00, 101.00, 5, 0, 0),
    ("2025-08-04 18:03", 101.00, 101.00, 101.00, 101.00, 0, 0, 0),  # placeholder, dropped
]


def test_to_utc_converts_eastern_wall_clock():
    t = to_utc(minute_raw(MINUTES[:1])["ticks"])
    assert str(t[0]) == "2025-08-04 22:00:00+00:00"


def test_to_utc_infers_the_repeated_fall_back_hour():
    # BTC trades 24/7 since 2026-05-29: a session can cross 01:00-02:00 on the first Sunday of November twice
    walls = ["2026-11-01 00:59", "2026-11-01 01:30", "2026-11-01 01:59", "2026-11-01 01:00", "2026-11-01 01:30", "2026-11-01 02:00"]
    t = to_utc(np.array([dotnet_ticks(pd.Timestamp(w)) for w in walls]))
    assert t.is_monotonic_increasing and t.is_unique
    assert str(t[1]) == "2026-11-01 05:30:00+00:00" and str(t[4]) == "2026-11-01 06:30:00+00:00"


def test_tick_rows_keep_every_record_with_its_side():
    t = tick_rows(tick_raw(TICKS), "202509")
    assert list(t.columns) == TICK_COLUMNS
    assert t["side"].tolist() == [1, -1, 1, -1, 0, 0]
    assert t["seq"].tolist() == [2, 3, 6, 10, 10, 15]
    assert str(t["ts_utc"].dtype) == "datetime64[ms, UTC]"


def test_tick_bars_open_label_ohlc_volume_and_sides():
    b = tick_bars(tick_rows(tick_raw(TICKS), "202509"))
    assert list(b.columns) == MINUTE_COLUMNS
    assert b["ts_utc"].dt.strftime("%H:%M").tolist() == ["22:00", "22:01", "22:02"]  # bar OPEN, UTC
    first = b.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume, first.ask_vol, first.bid_vol) == (100.0, 100.5, 99.75, 99.75, 6, 5, 1)
    assert b["volume"].tolist() == [6, 4, 5]  # the size-0 record adds nothing
    assert b.iloc[2].ask_vol == 0 and b.iloc[2].bid_vol == 0  # a print between bid and ask has no side
    assert (b["source"] == "tick").all()


def test_size_zero_records_count_for_prices_not_volume():
    # 2026-05-26+ feed: DeepCharts' own minute bars include size-0 prices
    rows = [("2026-06-15 10:00:00", 100.0, 0, 1), ("2026-06-15 10:00:01", 100.5, 2, 1), ("2026-06-15 10:00:02", 99.5, 0, -1),
            ("2026-06-15 10:00:03", 100.25, 1, -1), ("2026-06-15 10:01:00", 101.0, 0, 0)]  # fmt: skip
    b = tick_bars(tick_rows(tick_raw(rows), "202606"))
    assert len(b) == 1  # 10:01 holds only a size-0 record: no bar
    first = b.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume, first.ask_vol, first.bid_vol) == (100.0, 100.5, 99.5, 100.25, 3, 2, 1)


def test_minute_records_merge_same_label_and_drop_placeholders():
    b = minute_record_bars(minute_raw(MINUTES), "202509")
    assert len(b) == 3 and (b["source"] == "minute").all()
    first = b.iloc[0]
    assert (first.open, first.high, first.low, first.close, first.volume, first.bid_vol, first.ask_vol) == (100.0, 100.5, 99.75, 99.75, 6, 1, 5)


def test_opening_minute_keeps_its_zero_volume_open_record():
    # real NQ pattern (session 20240305): the 18:00 label is open (volume 0), high, low, close records
    rows = [("2024-03-05 18:00", 17981.0, 17981.0, 17981.0, 17981.0, 0, 0, 0),
            ("2024-03-05 18:00", 17985.25, 17985.25, 17985.25, 17985.25, 179, 0, 179),
            ("2024-03-05 18:00", 17972.5, 17972.5, 17972.5, 17972.5, 196, 196, 0),
            ("2024-03-05 18:00", 17972.75, 17972.75, 17972.75, 17972.75, 32, 0, 0)]  # fmt: skip
    b = minute_record_bars(minute_raw(rows), "202403").iloc[0]
    assert (b.open, b.high, b.low, b.close, b.volume) == (17981.0, 17985.25, 17972.5, 17972.75, 407)


def test_minute_rule_uses_the_minute_records_whenever_they_have_volume():
    r = session_bars(minute_raw(MINUTES), tick_raw(TICKS), "202509", 2.0, 0.99, bars="minute")
    assert r.quality["bar_source"] == "minute" and (r.bars["source"] == "minute").all() and r.quality["reason"] == ""
    assert len(r.ticks) == len(TICKS)  # ticks are stored either way
    extra = TICKS + [("2025-08-04 18:02:30", 101.0, 1, 1)]
    r = session_bars(minute_raw(MINUTES), tick_raw(extra), "202509", 2.0, 0.99, bars="minute")
    assert r.quality["bar_source"] == "minute" and "+6.7%" in r.quality["reason"]  # the tick problem is still named
    r = session_bars(np.empty(0, RECORD), tick_raw(TICKS), "202509", 2.0, 0.99, bars="minute")
    assert r.quality["bar_source"] == "tick" and r.quality["reason"] == "no minute volume"
    with pytest.raises(ValueError, match="bars"):
        session_bars(minute_raw(MINUTES), None, "202509", 2.0, 0.99, bars="ticks")


def test_valid_ticks_build_the_bars():
    r = session_bars(minute_raw(MINUTES), tick_raw(TICKS), "202509", 2.0, 0.99)
    assert r.quality["bar_source"] == "tick" and r.quality["minute_match"] == 1.0
    assert (r.bars["source"] == "tick").all() and r.bars["volume"].sum() == 15
    assert len(r.ticks) == len(TICKS)


def test_no_tick_file_falls_back_to_minute_records():
    r = session_bars(minute_raw(MINUTES), None, "202509", 2.0, 0.99)
    assert r.quality["bar_source"] == "minute" and r.quality["reason"] == "no tick file"
    assert r.ticks is None and (r.bars["source"] == "minute").all()


def test_tick_volume_outside_tolerance_falls_back():
    extra = TICKS + [("2025-08-04 18:02:30", 101.0, 1, 1)]  # 16 vs 15: +6.7%
    r = session_bars(minute_raw(MINUTES), tick_raw(extra), "202509", 2.0, 0.99)
    assert r.quality["bar_source"] == "minute" and "+6.7%" in r.quality["reason"]
    assert len(r.ticks) == len(extra)  # the ticks are still stored


def test_too_few_matching_minutes_falls_back():
    # total within 2% but volume moved between minutes: the GC 2025-11+ pattern
    moved = [(t, p, s + (1 if i == 0 else -1 if i == 3 else 0), sd) for i, (t, p, s, sd) in enumerate(TICKS)]
    r = session_bars(minute_raw(MINUTES), tick_raw(moved), "202509", 50.0, 0.99)
    assert r.quality["bar_source"] == "minute" and r.quality["minute_match"] == pytest.approx(1 / 3)
    assert "of minutes match" in r.quality["reason"]


def test_minute_with_no_ticks_keeps_its_record_in_a_tick_session():
    # 18:04 has a real record (volume 1) but no trade in the tick file: the bar keeps the record
    mins = MINUTES[:4] + [("2025-08-04 18:04", 102.0, 102.0, 102.0, 102.0, 1, 1, 0)]
    r = session_bars(minute_raw(mins), tick_raw(TICKS), "202509", 10.0, 0.5)
    assert r.quality["bar_source"] == "tick"
    assert r.bars["source"].tolist() == ["tick", "tick", "tick", "minute"]
    assert r.quality["minute_bars_in_tick_session"] == 1


# ---------------------------------------------------------------------------
# Builds


def write(path, raw):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw.tofile(path)
    return path


def make_source(src, sessions=("20250804", "20250805")):
    for s in sessions:
        day = pd.Timestamp(s)
        shift = lambda rows: [(str(pd.Timestamp(r[0]) + (day - pd.Timestamp("20250804"))), *r[1:]) for r in rows]  # noqa: E731
        write(src / "NQ-CME" / "202509" / "Minute" / f"{s}.data", minute_raw(shift(MINUTES)))
        write(src / "NQ-CME" / "202509" / "Tick" / f"{s}.data", tick_raw(shift(TICKS)))
    (src / "NQ-CME" / "202509" / "Minute" / "20250808.data").write_bytes(b"")  # Friday: no session
    return src


def config(tmp_path, **kw):
    cfg = StoreConfig(root=tmp_path / "store", source=tmp_path / "src", symbols={"NQ": "NQ-CME"}, tolerance_pct=2.0,
                      min_minute_match=0.99, session_end=time(18), settle_minutes=0, thin_ratio=0.2)  # fmt: skip
    return replace(cfg, **kw)


def snapshot(root):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*") if p.is_file()}


def test_build_writes_sessions_manifest_quality_rolls_readme(tmp_path):
    make_source(tmp_path / "src")
    rep = build_store(config(tmp_path), now=NOW, workers=1)[0]
    root = tmp_path / "store"
    assert rep.built == 2 and rep.tick_built == 0 and not rep.skipped and not rep.changed  # bars = "minute"
    m = pd.read_parquet(root / "NQ" / "minute" / "202509" / "20250804.parquet")
    assert list(m.columns) == MINUTE_COLUMNS and len(m) == 3
    t = pd.read_parquet(root / "NQ" / "tick" / "202509" / "20250805.parquet")
    assert list(t.columns) == TICK_COLUMNS and len(t) == len(TICKS)
    man = pd.read_csv(root / "manifest.csv", dtype=str)
    assert len(man) == 5  # 2 minute + 2 tick + the empty Friday file
    assert set(man["kind"]) == {"minute", "tick"}
    q = pd.read_csv(root / "NQ" / "quality.csv", dtype={"session": str, "contract": str})
    assert q["session"].tolist() == ["20250804", "20250805"] and (q["bar_source"] == "minute").all()
    assert q["tick_ok"].tolist() == [True, True]  # the ticks are stored and pass the tick check
    assert (root / "NQ" / "roll_calendar.csv").exists() and (root / "README.md").exists()
    out = root / "NQ" / "minute" / "202509" / "20250804.parquet"
    assert not os.access(out, os.W_OK)  # write-once: read-only


def test_second_build_writes_nothing(tmp_path):
    make_source(tmp_path / "src")
    cfg = config(tmp_path)
    build_store(cfg, now=NOW, workers=1)
    before = snapshot(tmp_path / "store")
    rep = build_store(cfg, now=NOW, workers=1)[0]
    assert rep.built == 0 and rep.files_written == 0 and rep.already == 2
    assert snapshot(tmp_path / "store") == before


def test_in_progress_and_cut_off_sessions_are_skipped_and_listed(tmp_path):
    src = make_source(tmp_path / "src")
    # the newest session (2025-08-19 18:00 -> 08-20 17:59) has not ended at NOW
    write(src / "NQ-CME" / "202509" / "Minute" / "20250819.data", minute_raw([("2025-08-19 18:00", 1, 1, 1, 1, 1, 1, 0)]))
    # a finished session whose tick file is cut mid-record
    write(src / "NQ-CME" / "202509" / "Minute" / "20250806.data", minute_raw([("2025-08-06 18:00", 1, 1, 1, 1, 1, 1, 0)]))
    p = write(src / "NQ-CME" / "202509" / "Tick" / "20250806.data", tick_raw([("2025-08-06 18:00", 1.0, 1, 1)]))
    p.write_bytes(p.read_bytes()[:-5])
    rep = build_store(config(tmp_path), now=NOW, workers=1)[0]
    reasons = dict((s, why) for s, why in rep.skipped)
    assert "in progress" in reasons["202509/20250819"] and "not a whole number" in reasons["202509/20250806"]
    assert rep.built == 2
    assert not (tmp_path / "store" / "NQ" / "minute" / "202509" / "20250806.parquet").exists()


def test_recently_modified_file_waits_for_settle(tmp_path):
    make_source(tmp_path / "src")
    rep = build_store(config(tmp_path, settle_minutes=10), now=NOW, workers=1)[0]
    assert rep.built == 0 and all("modified" in why for _, why in rep.skipped)


def test_changed_source_is_reported_not_overwritten_unless_rebuilt(tmp_path):
    src = make_source(tmp_path / "src")
    cfg = config(tmp_path)
    build_store(cfg, now=NOW, workers=1)
    out = tmp_path / "store" / "NQ" / "minute" / "202509" / "20250804.parquet"
    before = out.read_bytes()
    extra = TICKS + [("2025-08-04 18:03:00", 101.0, 1, 1)]
    write(src / "NQ-CME" / "202509" / "Tick" / "20250804.data", tick_raw(extra))
    rep = build_store(cfg, now=NOW, workers=1)[0]
    assert [s for s, _ in rep.changed] == ["202509/20250804"] and rep.built == 0
    assert out.read_bytes() == before
    rep = build_store(cfg, now=NOW, workers=1, rebuild=["NQ:20250804"])[0]
    assert rep.built == 1 and not rep.changed
    assert len(pd.read_parquet(tmp_path / "store" / "NQ" / "tick" / "202509" / "20250804.parquet")) == len(extra)


def test_source_is_never_written(tmp_path):
    src = make_source(tmp_path / "src")
    files = [p for p in src.rglob("*.data")]
    for p in files:
        os.chmod(p, stat.S_IREAD)
    digest = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    mtimes = {p: p.stat().st_mtime_ns for p in files}
    build_store(config(tmp_path), now=NOW, workers=1)
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in files} == digest
    assert {p: p.stat().st_mtime_ns for p in files} == mtimes


def test_roll_calendar_switches_after_volume_crosses_and_when_the_front_has_no_file(tmp_path):
    src = tmp_path / "src" / "NQ-CME"
    vols = {"202509": [10, 10, 4, 0], "202512": [1, 2, 9, 9]}  # crosses in session 3, old gone in session 4
    sessions = ["20250804", "20250805", "20250806", "20250807"]
    for c, vs in vols.items():
        for s, v in zip(sessions, vs):
            if v:
                write(src / c / "Minute" / f"{s}.data", minute_raw([(f"{pd.Timestamp(s):%Y-%m-%d} 18:00", 1, 1, 1, 1, v, v, 0)]))
    build_store(config(tmp_path), now=NOW, workers=1)
    cal = pd.read_csv(tmp_path / "store" / "NQ" / "roll_calendar.csv", dtype=str)
    assert cal[["old_contract", "new_contract", "switch_session"]].values.tolist() == [["202509", "202512", "20250807"]]
    q = pd.read_csv(tmp_path / "store" / "NQ" / "quality.csv", dtype={"session": str, "contract": str})
    front = q[q["front"]].set_index("session")["contract"].to_dict()
    assert front == {"20250804": "202509", "20250805": "202509", "20250806": "202509", "20250807": "202512"}

    # the front contract's file stops before the volume crosses: switch in that same session
    (src / "202509" / "Minute" / "20250806.data").unlink()
    build_store(config(tmp_path, root=tmp_path / "store2"), now=NOW, workers=1)
    cal = pd.read_csv(tmp_path / "store2" / "NQ" / "roll_calendar.csv", dtype=str)
    assert cal[["switch_session", "reason"]].values.tolist() == [["20250806", "front has no file"]]


def test_a_session_with_ticks_but_no_minute_volume_still_has_a_front_contract(tmp_path):
    # GC/BTC 2026-08-18..24: empty minute files, full tick files
    src = tmp_path / "src" / "NQ-CME" / "202509"
    for s in ("20250804", "20250805", "20250806"):
        d = pd.Timestamp(s)
        write(src / "Tick" / f"{s}.data", tick_raw([(f"{d:%Y-%m-%d} 18:00:01", 100.0, 5, 1)]))
        if s != "20250805":
            write(src / "Minute" / f"{s}.data", minute_raw([(f"{d:%Y-%m-%d} 18:00", 100.0, 100.0, 100.0, 100.0, 5, 0, 5)]))
        else:
            (src / "Minute" / f"{s}.data").write_bytes(b"")
    build_store(config(tmp_path), now=NOW, workers=1)
    q = pd.read_csv(tmp_path / "store" / "NQ" / "quality.csv", dtype={"session": str, "contract": str})
    assert q.set_index("session")["front"].to_dict() == {"20250804": True, "20250805": True, "20250806": True}
    assert q.set_index("session").loc["20250805", "bar_source"] == "tick"


def test_verify_reports_sr3_rates(tmp_path):
    make_source(tmp_path / "src")
    cfg = config(tmp_path)
    build_store(cfg, now=NOW, workers=1)
    v = verify_store(cfg)[0]
    assert v.minute_sessions == 2 and v.tick_sessions == 0 and v.passed and not v.problems
    cfg = config(tmp_path, root=tmp_path / "tick_store", bars="tick")
    build_store(cfg, now=NOW, workers=1)
    v = verify_store(cfg)[0]
    assert v.tick_sessions == 2 and v.volume_match == 1.0 and v.ohlc_match == 1.0 and v.passed and not v.problems


def test_cli_store_build_and_verify(tmp_path):
    make_source(tmp_path / "src")
    runner = CliRunner()
    args = ["--root", str(tmp_path / "store"), "--source", str(tmp_path / "src"), "--symbol", "NQ"]
    res = runner.invoke(app, ["store", "build", *args, "--workers", "1", "--settle-minutes", "0"])
    assert res.exit_code == 0, res.output
    assert "NQ: built 2 sessions (0 from ticks, 2 from minute records" in res.output
    res = runner.invoke(app, ["store", "verify", *args])
    assert res.exit_code == 0, res.output and "PASS" in res.output


def test_store_build_and_verify_share_identical_flags():
    import typer

    group = typer.main.get_command(app).commands["store"]
    params = {name: {p.name: p for p in group.commands[name].params} for name in ("build", "verify")}
    for name in ("symbols", "root", "source"):
        a, b = params["build"][name], params["verify"][name]
        assert (a.opts, a.default, a.help) == (b.opts, b.default, b.help), name


def test_rebuild_all_switches_the_rule_and_keeps_files_whose_source_is_gone(tmp_path):
    src = make_source(tmp_path / "src")
    build_store(config(tmp_path, bars="tick"), now=NOW, workers=1)
    root = tmp_path / "store"
    tick_out = root / "NQ" / "tick" / "202509" / "20250804.parquet"
    (src / "NQ-CME" / "202509" / "Tick" / "20250804.data").unlink()  # the source loses this tick file
    rep = build_store(config(tmp_path), now=NOW, workers=1, rebuild=["NQ:all"])[0]
    assert rep.rebuilt == 3 and rep.built == 2 and rep.tick_built == 0  # 3: the empty Friday file too
    for s in ("20250804", "20250805"):
        assert (pd.read_parquet(root / "NQ" / "minute" / "202509" / f"{s}.parquet")["source"] == "minute").all()
    assert tick_out.exists() and not os.access(tick_out, os.W_OK)  # kept, still read-only
    man = pd.read_csv(root / "manifest.csv", dtype=str, keep_default_na=False)
    assert "NQ-CME/202509/Tick/20250804.data" in set(man["source"]) and len(man) == 5
    q = pd.read_csv(root / "NQ" / "quality.csv", dtype={"session": str, "contract": str}).set_index("session")
    assert q.loc["20250804", "tick_volume"] == 15 and bool(q.loc["20250804", "tick_ok"])  # its tick check is kept
    assert verify_store(config(tmp_path))[0].passed
    before = snapshot(root)
    assert build_store(config(tmp_path), now=NOW, workers=1)[0].files_written == 0
    assert snapshot(root) == before


def test_rebuild_changed_and_same_content_with_a_new_timestamp(tmp_path):
    src = make_source(tmp_path / "src")
    cfg = config(tmp_path)
    build_store(cfg, now=NOW, workers=1)
    f = src / "NQ-CME" / "202509" / "Minute" / "20250805.data"
    os.utime(f, ns=(f.stat().st_atime_ns, f.stat().st_mtime_ns + 10**9))  # re-downloaded, same bytes
    write(src / "NQ-CME" / "202509" / "Tick" / "20250804.data", tick_raw(TICKS + [("2025-08-04 18:03:00", 101.0, 1, 1)]))
    rep = build_store(cfg, now=NOW, workers=1)[0]
    assert [k for k, _ in rep.changed] == ["202509/20250804"]  # the touched minute file is not a change
    assert build_store(cfg, now=NOW, workers=1)[0].files_written == 0  # its new timestamp is recorded once
    rep = build_store(cfg, now=NOW, workers=1, rebuild=["NQ:changed"])[0]
    assert rep.rebuilt == 1 and not rep.changed
    with pytest.raises(ValueError, match="SYM:changed"):
        build_store(cfg, now=NOW, workers=1, rebuild=["NQ:some"])


def test_a_symbol_whose_folder_is_gone_keeps_its_store(tmp_path):
    make_source(tmp_path / "src")
    cfg = config(tmp_path, symbols={"NQ": "NQ-CME", "GC": "GC-COMEX"})
    build_store(cfg, symbols=["NQ"], now=NOW, workers=1)
    (tmp_path / "src" / "NQ-CME").rename(tmp_path / "src" / "gone")
    before = snapshot(tmp_path / "store")
    reps = build_store(cfg, now=NOW, workers=1)
    assert reps[0].folder_missing and reps[0].gone == 3 and reps[0].built == 0
    assert snapshot(tmp_path / "store") == before
    v = verify_store(cfg, ["NQ"])[0]
    assert v.passed and v.sources_gone == 5 and v.not_compared == 2
