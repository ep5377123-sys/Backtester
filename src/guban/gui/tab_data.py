"""Data tab: choose the bars (symbol, timeframe, session, time zone, dates), split them into an in-sample and an
out-of-sample window, and look at them (table pages, close-price chart, quality report).

Windows are calendar dates in the chart time zone, both ends included, matched against each bar's timestamp (the
same rule as --data-from). Later tabs run a window as run_backtest(bars[:hi], trade_from=lo, flatten_at_end=True):
the warm-up always starts at the first loaded bar, so an out-of-sample window before the in-sample one works.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from guban.gui.handlers import State, method

DATA_DIR = Path("data")
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "4h", "1d"]
TIMEZONES = ["America/New_York", "America/Chicago", "UTC"]
DATASETS = {"is": "In sample", "oos": "Out of sample", "full": "Full"}
DEFAULTS = {"symbol": "NQ", "tf": "15m", "session": "eth", "tz": "America/New_York", "oos_fraction": 0.3, "dataset": "is", "roll": "volume"}
PAGE = 500
CHART_POINTS = 4000


@dataclass
class Loaded:
    """The Data tab's bars and windows (State.data)."""

    symbol: str
    tf: str
    session: str
    tz: str
    bars: pd.DataFrame
    windows: dict[str, tuple[date, date]] = field(default_factory=dict)
    dataset: str = "is"
    sub: pd.DataFrame | None = None

    @property
    def first(self) -> date:
        return self.bars.index[0].date()

    @property
    def last(self) -> date:
        return self.bars.index[-1].date()

    @property
    def label(self) -> str:
        a, b = self.bars.index[0], self.bars.index[-1]
        return f"{self.symbol}_{self.tf}_{self.session}_{a:%Y%m%d_%H%M}_{b:%Y%m%d_%H%M}"

    def window(self, name: str | None = None) -> tuple[date, date]:
        name = name or self.dataset
        return (self.first, self.last) if name == "full" else self.windows[name]

    def bounds(self, name: str | None = None) -> tuple[int, int]:
        """Bar positions [lo, hi) of a window."""
        return window_bounds(self.bars.index, *self.window(name))


def window_bounds(index: pd.DatetimeIndex, start: date, end: date) -> tuple[int, int]:
    """[lo, hi) positions of the bars whose timestamp date (index time zone) lies in start..end."""
    tz = index.tz
    lo = int(index.searchsorted(pd.Timestamp(start).tz_localize(tz), "left"))
    hi = int(index.searchsorted(pd.Timestamp(end + timedelta(days=1)).tz_localize(tz), "left"))
    return lo, hi


def loaded(state: State) -> Loaded:
    if state.data is None:
        raise ValueError("No data loaded: open the Data tab and click Load")
    return state.data


def _date(text, what: str) -> date:
    try:
        return date.fromisoformat(str(text))
    except ValueError:
        raise ValueError(f"{what} must be a date YYYY-MM-DD, got {text!r}") from None


def _span(pair, what: str) -> tuple[date, date]:
    if not isinstance(pair, (list, tuple)) or len(pair) != 2:
        raise ValueError(f"{what} needs a start and an end date")
    a, b = _date(pair[0], f"{what} start"), _date(pair[1], f"{what} end")
    if a > b:
        raise ValueError(f"{what} starts {a} after it ends {b}: swap the dates")
    return a, b


def availability(symbol: str) -> dict:
    """First and last date of the imported 1-minute bars, or available=False."""
    import pyarrow.parquet as pq

    path = DATA_DIR / symbol / "1m.parquet"
    if not path.is_file():
        return {"available": False}
    t = pq.read_table(path, columns=["time"]).column("time")
    if not len(t):
        return {"available": False}
    first, last = (pd.Timestamp(t[i].as_py()).tz_convert("America/New_York") for i in (0, len(t) - 1))
    return {"available": True, "first": first.date().isoformat(), "last": last.date().isoformat()}


def split_by_fraction(first: date, last: date, fraction: float) -> dict[str, tuple[date, date]]:
    """In sample = the first (1 - fraction) of the calendar days first..last, out of sample = the rest."""
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) or not 0 < fraction < 1:
        raise ValueError(f"OOS size must be a fraction between 0 and 1 (e.g. 0.3), got {fraction!r}")
    days = (last - first).days + 1
    oos_days = max(1, round(days * fraction))
    if oos_days >= days:
        raise ValueError(f"OOS size {fraction} leaves no in-sample days in {first} → {last}: use a smaller fraction")
    oos_start = last - timedelta(days=oos_days - 1)
    return {"is": (first, oos_start - timedelta(days=1)), "oos": (oos_start, last)}


def check_windows(d: Loaded, windows: dict[str, tuple[date, date]]) -> None:
    names = {"is": "In sample", "oos": "Out of sample"}
    for key, (a, b) in windows.items():
        if a < d.first or b > d.last:
            raise ValueError(f"{names[key]} {a} → {b} reaches outside the loaded bars ({d.first} → {d.last}): change it or load more data")
        lo, hi = window_bounds(d.bars.index, a, b)
        if hi <= lo:
            raise ValueError(f"{names[key]} {a} → {b} holds no bars")
    (a, b), (c, e) = windows["is"], windows["oos"]
    if a <= e and c <= b:
        raise ValueError(f"In sample {a} → {b} and out of sample {c} → {e} overlap ({max(a, c)} → {min(b, e)}): move one of them")


def _windows_info(d: Loaded) -> dict:
    info = {}
    for key in ("is", "oos", "full"):
        a, b = d.window(key)
        lo, hi = d.bounds(key)
        info[key] = {"start": a.isoformat(), "end": b.isoformat(), "bars": hi - lo, "label": DATASETS[key]}
    return {"windows": info, "dataset": d.dataset}


@dataclass
class _Check:
    label: str
    status: str
    value: str


def quality_report(d: Loaded) -> list[dict]:
    """Checks on the loaded bars plus the store's quality.csv flags for the same dates."""
    from guban.metrics import contract_rolls, data_gaps

    b = d.bars
    n = len(b)
    out: list[_Check] = []
    add = lambda label, status, value: out.append(_Check(label, status, value))
    fmt = lambda t: f"{t:%Y-%m-%d %H:%M}"
    add("Period", "info", f"{fmt(b.index[0])} → {fmt(b.index[-1])} ({d.tz})")
    add("Bars", "info", f"{n:,} at {d.tf}, {d.session} session")
    per = b.groupby("session_date").size()
    typical = int(per.mode().iat[0])
    add("Sessions", "info", f"{len(per):,}; bars per session typical {typical}, min {per.min()}, max {per.max()}")
    short = per[per < typical]
    add("Short sessions", "info" if len(short) else "ok", f"{len(short):,}" + (f" (e.g. {', '.join(short.index[:3].strftime('%Y-%m-%d'))})" if len(short) else ""))
    ohlcv = ["open", "high", "low", "close", "volume"]
    missing = [c for c in ohlcv if c not in b.columns]
    add("Columns", "bad" if missing else "ok", f"missing {', '.join(missing)}" if missing else "open, high, low, close, volume")
    nan = int(b[[c for c in ohlcv if c in b.columns]].isna().any(axis=1).sum())
    add("NaN values", "bad" if nan else "ok", f"{nan:,} bars" if nan else "none")
    dup = int(b.index.duplicated().sum())
    add("Duplicate timestamps", "bad" if dup else "ok", f"{dup:,}" if dup else "none")
    add("Chronological", "ok" if b.index.is_monotonic_increasing else "bad", "yes" if b.index.is_monotonic_increasing else "no: bars out of order")
    o, h, lo, c = (b[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    bad_ohlc = int(((h < np.maximum(o, c)) | (lo > np.minimum(o, c)) | (h < lo)).sum())
    add("OHLC integrity", "bad" if bad_ohlc else "ok", f"{bad_ohlc:,} bars with high/low outside open/close" if bad_ohlc else "high ≥ open, close ≥ low on every bar")
    nonpos = int((np.c_[o, h, lo, c] <= 0).any(axis=1).sum())
    add("Non-positive prices", "bad" if nonpos else "ok", f"{nonpos:,} bars" if nonpos else "none")
    zero_vol = int((b["volume"] <= 0).sum())
    add("Zero-volume bars", "warn" if zero_vol else "ok", f"{zero_vol:,}" if zero_vol else "none")
    gaps = data_gaps(b)
    add("Data gaps", "warn" if gaps else "ok",
        f"{len(gaps):,}; first before {fmt(b.index[gaps[0]])}, last before {fmt(b.index[gaps[-1]])}" if gaps else "none")
    rolls = contract_rolls(b)
    add("Contract rolls", "info", f"{len(rolls)}" + (f"; last {fmt(rolls[-1][0])} {rolls[-1][1]} → {rolls[-1][2]}" if rolls else ""))
    try:
        from guban.store.read import front_sessions, store_root

        q = front_sessions(store_root(), d.symbol)
        td = pd.to_datetime(q["trade_date"]).dt.date
        q = q[(td >= d.first) & (td <= d.last)]
        add("Store sessions", "info", f"{len(q):,} front-contract sessions in the store for these dates")
        ok = int(q["tick_ok"].fillna(False).astype(bool).sum())
        add("Tick data usable", "info", f"{ok:,} of {len(q):,} sessions (tick_ok)")
        thin = q[q["thin"].astype(bool)]
        add("Thin sessions", "warn" if len(thin) else "ok",
            f"{len(thin):,} ({', '.join(thin['trade_date'].head(5))}{' …' if len(thin) > 5 else ''})" if len(thin) else "none")
    except (OSError, KeyError, ValueError) as e:
        add("Store quality.csv", "warn", f"not readable: {e}")
    return [vars(x) for x in out]


def close_chart(d: Loaded) -> str:
    """Close price over the loaded bars, in-sample and out-of-sample windows shaded."""
    from guban.charts import styled

    return styled(_close_chart)(d)


def _close_chart(d: Loaded) -> str:
    import matplotlib.dates as mdates
    from matplotlib.ticker import FuncFormatter

    from guban.charts import COLORS, figure, to_data_uri

    b = d.bars
    step = max(1, math.ceil(len(b) / CHART_POINTS))
    pos = np.r_[np.arange(0, len(b), step), len(b) - 1]
    x = b.index[pos].tz_localize(None).to_numpy()
    fig, ax = figure(7.6, 3.9)
    for key, color, alpha in (("is", COLORS["s1"], 0.12), ("oos", COLORS["s3"], 0.16)):
        if key in d.windows:
            a, e = d.windows[key]
            ax.axvspan(np.datetime64(a), np.datetime64(e + timedelta(days=1)), color=color, alpha=alpha, lw=0, label=DATASETS[key])
    ax.plot(x, b["close"].to_numpy(float)[pos], color=COLORS["up"], linewidth=1)
    loc = mdates.AutoDateLocator()
    ax.xaxis.set_major_locator(loc)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(loc))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))
    ax.set_title(f"Close price · {d.symbol} {d.tf} {d.session}")
    ax.set_xlim(x[0], x[-1])
    if d.windows:
        ax.legend(loc="upper left")
    return to_data_uri(fig)


def page(d: Loaded, offset: int = 0, limit: int = PAGE) -> dict:
    n = len(d.bars)
    offset = max(0, min(int(offset), max(0, n - 1)))
    rows = d.bars.iloc[offset : offset + int(limit)]
    cols = ["open", "high", "low", "close", "volume", "contract"]
    return {
        "offset": offset,
        "total": n,
        "columns": ["time", *cols],
        "rows": [[f"{t:%Y-%m-%d %H:%M}", *vals] for t, vals in zip(rows.index, rows[cols].astype(object).itertuples(index=False, name=None))],
    }


def _view(d: Loaded) -> dict:
    return {**_windows_info(d), "chart": close_chart(d)}


@method("data.options")
def options(state: State, params: dict, emit) -> dict:
    from guban.config import _contracts, _sessions

    return {
        "symbols": [{"symbol": s, "name": spec.name, **availability(s)} for s, spec in _contracts(None).items()],
        "sessions": [{"key": k, "name": s.name} for k, s in _sessions(None).items()],
        "timeframes": TIMEFRAMES,
        "timezones": TIMEZONES,
        "datasets": DATASETS,
        "defaults": DEFAULTS,
    }


@method("data.import")
def import_data(state: State, params: dict, emit) -> dict:
    """Rebuild data/<SYM>/1m.parquet from the shared store (guban import)."""
    from guban.config import get_contract
    from guban.store.read import import_from_store

    symbol = get_contract(str(params.get("symbol", ""))).symbol
    roll = str(params.get("roll", DEFAULTS["roll"]))
    emit("progress", message=f"Importing {symbol} from the store ({roll} roll)…")
    summary = import_from_store(symbol, DATA_DIR, None, roll)
    return {"summary": str(summary), **availability(symbol)}


def load_data(params: dict, emit) -> Loaded:
    """Bars and windows from the Data tab's form values (also how the optimization process rebuilds them)."""
    from guban.config import get_contract, get_session
    from guban.data.resample import parse_timeframe
    from guban.optimize import DataSpec

    symbol = get_contract(str(params.get("symbol", ""))).symbol
    tf = str(params.get("tf", DEFAULTS["tf"])).strip()
    parse_timeframe(tf)
    session = get_session(str(params.get("session", DEFAULTS["session"]))).key
    tz = str(params.get("tz", DEFAULTS["tz"]))
    try:
        ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown time zone {tz!r}: use e.g. America/New_York or America/Chicago") from None
    start, end = _span((params.get("start"), params.get("end")), "Data")
    if not (DATA_DIR / symbol / "1m.parquet").is_file():
        import_data(None, {"symbol": symbol, "roll": params.get("roll", DEFAULTS["roll"])}, emit)
    emit("progress", message=f"Loading {symbol} {tf} {session} bars…")
    bars, _ = DataSpec(symbol, tf, session, tz, fill_mode="ohlc", data_dir=str(DATA_DIR), data_from=start.isoformat()).load()
    bars = bars.iloc[: window_bounds(bars.index, start, end)[1]]
    if not len(bars):
        raise ValueError(f"{symbol} has no {tf} {session} bars from {start} to {end}: change the dates")
    d = Loaded(symbol, tf, session, tz, bars, dataset=str(params.get("dataset", DEFAULTS["dataset"])))
    if d.dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {', '.join(DATASETS)}, got {d.dataset!r}")
    saved = params.get("windows")
    try:
        if not saved:
            raise ValueError
        d.windows = {k: _span(saved.get(k), DATASETS[k]) for k in ("is", "oos")}
        check_windows(d, d.windows)
    except (ValueError, AttributeError):
        d.windows = split_by_fraction(d.first, d.last, float(params.get("oos_fraction", DEFAULTS["oos_fraction"])))
    return d


@method("data.load")
def load(state: State, params: dict, emit) -> dict:
    d = state.data = load_data(params, emit)
    state.on_data_changed()
    bars = d.bars
    return {"label": d.label, "bars": len(bars), "first": d.first.isoformat(), "last": d.last.isoformat(),
            "quality": quality_report(d), "page": page(d), **_view(d)}


@method("data.page")
def data_page(state: State, params: dict, emit) -> dict:
    d = loaded(state)
    offset = params.get("offset", 0)
    if params.get("goto"):
        offset = window_bounds(d.bars.index, _date(params["goto"], "Go to"), d.last)[0]
    return page(d, offset, params.get("limit", PAGE))


@method("data.split")
def data_split(state: State, params: dict, emit) -> dict:
    d = loaded(state)
    d.windows = split_by_fraction(d.first, d.last, params.get("fraction"))
    state.on_data_changed()
    return _view(d)


@method("data.windows")
def data_windows(state: State, params: dict, emit) -> dict:
    d = loaded(state)
    windows = {k: _span(params.get(k), DATASETS[k]) for k in ("is", "oos")}
    check_windows(d, windows)
    dataset = str(params.get("dataset", d.dataset))
    if dataset not in DATASETS:
        raise ValueError(f"dataset must be one of {', '.join(DATASETS)}, got {dataset!r}")
    if windows != d.windows or dataset != d.dataset:
        d.windows, d.dataset = windows, dataset
        state.on_data_changed()
    return _view(d)
