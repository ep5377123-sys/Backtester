"""CLI polish (Phase 10): command list, shared flags, default run folders and error messages."""

from pathlib import Path

import json

import numpy as np
import pandas as pd
import pytest
import typer
from typer.testing import CliRunner

import guban.cli as cli
from guban.cli import app, run_dir

runner = CliRunner()

FLIP = '''from guban.engine import Strategy


class Flip(Strategy):
    inputs = dict(hold=3)

    def on_bar(self):
        if self.market_position == 0:
            self.buy("L", 1)
        elif self.bars_since_entry >= self.p.hold:
            self.sell("X")
'''

BOOM = '''from guban.engine import Strategy


class Boom(Strategy):
    inputs = dict(at_bar=5)

    def on_bar(self):
        if self.current_bar == self.p.at_bar:
            return 1 / 0
'''


BAD_INIT = '''from guban.engine import Strategy


class BadInit(Strategy):
    inputs = dict(n=0)

    def __init__(self, **kw):
        super().__init__(**kw)
        self.step = 1 / self.p.n

    def on_bar(self):
        pass
'''


def synthetic_bars() -> pd.DataFrame:
    idx = pd.date_range("2024-01-02 09:35", periods=40, freq="5min", tz="America/New_York")
    c = 17000 + np.cumsum(np.tile([4.0, -1.0, 3.0, -2.0], 10))
    df = pd.DataFrame({"open": c - 1, "high": c + 3, "low": c - 3, "close": c, "volume": 100}, index=idx)
    df["session_date"] = pd.Timestamp("2024-01-02")
    return df


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    """A temp working folder with strategy files and DataSpec.load stubbed to synthetic bars (no Parquet store)."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.DataSpec, "load", lambda self: (synthetic_bars(), None))
    monkeypatch.delenv("GUBAN_DEBUG", raising=False)
    (tmp_path / "flip.py").write_text(FLIP)
    (tmp_path / "boom.py").write_text(BOOM)
    (tmp_path / "bad_init.py").write_text(BAD_INIT)
    return tmp_path


def test_help_lists_the_six_commands_in_workflow_order():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    order = [res.output.index(f" {name} ") for name in ("import", "run", "compare", "optimize", "wfo", "propfirm")]
    assert order == sorted(order)
    assert "bars" in res.output and "Exit codes" in res.output
    for name in ("import", "bars", "run", "compare", "optimize", "wfo", "chart", "propfirm"):
        assert runner.invoke(app, [name, "--help"]).exit_code == 0


def test_no_args_shows_help_and_version():
    assert "Commands" in runner.invoke(app, []).output
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and res.output.startswith("guban ")


def test_run_optimize_wfo_share_identical_flags():
    group = typer.main.get_command(app)
    params = {name: {p.name: p for p in group.commands[name].params} for name in ("run", "optimize", "wfo")}
    shared = ["strategy_file", "symbol", "contract", "tf", "session", "history_from", "data_from", "tz", "fill_mode", "data_dir",
              "commission", "slippage", "inputs", "class_name"]
    for name in shared:
        ref = params["run"][name]
        for cmd in ("optimize", "wfo"):
            p = params[cmd][name]
            assert (p.opts, p.default, getattr(p, "help", None), p.type.name) == (ref.opts, ref.default, getattr(ref, "help", None), ref.type.name), (cmd, name)
    for cmd in ("optimize", "wfo"):
        for name in ("grid_specs", "min_trades", "workers", "yes"):
            assert params[cmd][name].opts == params["optimize"][name].opts
        assert params[cmd]["out"].opts == ["--out"] and params[cmd]["open_report"].opts == ["--open"]
    ref, p = params["run"]["trade_charts"], params["wfo"]["trade_charts"]
    assert (p.opts, p.secondary_opts, p.default, p.help) == (ref.opts, ref.secondary_opts, ref.default, ref.help)


def test_run_dir_names():
    f = Path("strategies/vault_break.py")
    assert run_dir(f, "MNQ", None, "5m", "eth") == Path("reports/vault_break_MNQ_5m_eth")
    assert run_dir(f, "MNQ", "mnq", "5m", "eth") == Path("reports/vault_break_MNQ_5m_eth")
    assert run_dir(f, "NQ", "MNQ", "5m", "eth", "_opt") == Path("reports/vault_break_NQ-MNQ_5m_eth_opt")


def test_run_writes_to_the_default_run_dir(workdir):
    res = runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--contract", "MNQ", "--fill-mode", "ohlc"])
    assert res.exit_code == 0, res.output
    out = workdir / "reports" / "flip_NQ-MNQ_5m_rth"
    assert {p.name for p in out.iterdir()} == {"trades.csv", "metrics.json", "report.html", "trades"}
    assert len(list((out / "trades").glob("trade_*.png"))) == json.loads((out / "metrics.json").read_text(encoding="utf-8"))["all"]["total_trades"]


def test_no_trade_charts_then_guban_chart_draws_them(workdir):
    res = runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--fill-mode", "ohlc", "--mc-sims", "0", "--no-trade-charts", "--out", "r"])
    assert res.exit_code == 0, res.output
    out = workdir / "r"
    assert not list(out.glob("trades/trade_*.png")) and "id='noimg'" in (out / "report.html").read_text(encoding="utf-8")
    n = json.loads((out / "metrics.json").read_text(encoding="utf-8"))["all"]["total_trades"]
    res = runner.invoke(app, ["chart", "r", "--trade", "2"])
    assert res.exit_code == 0, res.output
    assert [p.name for p in out.glob("trades/trade_*.png")] == ["trade_2.png"] and "Drew 1 trade chart" in res.output
    res = runner.invoke(app, ["chart", "r"])
    assert res.exit_code == 0, res.output
    assert len(list(out.glob("trades/trade_*.png"))) == n
    res = runner.invoke(app, ["chart", "r", "--trade", "999"])
    assert res.exit_code == 2 and "has no trade #999" in res.output
    res = runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--fill-mode", "ohlc", "--mc-sims", "0", "--no-trade-charts", "--out", "r"])
    assert not list(out.glob("trades/trade_*.png"))


def test_chart_refuses_bars_that_changed(workdir, monkeypatch):
    assert runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--fill-mode", "ohlc", "--mc-sims", "0", "--no-report", "--out", "r"]).exit_code == 0
    longer = pd.concat([synthetic_bars(), synthetic_bars().shift(40, freq="5min")])
    monkeypatch.setattr(cli.DataSpec, "load", lambda self: (longer, None))
    assert runner.invoke(app, ["chart", "r", "--trade", "1"]).exit_code == 0
    monkeypatch.setattr(cli.DataSpec, "load", lambda self: (synthetic_bars().iloc[3:], None))
    res = runner.invoke(app, ["chart", "r", "--trade", "1"])
    assert res.exit_code == 2 and "the bars changed since" in res.output


def test_compare_out_is_a_folder(workdir):
    assert runner.invoke(app, ["run", "flip.py", "--symbol", "MNQ", "--fill-mode", "ohlc", "--no-report", "--out", "a"]).exit_code == 0
    res = runner.invoke(app, ["compare", "--ours", "a/trades.csv", "--mc", "a/trades.csv", "--contract", "MNQ", "--out", "cmp"])
    assert res.exit_code in (0, 1), res.output
    assert (workdir / "cmp" / "diff.html").exists()


def test_strategy_exception_prints_its_line_and_writes_nothing(workdir):
    res = runner.invoke(app, ["run", "boom.py", "--symbol", "NQ", "--fill-mode", "ohlc"])
    assert res.exit_code == 2
    assert isinstance(res.exception, SystemExit)
    assert "Error: Boom.on_bar failed at bar 5 (" in res.output and "ZeroDivisionError" in res.output
    assert "boom.py\", line 9, in on_bar" in res.output and "return 1 / 0" in res.output
    assert not (workdir / "reports").exists()


def test_strategy_frames_from_a_worker_process(tmp_path):
    """A worker's StrategyError reaches the parent as RuntimeError <- _RemoteTraceback (text only); its strategy frames are kept."""
    from concurrent.futures.process import _RemoteTraceback

    lib = Path(cli.__file__).parent / "engine" / "backtest.py"
    user = tmp_path / "boom.py"
    text = (
        "Traceback (most recent call last):\n"
        f'  File "{lib}", line 442, in run_backtest\n    strategy.on_bar()\n'
        f'  File "{user}", line 9, in on_bar\n    return 1 / 0\n'
        "ZeroDivisionError: division by zero\n\nThe above exception was the direct cause of the following exception:\n\n"
        "Traceback (most recent call last):\n"
        f'  File "{lib}", line 444, in run_backtest\n    raise StrategyError(msg) from e\n'
        "guban.engine.backtest.StrategyError: Boom.on_bar failed\n"
    )
    err = RuntimeError("run with inputs {'at_bar': 5} failed: Boom.on_bar failed")
    err.__cause__ = RuntimeError("Boom.on_bar failed")
    err.__cause__.__cause__ = _RemoteTraceback(text)
    assert cli._strategy_frames(err) == [f'  File "{user}", line 9, in on_bar\n    return 1 / 0\n']
    assert cli._strategy_frames(RuntimeError("unrelated")) == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["run", "flip.py", "--symbol", "XYZ"], "Error: Unknown symbol 'XYZ'"),
        (["run", "flip.py", "--symbol", "NQ", "--tz", "Mars/Base"], "unknown time zone 'Mars/Base'"),
        (["run", "flip.py", "--symbol", "NQ", "--fill-mode", "exact"], "'exact' is not one of"),
        (["run", "flip.py", "--symbol", "NQ", "--set", "nope=1"], "use name=value with one of ['hold']"),
        (["run", "flip.py", "--symbol", "NQ", "--set", "hold=abc"], "'abc' is not a number"),
        (["run", "missing.py", "--symbol", "NQ"], "does not exist"),
        (["optimize", "flip.py", "--grid", "hold=1:3", "--symbol", "NQ", "--metric", "luck"], "--metric"),
        (["optimize", "flip.py", "--grid", "hold=1:6000", "--symbol", "NQ"], "Narrow it, or rerun with --yes"),
        (["import", "--source", "no_such_folder", "--symbol", "NQ"], "does not exist"),
        (["import", "--symbol", "NQ", "--roll", "monthly"], "'monthly' is not one of"),
        (["import", "--symbol", "ES", "--root", "."], "guban store build --symbol ES"),
        (["compare", "--ours", "flip.py", "--mc", "flip.py", "--contract", "MNQ"], "flip.py is not a guban trades.csv"),
        (["compare", "--ours", "flip.py", "--mc", "flip.py", "--contract", "MNQ", "--mc-tz", "Nowhere"], "unknown time zone"),
        (["run", "flip.py", "--symbol", "NQ", "--mc-sims", "-1"], "--mc-sims must be a whole number"),
        (["run", "flip.py", "--symbol", "NQ", "--mc-sims", "2000000"], "--mc-sims must be a whole number"),
        (["run", "flip.py", "--symbol", "NQ", "--mc-dd-limit", "0"], "--mc-dd-limit must be a drawdown"),
        (["run", "flip.py", "--symbol", "NQ", "--mc-risk", "-5"], "--mc-risk must be the risk per contract"),
        (["run", "flip.py", "--symbol", "NQ", "--mc-oos-from", "soon"], "'soon' is not a date"),
        (["chart", "."], "is not a run folder"),
    ],
)
def test_input_errors_are_one_line_messages_with_exit_2(workdir, args, message):
    res = runner.invoke(app, args, terminal_width=200)
    assert res.exit_code == 2, res.output
    assert isinstance(res.exception, SystemExit)
    assert message in res.output
    assert "Traceback" not in res.output


def test_missing_data_names_the_import_step(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GUBAN_DEBUG", raising=False)
    (tmp_path / "flip.py").write_text(FLIP)
    res = runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--data-dir", "empty"])
    assert res.exit_code == 2
    assert "not found; run `guban import` first" in res.output


def test_redirected_output_is_utf8():
    """Windows pipes/files default to cp1252, which cannot encode the − in the optimize/wfo summaries."""
    import os
    import subprocess
    import sys

    code = "from guban.cli import _utf8_streams; _utf8_streams(); print('net −1,234.00  IS 2024-01-01→2024-12-31')"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, env={**os.environ, "PYTHONIOENCODING": "cp1252"}, check=True)
    assert out.stdout.decode("utf-8").strip() == "net −1,234.00  IS 2024-01-01→2024-12-31"


def test_guban_debug_reraises(workdir, monkeypatch):
    monkeypatch.setenv("GUBAN_DEBUG", "1")
    res = runner.invoke(app, ["run", "flip.py", "--symbol", "XYZ"])
    assert isinstance(res.exception, KeyError)


def test_strategy_constructor_error_is_a_friendly_strategy_error(workdir):
    res = runner.invoke(app, ["run", "bad_init.py", "--symbol", "NQ", "--fill-mode", "ohlc"])
    assert res.exit_code == 2 and isinstance(res.exception, SystemExit), res.output
    assert "Error: BadInit() could not be created: ZeroDivisionError" in res.output
    assert "bad_init.py\", line 9, in __init__" in res.output
    assert not (workdir / "reports").exists()


def _run_json(workdir, *extra, out="mc"):
    import json

    res = runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--fill-mode", "ohlc", "--out", out, *extra])
    assert res.exit_code == 0, res.output
    return res, json.loads((workdir / out / "metrics.json").read_text(encoding="utf-8")), (workdir / out / "report.html").read_text(encoding="utf-8")


def test_run_has_a_trade_monte_carlo_by_default(workdir):
    res, m, page = _run_json(workdir, "--mc-dd-limit", "20")
    mc = m["monte_carlo"]
    assert mc["sims"] == 10_000 and mc["seed"] == 42 and mc["dd_basis"] == "close_to_close" and mc["dd_limit"] == 20
    assert mc["trades"] == m["all"]["total_trades"] >= 2
    assert mc["backtest"]["max_drawdown"] == m["account"]["max_drawdown_close_to_close"]
    assert mc["reorder"]["net_profit"]["p5"] == mc["reorder"]["net_profit"]["p95"] == m["all"]["net_profit"]
    assert "p_dd_reaches_limit" in mc["resample"]
    assert "monte carlo (10,000 runs" in res.output and "P(max DD reaches 20.00)" in res.output
    assert "<section id='monte-carlo'>" in page and "id='mc-band-resample'" in page and "id='mc-band-reorder'" in page
    assert {"day", "ev", "horizons"} <= set(mc) or "day" in mc.get("skipped", {})
    assert mc["ev"]["groups"]["all"]["trades"] == mc["trades"]


def test_run_has_an_evaluation(workdir):
    res, m, page = _run_json(workdir)
    ev = m["evaluation"]
    assert [c["key"] for c in ev["criteria"]] == ["sharpe", "profit_factor", "max_dd_r", "sortino"]
    assert ev["as_run"]["sharpe"] == m["account"]["sharpe_daily"] and ev["as_run"]["sortino"] == m["account"]["sortino_daily"]
    assert sum(g["trades"] for g in ev["by_year"]) == m["all"]["total_trades"]
    assert "  evaluation: as run " in res.output
    assert "<section id='evaluation'>" in page


def test_run_passes_the_ev_flags_to_the_monte_carlo(workdir):
    res, m, _ = _run_json(workdir, "--mc-risk", "50", "--mc-oos-from", "2024-01-02 11:00")
    ev = m["monte_carlo"]["ev"]
    assert (ev["risk_per_contract"], ev["risk_source"], ev["oos_from"]) == (50.0, "--mc-risk", "2024-01-02 11:00")
    groups = ev["groups"]
    assert groups["in_sample"]["trades"] + groups["out_of_sample"]["trades"] == groups["all"]["trades"]
    assert "R (--mc-risk, avg 50.00 per contract)" in res.output and "out of sample:" in res.output


def test_run_records_each_trades_risk_and_the_monte_carlo_uses_it(workdir):
    stops = "    def on_bar(self):\n        self.set_stop_loss(200)\n        self.set_profit_target(400)\n"
    (workdir / "flip_sl.py").write_text(FLIP.replace("class Flip", "class FlipSL").replace("    def on_bar(self):\n", stops))
    res = runner.invoke(app, ["run", "flip_sl.py", "--symbol", "NQ", "--fill-mode", "ohlc", "--no-trade-charts", "--out", "sl"])
    assert res.exit_code == 0, res.output
    tr = pd.read_csv(workdir / "sl" / "trades.csv")
    assert (tr["risk_usd"] == 200).all() and (tr["planned_rr"] == 2).all()
    assert np.allclose(tr["r_multiple"], tr["profit_usd"] / 200)
    m = json.loads((workdir / "sl" / "metrics.json").read_text(encoding="utf-8"))
    assert m["all"]["avg_planned_rr"] == 2 and m["all"]["trades_with_stop"] == len(tr)
    assert m["monte_carlo"]["ev"]["risk_source"] == "per-trade stop" and "R (per-trade stop" in res.output
    page = (workdir / "sl" / "report.html").read_text(encoding="utf-8")
    assert "'R', 'RR'" in page and "Avg planned reward : risk" in page


def test_run_monte_carlo_is_deterministic_and_seeded(workdir):
    a = _run_json(workdir, out="a")[1]["monte_carlo"]
    b = _run_json(workdir, out="b")[1]["monte_carlo"]
    c = _run_json(workdir, "--mc-seed", "7", out="c")[1]["monte_carlo"]
    assert a == b and a != c


def test_run_monte_carlo_off_and_skipped(workdir):
    res, m, page = _run_json(workdir, "--mc-sims", "0", out="off")
    assert "monte_carlo" not in m and "<section id='monte-carlo'>" not in page and "monte carlo" not in res.output
    res, m, page = _run_json(workdir, "--set", "hold=100", out="skip")
    assert m["monte_carlo"] == {"skipped": "needs at least 2 trades; this run has 0"}
    assert "Monte Carlo skipped: needs at least 2 trades" in page and "monte carlo: skipped" in res.output


def test_run_uses_the_contracts_default_costs_unless_flags_are_given(workdir):
    m = _run_json(workdir, "--mc-sims", "0", out="nq")[1]["run"]
    assert (m["commission"], m["slippage"], m["costs"]) == (2.50, 5.00, "contract default (config/contracts.toml)")
    m = _run_json(workdir, "--mc-sims", "0", "--contract", "MNQ", out="mnq")[1]["run"]
    assert (m["commission"], m["slippage"]) == (2.00, 0.50)
    m = _run_json(workdir, "--mc-sims", "0", "--commission", "0.4", "--slippage", "0", out="flags")[1]["run"]
    assert (m["commission"], m["slippage"], m["costs"]) == (0.4, 0.0, "flags")
    m = _run_json(workdir, "--mc-sims", "0", "--commission", "0", out="one")[1]["run"]
    assert (m["commission"], m["slippage"], m["costs"]) == (0.0, 5.00, "--commission; slippage: contract default")


def _one_minute_store(tmp_path):
    from guban.data.parquet import write_parquet

    idx = pd.date_range("2024-01-02 09:31", "2024-01-05 16:00", freq="1min", tz="America/New_York")
    idx = idx[(idx.time > pd.Timestamp("09:30").time()) & (idx.time <= pd.Timestamp("16:00").time())]
    df = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1, "bid_vol": 0, "ask_vol": 1, "contract": "202403"}, index=idx)
    write_parquet(df, tmp_path / "NQ" / "1m.parquet")
    return tmp_path


def test_data_from_drops_bars_and_sub_bars_before_the_date(tmp_path):
    from guban.optimize import DataSpec

    d = _one_minute_store(tmp_path)
    bars, sub = DataSpec("NQ", "5m", "rth", data_dir=str(d), data_from="2024-01-04").load()
    assert bars.index[0] == pd.Timestamp("2024-01-04 09:35", tz="America/New_York")
    assert sub.index[0] == pd.Timestamp("2024-01-04 09:31", tz="America/New_York")
    full, _ = DataSpec("NQ", "5m", "rth", data_dir=str(d)).load()
    assert len(full) == 4 * 78 and len(bars) == 2 * 78
    with pytest.raises(ValueError, match="--data-from 2025-01-01 is after the last bar"):
        DataSpec("NQ", "5m", "rth", data_dir=str(d), data_from="2025-01-01").load()
