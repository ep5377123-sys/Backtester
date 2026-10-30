"""Typo VM — stop-and-reverse on the "Typo VM" TradingView indicator's CVD-divergence signals, 20/20 point bracket.

INDICATOR (a port of the Pine v6 script, signal logic only; markers, alerts and live preview are display-only):
        delta = volume * (2 * close_pos - 1), close_pos = (C - L) / (H - L) (0.5 when H == L) — a bar-shape estimate,
        not real order flow. CVD = running sum of delta, reset on the first bar of a new trading day ("Daily") or
        week ("Weekly", the first session of an ISO week), or never ("None"). CVD is normalised over norm_len bars
        (Min-Max to -100..100, or Z-Score * 33.33 clipped to +/-100; 0 until norm_len bars exist or when the range /
        std is 0) and smoothed by an EMA (seeded with the SMA of its first ma_len values) or HMA.
BUY:    bull divergence (low below the lowest low of the previous div_bars bars, CVD above the lowest CVD of the
        previous div_bars bars, delta > 0) and normalised CVD above its value sig_lookback bars ago; in "Confirmed"
        mode also a normalised-CVD crossover of its MA. With weak signals on, a crossover while normalised CVD is
        below -weak_threshold (delta > 0) also counts. The signal fires on the first bar of a run of such bars,
        at least min_bars_between bars after the previous buy signal. SELL mirrors it.
CHAINS: the script has daytime defaults and an 18:00-08:00 preset. Both run on every bar, independently; bars
        labeled from night_from up to day_from (overnight) trade on the preset's signals, the rest on the daytime's.

TRADES: a buy signal on a bar inside an entry window goes long at the next bar's open, a sell signal goes short;
        an opposite signal reverses, a same-side one is ignored. Windows (HHMM, bar close labels): start <= time < end,
        08:00-11:30, 13:30-16:59, 18:00-20:00. A position still open on a bar outside the windows is closed at that
        bar's close (11:30, 16:59 before the daily halt, 20:00), and so is one carried by a data gap into another window
        (another session or window than the previous bar's); a signal on the bar before a window's end therefore
        fills on the end bar and is closed at its close. Every position has a stop stop_pts and a target target_pts
        from its fill (set_stop_loss / set_profit_target, USD per position). On a day the exchange halts early
        (config/calendar.toml: session_close before regular_close), entries stop and positions close
        early_close_buffer minutes before the halt.

Chart time is Eastern: run with `--tz America/New_York`, `--tf 1m`, `--session eth` (the overnight window and the
18:00 CVD reset need the 24-hour session).
"""

from __future__ import annotations

import math
from collections import deque

from guban.engine import Strategy


class _Chain:
    """One Typo VM indicator: the bar-shape delta's CVD, normalised, its MA, and the buy / sell signal edges."""

    def __init__(self, ma_len, ma_type, norm_len, norm_method, reset, sig_lookback, div_bars, min_between, mode,
                 weak, weak_threshold):  # fmt: skip
        for name, value, options in (
            ("ma_type", ma_type, ("EMA", "HMA")),
            ("norm_method", norm_method, ("Min-Max", "Z-Score")),
            ("cvd_reset", reset, ("None", "Daily", "Weekly")),
            ("signal_mode", mode, ("Early", "Confirmed")),
        ):
            if value not in options:
                raise ValueError(f"Typo VM: {name} must be one of {options}, got {value!r}")
        self.hma, self.zscore, self.reset, self.confirmed = ma_type == "HMA", norm_method == "Z-Score", reset, mode == "Confirmed"
        self.sig_lookback, self.min_between, self.weak, self.weak_threshold = sig_lookback, min_between, weak, weak_threshold
        self.cvd = 0.0
        self.cvds = deque(maxlen=norm_len)  # CVD of the last norm_len bars, this one included
        self.prev_lows, self.prev_highs, self.prev_cvds = (deque(maxlen=div_bars) for _ in range(3))  # previous bars only
        self.norms = deque(maxlen=sig_lookback + 1)  # normalised CVD, newest last
        self.prev_norm = self.prev_ma = math.nan
        # EMA: SMA of the first ma_len values, then recursive. HMA: WMA(2 WMA(x, n/2) - WMA(x, n), sqrt n).
        self.ma_len, self.alpha, self.ema = ma_len, 2 / (ma_len + 1), math.nan
        self.seed = deque(maxlen=ma_len)
        self.w_full, self.w_half = deque(maxlen=ma_len), deque(maxlen=max(ma_len // 2, 1))
        self.w_diff = deque(maxlen=max(math.isqrt(ma_len), 1))
        self.prev_final_buy = self.prev_final_sell = False
        self.bar, self.last_buy, self.last_sell = -1, -999, -999

    def update(self, h, l, c, v, new_day, new_week):
        """Feed one bar; returns (buy_sig, sell_sig, strong): strong = the signal is a divergence, not only a weak cross."""
        self.bar += 1
        rng = h - l
        close_pos = (c - l) / rng if rng > 0 else 0.5
        delta = v * close_pos - v * (1.0 - close_pos)
        reset = (self.reset == "Daily" and new_day) or (self.reset == "Weekly" and new_week)
        self.cvd = cvd = delta if reset else self.cvd + delta
        self.cvds.append(cvd)

        norm = 0.0
        full = len(self.cvds) == self.cvds.maxlen
        if full and self.zscore:
            mean = sum(self.cvds) / len(self.cvds)
            std = math.sqrt(sum((x - mean) ** 2 for x in self.cvds) / len(self.cvds))
            if std > 0:
                norm = max(min((cvd - mean) / std * 33.33, 100.0), -100.0)
        elif full:
            hi, lo = max(self.cvds), min(self.cvds)
            if hi - lo > 0:
                norm = (cvd - lo) / (hi - lo) * 200 - 100
        ma = self._ma(norm)

        self.norms.append(norm)
        n_back = self.norms[0] if len(self.norms) == self.norms.maxlen else 0.0  # nz(cvd_norm[sig_lookback])
        divs = len(self.prev_lows) == self.prev_lows.maxlen
        bull_div = divs and l < min(self.prev_lows) and cvd > min(self.prev_cvds) and delta > 0
        bear_div = divs and h > max(self.prev_highs) and cvd < max(self.prev_cvds) and delta < 0
        cross_up = norm > ma and self.prev_norm <= self.prev_ma  # False while either MA is NaN
        cross_down = norm < ma and self.prev_norm >= self.prev_ma

        raw_buy = bull_div and norm > n_back and (cross_up or not self.confirmed)
        raw_sell = bear_div and norm < n_back and (cross_down or not self.confirmed)
        final_buy = raw_buy or (self.weak and cross_up and norm < -self.weak_threshold and delta > 0)
        final_sell = raw_sell or (self.weak and cross_down and norm > self.weak_threshold and delta < 0)
        buy_sig = final_buy and not self.prev_final_buy and self.bar - self.last_buy >= self.min_between
        sell_sig = final_sell and not self.prev_final_sell and self.bar - self.last_sell >= self.min_between
        if buy_sig:
            self.last_buy = self.bar
        if sell_sig:
            self.last_sell = self.bar

        self.prev_final_buy, self.prev_final_sell = final_buy, final_sell
        self.prev_norm, self.prev_ma = norm, ma
        self.prev_lows.append(l)
        self.prev_highs.append(h)
        self.prev_cvds.append(cvd)
        return buy_sig, sell_sig, raw_buy if buy_sig else raw_sell

    def _ma(self, x):
        if not self.hma:
            if math.isnan(self.ema):
                self.seed.append(x)
                if len(self.seed) == self.ma_len:
                    self.ema = sum(self.seed) / self.ma_len
            else:
                self.ema = self.alpha * x + (1 - self.alpha) * self.ema
            return self.ema
        self.w_full.append(x)
        self.w_half.append(x)
        if len(self.w_full) < self.w_full.maxlen:
            return math.nan
        self.w_diff.append(2 * _wma(self.w_half) - _wma(self.w_full))
        return _wma(self.w_diff) if len(self.w_diff) == self.w_diff.maxlen else math.nan


def _wma(values):
    """Pine ta.wma over a full window, oldest first: the newest value weighs n, the oldest 1."""
    n = len(values)
    return sum(x * (i + 1) for i, x in enumerate(values)) / (n * (n + 1) / 2)


class TypoVM(Strategy):
    inputs = dict(
        # Daytime chain (the script's own inputs)
        cvd_ma_len=10,
        ma_type="EMA",  # EMA | HMA
        norm_len=60,
        norm_method="Min-Max",  # Min-Max | Z-Score
        cvd_reset="Daily",  # None | Daily | Weekly
        sig_lookback=3,
        sig_div_bars=5,
        min_bars_between=1,
        # Overnight chain (the script's 18:00-08:00 preset)
        n_cvd_ma_len=14,
        n_ma_type="HMA",
        n_norm_len=100,
        n_cvd_reset="None",
        n_sig_lookback=2,
        n_sig_div_bars=4,
        n_min_bars_between=5,
        # Both chains
        signal_mode="Early",  # Early (divergence) | Confirmed (MA cross)
        weak_signals=True,
        weak_threshold=50.0,
        # Which chain trades (HHMM): bars from night_from up to day_from use the overnight chain
        night_from=1800,
        day_from=800,
        # Entry windows (HHMM, start <= bar time < end); positions are closed outside them
        win1_start=800,
        win1_end=1130,
        win2_start=1330,
        win2_end=1659,
        win3_start=1800,
        win3_end=2000,
        qty=1,
        # Bracket (points from the fill)
        stop_pts=20.0,
        target_pts=20.0,
        # Scheduled early close: a session closing before regular_close (HHMM) stops early_close_buffer minutes before it
        flatten_early_close=True,
        early_close_buffer=10,
        regular_close=1700,
    )

    def on_start(self):
        p = self.p
        # the preset leaves the normalisation method and the signal settings below to the user's inputs
        shared = dict(norm_method=p.norm_method, mode=p.signal_mode, weak=p.weak_signals, weak_threshold=p.weak_threshold)
        self.day_chain = _Chain(p.cvd_ma_len, p.ma_type, p.norm_len, reset=p.cvd_reset, sig_lookback=p.sig_lookback,
                                div_bars=p.sig_div_bars, min_between=p.min_bars_between, **shared)  # fmt: skip
        self.night_chain = _Chain(p.n_cvd_ma_len, p.n_ma_type, p.n_norm_len, reset=p.n_cvd_reset,
                                  sig_lookback=p.n_sig_lookback, div_bars=p.n_sig_div_bars,
                                  min_between=p.n_min_bars_between, **shared)  # fmt: skip
        self.windows = ((p.win1_start, p.win1_end), (p.win2_start, p.win2_end), (p.win3_start, p.win3_end))
        self.prev_session = self.prev_key = None

    def on_bar(self):
        p = self.p
        h, l, c, v = self.high[0], self.low[0], self.close[0], self.volume[0]
        date, time, session = self.date[0], self.time[0], self.session_date[0]
        usd = self.big_point_value * p.qty
        self.set_stop_loss(p.stop_pts * usd)
        self.set_profit_target(p.target_pts * usd)

        new_day = self.prev_session is not None and session != self.prev_session
        new_week = new_day and tuple(session.isocalendar())[:2] != tuple(self.prev_session.isocalendar())[:2]
        self.prev_session = session
        day = self.day_chain.update(h, l, c, v, new_day, new_week)
        night = self.night_chain.update(h, l, c, v, new_day, new_week)
        buy_sig, sell_sig, strong = night if (time >= p.night_from or time < p.day_from) else day
        if buy_sig and sell_sig:
            raise RuntimeError(f"Typo VM: buy and sell signal on the same bar ({date} {time})")

        win = next((i for i, (start, end) in enumerate(self.windows) if start <= time < end), None)
        tradable = win is not None
        key = (session, win)  # the window instance this bar belongs to
        if key != self.prev_key and self.market_position != 0:
            tradable = False  # a data gap jumped from one window into another with the position still open
        self.prev_key = key
        close_at = self.session_close[0]
        if p.flatten_early_close and close_at is not None and close_at.date() == date:
            close_hhmm = close_at.hour * 100 + close_at.minute
            if close_hhmm < p.regular_close:  # a normal session is handled by the 16:59 window end
                cut = close_at.hour * 60 + close_at.minute - p.early_close_buffer
                tradable = tradable and time < cut // 60 * 100 + cut % 60

        pos = self.market_position
        if not tradable:
            if pos == 1:
                self.sell("TVM-L WinX", at="close")
            elif pos == -1:
                self.buy_to_cover("TVM-S WinX", at="close")
            return
        tag = "" if strong else " Weak"
        if buy_sig and pos != 1:
            self.buy("TVM-L" + tag, p.qty)
        elif sell_sig and pos != -1:
            self.sell_short("TVM-S" + tag, p.qty)
