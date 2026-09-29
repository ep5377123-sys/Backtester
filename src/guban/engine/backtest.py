"""Bar-by-bar backtest loop, order handling, and position/trade ledger (PowerLanguage semantics).

For each strategy bar i:
  1. Orders placed on bar i-1 are processed at bar i's open: market orders fill at the open, moved
     against the trade by Costs.slippage_ticks. An order is never filled on the bar that placed it.
  2. Series point at bar i and on_bar() runs. The orders it places are for bar i+1 only.
  3. Equity (closed net P&L + open gross P&L) is marked at bar i's close.

Position rules: one position at a time, no pyramiding (an entry in the current direction is ignored),
an opposite entry reverses (closes the whole position and opens the new one at the same fill), exits
are ignored unless they match the open position. Orders on one bar are processed in placement order.
A position still open after the last bar stays open: it is in BacktestResult.open_position, not in trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from guban.config import ContractSpec
from guban.engine.series import Clock
from guban.engine.strategy import BUY, SELL, SELL_SHORT, Strategy

TRADE_COLUMNS = [
    "trade_no", "direction", "qty",
    "entry_time", "entry_price", "entry_name", "entry_bar",
    "exit_time", "exit_price", "exit_name", "exit_bar",
    "gross_pnl", "commission", "slippage", "net_pnl", "cum_net_pnl",
]  # fmt: skip


@dataclass(frozen=True)
class Costs:
    """Trading costs.

    commission, slippage: USD per contract per round trip, deducted from P&L (as MC does; fill prices are
    unchanged). slippage_ticks: ticks moved against every fill price (buys higher, sells lower). Use one
    slippage style or the other, not both, unless you mean to double it.
    """

    commission: float = 0.0
    slippage: float = 0.0
    slippage_ticks: int = 0


@dataclass(frozen=True)
class Order:
    action: str
    name: str
    qty: int | None
    kind: str
    bar: int  # bar index the order was placed on


@dataclass
class Position:
    direction: int  # 1 long, -1 short
    qty: int
    entry_price: float
    entry_bar: int
    entry_time: pd.Timestamp
    entry_name: str


class StrategyError(RuntimeError):
    """The strategy raised inside on_bar(); the message names the bar and the position."""


class Broker:
    def __init__(self, spec: ContractSpec, costs: Costs, times: list[pd.Timestamp]) -> None:
        self.spec, self.costs, self._times = spec, costs, times
        self.position: Position | None = None
        self.pending: list[Order] = []
        self.trades: list[tuple] = []
        self.closed_pnl = 0.0

    def submit(self, action: str, name: str, qty: int | None, kind: str, bar: int) -> None:
        self.pending.append(Order(action, name, qty, kind, bar))

    def process_open(self, i: int, open_price: float) -> None:
        orders, self.pending = self.pending, []
        for o in orders:  # all market orders (the only kind so far)
            self._fill_market(o, i, open_price)

    def _slipped(self, price: float, side: int) -> float:
        return price + side * self.costs.slippage_ticks * self.spec.tick_size

    def _fill_market(self, o: Order, i: int, price: float) -> None:
        pos = self.position
        if o.action in (BUY, SELL_SHORT):
            d = 1 if o.action == BUY else -1
            if pos is not None and pos.direction == d:
                return  # no pyramiding
            px = self._slipped(price, d)
            if pos is not None:
                self._close(pos.qty, px, i, o.name)
            self.position = Position(d, o.qty, px, i, self._times[i], o.name)
        else:
            d = 1 if o.action == SELL else -1  # direction of the position this order exits
            if pos is None or pos.direction != d:
                return
            qty = pos.qty if o.qty is None else min(o.qty, pos.qty)
            self._close(qty, self._slipped(price, -d), i, o.name)

    def _close(self, qty: int, price: float, i: int, name: str) -> None:
        pos, c, spec = self.position, self.costs, self.spec
        gross = spec.pnl(pos.entry_price, price, qty, pos.direction)
        net = spec.pnl(pos.entry_price, price, qty, pos.direction, c.commission, c.slippage)
        self.closed_pnl += net
        self.trades.append((
            len(self.trades) + 1, "long" if pos.direction == 1 else "short", qty,
            pos.entry_time, pos.entry_price, pos.entry_name, pos.entry_bar,
            self._times[i], price, name, i,
            gross, c.commission * qty, c.slippage * qty, net, self.closed_pnl,
        ))  # fmt: skip
        pos.qty -= qty
        if pos.qty == 0:
            self.position = None

    def open_pnl(self, price: float) -> float:
        pos = self.position
        return 0.0 if pos is None else self.spec.pnl(pos.entry_price, price, pos.qty, pos.direction)


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series  # closed net + open gross P&L at each bar close
    open_position: Position | None
    strategy: Strategy

    @property
    def net_profit(self) -> float:
        return float(self.trades["net_pnl"].sum()) if len(self.trades) else 0.0

    def write_trades_csv(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.trades.to_csv(path, index=False, lineterminator="\n")
        return path


def _check_bars(bars: pd.DataFrame) -> None:
    missing = {"open", "high", "low", "close"} - set(bars.columns)
    if missing:
        raise ValueError(f"bars are missing columns {sorted(missing)}")
    if bars.empty:
        raise ValueError("bars are empty")
    if not isinstance(bars.index, pd.DatetimeIndex) or bars.index.tz is None:
        raise ValueError("bars need a tz-aware DatetimeIndex (close-time labels)")
    if not bars.index.is_monotonic_increasing or not bars.index.is_unique:
        raise ValueError("bar timestamps must be strictly increasing")


def run_backtest(strategy: Strategy, bars: pd.DataFrame, spec: ContractSpec, costs: Costs = Costs()) -> BacktestResult:
    """Run `strategy` over `bars` (e.g. guban.data.resample.load_bars output) for contract `spec`."""
    _check_bars(bars)
    idx = bars.index
    opens = bars["open"].to_numpy(float).tolist()
    closes = bars["close"].to_numpy(float).tolist()
    times = list(idx)
    columns = {
        "open": opens,
        "high": bars["high"].to_numpy(float).tolist(),
        "low": bars["low"].to_numpy(float).tolist(),
        "close": closes,
        "volume": bars["volume"].tolist() if "volume" in bars.columns else [0] * len(bars),
        "time": (idx.hour * 100 + idx.minute).tolist(),
        "date": list(idx.date),
        "session_date": pd.to_datetime(bars["session_date"]).dt.date.tolist() if "session_date" in bars.columns else list(idx.date),
        "timestamp": times,
    }

    clock = Clock()
    broker = Broker(spec, costs, times)
    strategy._bind(clock, columns, broker)
    strategy.on_start()
    equity = [0.0] * len(bars)

    for i in range(len(bars)):
        clock.i = i
        if broker.pending:
            broker.process_open(i, opens[i])
        for v in strategy._vars:
            v._carry(i)
        try:
            strategy.on_bar()
        except Exception as e:
            pos = broker.position
            state = "flat" if pos is None else f"{pos.direction:+d} x{pos.qty} @ {pos.entry_price} since bar {pos.entry_bar}"
            raise StrategyError(f"{type(strategy).__name__}.on_bar failed at bar {i} ({times[i]}), position {state}: {e!r}") from e
        equity[i] = broker.closed_pnl + broker.open_pnl(closes[i])

    return BacktestResult(
        trades=pd.DataFrame(broker.trades, columns=TRADE_COLUMNS),
        equity=pd.Series(equity, index=idx, name="equity"),
        open_position=broker.position,
        strategy=strategy,
    )
