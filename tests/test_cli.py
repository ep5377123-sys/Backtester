"""CLI polish (Phase 10): command list, shared flags, default run folders and error messages."""

from pathlib import Path

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
    return tmp_path


def test_help_lists_the_six_commands_in_workflow_order():
    res = runner.invoke(app, ["--help"])
    assert res.exit_code == 0
    order = [res.output.index(f" {name} ") for name in ("import", "run", "compare", "optimize", "wfo", "propfirm")]
    assert order == sorted(order)
    assert "bars" in res.output and "Exit codes" in res.output
    for name in ("import", "bars", "run", "compare", "optimize", "wfo", "propfirm"):
        assert runner.invoke(app, [name, "--help"]).exit_code == 0


def test_no_args_shows_help_and_version():
    assert "Commands" in runner.invoke(app, []).output
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0 and res.output.startswith("guban ")


def test_run_optimize_wfo_share_identical_flags():
    group = typer.main.get_command(app)
    params = {name: {p.name: p for p in group.commands[name].params} for name in ("run", "optimize", "wfo")}
    shared = ["strategy_file", "symbol", "contract", "tf", "session", "history_from", "tz", "fill_mode", "data_dir",
              "commission", "slippage", "inputs", "class_name"]  # fmt: skip
    for name in shared:
        ref = params["run"][name]
        for cmd in ("optimize", "wfo"):
            p = params[cmd][name]
            assert (p.opts, p.default, getattr(p, "help", None), p.type.name) == (ref.opts, ref.default, getattr(ref, "help", None), ref.type.name), (cmd, name)
    for cmd in ("optimize", "wfo"):  # search flags, and --out means a folder everywhere
        for name in ("grid_specs", "min_trades", "workers", "yes"):
            assert params[cmd][name].opts == params["optimize"][name].opts
        assert params[cmd]["out"].opts == ["--out"] and params[cmd]["open_report"].opts == ["--open"]


def test_run_dir_names():
    f = Path("strategies/vault_break.py")
    assert run_dir(f, "MNQ", None, "5m", "eth") == Path("reports/vault_break_MNQ_5m_eth")
    assert run_dir(f, "MNQ", "mnq", "5m", "eth") == Path("reports/vault_break_MNQ_5m_eth")
    assert run_dir(f, "NQ", "MNQ", "5m", "eth", "_opt") == Path("reports/vault_break_NQ-MNQ_5m_eth_opt")


def test_run_writes_to_the_default_run_dir(workdir):
    res = runner.invoke(app, ["run", "flip.py", "--symbol", "NQ", "--contract", "MNQ", "--fill-mode", "ohlc"])
    assert res.exit_code == 0, res.output
    out = workdir / "reports" / "flip_NQ-MNQ_5m_rth"
    assert {p.name for p in out.iterdir()} == {"trades.csv", "metrics.json", "report.html"}


def test_compare_out_is_a_folder(workdir):
    assert runner.invoke(app, ["run", "flip.py", "--symbol", "MNQ", "--fill-mode", "ohlc", "--no-report", "--out", "a"]).exit_code == 0
    res = runner.invoke(app, ["compare", "--ours", "a/trades.csv", "--mc", "a/trades.csv", "--contract", "MNQ", "--out", "cmp"])
    assert res.exit_code in (0, 1), res.output  # 1 = parity FAIL, still a completed comparison
    assert (workdir / "cmp" / "diff.html").exists()


def test_strategy_exception_prints_its_line_and_writes_nothing(workdir):
    res = runner.invoke(app, ["run", "boom.py", "--symbol", "NQ", "--fill-mode", "ohlc"])
    assert res.exit_code == 2
    assert isinstance(res.exception, SystemExit)  # no traceback
    assert "Error: Boom.on_bar failed at bar 5 (" in res.output and "ZeroDivisionError" in res.output  # = current_bar
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
        (["run", "missing.py", "--symbol", "NQ"], "does not exist"),
        (["optimize", "flip.py", "--grid", "hold=1:3", "--symbol", "NQ", "--metric", "luck"], "--metric"),
        (["optimize", "flip.py", "--grid", "hold=1:6000", "--symbol", "NQ"], "Narrow it, or rerun with --yes"),
        (["import", "no_such_folder", "--symbol", "NQ"], "does not exist"),
        (["import", ".", "--symbol", "NQ", "--roll", "monthly"], "'monthly' is not one of"),
        (["compare", "--ours", "flip.py", "--mc", "flip.py", "--contract", "MNQ"], "flip.py is not a guban trades.csv"),
        (["compare", "--ours", "flip.py", "--mc", "flip.py", "--contract", "MNQ", "--mc-tz", "Nowhere"], "unknown time zone"),
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
