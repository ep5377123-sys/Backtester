"""Grid optimizer (R8): grid spec parsing, pool results equal separate runs, ranking and min-trades filter,
the >5,000-combination guard, heatmap aggregation, and the HTML page."""

from __future__ import annotations

import math
import re
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest
from guban.engine.strategy import load_strategy_class
from guban.metrics import backtest_metrics
from guban.optimize import (
    RESULT_METRICS,
    OptimizeResult,
    TooManyCombinations,
    combinations,
    flatten_metrics,
    heatmap,
    optimize,
    parse_grid,
    rank_results,
)
from guban.optimize_report import heatmap_axes, write_optimize_report

NQ = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
COSTS = Costs(commission=4.2, slippage=5.0)


class Momentum(Strategy):
    """Reverses on n-bar momentum, with a dollar stop loss: exercises inputs of both int and float type."""

    inputs = dict(lookback=3, stop_usd=200.0, qty=1)

    def on_bar(self):
        n = self.p.lookback
        if self.current_bar <= n:
            return
        self.set_stop_loss(self.p.stop_usd)
        if self.close[0] > self.close[n] and self.market_position <= 0:
            self.buy("L", self.p.qty)
        elif self.close[0] < self.close[n] and self.market_position >= 0:
            self.sell_short("S", self.p.qty)


def market(days: int = 3, seed: int = 11) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(5-minute bars, the 1-minute bars they are built from): RTH random walk, close-time labels, ET."""
    rng = np.random.default_rng(seed)
    frames = []
    for d in pd.bdate_range("2024-01-02", periods=days):
        idx = pd.date_range(d + pd.Timedelta(hours=9, minutes=31), periods=390, freq="1min", tz="America/New_York", name="time")
        close = 17000 + np.cumsum(rng.normal(0, 6, len(idx))).round() * 0.25
        open_ = np.r_[close[0], close[:-1]]
        wick = rng.integers(0, 6, (2, len(idx))) * 0.25
        frames.append(pd.DataFrame({"open": open_, "high": np.maximum(open_, close) + wick[0], "low": np.minimum(open_, close) - wick[1],
                                    "close": close, "volume": 10, "session_date": d}, index=idx))
    m1 = pd.concat(frames)
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "session_date": "first"}
    m5 = m1.resample("5min", label="right", closed="right").agg(agg).dropna(subset=["open"])
    return m5, m1



def test_parse_grid_ranges_lists_and_types():
    g = parse_grid(["stop_usd=100:200:25", "lookback=2:4", "qty=1,3"], Momentum.inputs)
    assert g == {"stop_usd": [100.0, 125.0, 150.0, 175.0, 200.0], "lookback": [2, 3, 4], "qty": [1, 3]}
    assert all(type(v) is float for v in g["stop_usd"]) and all(type(v) is int for v in g["lookback"])
    g = parse_grid(["stop_usd=0.4:1.0:0.1"], Momentum.inputs)
    assert g["stop_usd"] == [0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    assert parse_grid(["lookback=5"], Momentum.inputs) == {"lookback": [5]}
    assert parse_grid(["lookback=2:6:2.0"], Momentum.inputs) == {"lookback": [2, 4, 6]}
    assert len(combinations({"a": [1, 2], "b": [3, 4, 5]})) == 6
    assert combinations({"a": [1, 2], "b": [3, 4]}) == [{"a": 1, "b": 3}, {"a": 1, "b": 4}, {"a": 2, "b": 3}, {"a": 2, "b": 4}]


@pytest.mark.parametrize(
    "spec, match",
    [
        ("nope=1:2", "one of"),
        ("lookback", "one of"),
        ("lookback=4:2", "stop >= start"),
        ("lookback=1:4:0", "step > 0"),
        ("lookback=1:2:3:4", "start:stop"),
        ("lookback=a:b", "numbers"),
        ("lookback=1:2:0.5", "whole number"),
        ("lookback=1,1", "repeated"),
        ("stop_usd='x'", "not a number"),
    ],
)
def test_parse_grid_rejects_bad_specs(spec, match):
    with pytest.raises(ValueError, match=match):
        parse_grid([spec], Momentum.inputs)
    with pytest.raises(ValueError, match="twice"):
        parse_grid(["qty=1", "qty=2"], Momentum.inputs)



def test_2x2_grid_equals_four_separate_runs():
    bars, sub = market()
    grid = {"lookback": [2, 5], "stop_usd": [150.0, 400.0]}
    opt = optimize(Momentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, fixed={"qty": 2}, workers=2)
    assert opt.workers == 2
    r = opt.results
    assert r["combo"].tolist() == [1, 2, 3, 4]
    assert r[["lookback", "stop_usd"]].values.tolist() == [[2, 150.0], [2, 400.0], [5, 150.0], [5, 400.0]]
    for i, params in enumerate(combinations(grid)):
        res = run_backtest(Momentum(qty=2, **params), bars, NQ, COSTS, sub_bars=sub)
        want = flatten_metrics(backtest_metrics(res, bars))
        assert want["total_trades"] > 10
        for key, _, _, _ in RESULT_METRICS:
            got = r.at[i, key]
            assert (math.isnan(got) and math.isnan(want[key])) or got == want[key], (params, key)
    assert r["net_profit"].nunique() == 4
    solo = optimize(Momentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, fixed={"qty": 2}, workers=1)
    pd.testing.assert_frame_equal(solo.results, r)


def test_strategy_file_path_and_input_checks(tmp_path):
    bars, _ = market(days=1)
    src = tmp_path / "strat.py"
    src.write_text(
        "from guban.engine import Strategy\n"
        "class Hold(Strategy):\n"
        "    inputs = dict(qty=1)\n"
        "    def on_bar(self):\n"
        "        if self.current_bar == 2:\n"
        "            self.buy('L', self.p.qty)\n"
        "        if self.current_bar == 10:\n"
        "            self.sell('X')\n",
        encoding="utf-8",
    )
    opt = optimize(src, {"qty": [1, 2]}, NQ, bars=bars, workers=2)
    assert opt.results["total_trades"].tolist() == [1, 1]
    assert opt.results.at[1, "net_profit"] == pytest.approx(2 * opt.results.at[0, "net_profit"])
    loaded = optimize(load_strategy_class(src), {"qty": [1, 2]}, NQ, bars=bars, workers=2)
    pd.testing.assert_frame_equal(loaded.results, opt.results)
    with pytest.raises(ValueError, match="unknown grid inputs"):
        optimize(Momentum, {"nope": [1]}, NQ, bars=bars)
    with pytest.raises(ValueError, match="both fixed and in the grid"):
        optimize(Momentum, {"qty": [1]}, NQ, bars=bars, fixed={"qty": 2})
    with pytest.raises(ValueError, match="unknown metric"):
        optimize(Momentum, {"qty": [1]}, NQ, bars=bars, metric="nope")

    class Local(Strategy):
        inputs = dict(a=1)

    with pytest.raises(ValueError, match="top level"):
        optimize(Local, {"a": [1]}, NQ, bars=bars)


def test_worker_errors_name_the_inputs():
    bars, _ = market(days=1)
    with pytest.raises(RuntimeError, match=r"inputs \{'stop_usd': -1.0\}"):
        optimize(Momentum, {"stop_usd": [100.0, -1.0]}, NQ, bars=bars, workers=2)


def test_grid_over_5000_needs_confirmation():
    bars, _ = market(days=1)
    grid = {"lookback": list(range(1, 72)), "stop_usd": [float(v) for v in range(100, 171)]}
    with pytest.raises(TooManyCombinations, match="5,041 combinations"):
        optimize(Momentum, grid, NQ, bars=bars)



def _table(net, trades, losers=None):
    n = len(net)
    return pd.DataFrame({"combo": range(1, n + 1), "a": range(n), "net_profit": net, "total_trades": trades,
                         "max_consecutive_losers": losers or [0] * n})


def test_rank_by_metric_with_min_trades_filter_and_stable_ties():
    t = _table([100.0, 300.0, 300.0, 900.0, np.nan], [40, 40, 50, 10, 60])
    assert rank_results(t, "net_profit", 30).tolist() == [3, 1, 2, pd.NA, pd.NA]
    assert rank_results(t, "net_profit", 0).tolist() == [4, 2, 3, 1, pd.NA]
    t = _table([1.0, 2.0, 3.0], [50, 50, 50], losers=[4, 2, 7])
    assert rank_results(t, "max_consecutive_losers", 0).tolist() == [2, 1, 3]


def _opt(grid, rows, min_trades=30, metric="net_profit"):
    combos = combinations(grid)
    res = pd.concat([pd.DataFrame(combos), pd.DataFrame(rows)], axis=1)
    for key, _, _, _ in RESULT_METRICS:
        if key not in res:
            res[key] = 0.0
    res.insert(0, "combo", np.arange(1, len(res) + 1))
    res["rank"] = rank_results(res, metric, min_trades)
    return OptimizeResult(grid, res, metric, min_trades, 1, 0.5)


def test_heatmap_takes_the_best_eligible_run_over_other_inputs():
    grid = {"a": [1, 2], "b": [10, 20], "c": [0.5, 1.5]}
    net = [5, 7, 1, 2, 9, -3, 4, 8]
    trades = [50, 50, 50, 50, 50, 50, 5, 5]
    opt = _opt(grid, {"net_profit": net, "total_trades": trades})
    h = heatmap(opt, "a", "b", "net_profit")
    assert h["x"] == [1, 2] and h["y"] == [10, 20]
    assert h["z"][0].tolist() == [7, 9]
    assert h["z"][1][0] == 2 and math.isnan(h["z"][1][1])
    assert h["combo"].tolist() == [[2, 5], [4, 0]]
    assert opt.best == {"a": 2, "b": 10, "c": 0.5}
    assert opt.ranked()["combo"].tolist()[:6] == [5, 2, 1, 4, 3, 6] and opt.ranked()["rank"].isna().sum() == 2
    assert heatmap_axes(grid, None, None) == ("a", "b")
    assert heatmap_axes(grid, "c", None) == ("c", "a")
    with pytest.raises(ValueError, match="not a grid input"):
        heatmap_axes(grid, "z", None)



def test_optimize_html_is_offline_and_has_heatmap_and_table(tmp_path):
    grid = {"a": [1, 2, 3], "b": [0.5, 1.0]}
    opt = _opt(grid, {"net_profit": [-50.0, 20.0, 80.0, 10.0, 30.0, -5.0], "total_trades": [40] * 6}, min_trades=30)
    meta = {"title": "Opt <test>", "details": {"Grid": "a: 1, 2, 3"}, "inputs": {"qty": 2}, "notes": ["n1"]}
    path = write_optimize_report(opt, tmp_path / "optimize.html", meta)
    text = path.read_text(encoding="utf-8")
    assert "<script src" not in text and "http" not in text and "Plotly" not in text
    assert "Opt &lt;test&gt;" in text and "qty = 2" in text and "n1" in text
    assert "Heatmap: b × a" in text and "id='heat'" in text and "<option value='net_profit' selected>" in text
    imgs = re.findall(r"<img class='chart' src='data:image/png;base64,[^']+' alt='[^']*' data-metric='(\w+)'( hidden='hidden')?>", text)
    assert [m for m, _ in imgs] == [k for k, *_ in RESULT_METRICS] and [m for m, h in imgs if not h] == ["net_profit"]
    assert text.count("<tr class=best>") == 1 and "80.00" in text
    opt1 = _opt({"a": [1, 2, 3], "b": [0.5]}, {"net_profit": [1.0, 2.0, 3.0], "total_trades": [40] * 3})
    t1 = write_optimize_report(opt1, tmp_path / "one.html", {}).read_text(encoding="utf-8")
    assert "Metric by input: a" in t1 and t1.count("data-metric=") == len(RESULT_METRICS)
    opt0 = _opt({"a": [1]}, {"net_profit": [1.0], "total_trades": [40]})
    t0 = write_optimize_report(opt0, tmp_path / "zero.html", {}).read_text(encoding="utf-8")
    assert "id='heat'" not in t0 and "Ranked results" in t0


def test_results_csv_ranked_first_then_filtered(tmp_path):
    opt = _opt({"a": [1, 2, 3]}, {"net_profit": [5.0, 9.0, 7.0], "total_trades": [40, 40, 3]})
    df = pd.read_csv(opt.write_csv(tmp_path / "r.csv"))
    assert df["combo"].tolist() == [2, 1, 3] and df["rank"].tolist()[:2] == [1, 2] and math.isnan(df["rank"].iat[2])


class Kinds(Strategy):
    inputs = dict(n=2, x=1.5, on=True, label="a", extra=None)

    def on_bar(self):
        pass


def test_parse_grid_takes_the_type_of_each_default():
    g = parse_grid(["on=true,False", "label=x,'y z',9:30", "x=1,2.5", "extra=None,3"], Kinds.inputs)
    assert g == {"on": [True, False], "label": ["x", "y z", "9:30"], "x": [1.0, 2.5], "extra": [None, 3]}


@pytest.mark.parametrize(
    "spec, match",
    [
        ("on=not_bool", "not true or false"),
        ("on=1", "not true or false"),
        ("on=0:1", "not true or false"),
        ("x=nan", "not a finite number"),
        ("x=inf,1", "not a finite number"),
        ("x=1e400", "not a finite number"),
    ],
)
def test_parse_grid_rejects_values_of_the_wrong_type(spec, match):
    with pytest.raises(ValueError, match=match):
        parse_grid([spec], Kinds.inputs)
    assert parse_grid(["label=123"], Kinds.inputs) == {"label": ["123"]}


@pytest.mark.parametrize(
    "kwargs, match",
    [
        (dict(grid={"on": ["yes"]}), "grid input on='yes' does not fit"),
        (dict(grid={"n": [1.5]}), "grid input n=1.5 does not fit"),
        (dict(grid={"x": [float("nan")]}), "grid input x=nan does not fit"),
        (dict(grid={"n": 3}), "give a list"),
        (dict(grid={"n": [1]}, fixed={"label": 5}), "fixed input label=5 does not fit"),
        (dict(grid={"n": [1]}, min_trades=-1), "min_trades must be"),
        (dict(grid={"n": [1]}, workers=0), "workers must be"),
    ],
)
def test_direct_api_grids_and_counts_are_checked(kwargs, match):
    bars, _ = market(days=1)
    with pytest.raises(ValueError, match=match):
        optimize(Kinds, spec=NQ, bars=bars, **kwargs)


def test_decimal_values_never_leak():
    g = parse_grid(["stop_usd=1:2:0.5"], Momentum.inputs)
    assert not any(isinstance(v, Decimal) for v in g["stop_usd"])



def test_window_scores_each_combo_as_a_run_on_that_window_only():
    from guban.metrics import window_metrics
    from guban.optimize import window_runs

    bars, sub = market(days=4)
    lo, hi = 78, 234
    grid = {"lookback": [2, 5], "stop_usd": [150.0, 400.0]}
    opt = optimize(Momentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, workers=2, window=(lo, hi))
    whole = optimize(Momentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, workers=1)
    assert not opt.results["net_profit"].equals(whole.results["net_profit"])
    for i, params in enumerate(combinations(grid)):
        res = run_backtest(Momentum(**params), bars.iloc[:hi], NQ, COSTS, sub_bars=sub, trade_from=lo, window_ends={hi})
        want = flatten_metrics(window_metrics(res, bars, lo, hi))
        for key, _, _, _ in RESULT_METRICS:
            got = opt.results.at[i, key]
            assert (math.isnan(got) and math.isnan(want[key])) or got == want[key], (params, key)
    items = [(combinations(grid)[0], lo, hi), (combinations(grid)[3], hi, len(bars))]
    runs = window_runs(Momentum, items, NQ, COSTS, bars=bars, sub_bars=sub, workers=2)
    assert runs[0].net_profit == opt.results.at[0, "net_profit"]
    assert len(runs[1].equity) == len(bars) - hi and (runs[1].trades["entry_bar"] >= hi).all()
    with pytest.raises(ValueError, match="window"):
        optimize(Momentum, grid, NQ, COSTS, bars=bars, sub_bars=sub, workers=1, window=(10, 5))
