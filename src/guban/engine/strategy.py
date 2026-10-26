"""Strategy base class. Subclasses override on_bar() (and optionally on_start()), PowerLanguage style.

- `inputs` holds defaults; constructor keyword arguments override them. Read them as self.p.<name>. Each
  instance gets its own deep copy, so mutating a list or dict input never leaks into another run.
- Bar series: open, high, low, close, volume, time (HHMM int, MC `Time`), date (calendar date of the bar
  close, MC `Date`), session_date (trading date), timestamp (pd.Timestamp), contract (the contract month of
  an unadjusted continuous series, e.g. "202609", or None when the bars have no contract column; compare
  contract[0] with contract[1] to see a roll coming and flatten before it), session_close (when the bar's
  session is scheduled to close, a pd.Timestamp in chart time: earlier on the exchange's early-close days from
  config/calendar.toml, which are published in advance; None when the bars have no session_close column).
  All support [n] lookback.
- `extra_columns` names further bar columns the strategy reads (e.g. "big_buy_body"), each bound as a series of
  floats. `guban run`/`optimize`/`wfo` add them to the bars (guban.data.features); run_backtest raises when a
  declared column is missing from the bars.
- buy / sell / sell_short / buy_to_cover place orders for the NEXT bar: at="market" (the next bar's open),
  or at="stop" / at="limit" with a price (filled intrabar; see guban.engine.backtest). An order lives for
  one bar only; re-issue it on every bar you want it active. at="close" is MC's `this bar on close`: it fills
  at the close of the bar that placed it, right after on_bar() returns (a market fill for slippage and costs),
  so market_position shows it from the next bar on.
- set_stop_loss / set_profit_target (MC SetStopLoss / SetProfitTarget, per position, in USD) stay in force
  until changed or cleared with None, and apply to every position from its entry fill onward.
- `plots` names self.series() variables (by attribute or by name=) that Guban Studio draws on its price chart;
  plot_series() returns their per-bar values after a run. It changes nothing in the backtest.
- Plain instance attributes play the role of intrabarpersist variables; self.series() gives a variable
  with [n] lookback.
- An instance runs one backtest: create a new one for each run (a second run_backtest with the same
  instance raises, since its variables would carry the first run's state).
"""

from __future__ import annotations

import copy
import importlib.util
import inspect
import math
import numbers
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

from guban.engine.series import Clock, Series, VarSeries

if TYPE_CHECKING:
    from guban.engine.backtest import Broker

BUY, SELL, SELL_SHORT, BUY_TO_COVER = "buy", "sell", "sell_short", "buy_to_cover"
ORDER_KINDS = ("market", "stop", "limit", "close")


class StrategyError(RuntimeError):
    """The strategy's own code raised (import, on_start or on_bar); the message says where."""


class Strategy:
    inputs: ClassVar[dict[str, Any]] = {}
    extra_columns: ClassVar[tuple[str, ...]] = ()
    plots: ClassVar[tuple[str, ...]] = ()

    def __init__(self, **overrides: Any) -> None:
        unknown = set(overrides) - set(self.inputs)
        if unknown:
            raise ValueError(f"{type(self).__name__}: unknown inputs {sorted(unknown)}; known: {sorted(self.inputs)}")
        self.p = SimpleNamespace(**copy.deepcopy({**self.inputs, **overrides}))
        self._broker: Broker | None = None
        self._clock: Clock | None = None
        self._vars: list[VarSeries] = []

    # --- hooks -------------------------------------------------------------------------------------
    def on_start(self) -> None:
        """Called once after data is attached, before the first bar. Create self.series() variables here."""

    def on_bar(self) -> None:
        raise NotImplementedError

    def plot_series(self) -> dict[str, list]:
        """After a run: each name in `plots` -> its value at every bar. A name is an attribute holding a
        self.series() variable, or the name= given to self.series()."""
        by_name = {v.name: v for v in self._vars}
        out = {}
        for name in self.plots:
            v = getattr(self, name, None)
            v = v if isinstance(v, VarSeries) else by_name.get(name)
            if v is None:
                raise ValueError(f"{type(self).__name__}.plots names {name!r}, but no self.series() variable has that "
                                 f"attribute or name= (create it in on_start)")  # fmt: skip
            out[name] = list(v._values)
        return out

    # --- wiring (called by the backtest) ------------------------------------------------------------
    def _bind(self, clock: Clock, columns: dict[str, list], broker: Broker) -> None:
        if self._clock is not None:
            raise RuntimeError(
                f"this {type(self).__name__} instance has already run a backtest; its variables still hold that run's "
                f"state, so create a new one ({type(self).__name__}(...)) for each run"
            )
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

    # --- orders (fill on the next bar, or at this bar's close with at="close") ---------------------------
    def buy(self, name: str = "Buy", qty: int = 1, at: str = "market", price: float | None = None) -> None:
        """Enter long. Reverses a short position; ignored while already long (no pyramiding)."""
        self._order(BUY, name, qty, at, price)

    def sell_short(self, name: str = "Short", qty: int = 1, at: str = "market", price: float | None = None) -> None:
        """Enter short. Reverses a long position; ignored while already short (no pyramiding)."""
        self._order(SELL_SHORT, name, qty, at, price)

    def sell(self, name: str = "Sell", qty: int | None = None, at: str = "market", price: float | None = None) -> None:
        """Exit long (all contracts when qty is None). Ignored unless long when the order fills."""
        self._order(SELL, name, qty, at, price)

    def buy_to_cover(self, name: str = "Cover", qty: int | None = None, at: str = "market", price: float | None = None) -> None:
        """Exit short (all contracts when qty is None). Ignored unless short when the order fills."""
        self._order(BUY_TO_COVER, name, qty, at, price)

    def _order(self, action: str, name: str, qty: int | None, at: str, price: float | None) -> None:
        if self._broker is None:
            raise RuntimeError("orders can only be placed while a backtest is running")
        if at not in ORDER_KINDS:
            raise ValueError(f"order type {at!r} not supported; use one of {ORDER_KINDS}")
        if at in ("market", "close"):
            if price is not None:
                raise ValueError(f"{at} orders take no price")
        elif price is None or not math.isfinite(price):
            raise ValueError(f"{at} orders need a finite price, got {price!r}")
        entry = action in (BUY, SELL_SHORT)
        if qty is not None or entry:
            if isinstance(qty, bool) or not isinstance(qty, numbers.Integral) or qty <= 0:
                what = "a positive int" if entry else "a positive int or None (all contracts)"
                raise ValueError(f"{action} qty must be {what}, got {qty!r}")
            qty = int(qty)
        self._broker.submit(action, name, qty, at, self._clock.i, None if price is None else float(price))

    # --- per-position dollar exits (MC SetStopLoss / SetProfitTarget) --------------------------------
    def set_stop_loss(self, amount: float | None) -> None:
        """Exit the whole position with a stop when its loss reaches `amount` USD (None clears it)."""
        self._broker.stop_loss_usd = _dollars(amount, "set_stop_loss")

    def set_profit_target(self, amount: float | None) -> None:
        """Exit the whole position with a limit when its profit reaches `amount` USD (None clears it)."""
        self._broker.profit_target_usd = _dollars(amount, "set_profit_target")

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
    def big_point_value(self) -> float:
        """USD per point per contract of the traded contract (MC BigPointValue)."""
        return self._broker.spec.point_value

    @property
    def tick_size(self) -> float:
        """Minimum price move of the traded contract (MC MinMove / PriceScale)."""
        return self._broker.spec.tick_size

    @property
    def current_bar(self) -> int:
        """1-based bar number (MC CurrentBar)."""
        return self._clock.i + 1


def load_strategy_class(path: str | Path, class_name: str | None = None) -> type[Strategy]:
    """Import a strategy file and return its Strategy subclass (`class_name` when the file defines several)."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f"guban_user_strategy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # so inspect.getfile() finds the file (the optimizer re-imports from it)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        raise StrategyError(f"importing {path.name} failed: {e!r}") from e
    found = {
        name: obj for name, obj in vars(module).items()
        if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__
    }  # fmt: skip
    if class_name:
        if class_name not in found:
            raise ValueError(f"{path} has no Strategy subclass {class_name!r}; found {sorted(found)}")
        return found[class_name]
    if len(found) != 1:
        raise ValueError(f"{path} defines {len(found)} Strategy subclasses {sorted(found)}; pick one with --class")
    return next(iter(found.values()))


def _dollars(amount: float | None, what: str) -> float | None:
    if amount is None:
        return None
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"{what} needs a positive USD amount or None, got {amount!r}")
    return float(amount)
