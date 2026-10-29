"""strategies/ten_am_retest.py: 50-point displacement from the 10:00 ET open, limit entry back at the open, 15/60 bracket."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, run_backtest
from guban.engine.strategy import load_strategy_class

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
TZ = "America/New_York"
TenAMRetest = load_strategy_class(Path(__file__).resolve().parents[1] / "strategies" / "ten_am_retest.py")


def days(n: int, seed: int, sigma: float = 12.0) -> pd.DataFrame:
    """n weekdays of 1-minute bars 09:31-16:00 ET, a tick-rounded walk pulled back toward 18000 (so it retests)."""
    rng = np.random.default_rng(seed)
    frames = []
    for d in pd.bdate_range("2024-01-02", periods=n):
        idx = pd.date_range(d + pd.Timedelta("09:31:00"), d + pd.Timedelta("16:00:00"), freq="1min", tz=TZ, name="time")
        steps = np.round(rng.normal(0, sigma, (len(idx), 3)) * 4) / 4
        x = np.zeros(len(idx))
        for k in range(1, len(idx)):
            x[k] = np.round((0.97 * x[k - 1] + steps[k, 0]) * 4) / 4
        close = 18000 + x
        steps[:, 1:] /= 3  # wicks
        open_ = np.r_[18000.0, close[:-1]]
        high = np.maximum(open_, close) + np.abs(steps[:, 1])
        low = np.minimum(open_, close) - np.abs(steps[:, 2])
        frames.append(pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": 1,
             "session_date": idx.tz_localize(None).normalize()}, index=idx))  # fmt: skip
    return pd.concat(frames)


def run(bars, mode="ohlc", **inputs):
    s = TenAMRetest(**inputs)
    kw = dict(sub_bars=bars) if mode == "magnifier" else {}
    res = run_backtest(s, bars, NQ, Costs(commission=4.0, slippage=5.0), fill_mode=mode, **kw)
    return s, res


def expected_entries(bars: pd.DataFrame, exits: dict[int, int], disp=50.0, cutoff=1230, max_trades=2):
    """Reference: (entry bar, direction, anchor) per setup, given the engine's exit bar for each entry bar."""
    o, h, l, c = (bars[k].to_numpy() for k in ("open", "high", "low", "close"))  # noqa: E741
    hhmm = bars.index.hour * 100 + bars.index.minute
    out = []
    for _, pos in pd.Series(np.arange(len(bars)), index=bars.index).groupby(bars.index.date):
        pos = pos.to_numpy()
        first = pos[hhmm[pos] > 1000][0]
        anchor, i, n = o[first], first, 0
        while n < max_trades:
            side = 0
            while i <= pos[-1] and hhmm[i] < cutoff:
                up, dn = h[i] >= anchor + disp, l[i] <= anchor - disp
                if up or dn:
                    side = (1 if c[i] >= o[i] else -1) if up and dn else (1 if up else -1)
                    break
                i += 1
            if not side:
                break
            j = i + 1  # the order placed on bar j - 1 can fill on bar j
            while j <= pos[-1] and hhmm[j - 1] < cutoff and not (l[j] <= anchor if side == 1 else h[j] >= anchor):
                j += 1
            if j > pos[-1] or hhmm[j - 1] >= cutoff:
                break
            out.append((j, side, anchor))
            n += 1
            i = exits[j] + 1
    return out


@pytest.mark.parametrize("mode", ["ohlc", "magnifier"])
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_trades_match_the_reference_rules(mode, seed):
    bars = days(30, seed)
    _, res = run(bars, mode)
    tr = res.trades
    assert len(tr) > 10 and res.open_position is None
    want = expected_entries(bars, dict(zip(tr.entry_bar, tr.exit_bar)))
    assert [(t.entry_bar, 1 if t.direction == "long" else -1) for t in tr.itertuples()] == [(j, s) for j, s, _ in want]
    hhmm = bars.index.hour * 100 + bars.index.minute
    opens = bars.open.to_numpy()
    for t, (j, side, anchor) in zip(tr.itertuples(), want):
        assert t.entry_price == (min(anchor, opens[j]) if side == 1 else max(anchor, opens[j]))  # limit: at or better
        assert hhmm[j] <= 1230
        if t.exit_name == "Stop Loss":
            assert t.exit_price == t.entry_price - side * 15
        elif t.exit_name == "Profit Target":
            assert t.exit_price == t.entry_price + side * 60
        else:
            assert t.exit_name.endswith("TimeX") and hhmm[t.exit_bar] == 1555
        assert t.risk_usd == 300.0 and t.reward_usd == 1200.0
    per_day = tr.entry_time.dt.date.value_counts()
    assert per_day.max() == 2  # the second-setup path is exercised, never a third


def test_a_long_after_a_50_point_rally_and_a_retest_of_the_open():
    bars = days(1, seed=0, sigma=0.0)  # flat at 18000 all day
    t = bars.index.hour * 100 + bars.index.minute
    bars.loc[t == 1030, "high"] = 18050.0  # displacement up
    bars.loc[t == 1100, "low"] = 18000.0  # the retest: the buy limit at the open fills here
    bars.loc[(t >= 1031) & (t < 1100), ["open", "high", "low", "close"]] = 18040.0
    bars.loc[t == 1100, ["open", "high", "close"]] = 18040.0
    bars.loc[t == 1110, "high"] = 18060.0  # profit target
    bars.loc[(t > 1100) & (t < 1110), ["open", "high", "low", "close"]] = 18030.0
    bars.loc[t == 1110, ["open", "low", "close"]] = 18030.0
    bars.loc[t > 1110, ["open", "high", "low", "close"]] = 18030.0
    _, res = run(bars)
    tr = res.trades
    assert len(tr) == 1
    row = tr.iloc[0]
    assert row.direction == "long" and row.entry_price == 18000.0 and row.exit_name == "Profit Target"
    assert row.exit_price == 18060.0


def test_no_trade_when_the_move_comes_after_the_cutoff():
    bars = days(1, seed=0, sigma=0.0)
    t = bars.index.hour * 100 + bars.index.minute
    bars.loc[t == 1240, "high"] = 18050.0
    bars.loc[t > 1240, ["open", "high", "low", "close"]] = 17990.0
    _, res = run(bars)
    assert res.trades.empty and res.open_position is None


def test_max_trades_per_day_caps_entries():
    bars = days(30, seed=2)
    _, res = run(bars, max_trades_per_day=1)
    per_day = res.trades.entry_time.dt.date.value_counts()
    assert per_day.max() == 1 and len(per_day) > 5
