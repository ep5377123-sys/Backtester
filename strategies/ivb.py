"""IVB 1-min closure — longs only, opening-range breakout on a 1-minute close, 1:1 target.

Reconstructed from the user's description and the MC trade export `IVB 1 min.csv`, not from the .el source
(its file name is unknown). Each rule below was checked against that export over the local NQ data
(2023-01 → 2026-09, 584 MC trades):

IVB:    the high / low of the 1-minute bars labeled after ivb_start up to ivb_end (08:31-09:00 CT = the first
        30 minutes of RTH).
LONG:   the first bar after the IVB that closes above the IVB high, up to latest_entry, buys at THAT bar's
        close (MC `this bar on close`; 540/584 MC entries are that bar). One trade per day.
SIZE:   floor(risk_usd / ((entry - IVB low) x BigPointValue)) contracts, at least 1 (562/584 match MC).
EXITS:  stop at the IVB low, target at entry + (entry - IVB low) (1:1), and a time exit at the close of the
        bar labeled time_exit (14:00 CT).

Chart time is exchange time: run with `--tz America/Chicago`, `--tf 1m`, 24-hour session (`--session eth`).
MC's costs for this export: commission 1.22, slippage 0.04 USD per contract per round trip.

Scheduled early closes (not in MC): on a day the exchange halts before time_exit (config/calendar.toml),
the position is closed early_close_buffer minutes before the halt. flatten_early_close=False holds it like MC.
"""

from __future__ import annotations

import math

from guban.engine import Strategy


class IVB(Strategy):
    inputs = dict(
        ivb_start=830,
        ivb_end=900,
        latest_entry=1200,
        max_trades_per_day=1,
        risk_usd=1000.0,
        reward_risk=1.0,
        time_exit=1400,
        flatten_early_close=True,
        early_close_buffer=10,
    )

    def on_start(self):
        self.ivb_high = None
        self.ivb_low = None
        self.d_trades = 0
        self.stop = None
        self.target = None

    def on_bar(self):
        if self.current_bar == 1:
            return
        p = self.p
        h, l, c = self.high[0], self.low[0], self.close[0]
        date, time = self.date[0], self.time[0]

        if date != self.date[1]:
            self.ivb_high = self.ivb_low = None
            self.d_trades = 0

        if p.ivb_start < time <= p.ivb_end:
            self.ivb_high = h if self.ivb_high is None else max(self.ivb_high, h)
            self.ivb_low = l if self.ivb_low is None else min(self.ivb_low, l)

        flatten = p.time_exit
        close_at = self.session_close[0]
        if p.flatten_early_close and close_at is not None and close_at.date() == date:
            cut = close_at.hour * 60 + close_at.minute - p.early_close_buffer
            flatten = min(flatten, cut // 60 * 100 + cut % 60)

        entered = False
        if (
            self.ivb_high is not None
            and p.ivb_end < time <= p.latest_entry
            and time < flatten
            and c > self.ivb_high
            and self.market_position == 0
            and self.d_trades < p.max_trades_per_day
        ):
            risk_pts = c - self.ivb_low
            qty = max(1, math.floor(p.risk_usd / (risk_pts * self.big_point_value)))
            self.buy("ORB-L MKT", qty, at="close")
            self.d_trades += 1
            self.stop = self.ivb_low
            self.target = c + p.reward_risk * risk_pts
            entered = True

        if self.market_position == 1 or entered:
            if time >= flatten and not entered:
                self.sell("ORB-L TimeX", at="close")
            else:
                self.sell("ORB-L SL", at="stop", price=self.stop)
                self.sell("ORB-L TP", at="limit", price=self.target)
