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


def test_plots_record_series_values_at_every_bar_and_change_nothing():
    class Plotted(Strategy):
        plots = ("fast", "slow_named")

        def on_start(self):
            self.fast = self.series(0.0)
            self._slow = self.series(0.0, name="slow_named")

        def on_bar(self):
            self.fast.value = self.close[0]
            if self.current_bar % 2 == 0:
                self._slow.value = self.close[0] * 2
            if self.current_bar == 2:
                self.buy()

    bars = make_bars([1, 2, 3, 4, 5], closes=[1, 2, 3, 4, 5])
    strat = Plotted()
    res = run_backtest(strat, bars, NQ)
    assert strat.plot_series() == {"fast": [1, 2, 3, 4, 5], "slow_named": [0.0, 4, 4, 8, 8]}
    assert res.equity.equals(run_backtest(Script(plan={1: [("buy", {})]}), bars, NQ).equity)

    class Missing(Plotted):
        plots = ("nope",)

    strat = Missing()
    run_backtest(strat, bars, NQ)
    with pytest.raises(ValueError, match="plots names 'nope'"):
        strat.plot_series()


def test_series_outside_on_start_is_an_error():
    class Early(Strategy):
        def __init__(self):
            super().__init__()
            self.series()

    with pytest.raises(RuntimeError, match="on_start"):
        Early()



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
    res = run({0: [("buy", {})], 3: [("sell", {})]}, bars)
    assert res.trades.empty
    assert res.open_position.entry_price == 101.0 and res.open_position.qty == 1



def test_close_order_fills_at_the_signal_bars_close():
    """at="close" is MC's `this bar on close`: it fills at the close of the bar that placed it, even the last one."""
    bars = make_bars([100, 101, 102, 103, 104], closes=[100.5, 101.25, 102.5, 103.75, 104.25])
    res = run({1: [("buy", dict(name="L", at="close"))], 2: [("sell", dict(name="X", at="close"))],
               3: [("sell_short", dict(name="S", at="close"))], 4: [("buy_to_cover", dict(name="C", at="close"))]}, bars)
    long_, short = res.trades.itertuples()
    assert (long_.entry_bar, long_.entry_time, long_.entry_price) == (1, bars.index[1], 101.25)
    assert (long_.exit_bar, long_.exit_time, long_.exit_price, long_.exit_name) == (2, bars.index[2], 102.5, "X")
    assert (short.entry_bar, short.entry_price, short.exit_bar, short.exit_price) == (3, 103.75, 4, 104.25)
    assert res.open_position is None
    assert res.equity.iloc[1] == 0.0


def test_close_order_position_is_visible_from_the_next_bar():
    states = []

    class S(Strategy):
        def on_bar(self):
            if self.current_bar == 1:
                self.buy(qty=2, at="close")
            states.append((self.market_position, self.entry_price, self.bars_since_entry))

    run_backtest(S(), make_bars([100, 101, 102]), NQ)
    assert states == [(0, None, None), (1, 100.5, 1), (1, 100.5, 2)]


def test_big_point_value_is_the_traded_contracts():
    seen = []

    class S(Strategy):
        def on_bar(self):
            seen.append(self.big_point_value)

    run_backtest(S(), make_bars([100]), NQ)
    assert seen == [20.0]


def test_close_orders_are_market_fills_for_costs_and_slippage():
    bars = make_bars([100, 101, 102, 103])
    res = run({0: [("buy", dict(at="close"))], 2: [("sell", dict(at="close"))]}, bars, Costs(commission=1.22, slippage=0.04, slippage_ticks=1))
    (t,) = res.trades.itertuples()
    assert (t.entry_price, t.exit_price) == (100.75, 102.25)
    assert t.slippage == pytest.approx(0.04)
    assert t.net_pnl == pytest.approx(1.5 * 20 - 1.22 - 0.04)


def test_close_entry_exits_start_on_the_next_bar_and_mix_with_next_bar_orders():
    bars = make_bars([100, 90, 100, 100], closes=[100.5, 95, 100.5, 100.5])
    res = run({1: [("buy", dict(at="close")), ("set_stop_loss", dict(amount=20 * 4))],
               2: [("sell", dict(name="next open"))]}, bars)
    (t,) = res.trades.itertuples()
    assert (t.entry_bar, t.entry_price) == (1, 95.0)
    assert (t.exit_bar, t.exit_price, t.exit_name) == (3, 100.0, "next open")


def test_close_orders_do_not_fill_before_trade_from():
    bars = make_bars([100, 101, 102, 103])
    plan = {0: [("buy", dict(at="close"))], 1: [("buy", dict(at="close"))], 2: [("buy", dict(at="close"))]}
    res = run_backtest(Script(plan=plan), bars, NQ, trade_from=2)
    assert res.trades.empty
    assert (res.open_position.entry_bar, res.open_position.entry_price) == (2, 102.5)


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
        0: [("buy_to_cover", {}), ("sell", {})],
        1: [("buy", dict(qty=1))],
        2: [("buy", dict(qty=5)), ("buy_to_cover", {})],
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



def test_pnl_uses_point_value_contracts_and_costs():
    bars = make_bars([100, 110, 120, 110, 100, 90])
    costs = Costs(commission=4.2, slippage=5.0)
    res = run({0: [("buy", dict(qty=2))], 1: [("sell", {})], 2: [("sell_short", dict(qty=3))], 4: [("buy_to_cover", {})]}, bars, costs)
    long_, short = res.trades.itertuples()
    assert (long_.gross_pnl, long_.commission, long_.slippage, long_.net_pnl) == (400.0, 8.4, 10.0, 400.0 - 18.4)
    assert short.gross_pnl == (110 - 90) * 20 * 3
    assert short.net_pnl == pytest.approx(1200 - 3 * 9.2)
    assert short.cum_net_pnl == pytest.approx(long_.net_pnl + short.net_pnl)
    assert res.net_profit == pytest.approx(short.cum_net_pnl)


def test_equity_is_closed_net_plus_open_gross_at_each_close():
    bars = make_bars([100, 101, 102, 103], closes=[100, 105, 106, 103])
    res = run({0: [("buy", {})], 1: [("sell", {})]}, bars, Costs(commission=2.0))
    assert res.equity.tolist() == [0.0, 80.0, 18.0, 18.0]



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
    with pytest.raises(StrategyError, match=r"bar 3 \(2024-01-02 09:45:00-05:00\), position \+1 x1 @ 101.0 since bar 2:") as e:
        run_backtest(Boom(), bars, NQ)
    assert isinstance(e.value.__cause__, ZeroDivisionError)


def test_bad_orders_and_bars_fail_loudly():
    with pytest.raises(StrategyError, match="not supported"):
        run({0: [("buy", dict(at="moc"))]}, make_bars([1, 2]))
    with pytest.raises(StrategyError, match="close orders take no price"):
        run({0: [("buy", dict(at="close", price=1.0))]}, make_bars([1, 2]))
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


FLAT = [100.0] * 8


def test_set_stop_loss_and_profit_target_give_risk_reward_and_r():
    plan = {0: [("buy", dict(qty=2)), ("set_stop_loss", dict(amount=800)), ("set_profit_target", dict(amount=1600))],
            3: [("sell", {})]}
    (t,) = run(plan, make_bars(FLAT), Costs(commission=4)).trades.itertuples()
    assert (t.risk_usd, t.reward_usd, t.planned_rr) == (800.0, 1600.0, 2.0)
    assert t.net_pnl == -8.0 and t.r_multiple == pytest.approx(-0.01)


def test_stop_and_limit_exit_orders_placed_with_a_close_entry_give_risk_reward():
    plan = {1: [("buy", dict(at="close")), ("sell", dict(name="SL", at="stop", price=90)), ("sell", dict(name="TP", at="limit", price=120))],
            4: [("sell", {})]}
    (t,) = run(plan, make_bars(FLAT)).trades.itertuples()
    assert t.entry_price == 100.5
    assert (t.risk_usd, t.reward_usd) == (210.0, 390.0) and t.planned_rr == pytest.approx(390 / 210)


def test_protection_placed_after_the_entry_counts_from_when_it_is_live():
    plan = {0: [("buy", {})], 2: [("sell", dict(at="stop", price=95))], 3: [("set_profit_target", dict(amount=300))], 5: [("sell", {})]}
    (t,) = run(plan, make_bars(FLAT)).trades.itertuples()
    assert (t.risk_usd, t.reward_usd, t.planned_rr) == (100.0, 300.0, 3.0)


def test_no_stop_or_a_stop_on_the_wrong_side_leaves_risk_empty():
    plain = run({0: [("buy", {})], 2: [("sell", {})]}, make_bars(FLAT)).trades
    assert plain[["risk_usd", "reward_usd", "planned_rr", "r_multiple"]].isna().all(axis=None)
    wrong = run({0: [("buy", {}), ("sell", dict(at="stop", price=150)), ("sell", dict(at="limit", price=50))], 2: [("sell", {})]}, make_bars(FLAT)).trades
    assert wrong[["risk_usd", "reward_usd"]].isna().all(axis=None)


def test_the_tightest_stop_and_nearest_target_win():
    plan = {0: [("buy", dict(qty=2)), ("set_stop_loss", dict(amount=800)), ("sell", dict(at="stop", price=90)),
                ("set_profit_target", dict(amount=1600)), ("sell", dict(at="limit", price=110))],
            3: [("sell", {})]}
    (t,) = run(plan, make_bars(FLAT)).trades.itertuples()
    assert (t.risk_usd, t.reward_usd) == (400.0, 400.0)


def test_partial_exits_split_the_risk_and_a_reversal_takes_its_own():
    plan = {0: [("buy", dict(qty=3)), ("set_stop_loss", dict(amount=600))], 1: [("sell", dict(qty=1))],
            2: [("sell_short", dict(name="S", qty=1))], 4: [("buy_to_cover", {})]}
    tr = run(plan, make_bars(FLAT)).trades
    assert tr["risk_usd"].tolist() == [200.0, 400.0, 600.0]
    assert tr["direction"].tolist() == ["long", "long", "short"]


def test_window_end_trades_carry_their_risk():
    plan = {0: [("buy", {}), ("set_stop_loss", dict(amount=200))]}
    res = run_backtest(Script(plan=plan), make_bars(FLAT), NQ, flatten_at_end=True)
    (t,) = res.trades.itertuples()
    assert (t.exit_name, t.risk_usd) == ("Window End", 200.0)
