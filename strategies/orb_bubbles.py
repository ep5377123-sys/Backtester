"""Bubble ORB -- 15-minute opening range breakout on a 1-minute candle backed by a big aggressive trade.

ORB:    the high / low of the 1-minute bars labeled after orb_start up to orb_end (09:31-09:45 ET).
LONG:   a 1-minute bar after the ORB, up to latest_entry, that
          - closes above the ORB high and closes green (close > open),
          - has a big buy bubble inside its body: big_buy_body > min_bubble (guban.data.bigtrades: buy-aggressor
            prints of one millisecond merged, priced at their size-weighted average, open <= price <= close),
          - has a body bigger than its top wick (close - open > high - close),
        buys at THAT bar's close (`at="close"`). Stop stop_ticks below the bar's low, target entry +
        reward_risk x (entry - stop).
SHORT:  the mirror, when allow_shorts: closes below the ORB low and red, a big SELL bubble (big_sell_body) inside
        its body, body bigger than its bottom wick (open - close > close - low); sells short at the close, stop
        stop_ticks above the bar's high. The first bar that qualifies either way is taken; max_trades_per_day
        trades per day (both sides together).
EXITS:  stop, target, and a time exit at the close of the bar labeled time_exit.

Days whose ticks are unusable (quality.csv tick_ok not True) have NaN bubble columns and take no trade. NQ ticks
start in 2024 and 2024-01 is defective: run from 2024-02-01. Chart time is Eastern: run with
`--tz America/New_York --tf 1m --session eth`. On a day the exchange halts early (config/calendar.toml) the
position is closed early_close_buffer minutes before the halt.
"""

from __future__ import annotations

from guban.engine import Strategy


class BubbleORB(Strategy):
    extra_columns = ("big_buy_body", "big_sell_body")
    inputs = dict(
        orb_start=930,
        orb_end=945,
        min_bubble=60,
        allow_shorts=True,
        latest_entry=1100,
        max_trades_per_day=1,
        qty=1,
        stop_ticks=1,
        reward_risk=1.0,
        time_exit=1555,
        flatten_early_close=True,
        early_close_buffer=10,
    )

    def on_start(self):
        self.orb_high = None
        self.orb_low = None
        self.d_trades = 0
        self.stop = None
        self.target = None

    def on_bar(self):
        if self.current_bar == 1:
            return
        p = self.p
        o, h, l, c = self.open[0], self.high[0], self.low[0], self.close[0]
        date, time = self.date[0], self.time[0]

        if date != self.date[1]:
            self.orb_high = self.orb_low = None
            self.d_trades = 0

        if p.orb_start < time <= p.orb_end:
            self.orb_high = h if self.orb_high is None else max(self.orb_high, h)
            self.orb_low = l if self.orb_low is None else min(self.orb_low, l)

        flatten = p.time_exit
        close_at = self.session_close[0]
        if p.flatten_early_close and close_at is not None and close_at.date() == date:
            cut = close_at.hour * 60 + close_at.minute - p.early_close_buffer
            flatten = min(flatten, cut // 60 * 100 + cut % 60)

        entered = 0
        if (
            self.orb_high is not None
            and p.orb_end < time <= p.latest_entry
            and time < flatten
            and self.market_position == 0
            and self.d_trades < p.max_trades_per_day
        ):
            if c > self.orb_high and c > o and c - o > h - c and self.big_buy_body[0] > p.min_bubble:
                self.buy("BubbleORB-L MKT", p.qty, at="close")
                self.stop = l - p.stop_ticks * self.tick_size
                self.target = c + p.reward_risk * (c - self.stop)
                entered = 1
            elif p.allow_shorts and c < self.orb_low and c < o and o - c > c - l and self.big_sell_body[0] > p.min_bubble:
                self.sell_short("BubbleORB-S MKT", p.qty, at="close")
                self.stop = h + p.stop_ticks * self.tick_size
                self.target = c - p.reward_risk * (self.stop - c)
                entered = -1
            if entered:
                self.d_trades += 1

        pos = entered or self.market_position
        if pos == 1:
            if time >= flatten and not entered:
                self.sell("BubbleORB-L TimeX", at="close")
            else:
                self.sell("BubbleORB-L SL", at="stop", price=self.stop)
                self.sell("BubbleORB-L TP", at="limit", price=self.target)
        elif pos == -1:
            if time >= flatten and not entered:
                self.buy_to_cover("BubbleORB-S TimeX", at="close")
            else:
                self.buy_to_cover("BubbleORB-S SL", at="stop", price=self.stop)
                self.buy_to_cover("BubbleORB-S TP", at="limit", price=self.target)
