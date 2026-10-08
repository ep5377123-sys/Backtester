"""Walk-forward (R9): window layout (no out-of-sample window overlaps its in-sample window), causality, per-window
selection and stitching equal to separate runs of each trading window (flat at its start, flattened at its end),
no look-ahead across window boundaries (FIX.md #1), efficiency, sat-out windows, and the HTML page."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest
from guban.engine.backtest import WINDOW_END
from guban.metrics import backtest_metrics, session_keys, window_metrics
from guban.optimize import combinations
from guban.wfo import make_windows, walk_forward
from guban.wfo_report import write_wfo_report

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
COSTS = Costs(commission=4.2, slippage=5.0)
T = pd.Timestamp


class DayMomentum(Strategy):
    """Intraday: follows n-bar momentum with a dollar stop loss, flat from 15:45 (so no trade spans a window edge)."""

    inputs = dict(lookback=3, stop_usd=200.0, qty=1)

    def on_bar(self):
        n = self.p.lookback
        if self.current_bar <= n:
            return
        self.set_stop_loss(self.p.stop_usd)
        if self.time[0] >= 1545:
            if self.market_position == 1:
                self.sell("X")
            elif self.market_position == -1:
                self.buy_to_cover("X")
            return
        if self.close[0] > self.close[n] and self.market_position <= 0:
            self.buy("L", self.p.qty)
        elif self.close[0] < self.close[n] and self.market_position >= 0:
            self.sell_short("S", self.p.qty)


class Momentum(Strategy):
    """Always in the market (reverses on n-bar momentum)."""

    inputs = dict(lookback=3)

    def on_bar(self):
        n = self.p.lookback
        if self.current_bar <= n:
            return
        if self.close[0] > self.close[n] and self.market_position <= 0:
            self.buy("L")
        elif self.close[0] < self.close[n] and self.market_position >= 0:
            self.sell_short("S")


def market(start: str = "2024-01-01", days: int = 130, seed: int = 5) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(5-minute bars, the 1-minute bars they are built from): RTH random walk, close-time labels, ET."""
    rng = np.random.default_rng(seed)
    frames = []
    for d in pd.bdate_range(start, periods=days):
        idx = pd.date_range(d + pd.Timedelta(hours=9, minutes=31), periods=390, freq="1min", tz="America/New_York", name="time")
        close = 17000 + np.cumsum(rng.normal(0, 6, len(idx))).round() * 0.25
        open_ = np.r_[close[0], close[:-1]]
        wick = rng.integers(0, 6, (2, len(idx))) * 0.25
        frames.append(pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick[0], "low": np.minimum(open_, close) - wick[1],
                                    "close": close, "volume": 10, "session_date": d}, index=idx))  # fmt: skip
    m1 = pd.concat(frames)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "session_date": "first"}
    m5 = m1.resample("5min", label="right", closed="right").agg(agg).dropna(subset=["open"])
    return m5, m1


# --- windows --------------------------------------------------------------------------------------


def test_rolling_windows_default_layout_and_no_overlap():
    ws = make_windows(T("2022-12-28"), T("2026-09-17"))  # the Vault Break data span
    assert len(ws) == 11
    assert (ws[0].is_start, ws[0].is_end, ws[0].oos_end) == (T("2023-01-01"), T("2024-01-01"), T("2024-04-01"))
    assert (ws[-1].oos_start, ws[-1].oos_end, ws[-1].partial) == (T("2026-07-01"), T("2026-09-18"), True)
    assert not any(w.partial for w in ws[:-1])
    for w in ws:
        assert w.is_start < w.is_end <= w.oos_start < w.oos_end  # R9: out of sample never overlaps its in sample
        assert w.is_end == w.oos_start and w.is_start + pd.DateOffset(months=12) == w.is_end
    for a, b in zip(ws, ws[1:]):
        assert a.oos_end == b.oos_start  # out-of-sample windows tile the span, none shares a day
        assert b.is_start == a.is_start + pd.DateOffset(months=3)
    assert ws[0].is_years == pytest.approx(365 / 365.25)


def test_anchored_step_and_start_options():
    ws = make_windows(T("2023-01-03"), T("2024-12-31"), is_months=6, oos_months=2, anchored=True, start="2023-01-03")
    assert all(w.is_start == T("2023-01-03") for w in ws)
    assert [w.is_end for w in ws[:3]] == [T("2023-07-03"), T("2023-09-03"), T("2023-11-03")]
    assert all(w.is_end == w.oos_start for w in ws)
    gaps = make_windows(T("2023-01-01"), T("2024-12-31"), is_months=6, oos_months=1, step_months=3)
    assert gaps[1].oos_start - gaps[0].oos_end == pd.Timedelta(days=61)  # step > oos leaves out-of-sample gaps
    with pytest.raises(ValueError, match="share out-of-sample days"):
        make_windows(T("2023-01-01"), T("2024-12-31"), oos_months=3, step_months=2)
    with pytest.raises(ValueError, match="not enough data"):
        make_windows(T("2023-01-01"), T("2023-12-31"))
    with pytest.raises(ValueError, match="before the first session"):
        make_windows(T("2023-01-05"), T("2024-12-31"), start="2023-01-01")
    with pytest.raises(ValueError, match="at least 1 month"):
        make_windows(T("2023-01-01"), T("2024-12-31"), oos_months=0)


# --- causality ------------------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["magnifier", "ohlc"])
def test_later_bars_never_change_earlier_trades(mode):
    bars, sub = market(days=6)
    k = 250
    kw = dict(sub_bars=sub, fill_mode="magnifier") if mode == "magnifier" else {}
    full = run_backtest(Momentum(), bars, NQ, COSTS, **kw)
    cut = run_backtest(Momentum(), bars.iloc[:k], NQ, COSTS, **kw)
    done = full.trades[full.trades["exit_bar"] < k]
    assert len(cut.trades) == len(done) > 20
    pd.testing.assert_frame_equal(cut.trades, done)
    pd.testing.assert_series_equal(cut.equity, full.equity.iloc[:k])
    pd.testing.assert_series_equal(cut.equity_low, full.equity_low.iloc[:k])


# --- walk forward ---------------------------------------------------------------------------------


def _sessions(bars):
    return session_keys(bars.index, bars["session_date"])


def _window_run(strategy, bars, sess, lo_day, hi_day, **kw):
    """The trading window [lo_day, hi_day) run on its own: bars through its end, flat before it, flattened at its end."""
    lo, hi = int(sess.searchsorted(lo_day)), int(sess.searchsorted(hi_day))
    return run_backtest(strategy, bars.iloc[:hi], NQ, COSTS, trade_from=lo, flatten_at_end=True, **kw)


def test_walk_forward_equals_separate_runs_of_each_window():
    bars, sub = market(seed=2)  # a seed where the chosen inputs change between windows
    grid = {"lookback": [2, 6], "stop_usd": [100.0, 400.0]}
    wf = walk_forward(DayMomentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, is_months=2, oos_months=1, min_trades=5, workers=2)
    assert [(w.oos_start.month, w.partial) for w in wf.windows] == [(3, False), (4, False), (5, False), (6, True)]
    sess = _sessions(bars)
    combos = combinations(grid)
    stitched = []
    for w, (_, row) in zip(wf.windows, wf.summary.iterrows()):
        # in sample: every combination run as its own trading window
        runs = [_window_run(DayMomentum(**c), bars, sess, w.is_start, w.is_end, sub_bars=sub) for c in combos]
        nets = [r.net_profit for r in runs]
        best = int(np.argmax(nets)) + 1  # first of equal values, like the stable ranking
        assert row["combo"] == best, w
        assert row["is_net_profit"] == pytest.approx(nets[best - 1])
        # out of sample: the chosen inputs, run as their own trading window
        oos = _window_run(DayMomentum(**combos[best - 1]), bars, sess, w.oos_start, w.oos_end, sub_bars=sub)
        assert row["oos_net_profit"] == pytest.approx(oos.net_profit) and row["oos_trades"] == len(oos.trades)
        stitched.append(oos.trades)
    want = pd.concat(stitched, ignore_index=True)
    cols = ["direction", "qty", "entry_time", "entry_price", "exit_time", "exit_price", "exit_name", "net_pnl"]
    pd.testing.assert_frame_equal(wf.trades[cols], want[cols])
    assert wf.trades["trade_no"].tolist() == list(range(1, len(want) + 1))
    assert wf.summary["combo"].nunique() > 1  # the choice actually changes across windows

    # R9: every out-of-sample trade lies in its out-of-sample window, never in that window's in-sample span
    entry_sess = sess[wf.trades["entry_bar"].to_numpy()]
    first_oos = wf.windows[0].oos_start
    assert (entry_sess >= first_oos).all()
    for w in wf.windows:
        inside = (entry_sess >= w.oos_start) & (entry_sess < w.oos_end)
        assert not ((entry_sess[inside] >= w.is_start) & (entry_sess[inside] < w.is_end)).any()

    # stitched curve: out-of-sample bars only, ends at the stitched net profit (this strategy is flat every evening)
    assert wf.boundary_trades == 0
    assert (_sessions(bars)[bars.index.get_indexer(wf.equity.index)] >= first_oos).all()
    assert len(wf.equity) == int((sess >= first_oos).sum())
    assert wf.equity.iat[-1] == pytest.approx(wf.trades["net_pnl"].sum())
    assert wf.metrics["all"]["net_profit"] == pytest.approx(wf.trades["net_pnl"].sum())
    assert wf.trades["cum_net_pnl"].iat[-1] == pytest.approx(wf.trades["net_pnl"].sum())

    # efficiency: pooled annualized OOS / IS, and per window
    s = wf.summary
    is_years = sum(w.is_years for w in wf.windows)
    oos_years = sum(w.oos_years for w in wf.windows)
    is_py, oos_py = s["is_net_profit"].sum() / is_years, s["oos_net_profit"].sum() / oos_years
    assert wf.pooled["is_net_per_year"] == pytest.approx(is_py) and wf.pooled["oos_net_per_year"] == pytest.approx(oos_py)
    assert wf.wfe == (pytest.approx(oos_py / is_py) if is_py > 0 else None)
    for w, (_, r) in zip(wf.windows, s.iterrows()):
        if r["is_net_profit"] > 0:
            assert r["wfe"] == pytest.approx((r["oos_net_profit"] / w.oos_years) / (r["is_net_profit"] / w.is_years))

    # results: one row per window x combination; one process gives the same answer
    assert len(wf.results) == 4 * len(combos) and wf.combinations == len(combos)
    solo = walk_forward(DayMomentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, is_months=2, oos_months=1, min_trades=5, workers=1)
    pd.testing.assert_frame_equal(solo.results, wf.results)
    pd.testing.assert_frame_equal(solo.trades, wf.trades)
    pd.testing.assert_series_equal(solo.equity, wf.equity)


def test_windows_without_an_eligible_run_sit_out():
    bars, _ = market(days=90)
    wf = walk_forward(DayMomentum, {"lookback": [2, 4]}, NQ, bars=bars, is_months=2, oos_months=1, min_trades=10**6, workers=1)
    assert wf.summary["combo"].isna().all() and wf.summary["eligible"].eq(0).all()
    assert wf.trades.empty and (wf.equity == 0).all() and wf.wfe is None
    assert wf.metrics["all"]["total_trades"] == 0


def test_positions_never_cross_a_window_boundary():
    bars, _ = market(days=90)
    sess = _sessions(bars)
    grid = {"lookback": [2, 3, 5]}
    wf = walk_forward(Momentum, grid, NQ, COSTS, bars=bars, is_months=2, oos_months=1, workers=1)
    combos = combinations(grid)
    for w in wf.windows:
        # in sample: scored as a run that stops at the window's end, whatever the open trade does afterwards
        for c, r in zip(combos, wf.results[wf.results["window"] == w.no].itertuples()):
            alone = _window_run(Momentum(**c), bars, sess, w.is_start, w.is_end)
            assert r.is_net_profit == pytest.approx(alone.net_profit) and r.is_total_trades == len(alone.trades)
            assert alone.trades["exit_name"].iat[-1] == WINDOW_END  # always in the market: one trade cut at the end
    # always in the market: every out-of-sample window ends with a forced close, and starts flat
    assert wf.boundary_trades == len(wf.windows) == int((wf.trades["exit_name"] == WINDOW_END).sum())
    for w in wf.windows:
        lo, hi = int(sess.searchsorted(w.oos_start)), int(sess.searchsorted(w.oos_end))
        mine = wf.trades[(wf.trades["exit_bar"] >= lo) & (wf.trades["exit_bar"] < hi)]
        assert (mine["entry_bar"] >= lo).all()  # nothing carried in from before the window
        assert mine["exit_bar"].iat[-1] == hi - 1 and mine["exit_name"].iat[-1] == WINDOW_END
        assert wf.equity.loc[bars.index[hi - 1]] == pytest.approx(wf.trades.loc[mine.index[-1], "cum_net_pnl"])
    # equity, trades and metrics reconcile exactly
    assert wf.equity.iat[-1] == pytest.approx(wf.trades["net_pnl"].sum())
    assert wf.metrics["all"]["net_profit"] == pytest.approx(wf.equity.iat[-1])


class Script(Strategy):
    inputs = dict(plan={})

    def on_bar(self):
        for method in self.p.plan.get(self.current_bar - 1, []):
            getattr(self, method)()


def _flat_bars(closes):
    idx = pd.date_range("2024-01-02 09:35", periods=len(closes), freq="5min", tz="America/New_York", name="time")
    c = np.asarray(closes, float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1, "session_date": idx.tz_localize(None).normalize()}, index=idx)


def test_window_score_never_sees_the_exit_after_the_window():
    # FIX.md #1 repro: long from bar 1, exits at bar 3 for +$100; the window [0, 3) ends before the exit
    bars = _flat_bars([100, 100, 100, 105, 105])
    res = run_backtest(Script(plan={0: ["buy"], 2: ["sell"]}), bars, NQ, window_ends={3})
    assert res.trades["net_pnl"].tolist() == [100.0]  # the whole run's trade
    m = window_metrics(res, bars, 0, 3)
    assert m["all"]["net_profit"] == 0.0 and m["all"]["total_trades"] == 1  # closed at bar 2's close (100)
    alone = run_backtest(Script(plan={0: ["buy"], 2: ["sell"]}), bars.iloc[:3], NQ, flatten_at_end=True)
    assert m == backtest_metrics(alone, bars.iloc[:3])


def test_window_does_not_inherit_a_position_opened_before_it():
    # FIX.md #1 repro: a long opened at bar 1 would carry +$100 into a window starting at bar 2
    bars = _flat_bars([100, 100, 100, 105, 105])
    res = run_backtest(Script(plan={0: ["buy"], 3: ["sell"]}), bars, NQ, trade_from=2, window_ends={5})
    w = res.window(2, 5)
    assert w.trades.empty and (w.equity == 0).all()


def test_input_checks_pass_through():
    bars, _ = market(days=5)
    with pytest.raises(ValueError, match="unknown grid inputs"):
        walk_forward(DayMomentum, {"nope": [1]}, NQ, bars=bars)
    with pytest.raises(ValueError, match="not enough data"):
        walk_forward(DayMomentum, {"lookback": [2]}, NQ, bars=bars)


# --- output ---------------------------------------------------------------------------------------


def test_wfo_html_and_csvs(tmp_path):
    bars, _ = market(days=85)  # to 2024-04-26: two windows, the second partial
    wf = walk_forward(DayMomentum, {"lookback": [2, 4], "stop_usd": [150.0, 300.0]}, NQ, COSTS, bars=bars, is_months=2, oos_months=1, workers=1)
    meta = {"title": "WFO <test>", "details": {"Grid": "lookback"}, "inputs": {"qty": 1}, "notes": ["n1"]}
    text = write_wfo_report(wf, tmp_path / "wfo.html", meta).read_text(encoding="utf-8")
    assert "<script src" not in text and "http" not in text  # opens offline
    assert "WFO &lt;test&gt;" in text and "<h2>Walk-forward</h2>" in text and "Walk-forward efficiency" in text
    assert "Rolling: 2-month in-sample, 1-month out-of-sample windows, stepped 1 month" in text
    assert "mark the out-of-sample windows" in text and "W2</td>" in text and "id='equity'" in text and "List of trades" in text
    assert len(list((tmp_path / "trades").glob("trade_*.png"))) == len(wf.trades)  # the stitched OOS trades' images
    windows, results = wf.write_csvs(tmp_path)
    w = pd.read_csv(windows)
    assert w["window"].tolist() == [1, 2] and w["oos_from"].tolist() == ["2024-03-01", "2024-04-01"]
    r = pd.read_csv(results)
    assert len(r) == 8 and {"is_net_profit", "oos_net_profit", "is_rank", "oos_rank", "lookback"} <= set(r.columns)
    recs = wf.summary_records()
    assert all(not (isinstance(v, float) and math.isnan(v)) for rec in recs for v in rec.values())
    path = wf.oos_result().write_trades_csv(tmp_path / "trades.csv")
    assert len(pd.read_csv(path)) == len(wf.trades)
