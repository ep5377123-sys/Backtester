"""Bar-by-bar backtest loop, order handling, intrabar fills, and position/trade ledger (PowerLanguage semantics).

For each strategy bar i:
  1. Orders placed on bar i-1 become active at bar i's open and live for bar i only. Market orders fill at
     the open, in placement order. Stop and limit orders, and the set_stop_loss / set_profit_target exits of
     the open position, are then checked along bar i's intrabar price path (below).
  2. Series point at bar i and on_bar() runs. The orders it places are for bar i+1 only.
  3. Equity (closed net P&L + open gross P&L) is marked at bar i's close.

Intrabar path. fill_mode="magnifier" walks bar i's 1-minute sub-bars in order; fill_mode="ohlc" treats bar i
as its own single sub-bar. Each sub-bar is walked open -> high -> low -> close when the high is at least as
near the open as the low (MC's rule; a tie goes high first), else open -> low -> high -> close. Along the path:
  - A buy stop / sell limit fires when price rises to its price; a buy limit / sell stop when price falls to
    it. A long's stop loss is a sell stop and its profit target a sell limit (mirrored for a short), priced
    at entry -/+ amount / (point value x contracts), rounded to the nearest tick.
  - An order already through the price at a sub-bar's open (a gap) fills at that open; otherwise it fills at
    its own price. Market and stop fills are moved by Costs.slippage_ticks; limit fills are not.
  - After every fill the orders are re-checked at the fill price. So a stop loss is live from the entry fill
    on, including the rest of the entry bar, and an entry blocked by the open position (no pyramiding) can
    still fill after that position exits in the same bar.
  - Fills due at the same moment go in placement order, then stop loss, then profit target.
A trade is tagged `ambiguous` when its exit was decided by the path rule rather than by the data: both a
stop-type and a limit-type exit lay inside the exit sub-bar's high-low range, or the position was entered
intrabar in that same sub-bar.

Magnifier coverage: a bar's sub-bars must reproduce its open, high, low and close. A bar whose sub-bars do not
(minutes missing from the 1-minute data) uses its own OHLC path instead, like a bar with no sub-bars, and is
counted in BacktestResult.bars_with_partial_sub_bars.

Run-up and drawdown (MC's per-trade columns) come from the highest and lowest prices the position saw along
that same path, from its entry fill to its exit fill, less the entry-side costs (half the commission, plus
half the USD slippage when the entry was a market or stop fill); run-up is floored at 0. MC reports them the
same way: a Vault Break MNQ profit-target trade shows run-up 498.9 = 500 - 1.10, one that never went green 0.

Intrabar equity. Equity (closed net + open gross) is followed along the same path, point by point in time
order (and right after each fill, where the costs land). Per bar: equity_low / equity_high are its lowest and
highest equity, and bar_drawdown the deepest drop below a peak reached earlier in that same bar. Together with
the prior bars' peaks they give the true peak-to-valley drawdown (guban.metrics.intraday_drawdown).

Position rules: one position at a time, no pyramiding (an entry in the current direction is ignored), an
opposite entry reverses (closes the whole position and opens the new one at the same fill), exits are ignored
unless they match the open position at the moment they would fill. A position still open after the last bar
stays open: it is in BacktestResult.open_position, not in trades (unless flatten_at_end).

Trading windows (walk-forward). trade_from=k: the strategy runs from the first bar (indicators warm up) but no
order fills before bar k, so the run is flat until then. flatten_at_end: the position still open after the
last bar's on_bar() is closed at that bar's close, as a market fill named "Window End". window_ends: for each
end h, BacktestResult.window(trade_from, h) is the run's result over bars [trade_from, h) as if it had been run
on bars[:h] with flatten_at_end, without running it again (the engine is causal).
"""

from __future__ import annotations

import copy
import math
import numbers
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import ContractSpec
from guban.engine.series import Clock
from guban.engine.strategy import BUY, SELL, SELL_SHORT, Strategy, StrategyError

__all__ = ["BacktestResult", "Costs", "Position", "StrategyError", "run_backtest"]

TRADE_COLUMNS = [
    "trade_no", "direction", "qty",
    "entry_time", "entry_price", "entry_name", "entry_bar",
    "exit_time", "exit_price", "exit_name", "exit_bar",
    "gross_pnl", "commission", "slippage", "net_pnl", "cum_net_pnl", "runup", "drawdown", "ambiguous",
]  # fmt: skip
FILL_MODES = ("magnifier", "ohlc")
STOP_LOSS, PROFIT_TARGET = "Stop Loss", "Profit Target"  # MC's exit names for SetStopLoss / SetProfitTarget
WINDOW_END = "Window End"  # exit name of a position closed by flatten_at_end / a trading window's end

Path_ = list[tuple[float, float, float, float]]  # sub-bars as (open, high, low, close)


@dataclass(frozen=True)
class Costs:
    """Trading costs.

    commission: USD per contract per round trip, deducted from P&L (as MC does; fill prices are unchanged).
    slippage: USD per contract per round trip, also deducted from P&L: half is charged on each market or stop
    fill, none on a limit fill (limit orders, profit targets). MC does the same: in the Vault Break MNQ export,
    profit-target trades cost $0.02 per contract less than market- or stop-exit trades.
    slippage_ticks: ticks moved against every market and stop fill price (buys higher, sells lower); limit
    fills are never slipped. Use one slippage style or the other, not both, unless you mean to double it.
    All three must be finite and >= 0 (a negative cost would add profit).
    """

    commission: float = 0.0
    slippage: float = 0.0
    slippage_ticks: int = 0

    def __post_init__(self) -> None:
        for name in ("commission", "slippage"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, numbers.Real) or not math.isfinite(v) or v < 0:
                raise ValueError(f"Costs.{name} must be a finite USD amount >= 0, got {v!r}")
        t = self.slippage_ticks
        if isinstance(t, bool) or not isinstance(t, numbers.Integral) or t < 0:
            raise ValueError(f"Costs.slippage_ticks must be a whole number of ticks >= 0, got {t!r}")


@dataclass(frozen=True)
class Order:
    action: str
    name: str
    qty: int | None
    kind: str  # market | stop | limit
    bar: int  # bar index the order was placed on
    price: float | None = None  # stop/limit price


@dataclass
class Position:
    direction: int  # 1 long, -1 short
    qty: int
    entry_price: float
    entry_bar: int
    entry_time: pd.Timestamp
    entry_name: str
    entry_sub: tuple[int, int] | None = None  # (bar, sub-bar) of an intrabar entry
    entry_walk: bool = False  # filled along the path, not at a bar or sub-bar open
    entry_slip: bool = True  # entry was a market or stop fill (pays half the USD slippage)
    hi: float = field(init=False)  # highest / lowest price seen since the entry fill
    lo: float = field(init=False)

    def __post_init__(self) -> None:
        self.hi = self.lo = self.entry_price


def _ordered(o: float, h: float, l: float, c: float) -> tuple[float, float, float, float]:  # noqa: E741
    """A sub-bar's price path by the path rule: O-H-L-C when the high is at least as near the open, else O-L-H-C."""
    return (o, h, l, c) if h - o <= o - l else (o, l, h, c)


class Broker:
    def __init__(self, spec: ContractSpec, costs: Costs, times: list[pd.Timestamp]) -> None:
        self.spec, self.costs, self._times = spec, costs, times
        self.position: Position | None = None
        self.pending: list[Order] = []  # placed this bar, active next bar
        self.live: list[Order] = []  # stop/limit orders active in the bar being processed
        self.stop_loss_usd: float | None = None
        self.profit_target_usd: float | None = None
        self.trades: list[tuple] = []
        self.closed_pnl = 0.0
        self._i = 0
        self._sub: tuple[int, int] | None = None
        self._range = (0.0, 0.0)
        self._walking = False
        self.start_bar(0)

    def submit(self, action: str, name: str, qty: int | None, kind: str, bar: int, price: float | None = None) -> None:
        self.pending.append(Order(action, name, qty, kind, bar, price))

    def has_exits(self) -> bool:
        """True while a position is open and a stop loss or profit target is set."""
        return self.position is not None and (self.stop_loss_usd is not None or self.profit_target_usd is not None)

    # --- intrabar equity ----------------------------------------------------------------------------
    def start_bar(self, i: int) -> None:
        self._i = i
        self.eq_low = math.inf  # lowest equity seen in bar i
        self.eq_peak = -math.inf  # highest equity seen so far in bar i
        self.eq_dd = 0.0  # deepest drop below eq_peak so far in bar i (<= 0)

    def mark(self, eq: float) -> None:
        """Equity was `eq` at the next point in time along the bar's path."""
        if eq > self.eq_peak:
            self.eq_peak = eq
        elif eq - self.eq_peak < self.eq_dd:
            self.eq_dd = eq - self.eq_peak
        if eq < self.eq_low:
            self.eq_low = eq

    def see(self, *prices: float) -> None:
        """The open position saw these prices, in this order: update its run-up/drawdown extremes and the equity path."""
        pos = self.position
        if pos is None:
            return
        k = pos.direction * self.spec.point_value * pos.qty
        for p in prices:
            if p > pos.hi:
                pos.hi = p
            if p < pos.lo:
                pos.lo = p
            self.mark(self.closed_pnl + (p - pos.entry_price) * k)

    def see_path(self, path: Path_) -> None:
        for sub in path:
            self.see(*_ordered(*sub))

    # --- one strategy bar ---------------------------------------------------------------------------
    def process_bar(self, i: int, open_price: float, path_of: Callable[[int], Path_]) -> None:
        self._i, self._sub, self._walking = i, None, False
        orders, self.pending = self.pending, []
        self.live = [o for o in orders if o.kind != "market"]
        self.see(open_price)
        for o in orders:
            if o.kind == "market":
                self._execute(o, open_price, slip=True)
        path = path_of(i)
        j = 0
        if self.live or self.has_exits():
            for j, (o, h, l, c) in enumerate(path):  # noqa: E741
                if not (self.live or self.has_exits()):
                    break
                if not any(h >= t[0] if t[1] else l <= t[0] for t in self._triggers()):
                    self.see(*_ordered(o, h, l, c))
                    continue  # nothing can fire in this sub-bar
                self._sub, self._range = (i, j), (l, h)
                self._settle(o, walk=False)
                p = _ordered(o, h, l, c)
                self._walk(p[0], p[1])
                self._walk(p[1], p[2])
                self._walk(p[2], p[3])
            else:
                j = len(path)
        if self.position is not None:  # the rest of the bar, walked without orders
            self.see_path(path[j:])
        self.live, self._sub = [], None

    def _triggers(self) -> list[tuple]:
        """Orders that could fill now, as (price, upper, is_stop, ref). upper: fires when price >= its price."""
        pos = self.position
        d = 0 if pos is None else pos.direction
        out = []
        for o in self.live:
            if o.action in (BUY, SELL_SHORT):
                side = 1 if o.action == BUY else -1
                if d == side:
                    continue  # no pyramiding
            else:
                side = -1 if o.action == SELL else 1  # a sell exits a long; a buy-to-cover exits a short
                if d != -side:
                    continue
            is_stop = o.kind == "stop"
            out.append((o.price, (side == 1) == is_stop, is_stop, o))
        if pos is not None:
            per_point = self.spec.point_value * pos.qty
            if self.stop_loss_usd is not None:
                out.append((self._tick(pos.entry_price - d * self.stop_loss_usd / per_point), d == -1, True, STOP_LOSS))
            if self.profit_target_usd is not None:
                out.append((self._tick(pos.entry_price + d * self.profit_target_usd / per_point), d == 1, False, PROFIT_TARGET))
        return out

    def _settle(self, price: float, walk: bool) -> None:
        """Fill, at `price`, every order that is at or through `price` right now."""
        self.see(price)
        while True:
            trig = self._triggers()
            hit = next((t for t in trig if (price >= t[0] if t[1] else price <= t[0])), None)
            if hit is None:
                return
            self._fire(hit, price, trig, walk)

    def _walk(self, a: float, b: float) -> None:
        """Move price from a to b, filling orders at their own prices in the order price reaches them."""
        p = a
        while p != b:
            trig = self._triggers()
            if b > p:
                cands = [t for t in trig if t[1] and p < t[0] <= b]
                t = min(cands, key=lambda t: t[0]) if cands else None
            else:
                cands = [t for t in trig if not t[1] and b <= t[0] < p]
                t = max(cands, key=lambda t: t[0]) if cands else None
            if t is None:
                self.see(b)
                return
            self.see(t[0])
            self._fire(t, t[0], trig, walk=True)
            p = t[0]
            self._settle(p, walk=True)

    def _fire(self, t: tuple, price: float, trig: list[tuple], walk: bool) -> None:
        _, _, is_stop, ref = t
        pos = self.position
        amb = walk and pos is not None and self._ambiguous(pos, trig)
        self._walking = walk
        if isinstance(ref, Order):
            self.live.remove(ref)
            self._execute(ref, price, slip=is_stop, amb=amb)
        else:  # stop loss / profit target: exit the whole position
            self._close(pos.qty, self._slipped(price, -pos.direction) if is_stop else price, ref, amb, slip=is_stop)

    def _ambiguous(self, pos: Position, trig: list[tuple]) -> bool:
        if pos.entry_walk and pos.entry_sub == self._sub:
            return True
        lo, hi = self._range
        inside = {t[2] for t in trig if lo <= t[0] <= hi}  # is_stop values of the exits this sub-bar reached
        return len(inside) == 2

    # --- fills and ledger ---------------------------------------------------------------------------
    def _tick(self, price: float) -> float:
        tick = self.spec.tick_size
        return round(price / tick) * tick

    def _slipped(self, price: float, side: int) -> float:
        return price + side * self.costs.slippage_ticks * self.spec.tick_size

    def _execute(self, o: Order, price: float, slip: bool, amb: bool = False) -> None:
        pos = self.position
        if o.action in (BUY, SELL_SHORT):
            d = 1 if o.action == BUY else -1
            if pos is not None and pos.direction == d:
                return  # no pyramiding
            px = self._slipped(price, d) if slip else price
            if pos is not None:
                self._close(pos.qty, px, o.name, amb, slip)
            self.position = Position(d, o.qty, px, self._i, self._times[self._i], o.name, self._sub, self._walking, slip)
        else:
            d = 1 if o.action == SELL else -1  # direction of the position this order exits
            if pos is None or pos.direction != d:
                return
            qty = pos.qty if o.qty is None else min(o.qty, pos.qty)
            self._close(qty, self._slipped(price, -d) if slip else price, o.name, amb, slip)

    def _close(self, qty: int, price: float, name: str, amb: bool = False, slip: bool = True) -> None:
        pos, c, spec, i = self.position, self.costs, self.spec, self._i
        slippage = c.slippage * (pos.entry_slip + slip) / 2  # per contract: half per market/stop fill
        gross = spec.pnl(pos.entry_price, price, qty, pos.direction)
        net = spec.pnl(pos.entry_price, price, qty, pos.direction, c.commission, slippage)
        best, worst = (pos.hi, pos.lo) if pos.direction == 1 else (pos.lo, pos.hi)
        entry_cost = (c.commission + c.slippage * pos.entry_slip) / 2
        runup = max(0.0, spec.pnl(pos.entry_price, best, qty, pos.direction, entry_cost))  # MC floors run-up at 0
        drawdown = spec.pnl(pos.entry_price, worst, qty, pos.direction, entry_cost)
        self.closed_pnl += net
        self.trades.append((
            len(self.trades) + 1, "long" if pos.direction == 1 else "short", qty,
            pos.entry_time, pos.entry_price, pos.entry_name, pos.entry_bar,
            self._times[i], price, name, i,
            gross, c.commission * qty, slippage * qty, net, self.closed_pnl, runup, drawdown, amb,
        ))  # fmt: skip
        pos.qty -= qty
        if pos.qty == 0:
            self.position = None
        self.mark(self.closed_pnl + self.open_pnl(price))  # net costs land at the fill

    def open_pnl(self, price: float) -> float:
        pos = self.position
        return 0.0 if pos is None else self.spec.pnl(pos.entry_price, price, pos.qty, pos.direction)

    def flatten(self, price: float) -> None:
        """Close the open position at `price` (a bar's close) as a market fill named WINDOW_END."""
        pos = self.position
        if pos is not None:
            self._sub, self._walking = None, False
            self._close(pos.qty, self._slipped(price, -pos.direction), WINDOW_END, slip=True)

    def end_bar(self, close: float) -> tuple[float, float, float, float]:
        """Mark equity at the bar's close: (equity, equity_low, equity_high, bar_drawdown) of the bar."""
        eq = self.closed_pnl + self.open_pnl(close)
        self.mark(eq)
        return eq, self.eq_low, self.eq_peak, self.eq_dd

    def flattened_end(self, close: float) -> tuple[tuple | None, tuple[float, float, float, float]]:
        """What flatten(close) + end_bar(close) would give now, without changing this broker: (trade, bar figures)."""
        b = copy.copy(self)
        b.position = copy.copy(self.position)
        b.trades = []
        b.flatten(close)
        trade = (len(self.trades) + 1, *b.trades[0][1:]) if b.trades else None
        return trade, b.end_bar(close)


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # closed net + open gross P&L at each bar close
    open_position: Position | None
    strategy: Strategy | None
    fill_mode: str = "ohlc"
    bars_without_sub_bars: int = 0  # magnifier mode: bars that had no 1-minute data and used their own OHLC
    equity_low: pd.Series | None = None  # lowest equity within each bar (<= equity)
    spec: ContractSpec | None = None
    costs: Costs | None = None
    equity_high: pd.Series | None = None  # highest equity within each bar (>= equity)
    bar_drawdown: pd.Series | None = None  # deepest drop within each bar below a peak reached earlier in the bar
    bars_with_partial_sub_bars: int = 0  # magnifier mode: bars whose 1-minute data did not reproduce their OHLC
    trade_from: int = 0  # no fills before this bar
    window_ends: dict = field(default_factory=dict, repr=False)  # end -> (trade, (equity, low, high, drawdown))

    @property
    def net_profit(self) -> float:
        return float(self.trades["net_pnl"].sum()) if len(self.trades) else 0.0

    def write_trades_csv(self, path: str | Path, capital: float = 1_000_000.0) -> Path:
        """Write trades in MC's trade-export column layout (guban.tradelist), then the engine's own columns."""
        from guban.tradelist import to_mc_layout

        path = Path(path)
        table = to_mc_layout(self.trades, self.spec.point_value, capital)  # checks capital before creating anything
        path.parent.mkdir(parents=True, exist_ok=True)
        table.to_csv(path, index=False, lineterminator="\n")
        return path

    def window(self, lo: int, hi: int) -> BacktestResult:
        """The result over bars [lo, hi), flat at the start and flattened at bar hi-1's close: equal to
        run_backtest(..., bars[:hi], trade_from=lo, flatten_at_end=True) restricted to those bars. Needs a run with
        trade_from=lo and hi in window_ends. Bar numbers still index the run's bars."""
        if lo != self.trade_from:
            raise ValueError(f"window({lo}, {hi}) needs a run with trade_from={lo}, this one has trade_from={self.trade_from}")
        if hi not in self.window_ends:
            raise ValueError(f"window({lo}, {hi}) needs a run with {hi} in window_ends")
        if hi <= lo:
            raise ValueError(f"window({lo}, {hi}) is empty")
        trade, (eq, low, high, dd) = self.window_ends[hi]
        rows = list(self.trades[self.trades["exit_bar"] < hi].itertuples(index=False, name=None))
        trades = pd.DataFrame(rows + ([trade] if trade else []), columns=TRADE_COLUMNS)

        def cut(s: pd.Series | None, last: float) -> pd.Series | None:
            if s is None:
                return None
            out = s.iloc[lo:hi].copy()
            out.iat[-1] = last
            return out

        return BacktestResult(
            trades=trades, equity=cut(self.equity, eq), open_position=None, strategy=self.strategy, fill_mode=self.fill_mode,
            equity_low=cut(self.equity_low, low), spec=self.spec, costs=self.costs, equity_high=cut(self.equity_high, high),
            bar_drawdown=cut(self.bar_drawdown, dd), trade_from=lo,
        )  # fmt: skip


def _check_bars(bars: pd.DataFrame, what: str = "bars") -> None:
    cols = ["open", "high", "low", "close"]
    missing = set(cols) - set(bars.columns)
    if missing:
        raise ValueError(f"{what} are missing columns {sorted(missing)}")
    if bars.empty:
        raise ValueError(f"{what} are empty")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError(f"{what} need a tz-aware DatetimeIndex (close-time labels)")
    if not bars.index.is_monotonic_increasing or not bars.index.is_unique:
        raise ValueError(f"{what} timestamps must be strictly increasing")
    for c in cols + (["volume"] if "volume" in bars.columns else []):
        dt = bars[c].dtype
        if not pd.api.types.is_numeric_dtype(dt) or pd.api.types.is_bool_dtype(dt):
            raise ValueError(f"{what} column {c!r} must be numeric, got {dt}")
    a = bars[cols].to_numpy(float)
    o, h, l, c = a.T  # noqa: E741
    eps = 1e-9
    checks = [
        (~np.isfinite(a).all(axis=1), "a missing (NaN) or infinite price"),
        ((l > h + eps) | (o < l - eps) | (o > h + eps) | (c < l - eps) | (c > h + eps), "open/close outside low..high (or low > high)"),
    ]
    if "volume" in bars.columns:
        v = bars["volume"].to_numpy(float)
        checks.append((~np.isfinite(v) | (v < 0), "a negative, missing or infinite volume"))
    for bad, why in checks:
        if bad.any():
            k = int(np.flatnonzero(bad)[0])
            vol = f" volume={bars['volume'].iat[k]!r}" if "volume" in bars.columns else ""
            raise ValueError(
                f"{what}: {int(bad.sum())} bar(s) have {why}; first at {bars.index[k]}: "
                f"open={o[k]!r} high={h[k]!r} low={l[k]!r} close={c[k]!r}{vol}"
            )


def _map_sub_bars(bars: pd.DataFrame, sub: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, pd.DataFrame, int]:
    """Assign each 1-minute bar to the strategy bar it closes inside: the first bar labeled at or after it, in
    the same session. Returns per-bar [start, end) row ranges into the returned (filtered) sub-bar frame, and the
    number of bars whose sub-bars do not reproduce the bar's OHLC (their range is emptied: OHLC fallback)."""
    _check_bars(sub, "sub_bars")
    if str(sub.index.tz) != str(bars.index.tz):
        raise ValueError(f"sub_bars are in {sub.index.tz} but bars are in {bars.index.tz}")
    n = len(bars)
    owner = np.searchsorted(bars.index.as_unit("ns").asi8, sub.index.as_unit("ns").asi8, side="left")
    keep = owner < n
    if "session_date" in bars.columns:
        if "session_date" not in sub.columns:
            raise ValueError("sub_bars need a session_date column; use load_bars(symbol, '1m', session)")
        bar_sess = pd.to_datetime(bars["session_date"]).to_numpy()
        sub_sess = pd.to_datetime(sub["session_date"]).to_numpy()
        keep &= bar_sess[np.minimum(owner, n - 1)] == sub_sess
    sub, owner = sub[keep], owner[keep]
    eps = 1e-9
    bo, bh, bl, bc = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    so, sh, sl, sc = (sub[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    bad = (sh > bh[owner] + eps) | (sl < bl[owner] - eps)
    if bad.any():
        k = int(np.flatnonzero(bad)[0])
        raise ValueError(
            f"{int(bad.sum())} sub-bars fall outside their strategy bar's high-low range, first {sub.index[k]} "
            f"in bar {bars.index[owner[k]]}; sub_bars must be the 1-minute data the bars were built from"
        )
    bar_ids = np.arange(n)
    starts, ends = np.searchsorted(owner, bar_ids, "left"), np.searchsorted(owner, bar_ids, "right")
    has = np.flatnonzero(starts < ends)
    partial = 0
    if len(has):
        s, e = starts[has], ends[has]
        off = (
            (np.abs(so[s] - bo[has]) > eps) | (np.abs(sc[e - 1] - bc[has]) > eps)
            | (np.abs(np.maximum.reduceat(sh, s) - bh[has]) > eps) | (np.abs(np.minimum.reduceat(sl, s) - bl[has]) > eps)
        )  # fmt: skip
        partial = int(off.sum())
        ends[has[off]] = starts[has[off]]
    return starts, ends, sub, partial


def run_backtest(
    strategy: Strategy,
    bars: pd.DataFrame,
    spec: ContractSpec,
    costs: Costs = Costs(),
    sub_bars: pd.DataFrame | None = None,
    fill_mode: str | None = None,
    *,
    trade_from: int = 0,
    flatten_at_end: bool = False,
    window_ends: Iterable[int] = (),
) -> BacktestResult:
    """Run `strategy` over `bars` (e.g. guban.data.resample.load_bars output) for contract `spec`.

    sub_bars: the 1-minute bars the strategy bars were built from, e.g. load_bars(symbol, "1m", session).
    fill_mode: "magnifier" (walk sub_bars intrabar; the default when sub_bars are given) or "ohlc" (use each
    bar's own OHLC path; the default otherwise). A bar with no (or incomplete) sub-bars falls back to its own OHLC.
    trade_from, flatten_at_end, window_ends: trading windows, see the module docstring.
    """
    _check_bars(bars)
    fill_mode = fill_mode or ("magnifier" if sub_bars is not None else "ohlc")
    if fill_mode not in FILL_MODES:
        raise ValueError(f"fill_mode must be one of {FILL_MODES}, got {fill_mode!r}")
    if fill_mode == "magnifier" and sub_bars is None:
        raise ValueError("fill_mode='magnifier' needs sub_bars (the 1-minute bars)")
    n = len(bars)
    if isinstance(trade_from, bool) or not isinstance(trade_from, numbers.Integral) or not 0 <= trade_from < n:
        raise ValueError(f"trade_from must be a bar index in 0..{n - 1}, got {trade_from!r}")
    ends = set(window_ends)
    if any(not trade_from < h <= n for h in ends):
        raise ValueError(f"window_ends must lie in {trade_from + 1}..{n} (after trade_from, at most the bar count), got {sorted(ends)}")

    idx = bars.index
    opens = bars["open"].to_numpy(float).tolist()
    highs = bars["high"].to_numpy(float).tolist()
    lows = bars["low"].to_numpy(float).tolist()
    closes = bars["close"].to_numpy(float).tolist()
    times = list(idx)
    columns = {
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
        "volume": bars["volume"].tolist() if "volume" in bars.columns else [0] * n,
        "time": (idx.hour * 100 + idx.minute).tolist(),
        "date": list(idx.date),
        "session_date": pd.to_datetime(bars["session_date"]).dt.date.tolist() if "session_date" in bars.columns else list(idx.date),
        "timestamp": times,
        "contract": bars["contract"].astype(str).tolist() if "contract" in bars.columns else [None] * n,
    }

    def ohlc_path(i: int) -> Path_:
        return [(opens[i], highs[i], lows[i], closes[i])]

    path_of, no_subs, partial = ohlc_path, 0, 0
    if fill_mode == "magnifier":
        starts, stops, sub, partial = _map_sub_bars(bars, sub_bars)
        so, sh, sl, sc = (sub[k].to_numpy(float).tolist() for k in ("open", "high", "low", "close"))
        no_subs = int((starts == stops).sum()) - partial
        starts, stops = starts.tolist(), stops.tolist()

        def path_of(i: int) -> Path_:
            s, e = starts[i], stops[i]
            return list(zip(so[s:e], sh[s:e], sl[s:e], sc[s:e])) if s < e else ohlc_path(i)

    clock = Clock()
    broker = Broker(spec, costs, times)
    strategy._bind(clock, columns, broker)
    try:
        strategy.on_start()
    except Exception as e:
        raise StrategyError(f"{type(strategy).__name__}.on_start failed (before bar 1): {e!r}") from e
    equity, equity_low, equity_high, bar_dd = ([0.0] * n for _ in range(4))
    snaps: dict[int, tuple] = {}

    for i in range(n):
        clock.i = i
        broker.start_bar(i)
        if i < trade_from:
            broker.pending.clear()  # warm-up: nothing fills before trade_from
        elif broker.pending or broker.has_exits():
            broker.process_bar(i, opens[i], path_of)
        elif broker.position is not None:
            broker.see(opens[i])
            broker.see_path(path_of(i))
        for v in strategy._vars:
            v._carry(i)
        try:
            strategy.on_bar()
        except Exception as e:
            pos = broker.position
            # bar numbers are 1-based, as the strategy sees them (current_bar, MC CurrentBar)
            state = "flat" if pos is None else f"{pos.direction:+d} x{pos.qty} @ {pos.entry_price} since bar {pos.entry_bar + 1}"
            raise StrategyError(f"{type(strategy).__name__}.on_bar failed at bar {i + 1} ({times[i]}), position {state}: {e!r}") from e
        if i + 1 in ends:
            snaps[i + 1] = broker.flattened_end(closes[i])
        if flatten_at_end and i == n - 1:
            broker.flatten(closes[i])
        equity[i], equity_low[i], equity_high[i], bar_dd[i] = broker.end_bar(closes[i])

    return BacktestResult(
        trades=pd.DataFrame(broker.trades, columns=TRADE_COLUMNS),
        equity=pd.Series(equity, index=idx, name="equity"),
        open_position=broker.position,
        strategy=strategy,
        fill_mode=fill_mode,
        bars_without_sub_bars=no_subs,
        equity_low=pd.Series(equity_low, index=idx, name="equity_low"),
        spec=spec,
        costs=costs,
        equity_high=pd.Series(equity_high, index=idx, name="equity_high"),
        bar_drawdown=pd.Series(bar_dd, index=idx, name="bar_drawdown"),
        bars_with_partial_sub_bars=partial,
        trade_from=trade_from,
        window_ends=snaps,
    )
