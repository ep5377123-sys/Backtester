"""Strategy base class. Subclasses override on_bar() (and optionally on_start()), PowerLanguage style.

- `inputs` holds defaults; constructor keyword arguments override them. Read them as self.p.<name>.
- Bar series: open, high, low, close, volume, time (HHMM int, MC `Time`), date (calendar date of the bar
  close, MC `Date`), session_date (trading date), timestamp (pd.Timestamp). All support [n] lookback.
- buy / sell / sell_short / buy_to_cover place orders for the NEXT bar. An order lives for one bar only;
  re-issue it on every bar you want it active.
- Plain instance attributes play the role of intrabarpersist variables; self.series() gives a variable
  with [n] lookback.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

from guban.engine.series import Clock, Series, VarSeries

if TYPE_CHECKING:
    from guban.engine.backtest import Broker

BUY, SELL, SELL_SHORT, BUY_TO_COVER = "buy", "sell", "sell_short", "buy_to_cover"
ORDER_KINDS = ("market",)


class Strategy:
    inputs: ClassVar[dict[str, Any]] = {}

    def __init__(self, **overrides: Any) -> None:
        unknown = set(overrides) - set(self.inputs)
        if unknown:
            raise ValueError(f"{type(self).__name__}: unknown inputs {sorted(unknown)}; known: {sorted(self.inputs)}")
        self.p = SimpleNamespace(**{**self.inputs, **overrides})
        self._broker: Broker | None = None
        self._clock: Clock | None = None
        self._vars: list[VarSeries] = []

    # --- hooks -------------------------------------------------------------------------------------
    def on_start(self) -> None:
        """Called once after data is attached, before the first bar. Create self.series() variables here."""

    def on_bar(self) -> None:
        raise NotImplementedError

    # --- wiring (called by the backtest) ------------------------------------------------------------
    def _bind(self, clock: Clock, columns: dict[str, list], broker: Broker) -> None:
        self._clock, self._broker, self._n = clock, broker, len(columns["close"])
        for name, values in columns.items():
            setattr(self, name, Series(values, clock, name))

    def series(self, initial: Any = math.nan, name: str = "var") -> VarSeries:
        """A variable with [n] lookback. Set it with s.value = x; unassigned bars keep the previous value."""
        if self._clock is None:
            raise RuntimeError("series() needs bar data; call it in on_start(), not __init__()")
        v = VarSeries([initial] * self._n, self._clock, name)
        self._vars.append(v)
        return v

    # --- orders (fill on the next bar) --------------------------------------------------------------
    def buy(self, name: str = "Buy", qty: int = 1, at: str = "market") -> None:
        """Enter long. Reverses a short position; ignored while already long (no pyramiding)."""
        self._order(BUY, name, qty, at)

    def sell_short(self, name: str = "Short", qty: int = 1, at: str = "market") -> None:
        """Enter short. Reverses a long position; ignored while already short (no pyramiding)."""
        self._order(SELL_SHORT, name, qty, at)

    def sell(self, name: str = "Sell", qty: int | None = None, at: str = "market") -> None:
        """Exit long (all contracts when qty is None). Ignored unless long when the order fills."""
        self._order(SELL, name, qty, at)

    def buy_to_cover(self, name: str = "Cover", qty: int | None = None, at: str = "market") -> None:
        """Exit short (all contracts when qty is None). Ignored unless short when the order fills."""
        self._order(BUY_TO_COVER, name, qty, at)

    def _order(self, action: str, name: str, qty: int | None, at: str) -> None:
        if self._broker is None:
            raise RuntimeError("orders can only be placed while a backtest is running")
        if at not in ORDER_KINDS:
            raise ValueError(f"order type {at!r} not supported; use one of {ORDER_KINDS}")
        if qty is not None and (not isinstance(qty, int) or qty <= 0):
            raise ValueError(f"qty must be a positive int or None, got {qty!r}")
        self._broker.submit(action, name, qty, at, self._clock.i)

    # --- position state (as of the current bar's close) ---------------------------------------------
    @property
    def market_position(self) -> int:
        """1 long, -1 short, 0 flat (MC MarketPosition)."""
        pos = self._broker.position
        return 0 if pos is None else pos.direction

    @property
    def current_contracts(self) -> int:
        pos = self._broker.position
        return 0 if pos is None else pos.qty

    @property
    def entry_price(self) -> float | None:
        pos = self._broker.position
        return None if pos is None else pos.entry_price

    @property
    def bars_since_entry(self) -> int | None:
        pos = self._broker.position
        return None if pos is None else self._clock.i - pos.entry_bar

    @property
    def current_bar(self) -> int:
        """1-based bar number (MC CurrentBar)."""
        return self._clock.i + 1
