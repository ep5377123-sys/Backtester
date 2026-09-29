"""Prop-firm Monte Carlo (R10 / S6): the shipped rules file, trade loading, the daily pool, the simulation against a
verbatim copy of the notebook's code, hand-computed outcomes, degradation, the summary, the HTML page and the CLI."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from guban.cli import app
from guban.propfirm import (
    FAIL_DAILY,
    FAIL_DD,
    FAIL_TIME,
    PASS,
    Rules,
    Setup,
    daily_pool,
    degrade,
    load_setup,
    load_trades,
    run_propfirm,
    simulate,
    simulate_paths,
    wilson_ci,
)
from guban.propfirm.report import write_propfirm_report
from guban.propfirm.sim import add_costs, verdict

MC_HEADER = "trade_id,order_id,type,signal,excel_date,excel_time,price,contracts,profit_usd,profit_pct,cum_profit_usd,cum_profit_pct,runup_usd,runup_pct,drawdown_usd,drawdown_pct,direction"
EXCEL_EPOCH = pd.Timestamp("1899-12-30")


def write_mc_csv(path, rows: list[tuple[str, float, int, str]]) -> None:
    """MC TradesToCSV layout, every line wrapped in quotes. rows: (entry 'YYYY-MM-DD HH:MM', profit, contracts, 'LONG'/'SHORT')."""
    lines = [f'"{MC_HEADER}"']
    cum = 0.0
    for i, (when, profit, qty, d) in enumerate(rows, start=1):
        serial = (pd.Timestamp(when) - EXCEL_EPOCH) / pd.Timedelta(days=1)
        cum += profit
        kind = "EntryLong" if d == "LONG" else "EntryShort"
        lines.append(f'"{i},{2 * i - 1},{kind},Sig,{serial},{serial},100.0,{qty},{profit},0.1,{cum},0.1,0,0,0,0,{d}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def apex(**kw) -> Rules:
    base = dict(name="Test", account_size=25_000, profit_target=1_250, max_drawdown=1_000, eod_trailing=True, max_trading_days=21)
    return Rules(**{**base, **kw})


# --- the notebook's code, verbatim (cell 6), as the oracle ----------------------------------------------------


@dataclass
class ApexChallenge:
    name: str
    account_size: float
    profit_target: float
    max_drawdown: float
    eod_trailing: bool
    daily_loss_limit: float = 0
    min_trading_days: int = 0
    max_trading_days: int = 21


def nb_apply_day_degradation(day_pool, rng, degrade_wr=0.0, degrade_pnl=0.0):
    pool = day_pool.copy()
    if degrade_pnl > 0:
        pool["net_pnl_usd"] *= (1.0 - degrade_pnl)
    if degrade_wr < 0:
        win_idx = pool.index[pool["net_pnl_usd"] > 0].to_numpy()
        loss_vals = pool.loc[pool["net_pnl_usd"] < 0, "net_pnl_usd"].to_numpy()
        if len(win_idx) > 0:
            n_flip = min(int(abs(degrade_wr) * len(pool)), len(win_idx))
            flip_idx = rng.choice(win_idx, size=n_flip, replace=False)
            replacement = np.mean(loss_vals) if len(loss_vals) > 0 else -abs(pool["net_pnl_usd"].mean())
            pool.loc[flip_idx, "net_pnl_usd"] = replacement
    return pool


def nb_simulate_apex(daily_pool, challenge, n_sims, n_contracts, rng, degrade_wr=0.0, degrade_pnl=0.0):
    pool = nb_apply_day_degradation(daily_pool, rng, degrade_wr, degrade_pnl).copy()
    pool["scaled_day_pnl"] = pool["net_pnl_usd"] * n_contracts
    vals = pool["scaled_day_pnl"].to_numpy()
    passed = fail_dd = fail_daily = fail_time = 0
    days_to_pass, max_dds_all, final_equities = [], [], []
    for _ in range(n_sims):
        equity = eod_hwm = sim_max_dd = 0.0
        outcome = None
        for day in range(1, challenge.max_trading_days + 1):
            day_pnl = vals[rng.integers(len(vals))]
            equity += day_pnl
            current_dd = eod_hwm - equity if challenge.eod_trailing else (eod_hwm - equity if equity <= eod_hwm else 0.0)
            if not challenge.eod_trailing and equity > eod_hwm:
                eod_hwm = equity
            sim_max_dd = max(sim_max_dd, current_dd)
            if current_dd >= challenge.max_drawdown:
                outcome = "fail_dd"; break  # noqa: E702
            if challenge.daily_loss_limit > 0 and day_pnl <= -challenge.daily_loss_limit:
                outcome = "fail_daily"; break  # noqa: E702
            if challenge.eod_trailing and equity > eod_hwm:
                eod_hwm = equity
            if equity >= challenge.profit_target and day >= challenge.min_trading_days:
                outcome = "pass"; days_to_pass.append(day); break  # noqa: E702
        if outcome is None:
            outcome = "fail_time"
        if outcome == "pass":
            passed += 1
        elif outcome == "fail_dd":
            fail_dd += 1
        elif outcome == "fail_daily":
            fail_daily += 1
        else:
            fail_time += 1
        max_dds_all.append(sim_max_dd)
        final_equities.append(equity)
    return {"passed": passed, "fail_dd": fail_dd, "fail_daily": fail_daily, "fail_time": fail_time,
            "days_to_pass": np.array(days_to_pass), "max_dds_all": np.array(max_dds_all), "final_equities": np.array(final_equities)}  # fmt: skip


def random_pool(seed: int = 7, n: int = 300) -> np.ndarray:
    """A pool shaped like a real one: many zero days, small winners, occasional big losers."""
    g = np.random.default_rng(seed)
    v = np.where(g.random(n) < 0.6, 0.0, np.where(g.random(n) < 0.85, g.normal(150, 60, n), g.normal(-700, 200, n)))
    return np.round(v, 2)


# --- rules file ----------------------------------------------------------------------------------------------


def test_shipped_rules_are_the_notebooks_values():
    s = load_setup("apex_50k_eod")
    r = s.rules
    assert (r.name, r.account_size, r.profit_target, r.max_drawdown) == ("Apex 50k EOD", 25_000, 1_250, 1_000)
    assert (r.eod_trailing, r.daily_loss_limit, r.min_trading_days, r.max_trading_days) == (True, 0, 0, 21)
    assert (s.commission, s.slippage, s.risk_per_contract) == (2.25, 5.0, 1000.0)
    assert (s.sims, s.sweep_sims, s.seed, s.size, s.paths, s.bootstrap_sims) == (20_000, 5_000, 42, 1.0, 300, 10_000)
    assert s.sweep == (0.3, 0.5, 0.7, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 1.9, 2.0)
    assert [(x.win_rate_adj, x.pnl_shrink) for x in s.scenarios] == [(0.0, 0.0), (-0.03, 0.10), (-0.05, 0.15), (-0.05, 0.25), (-0.08, 0.30)]
    assert any("25,000" in n for n in s.notes)  # the 50K-vs-$25k flag travels with the rules


def test_rules_lookup_and_overrides(tmp_path):
    with pytest.raises(FileNotFoundError, match="apex_50k_eod"):
        load_setup("no_such_firm")
    p = tmp_path / "mine.toml"
    p.write_text('[rules]\nname = "Mine"\nprofit_target = 3000\nmax_drawdown = 2500\n', encoding="utf-8")
    s = load_setup(p)
    assert s.key == "mine" and s.rules.max_trading_days == 21 and s.scenarios == () and s.sweep == ()
    t = s.with_overrides(size=2.0, sims=None, sweep=())
    assert t.size == 2.0 and t.sims == s.sims and t.sweep == ()


@pytest.mark.parametrize(
    "body, match",
    [
        ('eod_trailing = "false"', "eod_trailing must be true or false"),  # FIX.md #10: bool("false") was True
        ("profit_target = -5", "profit_target must be > 0"),
        ("max_drawdown = 0", "max_drawdown must be > 0"),
        ("min_trading_days = 30", "more than max_trading_days"),
        ("max_trading_days = 0", "max_trading_days must be >= 1"),
        ("max_trading_days = 2.5", "max_trading_days must be a whole number"),
        ("daily_loss = 100", "unknown keys"),
        ('profit_target = "3000"', "profit_target must be a number"),
    ],
)
def test_rules_files_are_read_strictly(tmp_path, body, match):
    p = tmp_path / "bad.toml"
    base = {"name": '"Bad"', "profit_target": "3000", "max_drawdown": "2500"}
    key = body.split("=")[0].strip()
    lines = [f"{k} = {v}" for k, v in base.items() if k != key] + [body]
    p.write_text("[rules]\n" + "\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_setup(p)


@pytest.mark.parametrize(
    "section, body, match",
    [
        ("costs", "risk_per_contract = 0", "risk_per_contract must be > 0"),  # would make R multiples infinite
        ("costs", "commission = -1", "commission must be >= 0"),
        ("simulation", "sims = 0", "sims must be >= 1"),
        ("simulation", "size = -1", "size must be > 0"),
        ("simulation", "sweep = [0.5, -1]", "every sweep size must be > 0"),
        ("scenarios", 'label = "x"\nwin_rate_adj = 0.05', "win_rate_adj must be <= 0"),
        ("scenarios", 'label = "x"\npnl_shrink = 1.0', "pnl_shrink must be < 1"),
    ],
)
def test_costs_simulation_and_scenarios_are_checked(tmp_path, section, body, match):
    p = tmp_path / "bad.toml"
    head = "[[scenarios]]" if section == "scenarios" else f"[{section}]"
    p.write_text(f'[rules]\nname = "Bad"\nprofit_target = 3000\nmax_drawdown = 2500\n\n{head}\n{body}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=match):
        load_setup(p)


def test_overrides_are_checked_too():
    s = load_setup("apex_50k_eod")
    for kw, match in ((dict(sims=0), "sims must be >= 1"), (dict(size=0.0), "size must be > 0"), (dict(paths=0), "paths must be >= 1")):
        with pytest.raises(ValueError, match=match):
            s.with_overrides(**kw)


def test_bootstrap_in_batches_equals_the_notebooks_single_draw(monkeypatch):
    import guban.propfirm.sim as sim

    x = np.random.default_rng(3).normal(size=37)
    want = np.random.default_rng(42).choice(x, size=(501, 37), replace=True).mean(axis=1)
    for batch in (37, 100, 10**9):  # one row per batch, a few rows, everything at once
        monkeypatch.setattr(sim, "BOOTSTRAP_BATCH", batch)
        boot, lo, hi = sim.bootstrap_ci(x, 501, seed=42)
        assert np.array_equal(boot, want)
        assert (lo, hi) == (float(np.quantile(want, 0.025)), float(np.quantile(want, 0.975)))


# --- trades and daily pool -----------------------------------------------------------------------------------


def test_load_trades_quoted_mc_csv(tmp_path):
    p = tmp_path / "mc.csv"
    write_mc_csv(p, [("2024-01-02 09:50", 100.0, 2, "LONG"), ("2024-01-02 11:00", -50.0, 2, "SHORT"), ("2024-01-04 23:59", 25.0, 2, "LONG")])
    df = load_trades(p)
    assert df["trade_id"].tolist() == [1, 2, 3]
    assert df["profit_usd"].tolist() == [100.0, -50.0, 25.0]
    assert df["direction"].tolist() == ["LONG", "SHORT", "LONG"]
    assert df["date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-02", "2024-01-02", "2024-01-04"]
    assert df["datetime"].dt.tz is None


def test_costs_and_daily_pool_fill_weekdays_and_drop_weekends(tmp_path):
    p = tmp_path / "mc.csv"
    # Fri 5th, Sun 7th (weekend: not in the pool), Tue 9th x2
    write_mc_csv(p, [("2024-01-05 10:00", 100.0, 1, "LONG"), ("2024-01-07 17:30", 999.0, 1, "LONG"),
                     ("2024-01-09 10:00", 40.0, 2, "LONG"), ("2024-01-09 12:00", -60.0, 2, "SHORT")])  # fmt: skip
    setup = Setup(key="t", rules=apex(), commission=2.0, slippage=3.0, risk_per_contract=500.0)
    df = add_costs(load_trades(p), setup)
    assert df["costs_usd"].tolist() == [5.0, 5.0, 10.0, 10.0]
    assert df["r_mult"].iat[2] == pytest.approx(30.0 / 1000.0)
    d = daily_pool(df)
    assert d["date"].dt.strftime("%a %d").tolist() == ["Fri 05", "Mon 08", "Tue 09"]
    assert d["n_trades"].tolist() == [1, 0, 2]
    assert d["net_pnl_usd"].tolist() == [95.0, 0.0, -40.0]
    assert d["is_trade_day"].tolist() == [True, False, True]
    res = run_propfirm(load_trades(p), Setup(key="t", rules=apex(), sims=50, sweep_sims=10, paths=5, bootstrap_sims=100))
    assert (res.weekend_trades, res.weekend_days) == (1, 1)
    assert any("weekend" in n for n in res.notes)
    # FIX.md #11: the method's modeling assumptions are labeled apart from the firm's rules
    assert sum(n.startswith("Assumption:") for n in res.notes) >= 3 and any("entry date" in n for n in res.notes)


# --- simulation ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rules", [
    apex(),
    apex(eod_trailing=False),
    apex(daily_loss_limit=500),
    apex(min_trading_days=8, max_trading_days=15),
])  # fmt: skip
@pytest.mark.parametrize("size, wr, shrink", [(1.0, 0.0, 0.0), (1.7, 0.0, 0.0), (1.0, -0.05, 0.15), (0.5, -0.08, 0.0)])
def test_simulate_equals_the_notebook(rules, size, wr, shrink):
    net = random_pool()
    ch = ApexChallenge(**{k: getattr(rules, k) for k in ("name", "account_size", "profit_target", "max_drawdown", "eod_trailing",
                                                           "daily_loss_limit", "min_trading_days", "max_trading_days")})  # fmt: skip
    nb = nb_simulate_apex(pd.DataFrame({"net_pnl_usd": net}), ch, 2_000, size, np.random.default_rng(42), wr, shrink)
    ours = simulate(net, rules, 2_000, size, np.random.default_rng(42), wr, shrink)
    assert (ours.passed, ours.count(FAIL_DD), ours.count(FAIL_DAILY), ours.count(FAIL_TIME)) == (nb["passed"], nb["fail_dd"], nb["fail_daily"], nb["fail_time"])
    np.testing.assert_array_equal(ours.days_to_pass, nb["days_to_pass"])
    np.testing.assert_array_equal(ours.max_dd, nb["max_dds_all"])
    np.testing.assert_array_equal(ours.final_equity, nb["final_equities"])


def test_hand_computed_outcomes():
    r = apex()
    rng = lambda: np.random.default_rng(1)  # noqa: E731
    up = simulate(np.array([500.0]), r, 10, 1.0, rng())  # 500, 1000, 1500 -> pass on day 3
    assert up.pass_rate == 1.0 and set(up.days) == {3} and set(up.final_equity) == {1500.0} and up.max_dd.max() == 0.0
    down = simulate(np.array([-600.0]), r, 10, 1.0, rng())  # drawdown 600, then 1200 >= 1000 on day 2
    assert down.rate(FAIL_DD) == 1.0 and set(down.days) == {2} and set(down.max_dd) == {1200.0}
    half = simulate(np.array([-600.0]), r, 10, 0.5, rng())  # size 0.5: -300/day, drawdown 1,200 on day 4
    assert half.rate(FAIL_DD) == 1.0 and set(half.days) == {4}
    flat = simulate(np.array([0.0]), r, 10, 1.0, rng())
    assert flat.rate(FAIL_TIME) == 1.0 and set(flat.days) == {21}
    exact = simulate(np.array([-1000.0]), r, 5, 1.0, rng())  # drawdown exactly at the limit fails
    assert exact.rate(FAIL_DD) == 1.0 and set(exact.days) == {1}
    daily = simulate(np.array([-500.0]), apex(daily_loss_limit=500), 5, 1.0, rng())
    assert daily.rate(FAIL_DAILY) == 1.0 and set(daily.days) == {1}
    slow = simulate(np.array([700.0]), apex(min_trading_days=4), 5, 1.0, rng())  # target reached day 2, pass needs day 4
    assert slow.pass_rate == 1.0 and set(slow.days) == {4}


def test_trailing_drawdown_follows_the_end_of_day_high():
    # Pool [+900, -950]: drawn in order +900, -950 -> equity -50 after a 900 high: drawdown 950 < 1000, no fail.
    # A second -950 would make it 1900: fail.
    r = apex(profit_target=100_000)
    class Seq:  # rng stand-in that draws days in a fixed order
        def __init__(self, idx):
            self.idx = iter(idx)

        def integers(self, n):
            return next(self.idx)

    res = simulate(np.array([900.0, -950.0]), r, 1, 1.0, Seq([0, 1, 1]))
    assert res.outcome[0] == FAIL_DD and res.days[0] == 3 and res.max_dd[0] == pytest.approx(1900.0)
    res = simulate(np.array([900.0, -950.0]), r, 1, 1.0, Seq([0, 1] + [0] * 19))
    assert res.outcome[0] == FAIL_TIME and res.max_dd[0] == pytest.approx(950.0)


def test_degradation():
    net = np.array([100.0, 200.0, 0.0, -300.0, -500.0, 50.0, 0.0, 0.0, 0.0, 0.0])
    shrunk = degrade(net, np.random.default_rng(0), 0.0, 0.2)
    np.testing.assert_allclose(shrunk, net * 0.8)
    out = degrade(net, np.random.default_rng(0), -0.2, 0.0)  # int(0.2 x 10) = 2 winning days -> mean loss -400
    assert (out == -400.0).sum() == 2 and (out > 0).sum() == 1 and (out == 0).sum() == 5
    nb = nb_apply_day_degradation(pd.DataFrame({"net_pnl_usd": net}), np.random.default_rng(0), -0.2, 0.1)["net_pnl_usd"].to_numpy()
    np.testing.assert_array_equal(degrade(net, np.random.default_rng(0), -0.2, 0.1), nb)
    only_wins = degrade(np.array([10.0, 20.0, 30.0, 40.0]), np.random.default_rng(0), -0.5, 0.0)  # no losers: minus |mean|
    assert (only_wins == -25.0).sum() == 2


def test_paths_stop_on_failure_and_flatline_after_pass():
    r = apex(max_trading_days=6)
    up, oc = simulate_paths(np.array([500.0]), r, 3, 1.0, np.random.default_rng(0))
    assert (oc == PASS).all()
    np.testing.assert_array_equal(up[0], [500, 1000, 1500, 1500, 1500, 1500])
    down, oc = simulate_paths(np.array([-600.0]), r, 2, 1.0, np.random.default_rng(0))
    assert (oc == FAIL_DD).all() and down[0, 1] == -1200 and np.isnan(down[0, 2:]).all()


def test_wilson_and_verdict():
    lo, hi = wilson_ci(4836, 20_000)
    assert (round(lo, 3), round(hi, 3)) == (0.236, 0.248)  # the notebook's printed CI for 24.2%
    assert np.isnan(wilson_ci(0, 0)[0])
    assert [verdict(p)[0] for p in (0.9, 0.7, 0.5, 0.35, 0.2, 0.0)] == ["STRONG", "STRONG", "SOLID", "MODERATE", "RISKY", "POOR"]


# --- full run, report, CLI -----------------------------------------------------------------------------------


def fixture_trades(tmp_path, n_days: int = 120):
    g = np.random.default_rng(3)
    rows, day = [], pd.Timestamp("2024-01-01")
    while len(rows) < n_days:
        day += pd.Timedelta(days=1)
        if day.dayofweek < 5 and g.random() < 0.5:
            rows.append((f"{day:%Y-%m-%d} 10:00", float(round(g.choice([120.0, 150.0, -400.0], p=[0.5, 0.35, 0.15]), 2)), 1, "LONG"))
    p = tmp_path / "Some Strategy.csv"
    write_mc_csv(p, rows)
    return p


def test_run_propfirm_is_deterministic_and_consistent(tmp_path):
    p = fixture_trades(tmp_path)
    setup = load_setup("apex_50k_eod").with_overrides(sims=400, sweep_sims=200, sweep=(0.5, 1.0, 2.0), paths=20, bootstrap_sims=200)
    a, b = run_propfirm(load_trades(p), setup), run_propfirm(load_trades(p), setup)
    np.testing.assert_array_equal(a.main.outcome, b.main.outcome)
    assert a.sweep_table().equals(b.sweep_table()) and a.scenario_table().equals(b.scenario_table())
    # the baseline scenario is the main settings with the degradation seed; size 1.0 in the sweep likewise with the sweep seed
    same = simulate(a.daily["net_pnl_usd"].to_numpy(), setup.rules, 200, 1.0, np.random.default_rng(setup.seed + 2000))
    np.testing.assert_array_equal(a.sweep[1].outcome, same.outcome)
    s = a.summary()
    pr = a.main.pass_rate
    assert s["cumulative"][2] == pytest.approx(1 - (1 - pr) ** 2)
    assert a.best_size is None or a.best_size.pass_rate == max(r.pass_rate for r in a.sweep)
    assert a.sweep_table()["contracts"].tolist() == [0.5, 1.0, 2.0]
    j = a.to_json()
    assert j["main"]["passed"] + j["main"]["fail_dd"] + j["main"]["fail_daily"] + j["main"]["fail_time"] == 400


def test_html_report_is_offline(tmp_path):
    p = fixture_trades(tmp_path)
    setup = load_setup("apex_50k_eod").with_overrides(sims=300, sweep_sims=100, paths=30, bootstrap_sims=100)
    res = run_propfirm(load_trades(p), setup)
    path = write_propfirm_report(res, tmp_path / "out" / "propfirm.html", {"title": "T", "details": {"Rules": "x"}, "notes": res.notes})
    text = path.read_text(encoding="utf-8")
    for needle in ("id='paths'", "id='dtp'", "id='mdd'", "id='degr'", "id='sweep'", "Passing within N attempts", "Degradation scenarios", "Size sweep"):
        assert needle in text
    assert "src='http" not in text and 'src="http' not in text and "<link" not in text


def test_cli_propfirm(tmp_path):
    p = fixture_trades(tmp_path)
    out = tmp_path / "pf"
    r = CliRunner().invoke(app, ["propfirm", "--trades", str(p), "--sims", "300", "--sweep-sims", "100", "--sweep", "0.5,1", "--seed", "7",
                                 "--commission", "0", "--slippage", "0", "--out", str(out)])  # fmt: skip
    assert r.exit_code == 0, r.output
    assert "PASS RATE" in r.output
    for f in ("propfirm.json", "propfirm.html", "sweep.csv", "degradation.csv", "daily.csv"):
        assert (out / f).exists()
    sweep = pd.read_csv(out / "sweep.csv")
    assert sweep["size"].tolist() == [0.5, 1.0]
    import json

    j = json.loads((out / "propfirm.json").read_text(encoding="utf-8"))
    assert j["settings"]["seed"] == 7 and j["settings"]["commission"] == 0 and j["trades"]["costs_total"] == 0
    bad = CliRunner().invoke(app, ["propfirm", "--trades", str(p), "--rules", "nope"])
    assert bad.exit_code != 0
