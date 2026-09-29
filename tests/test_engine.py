"""Engine core: series lookback, next-bar market fills (R3), P&L and costs (R5), determinism (R11)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, StrategyError, run_backtest

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)


def make_bars(opens, closes=None, start="2024-01-02 09:35", freq="5min") -> pd.DataFrame:
    """Synthetic close-time-labeled ET bars. high/low bracket open and close by 1 point."""
    opens = np.asarray(opens, float)
    closes = opens + 0.5 if closes is None else np.asarray(closes, float)
    idx = pd.date_range(start, periods=len(opens), freq=freq, tz="America/New_York", name="time")
    return pd.DataFrame(
        {
            "open": opens,
            "high": np.maximum(opens, closes) + 1,
            "low": np.minimum(opens, closes) - 1,
            "close": closes,
            "volume": 100,
            "session_date": idx.tz_localize(None).normalize(),
        },
        index=idx,
    )


class Script(Strategy):
    """Places the orders listed per bar index: {bar: [(method, kwargs), ...]}."""

    inputs = dict(plan={})

    def on_bar(self):
        for method, kwargs in self.p.plan.get(self.current_bar - 1, []):
            getattr(self, method)(**kwargs)


def run(plan, bars, costs=Costs()):
    return run_backtest(Script(plan=plan), bars, NQ, costs)


# --- series ---------------------------------------------------------------------------------------


def test_series_lookback_and_bar_fields():
    seen = []

    class Look(Strategy):
        def on_bar(self):
            prev = self.close[1] if self.current_bar > 1 else None
            seen.append((self.close[0], prev, self.time[0], self.date[0], len(self.close)))

    bars = make_bars([10, 20, 30], closes=[11, 21, 31])
    run_backtest(Look(), bars, NQ)
    d = pd.Timestamp("2024-01-02").date()
    assert seen == [(11, None, 935, d, 1), (21, 11, 940, d, 2), (31, 21, 945, d, 3)]


def test_series_rejects_history_before_first_bar_and_future():
    class Bad(Strategy):
        def on_bar(self):
            self.close[1]

    with pytest.raises(StrategyError, match=r"bar 1 .*reaches before the first bar"):
        run_backtest(Bad(), make_bars([1, 2]), NQ)

    class Future(Strategy):
        def on_bar(self):
            self.close[-1]

    with pytest.raises(StrategyError, match="future"):
        run_backtest(Future(), make_bars([1, 2]), NQ)


def test_var_series_carries_value_forward():
    out = []

    class Var(Strategy):
        def on_start(self):
            self.v = self.series(0.0)

        def on_bar(self):
            if self.current_bar in (2, 4):
                self.v.value = self.close[0]
            out.append((self.v[0], self.v[1] if self.current_bar > 1 else None))

    run_backtest(Var(), make_bars([1, 2, 3, 4, 5], closes=[1, 2, 3, 4, 5]), NQ)
    assert out == [(0.0, None), (2, 0.0), (2, 2), (4, 2), (4, 4)]


def test_series_outside_on_start_is_an_error():
    class Early(Strategy):
        def __init__(self):
            super().__init__()
            self.series()

    with pytest.raises(RuntimeError, match="on_start"):
        Early()


# --- R3: market orders fill at the next bar's open ------------------------------------------------


def test_market_order_fills_at_next_bar_open_never_signal_bar():
    bars = make_bars([100, 101, 102, 103, 104, 105])
    res = run({1: [("buy", dict(name="L"))], 3: [("sell", dict(name="X"))]}, bars)
    (t,) = res.trades.itertuples()
    assert (t.entry_bar, t.entry_time, t.entry_price, t.entry_name) == (2, bars.index[2], 102.0, "L")
    assert (t.exit_bar, t.exit_time, t.exit_price, t.exit_name) == (4, bars.index[4], 104.0, "X")
    assert res.open_position is None


def test_market_position_updates_only_after_the_fill():
    states = []

    class S(Strategy):
        def on_bar(self):
            if self.current_bar == 1:
                self.buy(qty=2)
            states.append((self.market_position, self.current_contracts, self.entry_price, self.bars_since_entry))

    run_backtest(S(), make_bars([100, 101, 102]), NQ)
    assert states == [(0, 0, None, None), (1, 2, 101.0, 0), (1, 2, 101.0, 1)]


def test_slippage_ticks_move_fills_against_the_trade():
    bars = make_bars([100, 101, 102, 103, 104, 105])
    res = run({0: [("buy", {})], 1: [("sell", {})], 2: [("sell_short", {})], 3: [("buy_to_cover", {})]}, bars, Costs(slippage_ticks=2))
    assert res.trades[["entry_price", "exit_price"]].values.tolist() == [[101.5, 101.5], [102.5, 104.5]]


def test_orders_last_one_bar_and_last_bar_orders_never_fill():
    bars = make_bars([100, 101, 102, 103])
    res = run({0: [("buy", {})], 3: [("sell", {})]}, bars)  # the sell is placed on the final bar
    assert res.trades.empty
    assert res.open_position.entry_price == 101.0 and res.open_position.qty == 1


# --- position rules -------------------------------------------------------------------------------


def test_reversal_closes_and_opens_at_the_same_fill():
    bars = make_bars([100, 101, 102, 103, 104])
    res = run({0: [("buy", dict(name="L", qty=2))], 2: [("sell_short", dict(name="S", qty=1))]}, bars)
    (t,) = res.trades.itertuples()
    assert (t.direction, t.qty, t.entry_price, t.exit_price, t.exit_name) == ("long", 2, 101.0, 103.0, "S")
    pos = res.open_position
    assert (pos.direction, pos.qty, pos.entry_price, pos.entry_name) == (-1, 1, 103.0, "S")


def test_no_pyramiding_and_mismatched_exits_are_ignored():
    bars = make_bars([100, 101, 102, 103, 104, 105])
    plan = {
        0: [("buy_to_cover", {}), ("sell", {})],  # flat: both exits ignored
        1: [("buy", dict(qty=1))],
        2: [("buy", dict(qty=5)), ("buy_to_cover", {})],  # already long: second entry and short exit ignored
        3: [("sell", {})],
    }
    res = run(plan, bars)
    assert res.trades[["qty", "entry_price", "exit_price"]].values.tolist() == [[1, 102.0, 104.0]]


def test_partial_exits_split_the_position():
    bars = make_bars([100, 101, 102, 103, 104, 105])
    res = run({0: [("buy", dict(qty=3))], 1: [("sell", dict(qty=1))], 2: [("sell", dict(qty=5))]}, bars)
    assert res.trades[["qty", "entry_price", "exit_price"]].values.tolist() == [[1, 101.0, 102.0], [2, 101.0, 103.0]]
    assert res.open_position is None


def test_orders_on_one_bar_process_in_placement_order():
    bars = make_bars([100, 101, 102, 103])
    res = run({0: [("buy", {})], 1: [("sell", dict(name="X")), ("buy", dict(name="L2"))]}, bars)
    assert res.trades["exit_name"].tolist() == ["X"]
    assert res.open_position.entry_name == "L2" and res.open_position.entry_price == 102.0


# --- R5: P&L from the contract spec ---------------------------------------------------------------


def test_pnl_uses_point_value_contracts_and_costs():
    bars = make_bars([100, 110, 120, 110, 100, 90])
    costs = Costs(commission=4.2, slippage=5.0)
    res = run({0: [("buy", dict(qty=2))], 1: [("sell", {})], 2: [("sell_short", dict(qty=3))], 4: [("buy_to_cover", {})]}, bars, costs)
    long_, short = res.trades.itertuples()
    # long 2 from bar 1 open (110) to bar 2 open (120): 10 pts x $20 x 2 = 400, costs (4.2 + 5) x 2
    assert (long_.gross_pnl, long_.commission, long_.slippage, long_.net_pnl) == (400.0, 8.4, 10.0, 400.0 - 18.4)
    assert short.gross_pnl == (110 - 90) * 20 * 3  # short from bar 3 open (110) to bar 5 open (90)
    assert short.net_pnl == pytest.approx(1200 - 3 * 9.2)
    assert short.cum_net_pnl == pytest.approx(long_.net_pnl + short.net_pnl)
    assert res.net_profit == pytest.approx(short.cum_net_pnl)


def test_equity_is_closed_net_plus_open_gross_at_each_close():
    bars = make_bars([100, 101, 102, 103], closes=[100, 105, 106, 103])
    res = run({0: [("buy", {})], 1: [("sell", {})]}, bars, Costs(commission=2.0))
    # bar1: long from 101, close 105 -> +80 open; bar2: sold at 102 open -> 20 - 2 closed.
    assert res.equity.tolist() == [0.0, 80.0, 18.0, 18.0]


# --- inputs, errors, determinism (R11) ------------------------------------------------------------


def test_inputs_defaults_overrides_and_unknown_names():
    class P(Strategy):
        inputs = dict(a=1, b=2)

        def on_bar(self):
            pass

    assert vars(P(b=5).p) == {"a": 1, "b": 5}
    with pytest.raises(ValueError, match="unknown inputs"):
        P(c=1)


def test_strategy_exception_names_bar_and_position():
    class Boom(Strategy):
        def on_bar(self):
            if self.current_bar == 1:
                self.buy()
            if self.current_bar == 3:
                raise ZeroDivisionError("x")

    bars = make_bars([100, 101, 102])
    # bar numbers match current_bar (1-based): raised on bar 3, entered at bar 2's open
    with pytest.raises(StrategyError, match=r"bar 3 \(2024-01-02 09:45:00-05:00\), position \+1 x1 @ 101.0 since bar 2:") as e:
        run_backtest(Boom(), bars, NQ)
    assert isinstance(e.value.__cause__, ZeroDivisionError)


def test_bad_orders_and_bars_fail_loudly():
    with pytest.raises(StrategyError, match="not supported"):
        run({0: [("buy", dict(at="moc"))]}, make_bars([1, 2]))
    with pytest.raises(StrategyError, match="positive int"):
        run({0: [("buy", dict(qty=0))]}, make_bars([1, 2]))
    with pytest.raises(ValueError, match="missing columns"):
        run({}, make_bars([1, 2]).drop(columns="low"))
    with pytest.raises(ValueError, match="strictly increasing"):
        run({}, make_bars([1, 2]).iloc[::-1])


class Flipper(Strategy):
    """Trades often on a random walk: reverses on every 3-bar momentum change."""

    inputs = dict(qty=2)

    def on_bar(self):
        if self.current_bar < 4:
            return
        up = self.close[0] > self.close[3]
        if up and self.market_position <= 0:
            self.buy("L", self.p.qty)
        elif not up and self.market_position >= 0:
            self.sell_short("S", self.p.qty)


def test_backtest_is_deterministic(tmp_path):
    rng = np.random.default_rng(7)
    closes = 15000 + np.cumsum(rng.normal(0, 4, 2000)).round() * 0.25
    bars = make_bars(np.r_[closes[0], closes[:-1]], closes)
    costs = Costs(commission=4.2, slippage=5.0)
    a = run_backtest(Flipper(), bars, NQ, costs).write_trades_csv(tmp_path / "a.csv")
    b = run_backtest(Flipper(), bars, NQ, costs).write_trades_csv(tmp_path / "b.csv")
    assert a.read_bytes() == b.read_bytes()
    assert len(pd.read_csv(a)) > 100
