"""strategies/typo_vm.py: the Pine indicator's signals (vs a pandas reference), windows, stop-and-reverse, 20/20 bracket."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, run_backtest
from guban.engine.strategy import load_strategy_class

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
TZ = "America/New_York"
TypoVM = load_strategy_class(Path(__file__).resolve().parents[1] / "strategies" / "typo_vm.py")
Chain = sys.modules[TypoVM.__module__]._Chain

DAY = dict(ma_len=10, ma_type="EMA", norm_len=60, reset="Daily", sig_lookback=3, div_bars=5, min_between=1)
NIGHT = dict(ma_len=14, ma_type="HMA", norm_len=100, reset="None", sig_lookback=2, div_bars=4, min_between=5)
WINDOWS = ((800, 1130), (1330, 1659), (1800, 2000))


def sessions(n: int, seed: int, early_close: int | None = None) -> pd.DataFrame:
    """n Globex sessions of 1-minute bars, 18:01 the evening before to 17:00 ET, a tick-rounded random walk with
    some flat (H == L) and zero-volume bars. Session `early_close` (an index) closes at 13:00 instead of 17:00."""
    rng = np.random.default_rng(seed)
    frames, last = [], 18000.0
    for i, d in enumerate(pd.bdate_range("2024-01-02", periods=n)):
        idx = pd.date_range(d - pd.Timedelta("05:59:00"), d + pd.Timedelta("17:00:00"), freq="1min", tz=TZ, name="time")
        steps = np.round(rng.normal(0, 2.0, (len(idx), 3)) * 4) / 4
        close = last + np.cumsum(steps[:, 0])
        open_ = np.r_[last, close[:-1]]
        high = np.maximum(open_, close) + np.abs(steps[:, 1])
        low = np.minimum(open_, close) - np.abs(steps[:, 2])
        flat = rng.random(len(idx)) < 0.02
        high[flat] = low[flat] = open_[flat] = close[flat] = np.r_[last, close[:-1]][flat]
        last = close[-1]
        close_at = d + pd.Timedelta("13:00:00" if i == early_close else "17:00:00")
        frames.append(pd.DataFrame(
            {"open": open_, "high": high, "low": low, "close": close,
             "volume": rng.integers(0, 400, len(idx)) * (rng.random(len(idx)) > 0.01),
             "session_date": pd.Timestamp(d), "session_close": close_at.tz_localize(TZ)}, index=idx))  # fmt: skip
    return pd.concat(frames)


def pine_signals(bars, ma_len, ma_type, norm_len, reset, sig_lookback, div_bars, min_between,
                 norm_method="Min-Max", mode="Early", weak=True, weak_threshold=50.0):  # fmt: skip
    """The Pine script's buy_sig / sell_sig, written with pandas rolling functions (Pine semantics, see typo_vm.py)."""
    h, l, c, v = (bars[k].astype(float) for k in ("high", "low", "close", "volume"))
    rng = h - l
    close_pos = ((c - l) / rng).where(rng > 0, 0.5)
    delta = v * close_pos - v * (1 - close_pos)
    sess = bars["session_date"]
    key = {"Daily": sess, "Weekly": sess.map(lambda s: s.isocalendar()[:2]), "None": pd.Series(0, index=bars.index)}[reset]
    cvd = delta.groupby((key != key.shift()).cumsum()).cumsum()
    if norm_method == "Min-Max":
        hi, lo = cvd.rolling(norm_len).max(), cvd.rolling(norm_len).min()
        norm = ((cvd - lo) / (hi - lo) * 200 - 100).where(hi - lo > 0, 0.0)
    else:
        mean, std = cvd.rolling(norm_len).mean(), cvd.rolling(norm_len).std(ddof=0)
        norm = ((cvd - mean) / std * 33.33).clip(-100, 100).where(std > 0, 0.0)
    if ma_type == "EMA":
        x, ma, alpha = norm.to_numpy(), np.full(len(norm), np.nan), 2 / (ma_len + 1)
        ma[ma_len - 1] = x[:ma_len].mean()
        for i in range(ma_len, len(x)):
            ma[i] = alpha * x[i] + (1 - alpha) * ma[i - 1]
        ma = pd.Series(ma, index=norm.index)
    else:
        def wma(s, n):
            w = np.arange(1, n + 1)
            return s.rolling(n).apply(lambda a: a @ w / w.sum(), raw=True)
        ma = wma(2 * wma(norm, ma_len // 2) - wma(norm, ma_len), int(np.sqrt(ma_len)))
    cross_up = (norm > ma) & (norm.shift() <= ma.shift())
    cross_down = (norm < ma) & (norm.shift() >= ma.shift())
    back = norm.shift(sig_lookback).fillna(0.0)
    bull = (l < l.shift().rolling(div_bars).min()) & (cvd > cvd.shift().rolling(div_bars).min()) & (delta > 0)
    bear = (h > h.shift().rolling(div_bars).max()) & (cvd < cvd.shift().rolling(div_bars).max()) & (delta < 0)
    raw_buy, raw_sell = bull & (norm > back), bear & (norm < back)
    if mode == "Confirmed":
        raw_buy, raw_sell = raw_buy & cross_up, raw_sell & cross_down
    final_buy = raw_buy | (weak & cross_up & (norm < -weak_threshold) & (delta > 0))
    final_sell = raw_sell | (weak & cross_down & (norm > weak_threshold) & (delta < 0))
    out = []
    for final in (final_buy.to_numpy(), final_sell.to_numpy()):
        sig, last = np.zeros(len(final), bool), -999
        for i in range(len(final)):
            if final[i] and not (i > 0 and final[i - 1]) and i - last >= min_between:
                sig[i], last = True, i
        out.append(sig)
    return out[0], out[1]


def chain_signals(bars, **cfg):
    norm_method, mode = cfg.pop("norm_method", "Min-Max"), cfg.pop("mode", "Early")
    ch = Chain(norm_method=norm_method, mode=mode, weak=True, weak_threshold=50.0, **cfg)
    sess = bars["session_date"].tolist()
    buys, sells = [], []
    for i, (h, l, c, v) in enumerate(bars[["high", "low", "close", "volume"]].itertuples(index=False)):
        new_day = i > 0 and sess[i] != sess[i - 1]
        new_week = new_day and sess[i].isocalendar()[:2] != sess[i - 1].isocalendar()[:2]
        b, s, _ = ch.update(h, l, c, v, new_day, new_week)
        buys.append(b)
        sells.append(s)
    return np.array(buys), np.array(sells)


@pytest.mark.parametrize("cfg", [
    DAY, NIGHT,
    {**DAY, "reset": "Weekly", "norm_method": "Z-Score"},
    {**NIGHT, "ma_len": 9, "mode": "Confirmed", "reset": "Daily"},
    {**DAY, "ma_type": "HMA", "min_between": 3, "norm_method": "Z-Score", "mode": "Confirmed"},
])  # fmt: skip
def test_chain_matches_a_pandas_reference_of_the_pine_script(cfg):
    bars = sessions(7, seed=1)  # spans a weekend
    got_buy, got_sell = chain_signals(bars, **dict(cfg))
    ref_buy, ref_sell = pine_signals(bars, **cfg)
    assert got_buy.sum() > 5 and got_sell.sum() > 5
    np.testing.assert_array_equal(got_buy, ref_buy)
    np.testing.assert_array_equal(got_sell, ref_sell)
    assert not (got_buy & got_sell).any()


def run(bars, mode="ohlc", **inputs):
    s = TypoVM(**inputs)
    kw = dict(sub_bars=bars) if mode == "magnifier" else {}
    return run_backtest(s, bars, NQ, Costs(commission=4.0, slippage=5.0), fill_mode=mode, **kw)


@pytest.mark.parametrize("mode", ["ohlc", "magnifier"])
@pytest.mark.parametrize("seed", [2, 3])
def test_every_window_signal_enters_or_reverses_and_nothing_is_held_outside_the_windows(mode, seed):
    bars = sessions(6, seed, early_close=3)
    res = run(bars, mode)
    tr = res.trades
    n = len(bars)
    hhmm = (bars.index.hour * 100 + bars.index.minute).to_numpy()
    day_b, day_s = pine_signals(bars, **DAY)
    night_b, night_s = pine_signals(bars, **NIGHT)
    overnight = (hhmm >= 1800) | (hhmm < 800)
    side = np.where(overnight, night_b.astype(int) - night_s, day_b.astype(int) - day_s)
    tradable = np.zeros(n, bool)
    for start, end in WINDOWS:
        tradable |= (hhmm >= start) & (hhmm < end)
    close_at = pd.DatetimeIndex(bars["session_close"])
    early = (close_at.hour == 13) & (close_at.date == bars.index.date)
    tradable &= ~(early & (hhmm >= 1250))

    direction = tr["direction"].map({"long": 1, "short": -1}).to_numpy()
    pos = np.zeros(n, int)  # position at each bar's close
    for d, a, b in zip(direction, tr["entry_bar"], tr["exit_bar"]):
        pos[a:b] = d
    assert res.open_position is None
    assert (pos[~tradable] == 0).all()  # flat outside the windows, the early-close cut included

    expected = [(k + 1, side[k]) for k in range(n - 1) if tradable[k] and side[k] != 0 and pos[k] != side[k]]
    assert len(expected) > 20
    assert list(zip(tr["entry_bar"], direction)) == expected  # one entry per acted-on signal, none extra

    exits = set()
    for i, t in enumerate(tr.itertuples()):
        sign = 1 if t.direction == "long" else -1
        if t.exit_name == "Stop Loss":
            assert t.exit_price == t.entry_price - sign * 20
        elif t.exit_name == "Profit Target":
            assert t.exit_price == t.entry_price + sign * 20
        elif t.exit_name.endswith("WinX"):
            assert not tradable[t.exit_bar]
        else:  # reversed by the next trade's entry
            nxt = tr.iloc[i + 1]
            assert nxt.entry_bar == t.exit_bar and nxt.direction != t.direction
            exits.add("reverse")
        exits.add(t.exit_name.split()[-1])
        assert t.risk_usd == 400.0 and t.reward_usd == 400.0
    assert {"Loss", "Target", "WinX", "reverse"} <= exits


def test_a_data_gap_into_the_next_days_window_closes_the_position():
    bars = sessions(4, seed=5)
    tr = run(bars).trades
    hhmm = tr.entry_time.dt.hour * 100 + tr.entry_time.dt.minute
    t = tr[(hhmm >= 1800) & (hhmm < 1958) & (tr.exit_bar > tr.entry_bar + 1)].iloc[0]  # held overnight-window trade
    entry = bars.index[t.entry_bar]
    holed = bars[(bars.index <= entry) | (bars.index >= entry + pd.Timedelta("1D"))]  # same clock time, next session
    after = run(holed).trades
    same = after[after.entry_bar == t.entry_bar].iloc[0]
    assert holed.index[same.entry_bar + 1] == entry + pd.Timedelta("1D")
    assert same.exit_bar == same.entry_bar + 1  # closed on the first bar after the gap, not carried a day


def test_inputs_are_validated():
    with pytest.raises(Exception, match="ma_type"):
        run(sessions(1, seed=4), ma_type="SMA")
