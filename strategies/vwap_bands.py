"""VWAP band fade — buy the RTH VWAP -2 sigma band, short the +2 sigma band, market-if-touched, 1:1 fixed points.

VWAP:   session VWAP of the RTH bars so far (rth_start < time <= rth_end), reset on the first RTH bar of each date.
        Typical price tp = (H + L + C) / 3, VWAP = sum(v * tp) / sum(v), sigma = the volume-weighted standard
        deviation of tp around VWAP, sqrt(sum(v * tp^2) / sum(v) - VWAP^2). Bands = VWAP +/- band_mult * sigma.
LONG:   a bar closing below VWAP places a buy at="touch" (market-if-touched) at the lower band, rounded down to a
        tick, for the next bar: it fills where price touches the band (at the open if the bar opens below it).
SHORT:  mirror: a bar closing above VWAP places a sell_short at="touch" at the upper band, rounded up to a tick.
        Only one side is live at a time, so a fill never reverses within a bar. Entries only while flat, from the
        bar labeled earliest_entry up to (not including) latest_entry; max_trades_per_day caps them (0 = no cap).
RE-ARM: an entry disarms the strategy; it re-arms when a bar closes strictly between the bands while flat. Each
        date starts armed.
EXITS:  stop stop_pts and target target_pts from the fill price (set_stop_loss / set_profit_target, in USD per
        position), and a time exit at the close of the bar labeled time_exit.

Chart time is Eastern: run with `--tz America/New_York`, `--tf 1m`, 24-hour session (`--session eth`); VWAP uses
the RTH bars only. On a day the exchange halts early (config/calendar.toml) the position is closed
early_close_buffer minutes before the halt.
"""

from __future__ import annotations

import math

from guban.engine import Strategy


class VWAPBands(Strategy):
    inputs = dict(
        # RTH window (HHMM, Eastern): bars with rth_start < time <= rth_end build VWAP
        rth_start=930,
        rth_end=1600,
        band_mult=2.0,
        # Entry
        earliest_entry=945,
        latest_entry=1500,
        max_trades_per_day=0,  # 0 = no cap
        qty=1,
        # Exits (points from the fill)
        stop_pts=30.0,
        target_pts=30.0,
        time_exit=1555,
        # Scheduled early close
        flatten_early_close=True,
        early_close_buffer=10,
    )
    plots = ("vwap", "upper", "lower")

    def on_start(self):
        self.vwap = self.series(name="vwap")
        self.upper = self.series(name="upper")
        self.lower = self.series(name="lower")
        self.day = None
        self.sv = self.svp = self.svp2 = 0.0  # sum(v), sum(v * tp), sum(v * tp^2) of today's RTH bars
        self.armed = True
        self.d_trades = 0
        self.pending = None  # (direction, price) of the touch entry placed on the previous bar

    def on_bar(self):
        p = self.p
        h, l, c, v = self.high[0], self.low[0], self.close[0], self.volume[0]
        date, time = self.date[0], self.time[0]
        usd = self.big_point_value * p.qty
        self.set_stop_loss(p.stop_pts * usd)
        self.set_profit_target(p.target_pts * usd)

        if date != self.day:
            self.day = date
            self.sv = self.svp = self.svp2 = 0.0
            self.armed = True
            self.d_trades = 0

        # --- Did last bar's touch entry fill? (it fills once price reaches its price, the open included) ---
        if self.pending is not None:
            side, price = self.pending
            if (l <= price) if side == 1 else (h >= price):
                self.armed = False
                self.d_trades += 1
            self.pending = None

        # --- RTH VWAP and bands ---
        vwap = upper = lower = math.nan
        if p.rth_start < time <= p.rth_end:
            tp = (h + l + c) / 3
            self.sv += v
            self.svp += v * tp
            self.svp2 += v * tp * tp
            if self.sv > 0:
                vwap = self.svp / self.sv
                sd = math.sqrt(max(self.svp2 / self.sv - vwap * vwap, 0.0))
                upper, lower = vwap + p.band_mult * sd, vwap - p.band_mult * sd
        self.vwap.value, self.upper.value, self.lower.value = vwap, upper, lower

        # --- Time exit, scheduled early close ---
        flat_at = p.time_exit
        close_at = self.session_close[0]
        if p.flatten_early_close and close_at is not None and close_at.date() == date:
            cut = close_at.hour * 60 + close_at.minute - p.early_close_buffer
            flat_at = min(flat_at, cut // 60 * 100 + cut % 60)

        pos = self.market_position
        if pos != 0:
            if time >= flat_at:
                if pos == 1:
                    self.sell("VWAP-L TimeX", at="close")
                else:
                    self.buy_to_cover("VWAP-S TimeX", at="close")
            return

        # --- Entries: flat, armed, inside the window, bands defined ---
        if not upper > lower:
            return
        if not self.armed and lower < c < upper:
            self.armed = True
        if (
            self.armed
            and p.earliest_entry <= time < min(p.latest_entry, flat_at)
            and (p.max_trades_per_day <= 0 or self.d_trades < p.max_trades_per_day)
        ):
            tick = self.tick_size
            if c < vwap:
                price = math.floor(lower / tick + 1e-9) * tick
                self.buy("VWAP-L MIT", p.qty, at="touch", price=price)
                self.pending = (1, price)
            elif c > vwap:
                price = math.ceil(upper / tick - 1e-9) * tick
                self.sell_short("VWAP-S MIT", p.qty, at="touch", price=price)
                self.pending = (-1, price)
