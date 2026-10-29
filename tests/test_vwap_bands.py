"""strategies/vwap_bands.py: RTH VWAP bands, touch entries at the band, 30-point bracket, re-arm rule."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, run_backtest
from guban.engine.strategy import load_strategy_class

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
TZ = "America/New_York"
VWAPBands = load_strategy_class(Path(__file__).resolve().parents[1] / "strategies" / "vwap_bands.py")


def days(n: int, seed: int) -> pd.DataFrame:
    """n weekdays of 1-minute bars 09:01-16:00 ET (a pre-RTH stretch, then RTH), a tick-rounded random walk."""
    rng = np.random.default_rng(seed)
    frames = []
    for d in pd.bdate_range("2024-01-02", periods=n):
        idx = pd.date_range(d + pd.Timedelta("09:01:00"), d + pd.Timedelta("16:00:00"), freq="1min", tz=TZ, name="time")
        steps = np.round(rng.normal(0, 3.0, (len(idx), 3)) * 4) / 4
        close = 18000 + np.cumsum(steps[:, 0])
        open_ = np.r_[18000.0, close[:-1]]
        high = np.maximum(open_, close) + np.abs(steps[:, 1])
        low = np.minimum(open_, close) - np.abs(steps[:, 2])
        frames.append(pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close, "volume": rng.integers(1, 500, len(idx)),
             "session_date": idx.tz_localize(None).normalize()}, index=idx))  # fmt: skip
    return pd.concat(frames)


def run(bars, mode="ohlc", **inputs):
    s = VWAPBands(**inputs)
    kw = dict(sub_bars=bars) if mode == "magnifier" else {}
    res = run_backtest(s, bars, NQ, Costs(commission=4.0, slippage=5.0), fill_mode=mode, **kw)
    return s, res


def reference_bands(bars: pd.DataFrame, mult: float = 2.0) -> pd.DataFrame:
    t = bars.index.hour * 100 + bars.index.minute
    rth = bars[(t > 930) & (t <= 1600)]
    tp = (rth.high + rth.low + rth.close) / 3
    g = rth.index.date
    sv = rth.volume.groupby(g).cumsum()
    vwap = (rth.volume * tp).groupby(g).cumsum() / sv
    sd = np.sqrt(((rth.volume * tp * tp).groupby(g).cumsum() / sv - vwap**2).clip(lower=0))
    out = pd.DataFrame({"vwap": vwap, "upper": vwap + mult * sd, "lower": vwap - mult * sd})
    return out.reindex(bars.index)


def test_bands_match_a_pandas_reference_and_are_nan_outside_rth():
    bars = days(2, seed=1)
    s, _ = run(bars)
    got = pd.DataFrame(s.plot_series(), index=bars.index)
    ref = reference_bands(bars)
    pd.testing.assert_frame_equal(got, ref, check_dtype=False, rtol=1e-9)
    assert got.loc[got.index.hour == 9].loc[lambda f: f.index.minute <= 30].isna().all().all()


@pytest.mark.parametrize("mode", ["ohlc", "magnifier"])
@pytest.mark.parametrize("seed", [2, 3, 4])
def test_trades_enter_at_the_band_exit_at_30_points_and_re_arm_inside_the_bands(mode, seed):
    bars = days(10, seed)
    s, res = run(bars, mode)
    tr = res.trades
    assert len(tr) > 5
    bands = pd.DataFrame(s.plot_series(), index=bars.index)
    vwap, up, lo = bands.vwap.to_numpy(), bands.upper.to_numpy(), bands.lower.to_numpy()
    close, open_ = bars.close.to_numpy(), bars.open.to_numpy()
    hhmm = bars.index.hour * 100 + bars.index.minute
    prev_exit = None
    for t in tr.itertuples():
        k = t.entry_bar - 1  # the bar that placed the touch order
        assert 945 <= hhmm[k] < 1500
        if t.direction == "long":
            assert close[k] < vwap[k] and t.entry_name == "VWAP-L MIT"
            band = math.floor(lo[k] / 0.25 + 1e-9) * 0.25
            assert t.entry_price == min(band, open_[t.entry_bar])
        else:
            assert close[k] > vwap[k] and t.entry_name == "VWAP-S MIT"
            band = math.ceil(up[k] / 0.25 - 1e-9) * 0.25
            assert t.entry_price == max(band, open_[t.entry_bar])
        sign = 1 if t.direction == "long" else -1
        if t.exit_name == "Stop Loss":
            assert t.exit_price == t.entry_price - sign * 30
        elif t.exit_name == "Profit Target":
            assert t.exit_price == t.entry_price + sign * 30
        else:
            assert t.exit_name.endswith("TimeX") and hhmm[t.exit_bar] == 1555
        # the touch entry pays half the slippage; a profit target (limit) exit pays none
        assert t.slippage == (2.5 if t.exit_name == "Profit Target" else 5.0)
        assert t.risk_usd == 600.0 and t.reward_usd == 600.0
        if prev_exit is not None:
            assert t.entry_bar > prev_exit  # one position at a time, no reversal
            same_day = bars.index[prev_exit].date() == bars.index[t.entry_bar].date()
            if same_day:  # re-armed: a close strictly inside the bands, flat, between the trades
                between = range(prev_exit, t.entry_bar)
                assert any(lo[j] < close[j] < up[j] for j in between)
        prev_exit = t.exit_bar
    assert res.open_position is None


def test_max_trades_per_day_caps_entries():
    bars = days(10, seed=3)
    _, res = run(bars, max_trades_per_day=1)
    per_day = res.trades.entry_time.dt.date.value_counts()
    assert per_day.max() == 1 and len(per_day) > 1
