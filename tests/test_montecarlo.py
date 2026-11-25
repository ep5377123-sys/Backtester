"""Trade Monte Carlo (Phase 11): resample and reorder a run's trades."""

import numpy as np
import pandas as pd
import pytest

import guban.montecarlo as mc
from guban.metrics import compute_metrics
from guban.montecarlo import PERCENTILES, check_settings, run_monte_carlo

NET = np.array([100.0, -250.0, 50.0, -75.0, 300.0])


def trades_frame(net: np.ndarray) -> pd.DataFrame:
    n = len(net)
    t = pd.date_range("2024-01-02 10:00", periods=n, freq="h", tz="America/New_York")
    return pd.DataFrame({
        "trade_no": np.arange(1, n + 1), "direction": "long", "net_pnl": net, "cum_net_pnl": np.cumsum(net),
        "entry_bar": np.arange(n) * 2, "exit_bar": np.arange(n) * 2 + 1, "commission": 0.0, "slippage": 0.0,
        "ambiguous": False, "exit_time": t,
    })


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
    floor = {"resample": len(NET) * NET.min(), "reorder": NET[NET < 0].sum()}
    for name in ("resample", "reorder"):
        m = res.methods[name]
        assert m.net.shape == m.max_dd.shape == (200,)
        assert (m.max_dd <= 0).all()
        assert (m.max_dd >= floor[name] - 1e-9).all()
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
    for name in ("resample", "reorder"):
        assert np.array_equal(a.methods[name].bands, b.methods[name].bands)
        assert np.array_equal(a.methods[name].paths, b.methods[name].paths)
    assert a.summary() != c.summary()


def test_batch_size_does_not_change_results(monkeypatch):
    ref = run_monte_carlo(NET, sims=250, seed=3)
    monkeypatch.setattr(mc, "BATCH_CELLS", 7 * len(NET))
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
        assert np.allclose(res.methods[name].bands[:, -1], 200.0)
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
    assert set(s["resample"]) == {"net_profit", "max_drawdown", "max_drawdown_depth", "p_dd_worse_than_backtest", "p_net_le_0"}
    assert set(s["reorder"]) == {"net_profit", "max_drawdown", "max_drawdown_depth", "p_dd_worse_than_backtest"}
    assert {"ev", "horizons"} <= set(s) and "day" not in s


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
    check_settings(0, 42)
    with pytest.raises(ValueError, match="at least 2 trades"):
        run_monte_carlo([10.0], sims=10)
    with pytest.raises(ValueError, match="NaN"):
        run_monte_carlo([10.0, np.nan], sims=10)
    with pytest.raises(ValueError, match="off"):
        run_monte_carlo(NET, sims=0)



def test_day_method_keeps_trades_of_one_day_together():
    net = np.tile([300.0, -200.0], 50)
    days = np.repeat(np.arange(50), 2)
    res = run_monte_carlo(net, sims=2_000, seed=1, days=days)
    day, rs = res.methods["day"], res.methods["resample"]
    assert np.allclose(day.net, net.sum())
    assert np.allclose(day.max_dd, -200.0)
    assert rs.max_dd.min() < -200.0
    assert len(day.x) == 51 and day.x[-1] == 50


def test_day_drawdown_equals_the_trade_level_path():
    rng = np.random.default_rng(3)
    net = rng.normal(5, 100, 60)
    days = np.sort(rng.integers(0, 15, 60))
    res = run_monte_carlo(net, sims=50, seed=8, days=days)
    labels, inv = np.unique(days, return_inverse=True)
    draw = np.random.default_rng(8 + 2).integers(0, len(labels), (50, len(labels)))
    for k in (0, 21, 49):
        seq = np.concatenate([net[inv == d] for d in draw[k]])
        cum = np.r_[0.0, np.cumsum(seq)]
        assert res.methods["day"].max_dd[k] == pytest.approx((cum - np.maximum.accumulate(cum)).min())
        assert res.methods["day"].net[k] == pytest.approx(seq.sum())


def test_day_method_does_not_change_resample_or_reorder():
    days = np.array([0, 0, 1, 2, 2])
    a, b = run_monte_carlo(NET, sims=300, seed=7), run_monte_carlo(NET, sims=300, seed=7, days=days)
    for name in ("resample", "reorder"):
        assert np.array_equal(a.methods[name].net, b.methods[name].net)
        assert np.array_equal(a.methods[name].max_dd, b.methods[name].max_dd)
    assert "day" not in a.methods and "day" in b.methods and "day" in b.summary()
    one_day = run_monte_carlo(NET, sims=10, seed=7, days=np.zeros(5))
    assert "day" not in one_day.methods and "1 trading day" in one_day.skipped["day"]


def test_day_batch_size_does_not_change_results(monkeypatch):
    days = np.array([0, 0, 1, 2, 2])
    ref = run_monte_carlo(NET, sims=250, seed=3, days=days)
    monkeypatch.setattr(mc, "BATCH_CELLS", 7 * len(NET))
    small = run_monte_carlo(NET, sims=250, seed=3, days=days)
    assert np.array_equal(ref.methods["day"].max_dd, small.methods["day"].max_dd)
    assert np.array_equal(ref.methods["day"].paths, small.methods["day"].paths)
    assert ref.summary() == small.summary()


def test_ev_bootstrap_ci_contains_the_mean_and_widens_with_the_level():
    rng = np.random.default_rng(0)
    net = rng.normal(40, 300, 400)
    sides = np.where(np.arange(400) % 3 == 0, "short", "long")
    ev = run_monte_carlo(net, sims=3_000, seed=4, contracts=np.full(400, 2), sides=sides, risk=500).ev
    a = ev["groups"]["all"]
    assert a["trades"] == 400 and a["usd"]["mean"] == pytest.approx(net.mean())
    assert a["usd"]["lo"] < net.mean() < a["usd"]["hi"]
    assert a["r"]["mean"] == pytest.approx(net.mean() / (2 * 500))
    assert 0 <= a["p_ev_le_0"] <= 0.05
    assert ev["groups"]["long"]["trades"] + ev["groups"]["short"]["trades"] == 400
    assert ev["risk_per_contract"] == 500 and ev["risk_source"] == "--mc-risk"
    wide = run_monte_carlo(net, sims=3_000, seed=4, ci=0.99).ev["groups"]["all"]["usd"]
    assert wide["hi"] - wide["lo"] > a["usd"]["hi"] - a["usd"]["lo"]


def test_default_risk_is_the_average_losing_trade_per_contract():
    net = np.array([300.0, -200.0, -400.0, 100.0])
    ev = run_monte_carlo(net, sims=100, seed=0, contracts=np.array([1, 2, 1, 1])).ev
    assert ev["risk_per_contract"] == pytest.approx((100 + 400) / 2)
    assert ev["risk_source"] == "average losing trade per contract"
    assert run_monte_carlo(net, sims=100, seed=0).ev["risk_per_contract"] is None


def test_r_uses_each_trades_own_risk_and_mc_risk_overrides_it():
    net = np.array([500.0, -250.0, 1000.0, -500.0])
    risks = np.array([500.0, 250.0, 500.0, 500.0])
    contracts = np.array([5, 5, 5, 5])
    ev = run_monte_carlo(net, sims=500, seed=0, contracts=contracts, risks=risks).ev
    assert ev["groups"]["all"]["r"]["mean"] == pytest.approx(np.mean(net / risks))
    assert ev["risk_source"] == "per-trade stop" and ev["r_trades"] == 4
    assert ev["risk_per_contract"] == pytest.approx(np.mean(risks / contracts))
    over = run_monte_carlo(net, sims=500, seed=0, contracts=contracts, risks=risks, risk=50).ev
    assert over["risk_source"] == "--mc-risk" and over["groups"]["all"]["r"]["mean"] == pytest.approx(np.mean(net / (5 * 50)))


def test_trades_without_a_stop_are_left_out_of_r_only():
    rng = np.random.default_rng(3)
    net = rng.normal(20, 200, 60)
    risks = np.where(np.arange(60) % 4 == 0, np.nan, 400.0)
    a = run_monte_carlo(net, sims=400, seed=2, contracts=np.ones(60), risks=risks).ev
    b = run_monte_carlo(net, sims=400, seed=2, contracts=np.ones(60)).ev
    assert a["groups"]["all"]["usd"] == b["groups"]["all"]["usd"] and a["groups"]["all"]["p_ev_le_0"] == b["groups"]["all"]["p_ev_le_0"]
    known = ~np.isnan(risks)
    r = a["groups"]["all"]["r"]
    assert r["mean"] == pytest.approx(np.mean(net[known] / 400)) and r["lo"] < r["mean"] < r["hi"]
    assert a["r_trades"] == 45 and "15 of 60 trades without a stop" in a["risk_source"]
    none = run_monte_carlo(net, sims=100, seed=2, contracts=np.ones(60), risks=np.full(60, np.nan)).ev
    assert none["risk_source"] == "average losing trade per contract"
    with pytest.raises(ValueError, match="risks must be > 0"):
        run_monte_carlo(net, sims=100, risks=np.zeros(60))


def test_ev_splits_in_and_out_of_sample():
    net = np.r_[np.full(30, 50.0), np.full(20, -10.0)]
    times = pd.date_range("2025-12-01", periods=50, freq="D", tz="America/New_York")
    ev = run_monte_carlo(net, sims=200, seed=1, times=times, oos_from="2025-12-31").ev
    g = ev["groups"]
    assert g["in_sample"]["trades"] == 30 and g["out_of_sample"]["trades"] == 20
    assert g["in_sample"]["usd"]["mean"] == 50.0 and g["out_of_sample"]["usd"]["mean"] == -10.0
    assert g["out_of_sample"]["p_ev_le_0"] == 1.0 and ev["oos_from"] == "2025-12-31"


def test_horizons_and_depth_percentiles():
    rng = np.random.default_rng(2)
    net = rng.normal(20, 150, 60)
    res = run_monte_carlo(net, sims=1_000, seed=5, days=np.repeat(np.arange(30), 2))
    h = res.horizons
    assert [r["n"] for r in h["resample"]] == [10, 20, 40]
    assert [r["n"] for r in h["day"]] == [10, 20]
    for row in h["resample"] + h["day"]:
        d = [row["max_drawdown_depth"][f"p{p}"] for p in mc.DEPTH_PERCENTILES]
        assert d == sorted(d) and d[0] >= 0
        assert 0 <= row["p_net_lt_0"] <= 1
    s = res.method_summary("resample")["max_drawdown_depth"]
    assert s["p50"] == pytest.approx(np.percentile(-res.methods["resample"].max_dd, 50))


def test_sample_paths_are_the_first_runs():
    res = run_monte_carlo(NET, sims=150, seed=6, days=np.array([0, 0, 1, 2, 2]))
    for name in ("resample", "reorder", "day"):
        p = res.methods[name].paths
        assert p.shape == (mc.SAMPLE_PATHS, len(res.methods[name].x))
        assert np.allclose(p[:, -1], res.methods[name].net[: mc.SAMPLE_PATHS])
