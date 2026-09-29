"""Exits and intrabar fills (R4): dollar stop/target, stop and limit orders, magnifier vs OHLC path."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, StrategyError, run_backtest

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
TZ = "America/New_York"
FLAT = (100, 100.5, 99.5, 100)


def bars_from(rows, start="2024-01-02 09:35", freq="5min") -> pd.DataFrame:
    """Close-time-labeled ET bars from (open, high, low, close) rows."""
    a = np.asarray(rows, float)
    idx = pd.date_range(start, periods=len(a), freq=freq, tz=TZ, name="time")
    return pd.DataFrame(
        {"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "volume": 1,
         "session_date": idx.tz_localize(None).normalize()},
        index=idx,
    )  # fmt: skip


def minutes(rows) -> pd.DataFrame:
    return bars_from(rows, "2024-01-02 09:31", "1min")


def five(mins: pd.DataFrame) -> pd.DataFrame:
    """5-minute bars from complete groups of 5 one-minute bars."""
    g = np.arange(len(mins)) // 5
    out = mins.groupby(g).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), session_date=("session_date", "first"),
    )  # fmt: skip
    out.index = mins.index[4::5]
    return out


class Script(Strategy):
    """Calls the methods listed per bar index: {bar: [(method, kwargs), ...]}."""

    inputs = dict(plan={})

    def on_bar(self):
        for method, kwargs in self.p.plan.get(self.current_bar - 1, []):
            getattr(self, method)(**kwargs)


def run(plan, bars, costs=Costs(), **kw):
    return run_backtest(Script(plan=plan), bars, NQ, costs, **kw)


def one_trade(res):
    (t,) = res.trades.itertuples()
    return t


LONG_SL100 = {0: [("set_stop_loss", dict(amount=100)), ("buy", {})]}  # long at bar 1 open, stop 5 NQ pts away
LONG_BRACKET = {0: [("set_stop_loss", dict(amount=100)), ("set_profit_target", dict(amount=100)), ("buy", {})]}


# --- dollar stop loss / profit target -------------------------------------------------------------


def test_stop_loss_exits_long_at_its_dollar_level_on_the_entry_bar():
    t = one_trade(run(LONG_SL100, bars_from([FLAT, (100, 102, 94, 95), FLAT])))
    assert (t.entry_bar, t.entry_price, t.exit_bar, t.exit_price, t.exit_name) == (1, 100.0, 1, 95.0, "Stop Loss")
    assert t.gross_pnl == -100.0 and not t.ambiguous


def test_profit_target_is_per_position_for_a_short():
    plan = {0: [("set_profit_target", dict(amount=200)), ("sell_short", dict(qty=2))]}  # 200 / (20 x 2) = 5 pts
    t = one_trade(run(plan, bars_from([FLAT, (100, 101, 96, 97), (97, 98, 94.5, 95)])))
    assert (t.direction, t.exit_bar, t.exit_price, t.exit_name, t.gross_pnl) == ("short", 2, 95.0, "Profit Target", 200.0)


def test_dollar_exit_price_rounds_to_the_tick():
    plan = {0: [("set_stop_loss", dict(amount=107)), ("buy", {})]}  # 5.35 pts -> 94.65 -> 94.75
    t = one_trade(run(plan, bars_from([FLAT, (100, 100.5, 90, 91)])))
    assert t.exit_price == 94.75 and t.gross_pnl == -105.0


def test_gap_through_the_stop_fills_at_the_open():
    t = one_trade(run(LONG_SL100, bars_from([FLAT, FLAT, (90, 91, 89, 90)])))
    assert (t.exit_bar, t.exit_price, t.exit_name, t.ambiguous) == (2, 90.0, "Stop Loss", False)


def test_stop_loss_persists_until_cleared():
    bars = bars_from([FLAT, FLAT, (100, 100.5, 90, 91), FLAT])
    t = one_trade(run({0: [("buy", {})], 1: [("set_stop_loss", dict(amount=100))]}, bars))  # set after entry
    assert (t.exit_bar, t.exit_price) == (2, 95.0)
    res = run({0: LONG_SL100[0], 1: [("set_stop_loss", dict(amount=None))]}, bars)
    assert res.trades.empty and res.open_position.entry_price == 100.0


# --- stop and limit orders ------------------------------------------------------------------------


def test_stop_entries_fill_at_the_stop_or_the_gapped_open_and_last_one_bar():
    buy_stop = {0: [("buy", dict(name="BS", at="stop", price=103))]}
    assert run(buy_stop, bars_from([FLAT, (100, 104, 99, 103.5)])).open_position.entry_price == 103.0
    assert run(buy_stop, bars_from([FLAT, (105, 106, 104, 105)])).open_position.entry_price == 105.0
    res = run(buy_stop, bars_from([FLAT, (100, 102.75, 99, 102), (102, 110, 101, 109)]))
    assert res.open_position is None and res.trades.empty  # not reached on bar 1, expired before bar 2
    short = run({0: [("sell_short", dict(at="stop", price=97))]}, bars_from([FLAT, (100, 101, 96, 97)]))
    assert (short.open_position.direction, short.open_position.entry_price) == (-1, 97.0)


def test_limit_entries_fill_at_the_limit_or_the_better_open():
    buy_limit = {0: [("buy", dict(at="limit", price=97))]}
    assert run(buy_limit, bars_from([FLAT, (100, 101, 96, 98)])).open_position.entry_price == 97.0
    assert run(buy_limit, bars_from([FLAT, (95, 96, 94, 95)])).open_position.entry_price == 95.0
    assert run(buy_limit, bars_from([FLAT, (100, 101, 97.25, 98)])).open_position is None
    short_limit = {0: [("sell_short", dict(at="limit", price=103))]}
    assert run(short_limit, bars_from([FLAT, (100, 104, 99, 101)])).open_position.entry_price == 103.0
    assert run(short_limit, bars_from([FLAT, (105, 106, 104, 105)])).open_position.entry_price == 105.0


def test_bracket_exit_orders_and_stop_reversal():
    bracket = {0: [("buy", {})], 1: [("sell", dict(name="TP", at="limit", price=104)), ("sell", dict(name="SL", at="stop", price=96))]}
    t = one_trade(run(bracket, bars_from([FLAT, FLAT, (100, 104.5, 99, 104)])))
    assert (t.exit_price, t.exit_name, t.ambiguous) == (104.0, "TP", False)

    rev = {0: [("buy", {})], 1: [("sell_short", dict(name="S", at="stop", price=96))]}
    res = run(rev, bars_from([FLAT, FLAT, (100, 101, 95, 96)]))
    t = one_trade(res)
    assert (t.direction, t.exit_price, t.exit_name) == ("long", 96.0, "S")
    assert (res.open_position.direction, res.open_position.entry_price) == (-1, 96.0)


def test_exits_fill_only_while_a_matching_position_is_open():
    assert run({0: [("sell", dict(at="stop", price=99))]}, bars_from([FLAT, (100, 102, 98, 98.5)])).trades.empty
    # Placed while flat, the sell stop fills after the buy stop opens a long earlier in the same bar.
    plan = {0: [("buy", dict(at="stop", price=101)), ("sell", dict(at="stop", price=99))]}
    t = one_trade(run(plan, bars_from([FLAT, (100, 102, 98, 98.5)])))
    assert (t.entry_price, t.exit_price, t.ambiguous) == (101.0, 99.0, True)


def test_entry_blocked_by_the_position_can_fill_after_it_exits_in_the_same_bar():
    plan = {**LONG_SL100, 1: [("buy", dict(name="Re", at="stop", price=99))]}
    res = run(plan, bars_from([FLAT, FLAT, (100, 100.5, 94, 99.5)]))  # 100 -> 100.5 -> 94 -> 99.5
    assert one_trade(res).exit_price == 95.0
    assert (res.open_position.entry_name, res.open_position.entry_price, res.open_position.entry_bar) == ("Re", 99.0, 2)


# --- R4: same-bar stop and target, OHLC path vs magnifier -----------------------------------------


@pytest.mark.parametrize(
    "bar, exit_name, exit_price",
    [
        ((100, 106, 93, 100), "Profit Target", 105.0),  # high nearer the open: high first
        ((100, 107, 94, 100), "Stop Loss", 95.0),  # low nearer: low first
        ((100, 106, 94, 100), "Profit Target", 105.0),  # tie: high first
    ],
)
def test_ohlc_mode_orders_stop_and_target_by_the_path_rule(bar, exit_name, exit_price):
    t = one_trade(run(LONG_BRACKET, bars_from([FLAT, bar])))
    assert (t.exit_name, t.exit_price, t.ambiguous) == (exit_name, exit_price, True)


BAR1_STOP_FIRST = [(100, 100.5, 94, 95), (95, 101, 95, 101), (101, 105.5, 100, 105), (105, 105.25, 104, 105), (105, 105.25, 104, 105)]


def test_magnifier_uses_the_minute_order_where_the_ohlc_rule_guesses_wrong():
    mins = minutes([FLAT] * 5 + BAR1_STOP_FIRST)
    bars = five(mins)
    assert tuple(bars.iloc[1][["open", "high", "low", "close"]]) == (100, 105.5, 94, 105)  # high nearer the open

    mag = run(LONG_BRACKET, bars, sub_bars=mins)
    t = one_trade(mag)
    assert mag.fill_mode == "magnifier" and mag.bars_without_sub_bars == 0
    assert (t.exit_name, t.exit_price, t.exit_bar, t.ambiguous) == ("Stop Loss", 95.0, 1, False)

    ohlc = run(LONG_BRACKET, bars, sub_bars=mins, fill_mode="ohlc")
    t = one_trade(ohlc)
    assert ohlc.fill_mode == "ohlc" and (t.exit_name, t.exit_price, t.ambiguous) == ("Profit Target", 105.0, True)


def test_magnifier_stop_and_target_in_one_minute_use_the_path_rule_and_are_ambiguous():
    mins = minutes([FLAT] * 5 + [(100, 106, 93, 100)] + [FLAT] * 4)
    t = one_trade(run(LONG_BRACKET, five(mins), sub_bars=mins))
    assert (t.exit_name, t.exit_price, t.ambiguous) == ("Profit Target", 105.0, True)


def test_stop_loss_is_live_from_an_intrabar_entry_on_the_remaining_minutes():
    plan = {0: [("set_stop_loss", dict(amount=100)), ("buy", dict(at="stop", price=101))]}  # stop at 96 after entry
    same = minutes([FLAT] * 5 + [(100, 102, 95, 96)] + [(96, 96.5, 95.5, 96)] * 4)
    t = one_trade(run(plan, five(same), sub_bars=same))
    assert (t.entry_price, t.exit_price, t.exit_bar, t.ambiguous) == (101.0, 96.0, 1, True)  # same minute

    later = minutes([FLAT] * 5 + [(100, 101.5, 99.5, 101), (101, 101.25, 100, 100.5), (100.5, 100.75, 95.5, 96), FLAT, FLAT])
    t = one_trade(run(plan, five(later), sub_bars=later))
    assert (t.entry_price, t.exit_price, t.exit_bar, t.ambiguous) == (101.0, 96.0, 1, False)


def test_bars_without_minutes_fall_back_to_their_own_ohlc():
    mins = minutes([FLAT] * 5 + BAR1_STOP_FIRST)
    bars = five(mins)
    res = run(LONG_BRACKET, bars, sub_bars=mins.iloc[:5])
    assert res.bars_without_sub_bars == 1
    assert one_trade(res).exit_name == "Profit Target"  # same as OHLC mode


def test_minutes_from_another_session_are_ignored():
    mins = minutes([FLAT] * 10)
    stray = bars_from([(50, 50, 50, 50)], "2024-01-01 16:00", "1min")
    stray["session_date"] = pd.Timestamp("2024-01-01")
    res = run(LONG_SL100, five(mins), sub_bars=pd.concat([stray, mins]))  # would be out of bar 0's range
    assert res.trades.empty and res.bars_without_sub_bars == 0


# --- slippage and validation ----------------------------------------------------------------------


def test_slippage_ticks_apply_to_stop_fills_but_not_limit_fills():
    costs = Costs(slippage_ticks=1)
    plan = {0: [("set_profit_target", dict(amount=100)), ("buy", dict(at="stop", price=103))]}
    t = one_trade(run(plan, bars_from([FLAT, (100, 104, 99, 103.5), (104, 109, 103.5, 108)]), costs))
    assert (t.entry_price, t.exit_price, t.exit_name) == (103.25, 108.25, "Profit Target")

    plan = {0: [("set_stop_loss", dict(amount=100)), ("buy", dict(at="limit", price=97))]}
    t = one_trade(run(plan, bars_from([FLAT, (100, 101, 96, 98), (97, 98, 91, 92)]), costs))
    assert (t.entry_price, t.exit_price, t.exit_name) == (97.0, 91.75, "Stop Loss")


def test_bad_order_arguments_fail_loudly():
    bars = bars_from([FLAT, FLAT])
    with pytest.raises(StrategyError, match="need a finite price"):
        run({0: [("buy", dict(at="stop"))]}, bars)
    with pytest.raises(StrategyError, match="take no price"):
        run({0: [("buy", dict(price=100))]}, bars)
    with pytest.raises(StrategyError, match="positive USD amount"):
        run({0: [("set_stop_loss", dict(amount=0))]}, bars)


def test_bad_fill_mode_or_sub_bars_fail_loudly():
    mins = minutes([FLAT] * 10)
    bars = five(mins)
    with pytest.raises(ValueError, match="needs sub_bars"):
        run({}, bars, fill_mode="magnifier")
    with pytest.raises(ValueError, match="fill_mode must be"):
        run({}, bars, fill_mode="tick")
    with pytest.raises(ValueError, match="session_date"):
        run({}, bars, sub_bars=mins.drop(columns="session_date"))
    wide = mins.copy()
    wide.iloc[7, wide.columns.get_loc("high")] = 200
    with pytest.raises(ValueError, match="1 sub-bars fall outside"):
        run({}, bars, sub_bars=wide)


class Bracket(Strategy):
    """When flat, brackets the last close with stop entries; exits via dollar stop and target."""

    def on_start(self):
        self.set_stop_loss(60)
        self.set_profit_target(80)

    def on_bar(self):
        if self.market_position == 0:
            self.buy("L", 1, at="stop", price=self.close[0] + 1)
            self.sell_short("S", 1, at="stop", price=self.close[0] - 1)


def test_magnifier_backtest_is_deterministic(tmp_path):
    rng = np.random.default_rng(11)
    closes = 15000 + np.cumsum(rng.normal(0, 4, 5000)).round() * 0.25
    opens = np.r_[closes[0], closes[:-1]]
    wick = np.abs(rng.normal(0, 3, (2, 5000))).round() * 0.25
    rows = np.c_[opens, np.maximum(opens, closes) + wick[0], np.minimum(opens, closes) - wick[1], closes]
    mins = minutes(rows)
    mins["session_date"] = pd.Timestamp("2024-01-02")  # one long session keeps every 5-minute group intact
    bars = five(mins)
    runs = [run_backtest(Bracket(), bars, NQ, Costs(commission=4.2), sub_bars=mins) for _ in range(2)]
    a, b = (r.write_trades_csv(tmp_path / f"{k}.csv") for k, r in zip("ab", runs))
    assert a.read_bytes() == b.read_bytes()
    trades = runs[0].trades
    assert len(trades) > 100 and set(trades["exit_name"]) >= {"Stop Loss", "Profit Target"}


# --- USD slippage per fill (MC: none on limit fills) ----------------------------------------------


def test_usd_slippage_is_half_per_market_or_stop_fill_and_none_on_limit_fills():
    costs = Costs(commission=1.0, slippage=0.5)
    target = one_trade(run(LONG_BRACKET, bars_from([FLAT, (100, 106, 99, 105), FLAT]), costs))  # market in, limit out
    stop = one_trade(run(LONG_BRACKET, bars_from([FLAT, (100, 101, 94, 95), FLAT]), costs))  # market in, stop out
    assert (target.exit_name, target.slippage, target.net_pnl) == ("Profit Target", 0.25, 100 - 1.0 - 0.25)
    assert (stop.exit_name, stop.slippage, stop.net_pnl) == ("Stop Loss", 0.5, -100 - 1.0 - 0.5)
    both_limit = {0: [("buy", dict(at="limit", price=99.5))], 1: [("sell", dict(at="limit", price=100.5))]}
    t = one_trade(run(both_limit, bars_from([FLAT, (100, 100, 99, 99.5), (100, 101, 99.5, 100.5)]), costs))
    assert (t.entry_price, t.exit_price, t.slippage) == (99.5, 100.5, 0.0)
