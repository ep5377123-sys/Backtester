"""Strategy tab: load a strategy file, edit its inputs, apply it to the Data tab's dataset and look at what it did
(entry / exit counts, every trade, a candlestick chart with the entries, exits and the strategy's `plots` lines).

Apply runs the backtest with the contract's default costs and 1-minute magnifier fills, on the current dataset's
window: the full range is exactly `guban run`; an in-sample / out-of-sample window [lo, hi) is
run_backtest(bars[:hi], trade_from=lo, window_ends=(hi,)).window(lo, hi): flat at its start, a position still open at
its end closed there ("Window End"), as metrics.window_metrics and the walk-forward score windows.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from guban.gui.handlers import State, method
from guban.gui.tab_data import DATA_DIR, DATASETS, Loaded, loaded

STRATEGIES = Path("strategies")
CHART_BARS = 500
MAX_CHART_BARS = 5000


@dataclass
class Applied:
    """One Apply: the run and what it ran on (State.applied)."""

    cls: type
    file: str
    inputs: dict
    dataset: str
    lo: int
    hi: int
    bars: pd.DataFrame
    result: object
    spec: object
    commission: float
    slippage: float
    fill_mode: str
    plots: dict = field(default_factory=dict)
    seconds: float = 0.0


def _strategy(state: State):
    if state.strategy is None:
        raise ValueError("No strategy loaded: pick a strategy file and click Load")
    return state.strategy


def applied(state: State) -> Applied:
    if state.applied is None:
        raise ValueError("No strategy applied: open the Strategy tab and click Apply")
    return state.applied


def _input_kind(v) -> str:
    if isinstance(v, (bool, np.bool_)):
        return "bool"
    if isinstance(v, (int, np.integer)):
        return "int"
    if isinstance(v, (float, np.floating)):
        return "float"
    return "str" if isinstance(v, str) else "other"


def coerce_inputs(cls, raw: dict) -> dict:
    """Form values (text, or true/false for checkboxes) -> input overrides typed like each input's default."""
    from guban.optimize import _coerce

    unknown = sorted(set(raw) - set(cls.inputs))
    if unknown:
        raise ValueError(f"{cls.__name__} has no input {', '.join(unknown)}; known: {', '.join(sorted(cls.inputs))}")
    return {k: _coerce(str(v), cls.inputs[k], f"{k}={v}", what="input") for k, v in raw.items()}


def bars_for(d: Loaded, extra: tuple[str, ...]) -> pd.DataFrame:
    """The loaded bars with a strategy's extra columns (built once per column set)."""
    if not extra:
        return d.bars
    key = tuple(sorted(extra))
    cache = d.__dict__.setdefault("_extra", {})
    if key not in cache:
        from guban.data.features import add_columns

        cache[key] = add_columns(d.bars, tuple(extra), d.symbol, d.tf, DATA_DIR)
    return cache[key]


def sub_bars(d: Loaded) -> pd.DataFrame:
    """1-minute bars for magnifier fills over the loaded range (loaded once)."""
    if d.sub is None:
        from guban.data.resample import load_bars

        sub = load_bars(d.symbol, "1m", d.session, DATA_DIR, tz=d.tz)
        d.sub = sub[(sub.index > d.bars.index[0] - pd.Timedelta(days=1)) & (sub.index <= d.bars.index[-1])]
    return d.sub


def run_window(state: State, cls, inputs: dict, commission: float | None = None, slippage: float | None = None,
               fill_mode: str = "magnifier", emit=None) -> Applied:
    """Backtest `cls(**inputs)` on the Data tab's current dataset window."""
    from guban.cli import _costs
    from guban.config import get_contract
    from guban.engine import Costs, StrategyError, run_backtest

    d = loaded(state)
    if fill_mode not in ("magnifier", "ohlc"):
        raise ValueError(f"fill mode must be magnifier or ohlc, got {fill_mode!r}")
    try:
        strategy = cls(**inputs)
    except Exception as e:
        raise StrategyError(f"{cls.__name__}({', '.join(f'{k}={v!r}' for k, v in inputs.items())}) could not be created: {e!r}") from e
    spec = get_contract(d.symbol)
    commission, slippage, _ = _costs(spec, commission, slippage)
    lo, hi = d.bounds()
    if emit:
        emit("progress", message="Preparing bars…")
    bars = bars_for(d, tuple(cls.extra_columns)).iloc[:hi]
    sub = sub_bars(d) if fill_mode == "magnifier" else None
    if emit:
        emit("progress", message=f"Backtesting {hi - lo:,} bars ({DATASETS[d.dataset].lower()})…")
    t0 = time.perf_counter()
    windowed = d.dataset != "full"
    res = run_backtest(strategy, bars, spec, Costs(commission=commission, slippage=slippage), sub_bars=sub, fill_mode=fill_mode,
                       trade_from=lo, window_ends=(hi,) if windowed else ())
    if windowed:
        res = res.window(lo, hi)
    seconds = time.perf_counter() - t0
    return Applied(cls, getattr(cls, "_studio_file", ""), vars(strategy.p), d.dataset, lo, hi, bars, res, spec, commission, slippage,
                   fill_mode, strategy.plot_series(), seconds)


def counts(a: Applied) -> list[list]:
    tr = a.result.trades
    entries = tr.drop_duplicates(["entry_bar", "entry_time", "direction"]) if len(tr) else tr
    long_e = int((entries["direction"] == "long").sum()) if len(tr) else 0
    long_x = int((tr["direction"] == "long").sum()) if len(tr) else 0
    rows = [["Bars", a.hi - a.lo], ["Long entries", long_e], ["Short entries", len(entries) - long_e],
            ["Long exits", long_x], ["Short exits", len(tr) - long_x], ["Trades (exit fills)", len(tr)],
            ["Net profit (default costs)", round(a.result.net_profit, 2)]]
    return rows


TRADE_TABLE = ["#", "side", "qty", "entry time", "entry", "entry name", "exit time", "exit", "exit name", "net P&L", "cum net", "R"]


def trade_table(a: Applied) -> dict:
    tr = a.result.trades
    fmt = lambda t: f"{t:%Y-%m-%d %H:%M}"
    rows = [[int(t.trade_no), t.direction.capitalize(), int(t.qty), fmt(t.entry_time), t.entry_price, t.entry_name,
             fmt(t.exit_time), t.exit_price, t.exit_name, round(t.net_pnl, 2), round(t.cum_net_pnl, 2),
             None if pd.isna(t.r_multiple) else round(t.r_multiple, 2)] for t in tr.itertuples()]
    bars = [[int(t.entry_bar) - a.lo, int(t.exit_bar) - a.lo] for t in tr.itertuples()]
    return {"columns": TRADE_TABLE, "rows": rows, "bars": bars}


def price_chart(a: Applied, start: int, count: int, title: str) -> dict:
    from guban.charts import styled

    n = a.hi - a.lo
    count = max(20, min(int(count), MAX_CHART_BARS))
    start = max(0, min(int(start), max(0, n - count)))
    return {"image": styled(_price_chart)(a, a.lo + start, min(a.hi, a.lo + start + count), title), "start": start, "count": count, "total": n}


def _price_chart(a: Applied, i0: int, i1: int, title: str) -> str:
    from matplotlib.collections import LineCollection, PolyCollection
    from matplotlib.lines import Line2D
    from matplotlib.ticker import FuncFormatter

    from guban.charts import COLORS, SERIES, figure, to_data_uri

    seg = a.bars.iloc[i0:i1]
    m = len(seg)
    x = np.arange(m)
    o, h, lo, c = (seg[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    up = c >= o
    col = np.where(up, COLORS["up"], COLORS["down"])
    fig, ax = figure(9.6, 4.6)
    ax.grid(axis="x", visible=False)
    ax.add_collection(LineCollection(np.stack([np.c_[x, lo], np.c_[x, h]], axis=1), colors=col, linewidths=0.8, zorder=1))
    w = 0.34
    b0, b1 = np.minimum(o, c), np.maximum(o, c)
    ax.add_collection(PolyCollection(np.stack([np.c_[x - w, b0], np.c_[x + w, b0], np.c_[x + w, b1], np.c_[x - w, b1]], axis=1),
                                     facecolors=col, edgecolors=col, linewidths=0.6, zorder=2))
    handles = []
    for k, (name, values) in enumerate(a.plots.items()):
        y = np.asarray(values[i0:i1], float)
        (line,) = ax.plot(x, y, color=SERIES[(k + 1) % len(SERIES)], linewidth=1.3, zorder=3, label=name)
        handles.append(line)
    tr = a.result.trades
    if len(tr):
        tr = tr[(tr["entry_bar"] < i1) & (tr["exit_bar"] >= i0)]
    for t in tr.itertuples():
        ei, xi = t.entry_bar - i0, t.exit_bar - i0
        ax.plot([ei, xi], [t.entry_price, t.exit_price], color=COLORS["ink2"], linewidth=0.9, linestyle="--", alpha=0.7, zorder=4)
        if 0 <= ei < m:
            long_ = t.direction == "long"
            ax.scatter([ei], [t.entry_price], marker="^" if long_ else "v", s=70, zorder=5, linewidths=0.8,
                       color=COLORS["up"] if long_ else COLORS["down"], edgecolors=COLORS["ink"])
        if xi < m:
            ax.scatter([xi], [t.exit_price], marker="X", s=60, color=COLORS["s2"], edgecolors=COLORS["surface"], linewidths=0.6, zorder=5)
    handles += [Line2D([], [], marker="^", ls="", color=COLORS["up"], label="Long entry"),
                Line2D([], [], marker="v", ls="", color=COLORS["down"], label="Short entry"),
                Line2D([], [], marker="X", ls="", color=COLORS["s2"], label="Exit")]
    ax.legend(handles=handles, loc="upper left", ncols=len(handles), fontsize=8)
    ymin, ymax = lo.min(), h.max()
    for values in a.plots.values():
        y = np.asarray(values[i0:i1], float)
        if np.isfinite(y).any():
            ymin, ymax = min(ymin, np.nanmin(y)), max(ymax, np.nanmax(y))
    pad = (ymax - ymin) * 0.06 or 1.0
    ax.set_ylim(ymin - pad, ymax + pad * 2.2)
    ax.set_xlim(-1, m)
    ticks = np.unique(np.linspace(0, m - 1, min(m, 9)).round().astype(int))
    ax.set_xticks(ticks)
    multi_day = seg.index[0].date() != seg.index[-1].date()
    ax.set_xticklabels([f"{t:%m-%d %H:%M}" if multi_day else f"{t:%H:%M}" for t in seg.index[ticks]])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.2f}".rstrip("0").rstrip(".")))
    ax.set_title(f"{title} · {seg.index[0]:%Y-%m-%d %H:%M} → {seg.index[-1]:%Y-%m-%d %H:%M}")
    return to_data_uri(fig)


def _title(a: Applied) -> str:
    return f"{a.cls.__name__} · {a.spec.symbol} · {DATASETS[a.dataset].lower()}"


@method("strategy.list")
def strategy_list(state: State, params: dict, emit) -> dict:
    files = sorted(STRATEGIES.glob("*.py"))
    return {"files": [{"file": p.as_posix(), "name": p.stem} for p in files if not p.name.startswith("_")]}


@method("strategy.load")
def strategy_load(state: State, params: dict, emit) -> dict:
    """Import (or re-import: Reload) a strategy file and describe its inputs."""
    from guban.engine.strategy import load_strategy_class

    path = Path(str(params.get("file", "")))
    if not path.is_file():
        raise FileNotFoundError(f"strategy file {path} not found: pick one from the list")
    cls = load_strategy_class(path, params.get("class") or None)
    cls._studio_file = path.as_posix()
    state.strategy = cls
    state.on_strategy_changed()
    doc = (cls.__doc__ or "").strip().splitlines()
    return {
        "class": cls.__name__,
        "file": path.as_posix(),
        "doc": doc[0] if doc else "",
        "inputs": [{"name": k, "kind": _input_kind(v), "value": (str(v).lower() if _input_kind(v) == "bool" else str(v) if _input_kind(v) != "other" else repr(v))}
                   for k, v in cls.inputs.items()],
        "plots": list(cls.plots),
        "extra_columns": list(cls.extra_columns),
    }


@method("strategy.apply")
def strategy_apply(state: State, params: dict, emit) -> dict:
    cls = _strategy(state)
    inputs = coerce_inputs(cls, params.get("inputs") or {})
    a = run_window(state, cls, inputs, fill_mode=str(params.get("fill_mode") or "magnifier"), emit=emit)
    state.applied = a
    state.on_applied()
    notes = []
    pos = a.result.open_position
    if pos is not None:
        notes.append(f"A position is still open at the last bar ({pos.direction:+d} x{pos.qty} from {pos.entry_time:%Y-%m-%d %H:%M}); it is not in the trade list.")
    if a.result.bars_without_sub_bars or a.result.bars_with_partial_sub_bars:
        notes.append(f"{a.result.bars_without_sub_bars + a.result.bars_with_partial_sub_bars:,} bars had no complete 1-minute data and were filled from their own OHLC.")
    lo_t, hi_t = a.bars.index[a.lo], a.bars.index[a.hi - 1]
    return {
        "class": cls.__name__,
        "inputs": a.inputs,
        "dataset": DATASETS[a.dataset],
        "window": f"{lo_t:%Y-%m-%d %H:%M} → {hi_t:%Y-%m-%d %H:%M}",
        "costs": f"commission ${a.commission:,.2f} + slippage ${a.slippage:,.2f} per contract round trip ({a.spec.symbol} defaults), {a.fill_mode} fills",
        "seconds": round(a.seconds, 2),
        "counts": counts(a),
        "trades": trade_table(a),
        "notes": notes,
        "chart": price_chart(a, params.get("start", 0), params.get("bars", CHART_BARS), _title(a)),
    }


@method("strategy.chart")
def strategy_chart(state: State, params: dict, emit) -> dict:
    a = applied(state)
    return price_chart(a, params.get("start", 0), params.get("bars", CHART_BARS), _title(a))
