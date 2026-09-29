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

Run-up and drawdown (MC's per-trade columns) come from the highest and lowest prices the position saw along
that same path, from its entry fill to its exit fill, less the entry-side costs (half the commission, plus
half the USD slippage when the entry was a market or stop fill); run-up is floored at 0. MC reports them the
same way: a Vault Break MNQ profit-target trade shows run-up 498.9 = 500 - 1.10, one that never went green 0. BacktestResult.equity_low is each bar's worst
equity along the path (closed net + open gross at the adverse extreme), for intraday peak-to-valley drawdown.

Position rules: one position at a time, no pyramiding (an entry in the current direction is ignored), an
opposite entry reverses (closes the whole position and opens the new one at the same fill), exits are ignored
unless they match the open position at the moment they would fill. A position still open after the last bar
stays open: it is in BacktestResult.open_position, not in trades.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from guban.config import ContractSpec
from guban.engine.series import Clock
from guban.engine.strategy import BUY, SELL, SELL_SHORT, Strategy

TRADE_COLUMNS = [
    "trade_no", "direction", "qty",
    "entry_time", "entry_price", "entry_name", "entry_bar",
    "exit_time", "exit_price", "exit_name", "exit_bar",
    "gross_pnl", "commission", "slippage", "net_pnl", "cum_net_pnl", "runup", "drawdown", "ambiguous",
]  # fmt: skip
FILL_MODES = ("magnifier", "ohlc")
STOP_LOSS, PROFIT_TARGET = "Stop Loss", "Profit Target"  # MC's exit names for SetStopLoss / SetProfitTarget

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
    """

    commission: float = 0.0
    slippage: float = 0.0
    slippage_ticks: int = 0


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


class StrategyError(RuntimeError):
    """The strategy raised inside on_bar(); the message names the bar and the position."""


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
        self.eq_low = np.inf  # worst equity seen in the current bar (reset by the loop each bar)
        self._i = 0
        self._sub: tuple[int, int] | None = None
        self._range = (0.0, 0.0)
        self._walking = False

    def submit(self, action: str, name: str, qty: int | None, kind: str, bar: int, price: float | None = None) -> None:
        self.pending.append(Order(action, name, qty, kind, bar, price))

    def has_exits(self) -> bool:
        """True while a position is open and a stop loss or profit target is set."""
        return self.position is not None and (self.stop_loss_usd is not None or self.profit_target_usd is not None)

    # --- one strategy bar ---------------------------------------------------------------------------
    def process_bar(self, i: int, open_price: float, path_of: Callable[[int], Path_]) -> None:
        self._i, self._sub, self._walking = i, None, False
        orders, self.pending = self.pending, []
        self.live = [o for o in orders if o.kind != "market"]
        self.see(open_price, open_price)
        for o in orders:
            if o.kind == "market":
                self._execute(o, open_price, slip=True)
        path = path_of(i)
        j = 0
        if self.live or self.has_exits():
            for j, (o, h, l, c) in enumerate(path):
                if not (self.live or self.has_exits()):
                    break
                if not any(h >= t[0] if t[1] else l <= t[0] for t in self._triggers()):
                    self.see(l, h)
                    continue  # nothing can fire in this sub-bar
                self._sub, self._range = (i, j), (l, h)
                self._settle(o, walk=False)
                p = (o, h, l, c) if h - o <= o - l else (o, l, h, c)
                self._walk(p[0], p[1])
                self._walk(p[1], p[2])
                self._walk(p[2], p[3])
            else:
                j = len(path)
        if self.position is not None:  # the rest of the bar, walked without orders
            for _, h, l, _ in path[j:]:
                self.see(l, h)
        self.live, self._sub = [], None

    def see(self, lo: float, hi: float) -> None:
        """The open position saw prices lo..hi: update its run-up/drawdown extremes and the bar's worst equity."""
        pos = self.position
        if pos is None:
            return
        if hi > pos.hi:
            pos.hi = hi
        if lo < pos.lo:
            pos.lo = lo
        worst = lo if pos.direction == 1 else hi
        eq = self.closed_pnl + (worst - pos.entry_price) * pos.direction * self.spec.point_value * pos.qty
        if eq < self.eq_low:
            self.eq_low = eq

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
        self.see(price, price)
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
                self.see(min(p, b), max(p, b))
                return
            self.see(min(p, t[0]), max(p, t[0]))
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
        eq = self.closed_pnl + self.open_pnl(price)  # net costs land at the fill
        if eq < self.eq_low:
            self.eq_low = eq

    def open_pnl(self, price: float) -> float:
        pos = self.position
        return 0.0 if pos is None else self.spec.pnl(pos.entry_price, price, pos.qty, pos.direction)


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # closed net + open gross P&L at each bar close
    open_position: Position | None
    strategy: Strategy
    fill_mode: str = "ohlc"
    bars_without_sub_bars: int = 0  # magnifier mode: bars that had no 1-minute data and used their own OHLC
    equity_low: pd.Series | None = None  # worst equity within each bar (<= equity), for intraday drawdown
    spec: ContractSpec | None = None
    costs: Costs | None = None

    @property
    def net_profit(self) -> float:
        return float(self.trades["net_pnl"].sum()) if len(self.trades) else 0.0

    def write_trades_csv(self, path: str | Path, capital: float = 1_000_000.0) -> Path:
        """Write trades in MC's trade-export column layout (guban.tradelist), then the engine's own columns."""
        from guban.tradelist import to_mc_layout

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        to_mc_layout(self.trades, self.spec.point_value, capital).to_csv(path, index=False, lineterminator="\n")
        return path


def _check_bars(bars: pd.DataFrame, what: str = "bars") -> None:
    missing = {"open", "high", "low", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"{what} are missing columns {sorted(missing)}")
    if bars.empty:
        raise ValueError(f"{what} are empty")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError(f"{what} need a tz-aware DatetimeIndex (close-time labels)")
    if not bars.index.is_monotonic_increasing or not bars.index.is_unique:
        raise ValueError(f"{what} timestamps must be strictly increasing")


def _map_sub_bars(bars: pd.DataFrame, sub: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    """Assign each 1-minute bar to the strategy bar it closes inside: the first bar labeled at or after it, in
    the same session. Returns per-bar [start, end) row ranges into the returned (filtered) sub-bar frame."""
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
    bad = (sub["high"].to_numpy(float) > bars["high"].to_numpy(float)[owner] + eps) | (
        sub["low"].to_numpy(float) < bars["low"].to_numpy(float)[owner] - eps
    )
    if bad.any():
        k = int(np.flatnonzero(bad)[0])
        raise ValueError(
            f"{int(bad.sum())} sub-bars fall outside their strategy bar's high-low range, first {sub.index[k]} "
            f"in bar {bars.index[owner[k]]}; sub_bars must be the 1-minute data the bars were built from"
        )
    bar_ids = np.arange(n)
    return np.searchsorted(owner, bar_ids, "left"), np.searchsorted(owner, bar_ids, "right"), sub


def run_backtest(
    strategy: Strategy,
    bars: pd.DataFrame,
    spec: ContractSpec,
    costs: Costs = Costs(),
    sub_bars: pd.DataFrame | None = None,
    fill_mode: str | None = None,
) -> BacktestResult:
    """Run `strategy` over `bars` (e.g. guban.data.resample.load_bars output) for contract `spec`.

    sub_bars: the 1-minute bars the strategy bars were built from, e.g. load_bars(symbol, "1m", session).
    fill_mode: "magnifier" (walk sub_bars intrabar; the default when sub_bars are given) or "ohlc" (use each
    bar's own OHLC path; the default otherwise). A bar with no sub-bars falls back to its own OHLC.
    """
    _check_bars(bars)
    fill_mode = fill_mode or ("magnifier" if sub_bars is not None else "ohlc")
    if fill_mode not in FILL_MODES:
        raise ValueError(f"fill_mode must be one of {FILL_MODES}, got {fill_mode!r}")
    if fill_mode == "magnifier" and sub_bars is None:
        raise ValueError("fill_mode='magnifier' needs sub_bars (the 1-minute bars)")

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
        "volume": bars["volume"].tolist() if "volume" in bars.columns else [0] * len(bars),
        "time": (idx.hour * 100 + idx.minute).tolist(),
        "date": list(idx.date),
        "session_date": pd.to_datetime(bars["session_date"]).dt.date.tolist() if "session_date" in bars.columns else list(idx.date),
        "timestamp": times,
    }

    def ohlc_path(i: int) -> Path_:
        return [(opens[i], highs[i], lows[i], closes[i])]

    path_of, no_subs = ohlc_path, 0
    if fill_mode == "magnifier":
        starts, ends, sub = _map_sub_bars(bars, sub_bars)
        so, sh, sl, sc = (sub[k].to_numpy(float).tolist() for k in ("open", "high", "low", "close"))
        starts, ends = starts.tolist(), ends.tolist()
        no_subs = sum(s == e for s, e in zip(starts, ends))

        def path_of(i: int) -> Path_:
            s, e = starts[i], ends[i]
            return list(zip(so[s:e], sh[s:e], sl[s:e], sc[s:e])) if s < e else ohlc_path(i)

    clock = Clock()
    broker = Broker(spec, costs, times)
    strategy._bind(clock, columns, broker)
    strategy.on_start()
    equity = [0.0] * len(bars)
    equity_low = [0.0] * len(bars)

    for i in range(len(bars)):
        clock.i = i
        broker.eq_low = np.inf
        if broker.pending or broker.has_exits():
            broker.process_bar(i, opens[i], path_of)
        elif broker.position is not None:
            broker.see(lows[i], highs[i])
        for v in strategy._vars:
            v._carry(i)
        try:
            strategy.on_bar()
        except Exception as e:
            pos = broker.position
            state = "flat" if pos is None else f"{pos.direction:+d} x{pos.qty} @ {pos.entry_price} since bar {pos.entry_bar}"
            raise StrategyError(f"{type(strategy).__name__}.on_bar failed at bar {i} ({times[i]}), position {state}: {e!r}") from e
        equity[i] = eq = broker.closed_pnl + broker.open_pnl(closes[i])
        equity_low[i] = min(eq, broker.eq_low)

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
    )
