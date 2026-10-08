"""Build and verify the shared market-data store (Phase 14; layout in README_TEXT below and PLAN.md).

The store is write-once: each finished DeepCharts session file becomes one read-only Parquet file per kind, recorded
in manifest.csv with its source's size, mtime and sha256. A later build skips recorded sources; when one has changed
(sha256 differs, a file appeared or disappeared) it is reported, never overwritten, unless rebuilt explicitly
(`rebuild=["SYM:YYYYMMDD"]`). The derived files (manifest.csv, quality.csv, roll_calendar.csv, README.md) are
rewritten only when their content changes, so a build with nothing new writes no file.

The DeepCharts folder is only ever opened for reading (np.fromfile / open "rb").
"""

from __future__ import annotations

import hashlib
import io
import os
import stat
import time as _time
import tomllib
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from guban.data.importer import RECORD, CorruptFileError, choose_contracts, read_records
from guban.data.parquet import write_parquet
from guban.data.ticks import TICK_RECORD, read_ticks
from guban.store.bars import match_counts, minute_record_bars, session_bars

CONFIG = Path(__file__).resolve().parents[3] / "config" / "store.toml"
TZ = "America/New_York"
KINDS = {"minute": ("Minute", RECORD), "tick": ("Tick", TICK_RECORD)}
MANIFEST_COLUMNS = ["symbol", "kind", "contract", "session", "source", "bytes", "mtime_ns", "sha256", "built_at", "output", "rows", "bar_source"]
QUALITY_COLUMNS = ["session", "trade_date", "contract", "minute_volume", "tick_volume", "diff_pct", "minute_match", "ohlc_match", "bars",
                   "minute_bars_in_tick_session", "bar_source", "reason", "tick_ok", "front", "thin"]  # fmt: skip
ROLL_COLUMNS = ["old_contract", "new_contract", "decision_session", "switch_session", "switch_trade_date", "reason"]
REBUILD_WORDS = ("changed", "all")
OHLC_TARGET = 0.999  # SR3: share of compared minutes with identical OHLC in tick-built sessions


@dataclass(frozen=True)
class StoreConfig:
    root: Path
    source: Path
    symbols: dict[str, str]  # store symbol -> DeepCharts folder name
    tolerance_pct: float
    min_minute_match: float
    session_end: time
    settle_minutes: float
    thin_ratio: float
    bars: str = "minute"  # which source builds the 1-minute bars: "minute" (user, 2026-10-07) or "tick" (Phase 14)


def load_store_config(path: Path = CONFIG) -> StoreConfig:
    with open(path, "rb") as f:
        c = tomllib.load(f)
    return StoreConfig(
        root=Path(c["root"]), source=Path(c["source"]), symbols=dict(c["symbols"]), tolerance_pct=float(c["tolerance_pct"]),
        min_minute_match=float(c["min_minute_match"]), session_end=time.fromisoformat(c["session_end"]),
        settle_minutes=float(c["settle_minutes"]), thin_ratio=float(c["thin_ratio"]), bars=str(c.get("bars", "minute")),
    )  # fmt: skip


@dataclass
class SymbolReport:
    symbol: str
    sessions: int = 0  # contract sessions found in the source
    already: int = 0  # stored by an earlier build
    empty: int = 0  # 0-byte files (no session), recorded without output
    built: int = 0
    tick_built: int = 0
    files_written: int = 0
    rebuilt: int = 0  # stored sessions built again (--rebuild)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (contract/session, reason)
    changed: list[tuple[str, str]] = field(default_factory=list)
    gone: int = 0  # stored sessions none of whose source files exist any more (kept as stored)
    folder_missing: bool = False
    rolls: int = 0
    seconds: float = 0.0


# ---------------------------------------------------------------------------
# Source scan, manifest, small file helpers


def _source_sessions(folder: Path) -> dict[tuple[str, str], dict[str, Path]]:
    """{(contract, session): {kind: path}} for every Minute/Tick .data file under a symbol's DeepCharts folder."""
    out: dict[tuple[str, str], dict[str, Path]] = {}
    for cdir in sorted(p for p in folder.iterdir() if p.is_dir() and p.name.isdigit() and len(p.name) == 6):
        for kind, (sub, _) in KINDS.items():
            d = cdir / sub
            if d.is_dir():
                for f in sorted(d.glob("*.data")):
                    out.setdefault((cdir.name, f.stem), {})[kind] = f
    return dict(sorted(out.items(), key=lambda kv: (kv[0][1], kv[0][0])))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 22):
            h.update(chunk)
    return h.hexdigest()


def _rel(path: Path, base: Path) -> str:
    return Path(path).relative_to(base).as_posix()


def read_manifest(root: Path) -> pd.DataFrame:
    path = Path(root) / "manifest.csv"
    if not path.exists():
        return pd.DataFrame(columns=MANIFEST_COLUMNS)
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _csv_text(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue()


def _write_if_changed(path: Path, text: str) -> int:
    """Write `text` atomically unless the file already holds it; returns files written (0 or 1)."""
    path = Path(path)
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="")
    os.replace(tmp, path)
    return 1


def _make_writable(path: Path) -> None:
    if path.exists():
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


def _session_start(session: str) -> pd.Timestamp:
    return pd.Timestamp(session).tz_localize(TZ) + pd.Timedelta(hours=18)


def _session_over(session: str, now: pd.Timestamp, end: time, settle_minutes: float) -> pd.Timestamp | None:
    """None when the session in a file dated `session` has ended (plus settle time) at `now`, else when it does."""
    ends = pd.Timestamp.combine(pd.Timestamp(session).date() + pd.Timedelta(days=1), end).tz_localize(TZ)
    ends += pd.Timedelta(minutes=settle_minutes)
    return None if now >= ends else ends


# ---------------------------------------------------------------------------
# One session (runs in a worker process)


def _build_session(job: dict) -> dict:
    """Read one contract session, write its Parquet files (read-only), return manifest rows and its quality row."""
    files: dict[str, Path] = job["files"]
    try:
        raws = {}
        for kind, path in files.items():
            if path.stat().st_size == 0:
                raws[kind] = None
            else:
                raws[kind] = read_records(path) if kind == "minute" else read_ticks(path)
        hashes = {kind: sha256(path) for kind, path in files.items()}
    except (CorruptFileError, OSError) as e:
        return {"skip": str(e)}
    root, sym, contract, session = Path(job["root"]), job["symbol"], job["contract"], job["session"]
    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows, written = [], 0
    if all(r is None for r in raws.values()):
        res = None
    else:
        res = session_bars(raws.get("minute"), raws.get("tick"), contract, job["tolerance_pct"], job["min_minute_match"], job["bars"])
    outputs = {}
    if res is not None and len(res.bars):
        outputs["minute"] = (root / sym / "minute" / contract / f"{session}.parquet", res.bars)
    if res is not None and res.ticks is not None and len(res.ticks):
        outputs["tick"] = (root / sym / "tick" / contract / f"{session}.parquet", res.ticks)
    for kind, (out, df) in outputs.items():
        _make_writable(out)  # an orphan from an interrupted build, or an explicit rebuild
        write_parquet(df, out, compression="zstd", index=False)
        os.chmod(out, stat.S_IREAD)
        written += 1
    for kind, path in files.items():
        st = path.stat()
        out = outputs.get(kind)
        rows.append({
            "symbol": sym, "kind": kind, "contract": contract, "session": session, "source": job["rel"][kind],
            "bytes": str(st.st_size), "mtime_ns": str(st.st_mtime_ns), "sha256": hashes[kind], "built_at": built_at,
            "output": _rel(out[0], root) if out else "", "rows": str(len(out[1])) if out else "0",
            "bar_source": res.quality["bar_source"] if (kind == "minute" and res is not None) else "",
        })  # fmt: skip
    quality = None
    if res is not None:
        quality = {"session": session, "trade_date": (pd.Timestamp(session) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
                   "contract": contract, **res.quality}  # fmt: skip
    return {"rows": rows, "quality": quality, "written": written}


# ---------------------------------------------------------------------------
# Roll calendar and quality flags


def roll_calendar(quality: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """(roll rows, front contract per session) from each contract session's minute volume, by Guban's volume rule
    (guban.data.importer.choose_contracts): a session trades the contract with the most volume in the previous
    session, never rolling back; when the front contract has no file in a session, the next contract takes over in
    that session."""
    if quality.empty:
        return pd.DataFrame(columns=ROLL_COLUMNS), pd.Series(dtype=str)
    # a session whose minute file holds no volume (GC/BTC 2026-08-18..24) counts its tick volume
    q = quality.assign(roll_volume=quality["minute_volume"].where(quality["minute_volume"] > 0, quality["tick_volume"]).fillna(0).astype("int64"))
    vol = q.pivot_table(index="session", columns="contract", values="roll_volume", aggfunc="sum", fill_value=0).sort_index()
    front = choose_contracts(vol)
    rows, sessions = [], list(front.index)
    for i in range(1, len(sessions)):
        old, new = front.iat[i - 1], front.iat[i]
        if new != old:
            s = sessions[i]
            reason = "front has no file" if vol.at[s, old] == 0 else "volume"
            rows.append({"old_contract": old, "new_contract": new, "decision_session": sessions[i - 1], "switch_session": s,
                         "switch_trade_date": (pd.Timestamp(s) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"), "reason": reason})  # fmt: skip
    return pd.DataFrame(rows, columns=ROLL_COLUMNS), front


_INTS = ["minute_volume", "tick_volume", "bars", "minute_bars_in_tick_session"]
_FLOATS = ["diff_pct", "minute_match", "ohlc_match"]


def _typed(quality: pd.DataFrame) -> pd.DataFrame:
    """Fixed column types, so quality.csv reads back and rewrites to the same text (nullable ints stay ints)."""
    q = quality.copy()
    for c in _INTS:
        q[c] = pd.to_numeric(q[c]).astype("Int64")
    for c in _FLOATS:
        q[c] = pd.to_numeric(q[c]).astype(float)
    for c in ("session", "trade_date", "contract", "bar_source", "reason"):
        q[c] = q[c].fillna("").astype(str)
    if "tick_ok" in q:
        q["tick_ok"] = q["tick_ok"].astype("boolean")
    return q


def read_quality(path: Path) -> pd.DataFrame:
    if not Path(path).exists():
        return pd.DataFrame(columns=QUALITY_COLUMNS)
    q = pd.read_csv(path, dtype={"session": str, "contract": str, "reason": str}, keep_default_na=False,
                    na_values={c: [""] for c in _INTS + _FLOATS + ["tick_ok"]})  # fmt: skip
    return _typed(q.reindex(columns=QUALITY_COLUMNS))


def _flag(quality: pd.DataFrame, front: pd.Series, cfg: StoreConfig) -> pd.DataFrame:
    """front, thin, and tick_ok: the session's ticks pass the tick check (total within tolerance_pct, >= min_minute_match
    of minutes equal) -- empty when there is no tick or no minute volume to check against. Tick consumers use tick_ok."""
    q = quality.copy()
    checked = (q["tick_volume"].fillna(0) > 0) & (q["minute_volume"].fillna(0) > 0)
    ok = (q["diff_pct"].abs() <= cfg.tolerance_pct) & (q["minute_match"] >= cfg.min_minute_match)
    q["tick_ok"] = pd.array([bool(o) if c else pd.NA for o, c in zip(ok.fillna(False), checked)], dtype="boolean")
    q["front"] = [front.get(s) == c for s, c in zip(q["session"], q["contract"])]
    q["thin"] = False
    f = q[q["front"]].sort_values("session")
    vol = f["minute_volume"].where(f["minute_volume"] > 0, f["tick_volume"]).astype(float).fillna(0).to_numpy()
    ref = pd.Series(vol).rolling(20, min_periods=5).median().shift().to_numpy()
    q.loc[f.index, "thin"] = np.nan_to_num(vol < cfg.thin_ratio * ref, nan=0).astype(bool)  # no reference yet: not thin
    return q


# ---------------------------------------------------------------------------
# Build


def build_store(
    cfg: StoreConfig,
    symbols: list[str] | None = None,
    rebuild: list[str] = (),
    now: pd.Timestamp | None = None,
    workers: int | None = None,
    progress=None,
) -> list[SymbolReport]:
    """Add every finished, whole-record session of `symbols` (default: all in cfg) to the store; see the module docs.
    rebuild: stored sessions to build again from their current source files -- "SYM:YYYYMMDD", "SYM:changed" (every
    session with a changed or added source file) or "SYM:all" (every session with a source file). A stored file whose
    source no longer exists is kept as stored. progress(symbol, done, total)."""
    now = pd.Timestamp.now(tz=TZ) if now is None else now
    symbols = list(cfg.symbols) if not symbols else symbols
    for s in symbols:
        if s not in cfg.symbols:
            raise KeyError(f"unknown store symbol {s!r}; config/store.toml lists {list(cfg.symbols)}")
    redo: dict[str, set[str]] = {}
    for r in rebuild:
        sym, sep, what = r.partition(":")
        if not sep or sym not in cfg.symbols or not ((what.isdigit() and len(what) == 8) or what in REBUILD_WORDS):
            raise ValueError(f"--rebuild {r!r}: use SYM:YYYYMMDD, SYM:changed or SYM:all, with SYM one of {list(cfg.symbols)}")
        redo.setdefault(sym, set()).add(what)
    root, source = Path(cfg.root), Path(cfg.source)
    if not source.is_dir():
        raise FileNotFoundError(f"DeepCharts folder not found: {source} (config/store.toml source, or --source)")
    root.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(root)
    reports = []
    for sym in symbols:
        rep = _build_symbol(cfg, sym, manifest, redo.get(sym, set()), now, workers, progress)
        manifest = rep.pop("manifest")
        reports.append(rep["report"])
    readme = README_TEXT.format(cfg=cfg, symbols=", ".join(f"{k} (DeepCharts {v})" for k, v in cfg.symbols.items()))
    reports[-1].files_written += _write_if_changed(root / "README.md", readme)
    return reports


def _build_symbol(cfg, sym, manifest, redo, now, workers, progress) -> dict:
    t0 = _time.perf_counter()
    root, base = Path(cfg.root), Path(cfg.source)
    folder = base / cfg.symbols[sym]
    rep = SymbolReport(sym)
    qpath = root / sym / "quality.csv"
    quality = read_quality(qpath)
    mine = manifest[manifest["symbol"] == sym].set_index("source")
    stored_sessions = {(c, s) for c, s in zip(mine["contract"], mine["session"])}
    sessions = _source_sessions(folder) if folder.is_dir() else {}
    rep.folder_missing = not folder.is_dir()
    rep.sessions = len(sessions)
    rep.gone = len(stored_sessions - set(sessions))
    refreshed: dict[str, str] = {}  # source -> new mtime_ns, for sources whose content (sha256) is unchanged
    jobs = []
    for (contract, session), files in sessions.items():
        key = f"{contract}/{session}"
        rel = {k: _rel(p, base) for k, p in files.items()}
        replace = []  # manifest sources this build replaces (a rebuild)
        kept_tick = False
        if (contract, session) in stored_sessions:
            notes = []
            for kind, path in files.items():
                if rel[kind] not in mine.index:
                    notes.append(f"{kind} file added after the build")
                    continue
                row, st = mine.loc[rel[kind]], path.stat()
                if (str(st.st_size), str(st.st_mtime_ns)) != (row["bytes"], row["mtime_ns"]):
                    if str(st.st_size) == row["bytes"] and sha256(path) == row["sha256"]:
                        refreshed[rel[kind]] = str(st.st_mtime_ns)  # same content, new timestamp
                    else:
                        notes.append(f"{kind} file changed ({row['bytes']} -> {st.st_size} bytes)")
            gone = set(mine.index[(mine["contract"] == contract) & (mine["session"] == session)]) - set(rel.values())
            again = "all" in redo or session in redo or ("changed" in redo and bool(notes))
            if not again:
                notes += [f"source removed: {g}" for g in sorted(gone)]
                if any(mine.loc[r, "output"] for r in rel.values() if r in mine.index):
                    rep.already += 1
                else:
                    rep.empty += 1
                if notes:
                    rep.changed.append((key, "; ".join(notes)))
                continue
            replace = [r for r in rel.values() if r in mine.index]  # outputs whose source is gone stay as stored
            kept_tick = any("/Tick/" in g for g in gone)
        waits = _session_over(session, now, cfg.session_end, cfg.settle_minutes)
        if waits is not None:
            rep.skipped.append((key, f"in progress (built after {waits:%Y-%m-%d %H:%M} ET)"))
            continue
        bad = []
        for kind, path in files.items():
            st = path.stat()
            size = KINDS[kind][1].itemsize
            if st.st_size % size:
                bad.append(f"{kind} file: {st.st_size:,} bytes is not a whole number of {size}-byte records")
            age = _time.time() - st.st_mtime
            if age < cfg.settle_minutes * 60:
                bad.append(f"{kind} file modified {age / 60:.0f} min ago (waits {cfg.settle_minutes:g} min)")
        if bad:
            rep.skipped.append((key, "; ".join(bad)))
            continue
        jobs.append({"root": str(root), "symbol": sym, "contract": contract, "session": session, "files": files, "rel": rel,
                     "tolerance_pct": cfg.tolerance_pct, "min_minute_match": cfg.min_minute_match, "bars": cfg.bars,
                     "replace": replace, "kept_tick": kept_tick})  # fmt: skip

    new_rows, new_quality = [], []

    replaced: set[str] = set()
    redone: dict[tuple[str, str], bool] = {}  # rebuilt (session, contract) -> its stored tick file is kept

    def collect(job, res):
        if "skip" in res:
            rep.skipped.append((f"{job['contract']}/{job['session']}", res["skip"]))
            return
        new_rows.extend(res["rows"])
        rep.files_written += res["written"]
        if job["replace"]:
            replaced.update(job["replace"])
            redone[(job["session"], job["contract"])] = job["kept_tick"]
            rep.rebuilt += 1
        if res["quality"] is None:
            rep.empty += 1
        else:
            new_quality.append(res["quality"])
            rep.built += 1
            rep.tick_built += res["quality"]["bar_source"] == "tick"

    if workers == 1 or len(jobs) <= 1:
        for i, job in enumerate(jobs):
            collect(job, _build_session(job))
            if progress:
                progress(sym, i + 1, len(jobs))
    else:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            for i, (job, res) in enumerate(zip(jobs, pool.map(_build_session, jobs, chunksize=4))):
                collect(job, res)
                if progress:
                    progress(sym, i + 1, len(jobs))

    if refreshed:
        hit = manifest["source"].isin(set(refreshed))
        manifest.loc[hit, "mtime_ns"] = manifest.loc[hit, "source"].map(refreshed)
    if replaced:
        manifest = manifest[~manifest["source"].isin(replaced)]
    if new_rows:
        manifest = pd.concat([manifest, pd.DataFrame(new_rows, columns=MANIFEST_COLUMNS)], ignore_index=True)
    manifest = manifest.sort_values(["symbol", "session", "contract", "kind"], kind="stable").reset_index(drop=True)
    if new_quality:
        new = _typed(pd.DataFrame(new_quality).reindex(columns=QUALITY_COLUMNS))
        if redone and not quality.empty:
            # a rebuilt session whose tick file is gone from the source keeps its stored ticks and their check
            old = quality.drop_duplicates(["session", "contract"]).set_index(["session", "contract"])
            for i, r in new.iterrows():
                k = (r["session"], r["contract"])
                if redone.get(k) and k in old.index and pd.isna(r["tick_volume"]):
                    for c in ("tick_volume", "diff_pct", "minute_match", "ohlc_match"):
                        new.at[i, c] = old.at[k, c]
            quality = quality[[k not in redone for k in zip(quality["session"], quality["contract"])]]
        quality = new if quality.empty else pd.concat([quality, new], ignore_index=True)
    quality = quality.sort_values(["session", "contract"], kind="stable").reset_index(drop=True)
    rolls, front = roll_calendar(quality)
    rep.rolls = len(rolls)
    if len(quality):
        quality = _typed(_flag(quality, front, cfg)[QUALITY_COLUMNS])
        rep.files_written += _write_if_changed(qpath, _csv_text(quality))
        rep.files_written += _write_if_changed(root / sym / "roll_calendar.csv", _csv_text(rolls))
    rep.files_written += _write_if_changed(root / "manifest.csv", _csv_text(manifest[MANIFEST_COLUMNS]))
    rep.seconds = _time.perf_counter() - t0
    return {"report": rep, "manifest": manifest}


# ---------------------------------------------------------------------------
# Verify


@dataclass
class VerifyResult:
    symbol: str
    sessions: int = 0
    tick_sessions: int = 0
    minutes: int = 0  # minutes compared (volume: in either; OHLC: in both)
    volume_equal: int = 0
    ohlc_minutes: int = 0
    ohlc_equal: int = 0
    volume_target: float = 0.99
    minute_sessions: int = 0  # minute-built sessions compared with their source minute file (must be identical)
    sources_gone: int = 0  # manifest sources no longer in the DeepCharts folder (their stored files are kept)
    not_compared: int = 0  # sessions whose source is gone or changed since the build
    problems: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)

    @property
    def volume_match(self) -> float:
        return self.volume_equal / self.minutes if self.minutes else 1.0

    @property
    def ohlc_match(self) -> float:
        return self.ohlc_equal / self.ohlc_minutes if self.ohlc_minutes else 1.0

    @property
    def passed(self) -> bool:
        return self.volume_match >= self.volume_target and self.ohlc_match >= OHLC_TARGET and not self.problems


def verify_store(cfg: StoreConfig, symbols: list[str] | None = None, progress=None) -> list[VerifyResult]:
    """Per symbol: every manifest output exists, is read-only and holds the recorded row count; every minute-built
    session equals its source minute records; tick-built sessions meet SR3 (volume per minute, OHLC per minute).
    Sessions whose source is gone or changed since the build are counted, not compared."""
    root, base = Path(cfg.root), Path(cfg.source)
    manifest = read_manifest(root)
    out = []
    for sym in symbols or list(cfg.symbols):
        if sym not in cfg.symbols:
            raise KeyError(f"unknown store symbol {sym!r}; config/store.toml lists {list(cfg.symbols)}")
        v = VerifyResult(sym, volume_target=cfg.min_minute_match)
        mine = manifest[manifest["symbol"] == sym]
        if mine.empty:
            v.problems.append("nothing stored; run `guban store build`")
            out.append(v)
            continue
        current = set()  # sources unchanged since the build
        for r in mine.itertuples(index=False):
            src = base / r.source
            if not src.exists():
                v.sources_gone += 1
            else:
                st = src.stat()
                if (str(st.st_size), str(st.st_mtime_ns)) != (r.bytes, r.mtime_ns):
                    v.changed.append(f"{r.source}: size/mtime changed since the build")
                else:
                    current.add(r.source)
            if r.output:
                p = root / r.output
                if not p.exists():
                    v.problems.append(f"{r.output}: missing")
                elif os.access(p, os.W_OK):
                    v.problems.append(f"{r.output}: not read-only")
                elif pq.read_metadata(p).num_rows != int(r.rows):
                    v.problems.append(f"{r.output}: {pq.read_metadata(p).num_rows} rows, manifest says {r.rows}")
        minute_rows = mine[(mine["kind"] == "minute") & (mine["output"] != "")]
        v.sessions = len(minute_rows)
        for i, r in enumerate(minute_rows.itertuples(index=False)):
            if r.source not in current:
                v.not_compared += 1
                continue
            stored = pd.read_parquet(root / r.output)
            rec = minute_record_bars(read_records(base / r.source), r.contract)
            ve, n, oe, b = match_counts(rec, stored)
            if r.bar_source == "tick":
                v.tick_sessions += 1
                v.volume_equal, v.minutes, v.ohlc_equal, v.ohlc_minutes = v.volume_equal + ve, v.minutes + n, v.ohlc_equal + oe, v.ohlc_minutes + b
            else:
                v.minute_sessions += 1
                if not (ve == n == len(stored) == len(rec) and oe == b):
                    v.problems.append(f"{r.output}: differs from its source minute records ({n - ve} minutes)")
            if progress:
                progress(sym, i + 1, len(minute_rows))
        out.append(v)
    return out


README_TEXT = """\
# Market Data store

Shared, write-once market data for Guban (`D:\\Guban Backtester`) and the Research Pipeline (`D:\\Research Pipeline`).
Built by `guban store build` from the DeepCharts folder `{cfg.source}` (opened read-only); checked by
`guban store verify`. Settings: `D:\\Guban Backtester\\config\\store.toml`. Do not edit files here by hand.

## Layout

```
manifest.csv                                    one row per stored source file (see below)
<SYM>/minute/<contract>/<YYYYMMDD>.parquet      1-minute bars of one contract session
<SYM>/tick/<contract>/<YYYYMMDD>.parquet        every trade record of one contract session
<SYM>/roll_calendar.csv                         volume roll (front contract changes)
<SYM>/quality.csv                               one row per contract session: volumes, match rates, bar source, flags
```

`<YYYYMMDD>` is the DeepCharts file date: the session that opens at 18:00 New York time on that date. Its CME trade
date is the next calendar day. Symbols: {symbols}.

## Columns

minute: `ts_utc` (bar OPEN time, UTC; the bar is known at ts_utc + 1 minute), `open, high, low, close`, `volume`,
`ask_vol` (buy-aggressor volume), `bid_vol` (sell-aggressor volume), `contract` (YYYYMM), `source` (`tick` or `minute`).
Prices are raw (unadjusted).

tick: `ts_utc` (UTC, ms), `price`, `size`, `side` (+1 at the ask = buy aggressor, -1 at the bid, 0 between),
`seq` (raw cumulative session volume; unreliable from 2026-05-26 11:23 ET, when the feed changed), `contract`.
Every record is kept in file order, including size-0 records (from 2026-05-26).

## Rules

- **Bar source** (`bars = "{cfg.bars}"` in store.toml): **1-minute bars come from the DeepCharts minute records**
  whenever a session's minute file holds volume (user, 2026-10-07: use 1-minute bars; ticks only for strategies that
  need them). Only a session with no minute volume is built from its ticks (`source` = `tick`). Minute records:
  minutes whose records hold no volume (placeholders) are dropped, and all records of a minute with volume are
  merged in file order; the session's opening minute is stored as four records (open with volume 0, high, low,
  close), so its zero-volume record is the true open. Tick bars: records by minute; O first, H max, L min, C last
  over every record (size-0 records of the 2026-05-26+ feed count for prices, as in DeepCharts' own minute bars),
  volume = sum of size. Sessions stored before 2026-10-07 whose minute file no longer exists (MNQ, GC) keep their
  tick-built bars.
- **Ticks**: stored for every session that has a tick file. Use a session's ticks only where quality.csv
  `tick_ok` is True: the tick file sums to within {cfg.tolerance_pct:g}% of the minute volume and at least
  {cfg.min_minute_match:.0%} of its minutes have exactly the minute record's volume. Empty `tick_ok` = no minute data to check
  against.
- **Write-once**: a session is stored once it has ended (18:00 New York the day after its file date, plus
  {cfg.settle_minutes:g} min) and its files are whole records; files are read-only. A later change to a stored source is
  reported by the build, never applied, unless rebuilt with `guban store build --rebuild SYM:YYYYMMDD` (or
  `SYM:changed`, `SYM:all`). A stored file whose source has since disappeared from the DeepCharts folder is kept.
- **Roll calendar**: a session trades the contract with the most volume (minute volume; tick volume when the
  minute file holds none, e.g. GC/BTC 2026-08-18..24) in the previous session, never rolling
  back; when the front contract has no file in a session, the next contract takes over in that session
  (`reason` = `front has no file`). `switch_session` is the first session of the new contract.
- **quality.csv**: `diff_pct` = tick vs minute volume, `minute_match` / `ohlc_match` = share of minutes whose tick
  bar matches the minute record, `tick_ok` = the ticks pass that check, `bar_source` / `reason` = what built the bars
  and any tick problem, `front` = the roll calendar's contract, `thin` = a front session with under
  {cfg.thin_ratio:g}x the median volume of the previous 20 front sessions (e.g. missing front-month files).

## Known source facts

- BTC trades 24/7 since 2026-05-29 (CME): weekend files and bars up to 17:59 New York exist from then on.
- Tick `bid`/`ask` are a side marker, not a real quote, before 2025-11.
- Tick files have defective sessions (tick_ok False): every NQ/ES session of 2024-01 (~1.2x volume), 2024-02-08,
  2024-02-11, 2024-06-03 (~3x), sessions cut at 09:29:59, GC from 2025-11 (~2% short), and many 2026-05..09 sessions.
- DeepCharts was reinstalled on 2026-10-07. Afterwards the folder held NQ, ES and BTC only: minute data from
  2020-01-01, NQ ticks from 2024-01-01, no ES ticks. MNQ, GC, ES ticks, NQ ticks before 2024 and NQ minutes before
  2020 exist only in this store. The reinstall filled ES 202609 for 2026-08-16..09-13 (before, the front month was
  missing there).
- Trade date 2020-11-30 holds ~1-2% of normal volume on NQ and ES (a source hole).
- MNQ files stop at session 20260916.

## manifest.csv

`symbol, kind (minute|tick), contract, session, source` (path under the DeepCharts folder), `bytes, mtime_ns,
sha256` (of the source when built), `built_at` (UTC), `output` (path under this folder; empty for a 0-byte source),
`rows`, `bar_source` (minute rows).
"""
