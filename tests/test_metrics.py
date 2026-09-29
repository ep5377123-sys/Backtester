"""Metrics (R6) on a hand-computed 5-trade fixture, and the engine's run-up / drawdown / intraday equity low."""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest
from guban.metrics import compute_metrics, intraday_drawdown, jsonable, trade_stats

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
IDX = pd.date_range("2024-01-02 09:35", periods=6, freq="5min", tz="America/New_York", name="time")


def fixture_trades() -> pd.DataFrame:
    """Net: +100 long, +200 short, 0 short, -50 long, -80 long -> cum 100, 300, 300, 250, 170."""
    net = [100.0, 200.0, 0.0, -50.0, -80.0]
    return pd.DataFrame({
        "trade_no": [1, 2, 3, 4, 5],
        "direction": ["long", "short", "short", "long", "long"],
        "net_pnl": net,
        "cum_net_pnl": np.cumsum(net),
        "entry_bar": [0, 1, 2, 3, 4],
        "exit_bar": [1, 2, 3, 4, 5],
        "exit_time": IDX[1:],
        "commission": 2.0,
        "slippage": 1.0,
        "ambiguous": [False, False, True, False, False],
    })  # fmt: skip


def fixture_metrics() -> dict:
    equity = pd.Series([0, 100, 300, 280, 250, 170], index=IDX, dtype=float)
    low = pd.Series([0, 50, 300, 150, 240, 160], index=IDX, dtype=float)
    sessions = pd.to_datetime(["2024-01-02"] * 3 + ["2024-01-03"] * 3)
    return compute_metrics(fixture_trades(), equity, low, sessions, open_position_pnl=12.5)


def test_trade_stats_all_trades():
    a = fixture_metrics()["all"]
    assert a["net_profit"] == 170
    assert (a["gross_profit"], a["gross_loss"]) == (300, -130)
    assert a["profit_factor"] == pytest.approx(300 / 130)
    assert (a["total_trades"], a["winning_trades"], a["losing_trades"], a["even_trades"]) == (5, 2, 2, 1)
    assert a["percent_profitable"] == pytest.approx(0.4)
    assert (a["avg_trade"], a["avg_winning_trade"], a["avg_losing_trade"]) == (34, 150, -65)
    assert a["ratio_avg_win_avg_loss"] == pytest.approx(150 / 65)
    assert (a["largest_winning_trade"], a["largest_losing_trade"]) == (200, -80)
    assert (a["max_consecutive_winners"], a["max_consecutive_losers"]) == (2, 2)
    assert a["avg_bars_in_trade"] == 1.0
    assert (a["total_commission"], a["total_slippage"], a["ambiguous_fills"]) == (10, 5, 1)


def test_trade_stats_long_and_short():
    m = fixture_metrics()
    lg, sh = m["long"], m["short"]
    assert (lg["net_profit"], lg["total_trades"], lg["winning_trades"], lg["losing_trades"]) == (-30, 3, 1, 2)
    assert lg["profit_factor"] == pytest.approx(100 / 130)
    assert lg["percent_profitable"] == pytest.approx(1 / 3)
    assert (lg["max_consecutive_winners"], lg["max_consecutive_losers"]) == (1, 2)
    assert (sh["net_profit"], sh["total_trades"], sh["winning_trades"], sh["even_trades"]) == (200, 2, 1, 1)
    assert sh["profit_factor"] == math.inf and sh["ratio_avg_win_avg_loss"] == math.inf
    assert sh["avg_losing_trade"] == 0.0 and sh["largest_losing_trade"] == 0.0


def test_drawdowns_return_on_account_and_sharpe():
    acc = fixture_metrics()["account"]
    # closed-trade equity 0, 100, 300, 300, 250, 170: peak 300 -> valley 170 at trade 5's exit
    assert acc["max_drawdown_close_to_close"] == -130
    assert acc["max_drawdown_close_to_close_time"] == IDX[5]
    # peak before each bar 0, 0, 100, 300, 300, 300; bar lows 0, 50, 300, 150, 240, 160 -> worst 150 - 300 at bar 3
    assert acc["max_drawdown_intraday"] == -150
    assert acc["max_drawdown_intraday_time"] == IDX[3]
    assert acc["return_on_account"] == pytest.approx(170 / 150)
    # session-end equity 300, 170 -> daily P&L 300, -130: mean 85, sd 215 * sqrt(2)
    assert acc["sharpe_daily"] == pytest.approx(85 / (215 * math.sqrt(2)) * math.sqrt(252))
    assert (acc["trading_days"], acc["open_position_pnl"]) == (2, 12.5)


def test_intraday_drawdown_without_equity_low_uses_closes():
    eq = pd.Series([0.0, 50.0, 20.0, 80.0, 10.0], index=IDX[:5])
    assert intraday_drawdown(eq).tolist() == [0, 0, -30, 0, -70]


def test_empty_trades_and_json_safety():
    empty = fixture_trades().iloc[:0]
    eq = pd.Series(0.0, index=IDX)
    m = compute_metrics(empty, eq)
    assert m["all"]["total_trades"] == 0 and m["all"]["profit_factor"] is None
    assert m["account"]["return_on_account"] is None and m["account"]["sharpe_daily"] is None
    text = json.dumps(jsonable(fixture_metrics()), allow_nan=False)
    assert json.loads(text)["short"]["profit_factor"] is None  # inf -> null
    assert json.loads(text)["account"]["max_drawdown_intraday_time"] == IDX[3].isoformat()


# --- engine: run-up, drawdown, equity_low ---------------------------------------------------------


def bars_from(ohlc) -> pd.DataFrame:
    a = np.asarray(ohlc, float)
    idx = pd.date_range("2024-01-02 09:35", periods=len(a), freq="5min", tz="America/New_York", name="time")
    return pd.DataFrame({"open": a[:, 0], "high": a[:, 1], "low": a[:, 2], "close": a[:, 3], "volume": 1,
                         "session_date": idx.tz_localize(None).normalize()}, index=idx)  # fmt: skip


class Script(Strategy):
    inputs = dict(plan={})

    def on_bar(self):
        for method, kwargs in self.p.plan.get(self.current_bar - 1, []):
            getattr(self, method)(**kwargs)


def test_runup_and_drawdown_follow_the_path_less_entry_costs():
    bars = bars_from([(100, 101, 99, 100), (101, 102.5, 100, 101.5), (102, 103.5, 101, 102.5), (103, 104.5, 102, 103.5), (104, 105, 103, 104.5)])
    res = run_backtest(Script(plan={0: [("buy", {})], 3: [("sell", {})]}), bars, NQ, Costs(commission=4.0, slippage=2.0))
    (t,) = res.trades.itertuples()
    assert (t.entry_price, t.exit_price, t.net_pnl) == (101, 104, 3 * 20 - 6)
    # seen from the entry at bar 1's open to the exit at bar 4's open: high 104.5 (bar 3), low 100 (bar 1)
    # entry-side cost = (commission + slippage) / 2 = 3
    assert (t.runup, t.drawdown) == ((104.5 - 101) * 20 - 3, (100 - 101) * 20 - 3)
    # equity_low: bar 1 worst = low 100 -> -20; bar 4 exits at the open -> closed net 54
    assert res.equity_low.tolist() == [0, -20, 0, 20, 54]
    assert res.equity.tolist() == [0, 10, 30, 50, 54]


def test_runup_is_floored_at_zero_and_limit_entries_pay_no_entry_slippage():
    bars = bars_from([(100, 100.5, 99.5, 100), (100, 100, 98, 99), (99, 99.5, 97, 98), (98, 98.5, 97.5, 98)])
    res = run_backtest(Script(plan={0: [("buy", {})], 1: [("sell", {})]}), bars, NQ, Costs(commission=4.0, slippage=2.0))
    (t,) = res.trades.itertuples()
    # long from bar 1's open (100, also its high) to bar 2's open (99): never above the entry, low 98
    assert (t.entry_price, t.runup, t.drawdown) == (100, 0.0, (98 - 100) * 20 - 3)
    res = run_backtest(Script(plan={0: [("buy", dict(at="limit", price=99))], 1: [("sell", {})]}), bars, NQ, Costs(commission=4.0, slippage=2.0))
    (t,) = res.trades.itertuples()
    # bar 1 walks 100 -> 100 -> 98 -> 99: the limit fills at 99 on the way down, then sees 98
    assert (t.entry_price, t.drawdown) == (99, (98 - 99) * 20 - 2)  # entry-side cost: commission only
