"""VaultBreak NQ — longs only, fixed TP / SL. Literal port of MultiCharts `sc_Vaultc20Break.el`.

LONG:  Close breaks above the upper noise boundary (session open + NoiseMult x DailyATR) and above VWAP.
EXIT:  profit target / stop loss in dollars per position, a VWAP cross held for N bars, or flatten at FlattenTime.

MC setup this port reproduces (recovered from the Vault Break MNQ trade export): 5-minute bars, chart in
exchange time (America/Chicago), 24-hour Globex session. So Time/Date and the input times below are Central,
`Date <> Date[1]` fires at midnight CT, and a "day" is a calendar date, Sunday evenings included.

DailyATR is the running mean of every completed day's range since the first bar (RngSum / RngCnt never roll
off), so it depends on where the data starts. atr_seed_sum / atr_seed_count / sess_count_seed stand in for
history before the first bar (the MC chart starts in Jan 2021; the local 1-minute store in Dec 2022). They are
not in the .el; left at 0 the port is literal.
"""

from __future__ import annotations

from guban.engine import Strategy


class VaultBreak(Strategy):
    inputs = dict(
        # Noise boundary
        noise_mult=0.60,
        daily_atr_len=14,
        # Entry timing (HHMM, chart time)
        earliest_entry=945,
        latest_entry=1500,
        # Re-entry
        max_trades_per_day=3,
        # VWAP exit
        vwap_exit_bars=2,
        # Fixed stop & target (USD per position)
        take_profit_usd=1000,
        stop_loss_usd=2000,
        # Time exit
        flatten_time=1550,
        # Position
        num_contracts=1,
        # Warm-up stand-ins for history before the first bar (not in the .el)
        atr_seed_sum=0.0,
        atr_seed_count=0,
        sess_count_seed=0,
    )

    def on_start(self):
        p = self.p
        # Session
        self.sess_open = 0.0
        self.sess_high = 0.0
        self.sess_low = 999999.0
        self.sess_count = p.sess_count_seed
        # Daily ATR
        self.rng_sum = float(p.atr_seed_sum)
        self.rng_cnt = p.atr_seed_count
        self.daily_atr = self.rng_sum / self.rng_cnt if self.rng_cnt >= p.daily_atr_len else 0.0
        # Noise level (long only)
        self.noise_up = 0.0
        # VWAP
        self.c_tpv = 0.0
        self.c_vol = 0.0
        self.vwap = 0.0
        self.vw_date = None
        # VWAP cross counter (long exit)
        self.bars_below_vw = 0
        # Trade tracking
        self.d_trades = 0

    def on_bar(self):
        if self.current_bar == 1:
            return  # MC does not calculate the first MaxBarsBack bars (Date[1] needs one bar of history)
        p = self.p
        o, h, l, c = self.open[0], self.high[0], self.low[0], self.close[0]
        date, time = self.date[0], self.time[0]

        # --- Session & daily ATR ---
        if date != self.date[1]:
            if self.sess_count >= 1:
                value1 = self.sess_high - self.sess_low
                if value1 > 0:
                    self.rng_sum += value1
                    self.rng_cnt += 1
                    if self.rng_cnt >= p.daily_atr_len:
                        self.daily_atr = self.rng_sum / self.rng_cnt
            self.sess_open = o
            self.sess_high = h
            self.sess_low = l
            self.sess_count += 1
            self.d_trades = 0
            if self.daily_atr > 0:
                self.noise_up = self.sess_open + p.noise_mult * self.daily_atr
            self.bars_below_vw = 0
        else:
            if h > self.sess_high:
                self.sess_high = h
            if l < self.sess_low:
                self.sess_low = l

        # --- VWAP ---
        if date != self.vw_date:
            self.c_tpv = 0.0
            self.c_vol = 0.0
            self.vw_date = date
        vol = self.volume[0]
        if vol > 0:
            self.c_tpv += ((h + l + c) / 3.0) * vol
            self.c_vol += vol
        self.vwap = self.c_tpv / self.c_vol if self.c_vol > 0 else c

        # --- VWAP cross tracking (for long exit) ---
        if c < self.vwap:
            self.bars_below_vw += 1
        else:
            self.bars_below_vw = 0

        # --- Entry: long only ---
        if (
            c > self.noise_up
            and c > self.vwap
            and time >= p.earliest_entry
            and time <= p.latest_entry
            and self.d_trades < p.max_trades_per_day
            and self.market_position == 0
            and self.sess_count >= p.daily_atr_len + 1
            and self.daily_atr > 0
        ):
            self.buy("VB_Long", p.num_contracts)
            self.d_trades += 1

        # --- Exits ---
        self.set_profit_target(p.take_profit_usd)
        self.set_stop_loss(p.stop_loss_usd)
        if self.market_position == 1 and self.bars_below_vw >= p.vwap_exit_bars:
            self.sell("VB_VWAP_X", p.num_contracts)
        if time >= p.flatten_time and self.market_position == 1:
            self.sell("VB_TimeX", p.num_contracts)
