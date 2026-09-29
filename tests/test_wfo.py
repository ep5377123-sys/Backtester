"""Walk-forward (R9): window layout (no out-of-sample window overlaps its in-sample window), causality (the basis
for scoring every window from one run over all the data), per-window selection and stitching equal to runs
truncated at each window's end, efficiency, sat-out windows, and the HTML page."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest
from guban.metrics import session_keys
from guban.optimize import combinations
from guban.report import _plotly_js
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


def _entered(res, bars, lo, hi):
    s = _sessions(bars)[res.trades["entry_bar"].to_numpy()]
    return res.trades[(s >= lo) & (s < hi)]


def test_walk_forward_equals_runs_truncated_at_each_window():
    bars, sub = market(seed=2)  # a seed where the chosen inputs change between windows
    grid = {"lookback": [2, 6], "stop_usd": [100.0, 400.0]}
    wf = walk_forward(DayMomentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, is_months=2, oos_months=1, min_trades=5, workers=2)
    assert [(w.oos_start.month, w.partial) for w in wf.windows] == [(3, False), (4, False), (5, False), (6, True)]
    sess = _sessions(bars)
    combos = combinations(grid)
    stitched = []
    for w, (_, row) in zip(wf.windows, wf.summary.iterrows()):
        # in sample: every combination run from the first bar through the in-sample end, scored on its window
        runs = [run_backtest(DayMomentum(**c), bars[sess < w.is_end], NQ, COSTS, sub_bars=sub) for c in combos]
        nets = [_entered(r, bars, w.is_start, w.is_end)["net_pnl"].sum() for r in runs]
        best = int(np.argmax(nets)) + 1  # first of equal values, like the stable ranking
        assert row["combo"] == best, w
        assert row["is_net_profit"] == pytest.approx(nets[best - 1])
        # out of sample: the chosen inputs run from the first bar through the out-of-sample end
        oos = run_backtest(DayMomentum(**combos[best - 1]), bars[sess < w.oos_end], NQ, COSTS, sub_bars=sub)
        assert oos.open_position is None
        mine = _entered(oos, bars, w.oos_start, w.oos_end)
        assert row["oos_net_profit"] == pytest.approx(mine["net_pnl"].sum()) and row["oos_trades"] == len(mine)
        stitched.append(mine)
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

    # stitched curve: out-of-sample bars only, ends at the stitched net profit (no trade spans a window edge)
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


def test_boundary_trades_are_counted():
    bars, _ = market(days=90)
    wf = walk_forward(Momentum, {"lookback": [2, 3, 5]}, NQ, bars=bars, is_months=2, oos_months=1, workers=1)
    # always in the market: the first window carries a trade in, and each switch of inputs splits one
    switches = int((wf.summary["combo"].diff().fillna(0) != 0).sum())
    assert wf.boundary_trades >= 1 + switches


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
    full = write_wfo_report(wf, tmp_path / "wfo.html", meta).read_text(encoding="utf-8")
    text = full.replace(_plotly_js(), "")
    assert "<script src" not in text and "http" not in text  # opens offline
    assert "WFO &lt;test&gt;" in text and "<h2>Walk-forward</h2>" in text and "Walk-forward efficiency" in text
    assert "Rolling: 2-month in-sample, 1-month out-of-sample windows, stepped 1 month" in text
    assert '"label":"W1"' in text and "W2</td>" in text and "id='equity'" in text and "List of trades" in text
    windows, results = wf.write_csvs(tmp_path)
    w = pd.read_csv(windows)
    assert w["window"].tolist() == [1, 2] and w["oos_from"].tolist() == ["2024-03-01", "2024-04-01"]
    r = pd.read_csv(results)
    assert len(r) == 8 and {"is_net_profit", "oos_net_profit", "is_rank", "oos_rank", "lookback"} <= set(r.columns)
    recs = wf.summary_records()
    assert all(not (isinstance(v, float) and math.isnan(v)) for rec in recs for v in rec.values())
    path = wf.oos_result().write_trades_csv(tmp_path / "trades.csv")
    assert len(pd.read_csv(path)) == len(wf.trades)
