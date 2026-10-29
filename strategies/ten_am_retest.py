"""10 AM open retest — after a 50-point move away from the 10:00 ET open, enter on a limit back at the open, 15/60.

ANCHOR: the open of the first bar labeled after anchor_time (the 10:00-10:01 bar, labeled 10:01), if it is labeled at
        most 5 minutes after anchor_time; else no trade that day.
SETUP:  from the anchor bar on, while flat and armed, the first bar whose high reaches anchor + displace_pts locks the
        setup LONG; whose low reaches anchor - displace_pts locks it SHORT. A bar that reaches both picks by its
        color (close >= open = long).
ENTRY:  once locked, every bar labeled before latest_entry places a buy (long) / sell_short (short) at="limit" at the
        anchor for the next bar, so no entry fills after latest_entry. The engine fills a limit when price touches it
        (at the open if the bar opens through it), with no slippage.
EXITS:  stop stop_pts and target target_pts from the fill (set_stop_loss / set_profit_target, USD per position), and
        a time exit at the close of the bar labeled time_exit; on a scheduled early close (config/calendar.toml) the
        position is closed early_close_buffer minutes before the halt.
AGAIN:  after an exit the lock clears; the bars after the exit bar look for a new displacement from the same anchor,
        either side, up to max_trades_per_day entries (0 = no cap). With stop_after_win, a trade that closes with a
        net profit (after costs) ends the day.

Chart time is Eastern: run with `--tz America/New_York`, `--tf 1m`, `--session eth`.
"""

from __future__ import annotations

import math

from guban.engine import Strategy


def _minutes(hhmm: int) -> int:
    return hhmm // 100 * 60 + hhmm % 100


class TenAMRetest(Strategy):
    inputs = dict(
        anchor_time=1000,  # HHMM Eastern; the anchor is the open of the bar that starts here
        displace_pts=50.0,
        # Entry
        latest_entry=1230,
        max_trades_per_day=2,  # 0 = no cap
        stop_after_win=False,  # no new entry today after a winning trade
        qty=1,
        # Exits (points from the fill)
        stop_pts=15.0,
        target_pts=60.0,
        time_exit=1555,
        # Scheduled early close
        flatten_early_close=True,
        early_close_buffer=10,
    )
    plots = ("anchor", "upper", "lower")

    def on_start(self):
        self.anchor_s = self.series(name="anchor")
        self.upper = self.series(name="upper")
        self.lower = self.series(name="lower")
        self.day = None
        self.anchor = None
        self.skip = False  # no anchor bar today
        self.side = 0  # locked setup: 1 long, -1 short
        self.d_trades = 0
        self.in_trade = False  # an entry filled and its exit bar has not been seen flat yet
        self.pnl_prev = 0.0  # net_profit at the previous bar's close
        self.pnl_before = 0.0  # net_profit before the open trade
        self.won = False  # a trade today closed with a net profit
        self.pending = None  # (direction, price) of the limit entry placed on the previous bar

    def on_bar(self):
        p = self.p
        o, h, l, c = self.open[0], self.high[0], self.low[0], self.close[0]  # noqa: E741
        date, time = self.date[0], self.time[0]
        usd = self.big_point_value * p.qty
        self.set_stop_loss(p.stop_pts * usd)
        self.set_profit_target(p.target_pts * usd)

        if date != self.day:
            self.day = date
            self.anchor, self.skip, self.side, self.d_trades, self.won = None, False, 0, 0, False

        # --- Did last bar's limit entry fill? (a touch fills it, the open included) ---
        if self.pending is not None:
            side, price = self.pending
            if (l <= price) if side == 1 else (h >= price):
                self.d_trades += 1
                self.side = 0
                self.in_trade = True
                self.pnl_before = self.pnl_prev  # flat through the previous bar, so no part of this trade
            self.pending = None
        self.pnl_prev = self.net_profit

        # --- Anchor: the open of the first bar after anchor_time ---
        if self.anchor is None and not self.skip and time > p.anchor_time:
            if _minutes(time) - _minutes(p.anchor_time) <= 5:
                self.anchor = o
            else:
                self.skip = True
        a = self.anchor
        self.anchor_s.value = math.nan if a is None else a
        self.upper.value = math.nan if a is None else a + p.displace_pts
        self.lower.value = math.nan if a is None else a - p.displace_pts

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
                    self.sell("10AM-L TimeX", at="close")
                else:
                    self.buy_to_cover("10AM-S TimeX", at="close")
            return
        if self.in_trade:  # flat on the exit bar: the next setup starts on the bars after it
            self.in_trade = False
            self.won = self.won or self.net_profit > self.pnl_before
            return

        # --- Setup and entry: anchored, under the cap, before the cutoff, no win yet (stop_after_win) ---
        if (
            a is None
            or (p.stop_after_win and self.won)
            or (p.max_trades_per_day > 0 and self.d_trades >= p.max_trades_per_day)
            or time >= min(p.latest_entry, flat_at)
        ):
            return
        if self.side == 0:
            up, dn = h >= a + p.displace_pts, l <= a - p.displace_pts
            if up and dn:
                self.side = 1 if c >= o else -1
            elif up or dn:
                self.side = 1 if up else -1
        if self.side:
            price = round(a / self.tick_size) * self.tick_size
            if self.side == 1:
                self.buy("10AM-L LMT", p.qty, at="limit", price=price)
            else:
                self.sell_short("10AM-S LMT", p.qty, at="limit", price=price)
            self.pending = (self.side, price)
