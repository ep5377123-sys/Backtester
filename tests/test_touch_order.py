"""Market-if-touched orders (at="touch"): trigger like a limit, fill and pay slippage like a market order."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
TZ = "America/New_York"
FLAT = (100, 100.5, 99.5, 100)


def bars_from(rows, start="2024-01-02 09:35", freq="5min") -> pd.DataFrame:
    a = np.asarray(rows, float)
    idx = pd.date_range(start, periods=len(a), freq=freq, tz=TZ, name="time")
    return pd.DataFrame(
        {"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "volume": 1,
         "session_date": idx.tz_localize(None).normalize()},
        index=idx,
    )  # fmt: skip


class Script(Strategy):
    inputs = dict(plan={})

    def on_bar(self):
        for method, kwargs in self.p.plan.get(self.current_bar - 1, []):
            getattr(self, method)(**kwargs)


def run(plan, bars, costs=Costs(), **kw):
    return run_backtest(Script(plan=plan), bars, NQ, costs, **kw)


def test_touch_entries_trigger_like_a_limit_and_fill_at_the_price_or_the_open():
    buy = {0: [("buy", dict(at="touch", price=97))]}
    assert run(buy, bars_from([FLAT, (100, 101, 96, 98)])).open_position.entry_price == 97.0
    assert run(buy, bars_from([FLAT, (95, 96, 94, 95)])).open_position.entry_price == 95.0  # gapped through
    assert run(buy, bars_from([FLAT, (100, 101, 97.25, 98)])).open_position is None  # not touched
    short = {0: [("sell_short", dict(at="touch", price=103))]}
    pos = run(short, bars_from([FLAT, (100, 104, 99, 101)])).open_position
    assert (pos.direction, pos.entry_price) == (-1, 103.0)
    assert run(short, bars_from([FLAT, (105, 106, 104, 105)])).open_position.entry_price == 105.0


def test_touch_fills_pay_slippage_like_a_market_order():
    bars = bars_from([FLAT, (100, 101, 96, 98), FLAT])
    for kind, slipped in (("touch", True), ("limit", False)):
        plan = {0: [("buy", dict(at="touch" if kind == "touch" else "limit", price=97))], 1: [("sell", {})]}
        (t,) = run(plan, bars, Costs(slippage=10.0)).trades.itertuples()
        assert t.slippage == (10.0 if slipped else 5.0)  # half per market-type fill; the exit is a market fill
        pos = run({0: plan[0]}, bars, Costs(slippage_ticks=1)).open_position
        assert pos.entry_price == (97.25 if slipped else 97.0) and pos.entry_slip is slipped


def test_touch_exit_is_a_slipped_target():
    plan = {0: [("set_stop_loss", dict(amount=100)), ("buy", {})], 1: [("sell", dict(name="TT", at="touch", price=104))]}
    res = run(plan, bars_from([FLAT, FLAT, (100, 104.5, 99, 104)]), Costs(slippage=10.0, slippage_ticks=1))
    (t,) = res.trades.itertuples()
    assert (t.exit_name, t.exit_price, t.slippage) == ("TT", 103.75, 10.0)  # sell moved down a tick
    assert t.reward_usd == pytest.approx((104 - 100.25) * 20) and not math.isnan(t.risk_usd)


def test_touch_short_on_one_minute_sub_bars():
    mins = bars_from([FLAT] * 5 + [(100, 101, 99, 100), (100, 103.5, 100, 103), FLAT, FLAT, FLAT], "2024-01-02 09:31", "1min")
    five = mins.groupby(np.arange(10) // 5).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"),
        volume=("volume", "sum"), session_date=("session_date", "first"),
    )  # fmt: skip
    five.index = mins.index[4::5]
    pos = run({0: [("sell_short", dict(at="touch", price=103))]}, five, sub_bars=mins).open_position
    assert (pos.direction, pos.entry_price, pos.entry_sub) == (-1, 103.0, (1, 1))


def test_touch_orders_need_a_price():
    with pytest.raises(Exception, match="touch orders need a finite price"):
        run({0: [("buy", dict(at="touch"))]}, bars_from([FLAT, FLAT]))
