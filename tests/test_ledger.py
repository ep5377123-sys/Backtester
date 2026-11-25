"""Trade ledger integrity: every signal fills once, every fill is recorded once, nothing is dropped or double counted."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
COSTS = Costs(commission=1.22, slippage=0.04)


def walk(minutes: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """A random-walk 1-minute series and the 5-minute bars built from it (close-time labels, one session)."""
    rng = np.random.default_rng(seed)
    c = 15000 + np.cumsum(rng.normal(0, 4, minutes)).round() * 0.25
    o = np.r_[c[0], c[:-1]]
    idx = pd.date_range("2024-01-02 09:31", periods=minutes, freq="1min", tz="America/New_York", name="time")
    m = pd.DataFrame({
        "open": o,
        "high": np.maximum(o, c) + rng.integers(0, 4, minutes) * 0.25,
        "low": np.minimum(o, c) - rng.integers(0, 4, minutes) * 0.25,
        "close": c,
        "volume": 1,
    }, index=idx)
    bars = m.resample("5min", label="right", closed="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    for f in (m, bars):
        f["session_date"] = pd.Timestamp("2024-01-02")
    return bars, m


BARS, SUB = walk(6000, seed=11)


def run(strategy: Strategy, mode: str):
    return run_backtest(strategy, BARS, NQ, COSTS, sub_bars=SUB if mode == "magnifier" else None, fill_mode=mode)


class EveryBreak(Strategy):
    """IVB-style: when flat and the bar closes up, buy at the close; stop/target from the next bar, time exit after `hold`."""

    inputs = dict(hold=4)

    def on_start(self):
        self.signal_bars = []

    def on_bar(self):
        if self.current_bar < 2:
            return
        entered = self.market_position == 0 and self.close[0] > self.close[1]
        if entered:
            self.buy("L", 1, at="close")
            self.signal_bars.append(self.current_bar - 1)
            self.stop, self.target = self.close[0] - 3, self.close[0] + 3
        if self.market_position == 1 and self.bars_since_entry >= self.p.hold:
            self.sell("T", at="close")
        elif self.market_position == 1 or entered:
            self.sell("SL", at="stop", price=self.stop)
            self.sell("TP", at="limit", price=self.target)


@pytest.mark.parametrize("mode", ["ohlc", "magnifier"])
def test_every_signal_makes_exactly_one_trade(mode):
    s = EveryBreak()
    res = run(s, mode)
    tr = res.trades
    entries = tr["entry_bar"].tolist() + ([res.open_position.entry_bar] if res.open_position else [])
    assert len(s.signal_bars) > 200
    assert entries == s.signal_bars
    assert set(tr["exit_name"]) <= {"SL", "TP", "T"}
    assert (tr["entry_time"].iloc[1:].to_numpy() >= tr["exit_time"].iloc[:-1].to_numpy()).all()


class RandomOrders(Strategy):
    """Places random orders of every kind; each order gets a unique name so its fills can be traced in the ledger."""

    inputs = dict(seed=0)

    def on_start(self):
        self.rng = np.random.default_rng(self.p.seed)
        self.k = 0
        self.entry_qty = {}
        self.seen = set()

    def on_bar(self):
        if self.market_position:
            self.seen.add(self._broker.position.entry_name)
        r, c = self.rng, self.close[0]
        for _ in range(r.integers(0, 3)):
            self.k += 1
            name = f"O{self.k}"
            action = r.choice(["buy", "sell_short", "sell", "buy_to_cover"])
            at = r.choice(["market", "close", "stop", "limit"])
            price = None if at in ("market", "close") else c + r.integers(-12, 13) * 0.25
            if action in ("buy", "sell_short"):
                qty = int(r.integers(1, 4))
                self.entry_qty[name] = qty
            else:
                qty = None if r.random() < 0.5 else int(r.integers(1, 3))
            getattr(self, action)(name, qty, at=at, price=price)
        if r.random() < 0.05:
            self.set_stop_loss(None if r.random() < 0.3 else float(r.integers(20, 200)))
        if r.random() < 0.05:
            self.set_profit_target(None if r.random() < 0.3 else float(r.integers(20, 200)))


@pytest.mark.parametrize("mode", ["ohlc", "magnifier"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_fill_is_recorded_once(mode, seed):
    s = RandomOrders(seed=seed)
    res = run(s, mode)
    tr, pos = res.trades, res.open_position
    assert len(tr) > 100
    assert tr["trade_no"].tolist() == list(range(1, len(tr) + 1))
    assert np.allclose(tr["cum_net_pnl"], tr["net_pnl"].cumsum())

    groups = (tr["entry_name"] != tr["entry_name"].shift()).cumsum()
    names = tr.groupby(groups, sort=False)["entry_name"].first()
    assert names.is_unique
    first_entry = tr.groupby(groups, sort=False)["entry_time"].first()
    last_exit = tr.groupby(groups, sort=False)["exit_time"].last()
    assert (first_entry.iloc[1:].to_numpy() >= last_exit.iloc[:-1].to_numpy()).all()

    closed_qty = tr.groupby("entry_name")["qty"].sum()
    for name, q in closed_qty.items():
        left = pos.qty if pos is not None and pos.entry_name == name else 0
        assert q + left == s.entry_qty[name], name

    recorded = set(closed_qty.index) | ({pos.entry_name} if pos else set())
    assert s.seen <= recorded
    exits = tr.loc[~tr["exit_name"].isin(list(s.entry_qty) + ["Stop Loss", "Profit Target"]), "exit_name"]
    assert exits.is_unique
    rev = tr["exit_name"].isin(list(s.entry_qty))
    nxt = tr["exit_name"][rev]
    later = {n: t for n, t in zip(tr["entry_name"], tr["entry_time"])} | ({pos.entry_name: pos.entry_time} if pos else {})
    assert all(later[n] == t for n, t in zip(nxt, tr["exit_time"][rev]))

    open_pnl = 0.0 if pos is None else NQ.pnl(pos.entry_price, BARS["close"].iat[-1], pos.qty, pos.direction)
    assert res.equity.iat[-1] == pytest.approx(res.net_profit + open_pnl)


def test_one_exit_when_stop_target_and_exit_orders_meet_in_one_bar():
    bars = pd.DataFrame(
        {"open": [100.0, 100.0, 100.0], "high": [101.0, 101.0, 110.0], "low": [99.0, 99.0, 90.0], "close": [100.0, 100.0, 100.0]},
        index=pd.date_range("2024-01-02 09:35", periods=3, freq="5min", tz="America/New_York"),
    )

    class S(Strategy):
        def on_bar(self):
            if self.current_bar == 1:
                self.buy("L", 1, at="close")
                self.set_stop_loss(20 * 5)
                self.set_profit_target(20 * 5)
            if self.current_bar == 2:
                self.sell("SL", at="stop", price=95)
                self.sell("TP", at="limit", price=105)
                self.sell("MKT")

    res = run_backtest(S(), bars, NQ)
    assert res.trades["exit_name"].tolist() == ["MKT"]
    assert res.open_position is None
