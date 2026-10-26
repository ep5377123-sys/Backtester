"""Big-trade bubbles (guban.data.bigtrades), the extra_columns pass-through and the Bubble ORB strategy (Phase 18)."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.data.bigtrades import body_bubbles, bubbles_of
from guban.data.features import add_columns
from guban.engine import Costs, Strategy, run_backtest
from guban.engine.strategy import load_strategy_class

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
TZ = "America/New_York"
ORB = load_strategy_class(Path(__file__).resolve().parents[1] / "strategies" / "orb_bubbles.py")


def ticks(rows):
    """(ms after 2025-03-10 14:46:00 UTC = 10:46 ET, price, size, side) -> store tick records."""
    t0 = pd.Timestamp("2025-03-10 14:46:00", tz="UTC")
    return pd.DataFrame({
        "ts_utc": [t0 + pd.Timedelta(milliseconds=ms) for ms, *_ in rows],
        "price": [r[1] for r in rows], "size": [r[2] for r in rows], "side": np.array([r[3] for r in rows], np.int8),
    })  # fmt: skip


def test_bubbles_merge_one_millisecond_of_one_side():
    every = bubbles_of(ticks([
        (5, 100.00, 30, 1), (5, 100.25, 15, 1),  # one 45-lot buy bubble, size-weighted price
        (5, 100.25, 50, -1), (5, 100.00, 20, -1),  # same millisecond, sell side: its own 70-lot sell bubble
        (6, 100.25, 41, 0),  # between the quotes: ignored
        (7, 100.50, 0, 1),  # size 0: adds nothing
        (8, 100.50, 41, 1),  # another millisecond: its own bubble
        (9, 100.50, 9, 1),  # under the floor (10)
        (60_001, 101.0, 12, 1),  # next minute
    ]))  # fmt: skip
    sell = every[every["side"] == -1]
    assert sell["size"].tolist() == [70] and sell["price"].iat[0] == pytest.approx((100.25 * 50 + 100.0 * 20) / 70)
    b = every[every["side"] == 1]
    assert b["size"].tolist() == [45, 41, 12]
    assert b["price"].iat[0] == pytest.approx((100.0 * 30 + 100.25 * 15) / 45)
    # labeled by bar close time: 10:46:00.005 ET -> the 10:47 bar, 10:47:00.001 -> the 10:48 bar
    assert [t.strftime("%H:%M") for t in b["time"]] == ["10:47", "10:47", "10:48"]


def minute_bars(prices, start="2025-03-10 10:47", contract="202503"):
    idx = pd.date_range(start, periods=len(prices), freq="1min", tz=TZ, name="time")
    o, h, l, c = np.array(prices, float).T  # noqa: E741
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1, "contract": contract,
                         "session_date": pd.Timestamp("2025-03-10")}, index=idx)  # fmt: skip


def test_body_bubbles_inside_body_only_and_nan_without_ticks():
    bars = minute_bars([(100, 103, 99, 102), (102, 102, 98, 99), (99, 100, 98, 99.5)])
    bubbles = pd.DataFrame({
        "contract": "202503",
        "time": pd.to_datetime(["2025-03-10 10:47", "2025-03-10 10:47", "2025-03-10 10:47", "2025-03-10 10:48",
                                "2025-03-10 10:49"]).tz_localize(TZ),
        "price": [100.0, 102.5, 101.0, 98.5, 99.5],  # bar 1: edge (in), top wick (out), body (in); bar 2: lower wick
        "size": [60, 90, 45, 70, 50],
    })  # fmt: skip
    ok = pd.DataFrame({"trade_date": ["20250310"], "contract": ["202503"]})
    col = body_bubbles(bars, bubbles, ok)
    assert col.tolist() == [60.0, 0.0, 50.0]
    # chart time zone does not matter
    assert body_bubbles(bars.tz_convert("America/Chicago"), bubbles, ok).tolist() == [60.0, 0.0, 50.0]
    # a bubble of another contract does not count; a day without usable ticks is NaN
    assert body_bubbles(bars, bubbles.assign(contract="202506"), ok).tolist() == [0.0, 0.0, 0.0]
    assert all(math.isnan(v) for v in body_bubbles(bars, bubbles, ok.iloc[:0]))


def test_extra_columns_need_a_1m_timeframe_and_a_known_name():
    bars = minute_bars([(100, 101, 99, 100)])
    with pytest.raises(ValueError, match="--tf 1m"):
        add_columns(bars, ("big_buy_body",), "NQ", "5m")
    with pytest.raises(ValueError, match="unknown extra column"):
        add_columns(bars, ("nope",), "NQ", "1m")
    assert add_columns(bars.assign(big_buy_body=1.0), ("big_buy_body",), "NQ", "5m")["big_buy_body"].iat[0] == 1.0


class Reads(Strategy):
    extra_columns = ("flow",)

    def on_start(self):
        self.seen = []

    def on_bar(self):
        self.seen.append(self.flow[0])


def test_engine_binds_declared_columns_and_refuses_missing_ones():
    bars = minute_bars([(100, 101, 99, 100)] * 3).assign(flow=[1, 2, np.nan])
    s = Reads()
    run_backtest(s, bars, NQ)
    assert s.seen[:2] == [1.0, 2.0] and math.isnan(s.seen[2])
    with pytest.raises(ValueError, match="extra_columns"):
        run_backtest(Reads(), bars.drop(columns="flow"), NQ)


# --- Bubble ORB ---------------------------------------------------------------------------------


def day(breakout, bubble=80.0, after=None, column="big_buy_body"):
    """One session 09:31..15:59: a flat 100..102 ORB, the breakout bar at 09:50 with `bubble` in `column`, then
    `after` bars (default flat at the breakout close)."""
    idx = pd.date_range("2025-03-10 09:31", "2025-03-10 15:59", freq="1min", tz=TZ, name="time")
    n = len(idx)
    o, h, l, c = (np.full(n, 101.0), np.full(n, 102.0), np.full(n, 100.0), np.full(n, 101.0))  # noqa: E741
    k = idx.get_loc(pd.Timestamp("2025-03-10 09:50", tz=TZ))
    o[k], h[k], l[k], c[k] = breakout
    o[k + 1:], h[k + 1:], l[k + 1:], c[k + 1:] = c[k], c[k] + 0.5, c[k] - 0.5, c[k]
    for j, bar in enumerate(after or []):
        o[k + 1 + j], h[k + 1 + j], l[k + 1 + j], c[k + 1 + j] = bar
    flows = {"big_buy_body": np.zeros(n), "big_sell_body": np.zeros(n)}
    flows[column][k] = bubble
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1, "session_date": pd.Timestamp("2025-03-10"),
                         **flows}, index=idx)  # fmt: skip


def trades(bars, **inputs):
    return run_backtest(ORB(**inputs), bars, NQ, Costs(), fill_mode="ohlc").trades


LONG = (101.5, 104.5, 101.25, 104.0)  # closes above the ORB high 102, green, top wick 0.5 < body 2.5
SHORT = (100.5, 100.75, 97.5, 98.0)  # closes below the ORB low 100, red, bottom wick 0.5 < body 2.5


def test_bubble_orb_long_on_the_breakout_close_with_a_one_to_one_bracket():
    # stop 1 tick under the low (101.0), target 107.0; the next bar reaches 107
    t = trades(day(LONG, after=[(104.0, 107.25, 103.5, 107.0)]))
    assert len(t) == 1
    r = t.iloc[0]
    assert r.entry_price == 104.0 and r.exit_price == 107.0 and r.exit_name == "BubbleORB-L TP"
    assert r.risk_usd == pytest.approx((104.0 - 101.0) * 20) and r.planned_rr == pytest.approx(1.0)


def test_bubble_orb_short_mirrors_the_long():
    # stop 1 tick over the high (101.0), target 95.0; the next bar reaches 95
    t = trades(day(SHORT, column="big_sell_body", after=[(98.0, 98.5, 94.75, 95.0)]))
    assert len(t) == 1
    r = t.iloc[0]
    assert r.direction == "short"
    assert r.entry_price == 98.0 and r.exit_price == 95.0 and r.exit_name == "BubbleORB-S TP"
    assert r.risk_usd == pytest.approx((101.0 - 98.0) * 20) and r.planned_rr == pytest.approx(1.0)
    assert trades(day(SHORT, column="big_sell_body"), allow_shorts=False).empty


@pytest.mark.parametrize(
    "breakout, bubble, column",
    [
        (LONG, 60.0, "big_buy_body"),  # bubble must be BIGGER than 60
        (LONG, math.nan, "big_buy_body"),  # no usable ticks
        (LONG, 80.0, "big_sell_body"),  # a sell bubble does not back a long
        ((103.0, 106.0, 101.25, 104.0), 80.0, "big_buy_body"),  # top wick 2.0 > body 1.0
        ((104.5, 105.0, 101.25, 104.0), 80.0, "big_buy_body"),  # red candle above the ORB
        ((101.0, 102.0, 100.75, 101.75), 80.0, "big_buy_body"),  # does not close above the ORB high (102)
        (SHORT, 80.0, "big_buy_body"),  # a buy bubble does not back a short
        ((99.0, 99.25, 96.0, 98.0), 80.0, "big_sell_body"),  # bottom wick 2.0 > body 1.0
        ((97.5, 100.75, 97.25, 98.0), 80.0, "big_sell_body"),  # green candle below the ORB
    ],
)
def test_bubble_orb_skips_bars_that_miss_a_condition(breakout, bubble, column):
    assert trades(day(breakout, bubble, column=column)).empty


def test_bubble_orb_one_trade_per_day_and_stop_one_tick_under_the_low():
    # the stop is hit on the next bar; a second qualifying breakout later that day is not taken
    after = [(104.0, 104.0, 100.5, 101.0), (101.5, 104.5, 101.25, 104.0)]
    bars = day(LONG, after=after)
    bars.iloc[bars.index.get_loc(pd.Timestamp("2025-03-10 09:52", tz=TZ)), bars.columns.get_loc("big_buy_body")] = 80.0
    t = trades(bars)
    assert len(t) == 1 and t.iloc[0].exit_price == 101.0 and t.iloc[0].exit_name == "BubbleORB-L SL"
    assert len(trades(bars, max_trades_per_day=2)) == 2


def test_bubble_orb_time_exit():
    t = trades(day(LONG))
    assert len(t) == 1 and t.iloc[0].exit_name == "BubbleORB-L TimeX" and t.iloc[0].exit_time.strftime("%H:%M") == "15:55"
