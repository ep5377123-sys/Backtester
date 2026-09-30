"""Trade Monte Carlo (Phase 11): resample and reorder a run's trades."""

import numpy as np
import pandas as pd
import pytest

import guban.montecarlo as mc
from guban.metrics import compute_metrics
from guban.montecarlo import PERCENTILES, check_settings, run_monte_carlo

NET = np.array([100.0, -250.0, 50.0, -75.0, 300.0])  # cum 0,100,-150,-100,-175,125 -> max DD -275 (100 -> -175)


def trades_frame(net: np.ndarray) -> pd.DataFrame:
    n = len(net)
    t = pd.date_range("2024-01-02 10:00", periods=n, freq="h", tz="America/New_York")
    return pd.DataFrame({
        "trade_no": np.arange(1, n + 1), "direction": "long", "net_pnl": net, "cum_net_pnl": np.cumsum(net),
        "entry_bar": np.arange(n) * 2, "exit_bar": np.arange(n) * 2 + 1, "commission": 0.0, "slippage": 0.0,
        "ambiguous": False, "exit_time": t,
    })  # fmt: skip


def test_backtest_drawdown_is_compute_metrics_close_to_close():
    res = run_monte_carlo(NET, sims=10, seed=1)
    tr = trades_frame(NET)
    equity = pd.Series(tr["cum_net_pnl"].to_numpy(), index=tr["exit_time"])
    assert res.max_dd == pytest.approx(-275.0)
    assert res.max_dd == pytest.approx(compute_metrics(tr, equity)["account"]["max_drawdown_close_to_close"])
    assert res.net == pytest.approx(125.0)
    assert res.path[0] == 0 and res.path[-1] == pytest.approx(125.0)


def test_simulated_drawdown_matches_a_direct_computation_per_run():
    res = run_monte_carlo(NET, sims=200, seed=5)
    floor = {"resample": len(NET) * NET.min(), "reorder": NET[NET < 0].sum()}  # deepest possible drawdown
    for name in ("resample", "reorder"):
        m = res.methods[name]
        assert m.net.shape == m.max_dd.shape == (200,)
        assert (m.max_dd <= 0).all()
        assert (m.max_dd >= floor[name] - 1e-9).all()
    # reorder: re-draw the same permutations and check each run by hand
    rng = np.random.default_rng(5 + 1)
    perm = rng.permuted(np.broadcast_to(NET, (200, 5)), axis=1)
    for k in (0, 17, 199):
        cum = np.r_[0.0, np.cumsum(perm[k])]
        assert res.methods["reorder"].max_dd[k] == pytest.approx((cum - np.maximum.accumulate(cum)).min())


def test_reorder_keeps_net_profit_resample_varies_it():
    res = run_monte_carlo(NET, sims=500, seed=2)
    assert np.allclose(res.methods["reorder"].net, NET.sum(), atol=1e-6)
    assert res.methods["resample"].net.std() > 0
    assert res.methods["reorder"].max_dd.std() > 0


def test_same_seed_same_result_other_seed_differs():
    a, b, c = (run_monte_carlo(NET, sims=300, seed=s) for s in (7, 7, 8))
    assert a.summary() == b.summary()
    assert a.report_data() == b.report_data()
    assert a.summary() != c.summary()


def test_batch_size_does_not_change_results(monkeypatch):
    ref = run_monte_carlo(NET, sims=250, seed=3)
    monkeypatch.setattr(mc, "BATCH_CELLS", 7 * len(NET))  # 7 runs per batch
    small = run_monte_carlo(NET, sims=250, seed=3)
    for name in ("resample", "reorder"):
        assert np.array_equal(ref.methods[name].net, small.methods[name].net)
        assert np.array_equal(ref.methods[name].max_dd, small.methods[name].max_dd)
        assert np.array_equal(ref.methods[name].bands, small.methods[name].bands)


def test_equal_trades_have_no_spread():
    res = run_monte_carlo(np.full(10, 20.0), sims=100, seed=0)
    for name in ("resample", "reorder"):
        s = res.method_summary(name)
        assert set(s["net_profit"].values()) == {200.0}
        assert set(s["max_drawdown"].values()) == {0.0}
        assert s["p_dd_worse_than_backtest"] == 0.0
        assert sum(res.report_data()[name]["dd_hist"]["counts"]) == 100
    assert res.method_summary("resample")["p_net_le_0"] == 0.0


def test_percentiles_are_ordered_and_probabilities_in_range():
    rng = np.random.default_rng(0)
    net = rng.normal(10, 200, 300)
    res = run_monte_carlo(net, sims=2_000, seed=4, dd_limit=1_500)
    for name in ("resample", "reorder"):
        s = res.method_summary(name)
        for key in ("net_profit", "max_drawdown"):
            vals = [s[key][f"p{p}"] for p in PERCENTILES]
            assert vals == sorted(vals)
        assert 0 <= s["p_dd_worse_than_backtest"] <= 1 and 0 <= s["p_dd_reaches_limit"] <= 1
        assert s["p_dd_reaches_limit"] == pytest.approx(np.mean(res.methods[name].max_dd <= -1_500))
        bands = res.methods[name].bands
        assert bands.shape == (5, len(res.x)) and (np.diff(bands, axis=0) >= -1e-6).all()
    assert "p_net_le_0" in res.method_summary("resample") and "p_net_le_0" not in res.method_summary("reorder")


def test_bands_are_sampled_at_most_band_points():
    res = run_monte_carlo(np.random.default_rng(1).normal(0, 50, 5_000), sims=20, seed=0)
    assert len(res.x) <= mc.BAND_POINTS and res.x[0] == 0 and res.x[-1] == 5_000
    small = run_monte_carlo(NET, sims=20, seed=0)
    assert small.x.tolist() == [0, 1, 2, 3, 4, 5]


def test_summary_shape():
    s = run_monte_carlo(NET, sims=50, seed=9).summary()
    assert s["sims"] == 50 and s["seed"] == 9 and s["dd_basis"] == "close_to_close" and s["unit"] == "trade"
    assert s["dd_limit"] is None and s["trades"] == 5
    assert set(s["resample"]) == {"net_profit", "max_drawdown", "p_dd_worse_than_backtest", "p_net_le_0"}
    assert set(s["reorder"]) == {"net_profit", "max_drawdown", "p_dd_worse_than_backtest"}


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ((-1, 42, None), "--mc-sims"),
        ((1_000_001, 42, None), "--mc-sims"),
        ((1.5, 42, None), "--mc-sims"),
        ((100, -1, None), "--mc-seed"),
        ((100, 42, 0.0), "--mc-dd-limit"),
        ((100, 42, -5.0), "--mc-dd-limit"),
        ((100, 42, float("nan")), "--mc-dd-limit"),
    ],
)
def test_bad_settings_name_the_flag(args, message):
    with pytest.raises(ValueError, match=message):
        check_settings(*args)


def test_too_few_trades_and_bad_values():
    check_settings(0, 42)  # 0 = off is a valid setting
    with pytest.raises(ValueError, match="at least 2 trades"):
        run_monte_carlo([10.0], sims=10)
    with pytest.raises(ValueError, match="NaN"):
        run_monte_carlo([10.0, np.nan], sims=10)
    with pytest.raises(ValueError, match="off"):
        run_monte_carlo(NET, sims=0)
