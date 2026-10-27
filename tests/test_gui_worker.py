"""Guban Studio worker protocol (Phase 19): JSON lines in, one reply per request, progress events, errors."""

import io
import json

import pytest

from guban.engine import StrategyError
from guban.gui import handlers
from guban.gui.worker import serve


def _serve(*requests) -> list[dict]:
    inp = io.StringIO("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in requests))
    out = io.StringIO()
    serve(inp, out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


@pytest.fixture
def fake_methods(monkeypatch):
    def progress(state, params, emit):
        for k in range(params["n"]):
            emit("progress", done=k + 1, total=params["n"])
        return {"n": params["n"], "nan": float("nan")}

    def bad_input(state, params, emit):
        raise ValueError("--tf 7x is not a timeframe")

    def strategy_bug(state, params, emit):
        try:
            1 / 0
        except ZeroDivisionError as e:
            raise StrategyError("MyStrategy.on_bar raised ZeroDivisionError") from e

    def bug(state, params, emit):
        return {}["missing"] if params.get("key") else None.attr

    for name, fn in {"t.progress": progress, "t.bad": bad_input, "t.strategy": strategy_bug, "t.bug": bug}.items():
        monkeypatch.setitem(handlers.METHODS, name, fn)


def test_ping_replies_with_versions():
    (reply,) = _serve({"id": 1, "method": "ping"})
    assert reply["id"] == 1 and reply["ok"] is True
    assert set(reply["result"]) >= {"guban", "python", "pid"}


def test_events_come_before_the_reply_and_results_are_json_safe(fake_methods):
    lines = _serve({"id": 5, "method": "t.progress", "params": {"n": 3}})
    assert [m.get("event") for m in lines] == ["progress", "progress", "progress", None]
    assert [m["done"] for m in lines[:3]] == [1, 2, 3] and all(m["id"] == 5 for m in lines)
    assert lines[-1] == {"id": 5, "ok": True, "result": {"n": 3, "nan": None}}


def test_expected_errors_reply_with_the_cli_message(fake_methods):
    (reply,) = _serve({"id": 2, "method": "t.bad"})
    assert reply["ok"] is False and reply["error"]["message"] == "--tf 7x is not a timeframe"
    assert reply["error"]["frames"] == []


def test_strategy_errors_carry_the_strategy_frames(fake_methods):
    (reply,) = _serve({"id": 3, "method": "t.strategy"})
    assert reply["error"]["message"].startswith("MyStrategy.on_bar raised")
    assert any("test_gui_worker.py" in f for f in reply["error"]["frames"])


def test_bugs_and_bad_lines_do_not_stop_the_worker(fake_methods):
    lines = _serve("not json", {"id": 4, "method": "t.bug"}, {"id": 5, "method": "nope"}, {"id": 6, "method": "ping"})
    assert lines[0]["id"] is None and lines[0]["error"]["message"].startswith("Bad request line")
    assert lines[1]["error"]["message"].startswith("Internal error: AttributeError") and "Traceback" in lines[1]["error"]["traceback"]
    assert lines[2]["error"]["message"].startswith("unknown method 'nope'")
    assert lines[3]["ok"] is True


def test_debug_chart_is_a_png_data_uri():
    (reply,) = _serve({"id": 1, "method": "debug.chart", "params": {"points": 50, "width": 3, "height": 2, "dpi": 50}})
    assert reply["result"]["image"].startswith("data:image/png;base64,")


# --- Data tab (guban.gui.tab_data) -------------------------------------------------------------------------------


def _bars(days: int = 10, per_day: int = 4) -> "pd.DataFrame":
    import numpy as np
    import pandas as pd

    idx = pd.DatetimeIndex([pd.Timestamp("2024-01-01 10:00") + pd.Timedelta(days=d, minutes=15 * k) for d in range(days) for k in range(per_day)]).tz_localize("America/New_York")
    close = 100 + np.arange(len(idx), dtype=float)
    return pd.DataFrame({"open": close - 0.5, "high": close + 1, "low": close - 1, "close": close, "volume": 10,
                         "contract": "202403", "session_date": idx.tz_localize(None).normalize(),
                         "session_close": idx.normalize() + pd.Timedelta(hours=11)}, index=idx)  # fmt: skip


def _loaded(**kw):
    from guban.gui.tab_data import Loaded

    return Loaded("NQ", "15m", "rth", "America/New_York", _bars(**kw))


def test_fraction_split_puts_the_last_days_out_of_sample():
    from datetime import date

    from guban.gui.tab_data import split_by_fraction

    w = split_by_fraction(date(2024, 1, 1), date(2024, 1, 10), 0.3)
    assert w == {"is": (date(2024, 1, 1), date(2024, 1, 7)), "oos": (date(2024, 1, 8), date(2024, 1, 10))}
    for bad in (0, 1, 1.5, "0.3", True):
        with pytest.raises(ValueError, match="OOS size"):
            split_by_fraction(date(2024, 1, 1), date(2024, 1, 10), bad)


def test_window_bounds_include_both_end_dates():
    from datetime import date

    from guban.gui.tab_data import window_bounds

    d = _loaded()
    assert window_bounds(d.bars.index, date(2024, 1, 2), date(2024, 1, 3)) == (4, 12)
    assert window_bounds(d.bars.index, date(2023, 1, 1), date(2024, 1, 1)) == (0, 4)


def test_windows_must_fit_hold_bars_and_not_overlap():
    from datetime import date

    from guban.gui.tab_data import check_windows

    d = _loaded()
    j = lambda a, b: (date(2024, 1, a), date(2024, 1, b))  # noqa: E731
    check_windows(d, {"is": j(6, 10), "oos": j(1, 5)})  # out of sample first is fine
    with pytest.raises(ValueError, match="overlap"):
        check_windows(d, {"is": j(1, 6), "oos": j(6, 10)})
    with pytest.raises(ValueError, match="outside the loaded bars"):
        check_windows(d, {"is": j(1, 5), "oos": (date(2024, 1, 6), date(2024, 2, 1))})


def test_quality_report_flags_bad_bars():
    from guban.gui.tab_data import quality_report

    d = _loaded()
    d.bars.iloc[3, d.bars.columns.get_loc("high")] = 0.0  # high under open/close, and a non-positive price
    items = {q["label"]: q for q in quality_report(d)}
    assert items["OHLC integrity"]["status"] == "bad" and items["Non-positive prices"]["status"] == "bad"
    assert items["NaN values"]["status"] == "ok" and items["Duplicate timestamps"]["status"] == "ok"


def test_data_methods_need_loaded_data_and_page_through_bars():
    from guban.gui.handlers import State
    from guban.gui.tab_data import page

    (reply,) = _serve({"id": 1, "method": "data.page"})
    assert reply["error"]["message"].startswith("No data loaded")
    p = page(_loaded(), offset=38, limit=5)
    assert p["offset"] == 38 and len(p["rows"]) == 2 and p["total"] == 40
    assert p["rows"][0][0] == "2024-01-10 10:30" and p["columns"][0] == "time"
    assert State().data is None


# --- Strategy and Backtest tabs (guban.gui.tab_strategy, tab_backtest) --------------------------------------------

from guban.engine import Strategy  # noqa: E402


class Flip(Strategy):
    """Long every few bars, flat the next time: plenty of small trades."""

    inputs = dict(every=3)
    plots = ("mark",)

    def on_start(self):
        self.mark = self.series(0.0)

    def on_bar(self):
        self.mark.value = self.close[0]
        if self.current_bar % self.p.every == 0:
            self.buy() if self.market_position == 0 else self.sell()


def _studio(dataset: str):
    from datetime import date

    from guban.gui.handlers import State

    state = State()
    d = _loaded(days=40, per_day=8)
    d.windows = {"is": (date(2024, 1, 11), date(2024, 1, 30)), "oos": (date(2024, 1, 31), date(2024, 2, 9))}
    d.dataset = dataset
    state.data, state.strategy = d, Flip
    return state, d


def test_apply_runs_the_dataset_window_like_the_engine():
    from guban.config import get_contract
    from guban.engine import Costs, run_backtest
    from guban.gui.handlers import METHODS

    spec = get_contract("NQ")
    costs = Costs(commission=spec.commission, slippage=spec.slippage)
    for dataset in ("full", "is"):
        state, d = _studio(dataset)
        r = METHODS["strategy.apply"](state, {"inputs": {"every": "4"}, "fill_mode": "ohlc", "bars": 50}, lambda *a, **k: None)
        lo, hi = d.bounds()
        flat = dataset != "full"
        ref = run_backtest(Flip(every=4), d.bars.iloc[:hi], spec, costs, trade_from=lo, flatten_at_end=flat)
        got = state.applied.result.trades
        assert len(got) == len(ref.trades) > 10
        assert got["net_pnl"].round(6).tolist() == ref.trades["net_pnl"].round(6).tolist()
        assert (got["entry_bar"] >= lo).all() and (got["exit_bar"] < hi).all()
        assert r["chart"]["image"].startswith("data:image/png") and r["chart"]["count"] == 50
        assert state.applied.plots["mark"][lo:hi] == d.bars["close"].iloc[lo:hi].tolist()


def test_backtest_run_matches_the_cli_metrics_and_draws_everything():
    from guban.gui.handlers import METHODS
    from guban.metrics import backtest_metrics, window_metrics

    emit = lambda *a, **k: None  # noqa: E731
    state, d = _studio("is")
    METHODS["strategy.apply"](state, {"inputs": {}, "fill_mode": "ohlc"}, emit)
    r = METHODS["backtest.run"](state, {"fill_mode": "ohlc", "mc_sims": "200", "commission": "1", "slippage": "0"}, emit)
    b = state.backtest
    lo, hi = d.bounds()
    from guban.engine import Costs, run_backtest

    raw = run_backtest(Flip(), d.bars.iloc[:hi], b.a.spec, Costs(commission=1, slippage=0), trade_from=lo, window_ends=(hi,))
    assert b.metrics == window_metrics(raw, d.bars.iloc[:hi], lo, hi)
    assert b.metrics == backtest_metrics(b.a.result, d.bars.iloc[lo:hi])
    assert all(r["charts"][k] for k in ("monthly", "equity", "drawdown", "daily_pnl", "daily_dd"))
    assert "monte_carlo" not in r["html"] and "id='evaluation'" in r["html"]["evaluation"]  # Monte Carlo: its own tab
    assert b.mc_settings == {"sims": 200, "seed": 42, "dd_limit": None, "risk": None, "oos_from": None}
    with pytest.raises(ValueError, match="commission must be at least 0"):
        METHODS["backtest.run"](state, {"commission": "-1"}, emit)


def test_new_data_clears_strategy_and_backtest_results():
    from guban.gui.handlers import METHODS

    emit = lambda *a, **k: None  # noqa: E731
    state, d = _studio("is")
    METHODS["strategy.apply"](state, {"inputs": {}, "fill_mode": "ohlc"}, emit)
    METHODS["backtest.run"](state, {"fill_mode": "ohlc", "mc_sims": "0"}, emit)
    METHODS["data.windows"](state, {"is": ["2024-01-11", "2024-01-30"], "oos": ["2024-01-31", "2024-02-09"], "dataset": "oos"}, emit)
    assert state.applied is None and state.backtest is None
    with pytest.raises(ValueError, match="No strategy applied"):
        METHODS["backtest.run"](state, {}, emit)


def test_optimize_count_parses_the_grid_like_the_cli():
    from guban.gui.handlers import METHODS, State

    emit = lambda *a, **k: None  # noqa: E731
    state = State()
    with pytest.raises(ValueError, match="No strategy loaded"):
        METHODS["optimize.count"](state, {"grid": {"every": "2:4"}}, emit)
    state.strategy = Flip
    assert METHODS["optimize.count"](state, {"grid": {"every": "2:6:2"}}, emit) == {"count": 3, "values": {"every": ["2", "4", "6"]}}
    with pytest.raises(ValueError, match="Tick at least one input"):
        METHODS["optimize.count"](state, {"grid": {"every": " "}}, emit)
    with pytest.raises(ValueError, match="0.5"):
        METHODS["optimize.count"](state, {"grid": {"every": "0.5,1"}}, emit)  # an int input rejects 0.5
    opts = METHODS["optimize.options"](state, {}, emit)
    assert {"key": "net_profit", "label": "Net profit"} in opts["metrics"] and opts["max_combinations"] == 5000


# --- Monte Carlo tab (guban.gui.tab_montecarlo) ------------------------------------------------------------------


def test_monte_carlo_tab_equals_the_backtest_save_and_reads_trade_files(tmp_path):
    from guban.gui.handlers import METHODS

    emit = lambda *a, **k: None  # noqa: E731
    state, d = _studio("full")
    with pytest.raises(ValueError, match="No backtest to simulate"):
        METHODS["mc.trade_run"](state, {"source": "backtest", "mc_sims": "300"}, emit)
    METHODS["strategy.apply"](state, {"inputs": {}, "fill_mode": "ohlc"}, emit)
    settings = {"mc_sims": "300", "mc_seed": "7", "mc_dd_limit": "50", "mc_oos_from": "2024-01-25"}
    METHODS["backtest.run"](state, {"fill_mode": "ohlc", **settings}, emit)
    METHODS["backtest.save"](state, {"out": str(tmp_path / "run")}, emit)
    saved = json.loads((tmp_path / "run" / "metrics.json").read_text(encoding="utf-8"))
    r = METHODS["mc.trade_run"](state, {"source": "backtest", **settings}, emit)
    tab = json.loads(json.dumps(r["summary"], default=str))
    assert tab == json.loads(json.dumps(saved["monte_carlo"], default=str))  # R7: the saved run's Monte Carlo, exactly
    assert "id='monte-carlo'" in r["html"] and r["source"].startswith("Backtest: Flip")
    f = METHODS["mc.trade_run"](state, {"source": "file", "file": str(tmp_path / "run" / "trades.csv"), **settings}, emit)
    assert f["summary"]["trades"] == tab["trades"]  # same trades; the days come from exit dates here, not sessions
    assert f["summary"]["resample"] == tab["resample"] and f["summary"]["reorder"] == tab["reorder"]  # neither uses days
    with pytest.raises(FileNotFoundError, match="trades file"):
        METHODS["mc.trade_run"](state, {"source": "file", "file": str(tmp_path / "nope.csv"), **settings}, emit)
    with pytest.raises(ValueError, match="No walk-forward"):
        METHODS["mc.trade_run"](state, {"source": "walkforward", **settings}, emit)
    srcs = {x["key"]: x["available"] for x in METHODS["mc.sources"](state, {}, emit)["sources"]}
    assert srcs == {"backtest": True, "walkforward": False, "file": True}


# --- Walk Forward tab (guban.gui.tab_walkforward) ----------------------------------------------------------------

WF_STRATEGY = '''from guban.engine import Strategy


class Swing(Strategy):
    """Long after `up` rising closes, out after `hold` bars."""

    inputs = dict(up=2, hold=3)

    def on_bar(self):
        if self.market_position == 0 and self.current_bar > self.p.up and all(self.close[k] > self.close[k + 1] for k in range(self.p.up)):
            self.buy("L", 1)
        elif self.market_position != 0 and self.bars_since_entry >= self.p.hold:
            self.sell("X")
'''


def _wf_store(tmp_path):
    """Five months of RTH 1-minute NQ bars: a random walk, so the inputs' rankings differ from month to month."""
    import numpy as np
    import pandas as pd

    from guban.data.parquet import write_parquet

    days = pd.bdate_range("2024-01-02", "2024-05-31")
    idx = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=31 + k) for d in days for k in range(390)]).tz_localize("America/New_York")
    c = 17000 + np.cumsum(np.random.default_rng(5).normal(0, 2.0, len(idx))).round(2)
    df = pd.DataFrame({"open": c, "high": c + 1.5, "low": c - 1.5, "close": c, "volume": 1, "bid_vol": 0, "ask_vol": 1, "contract": "202406"},
                      index=idx)  # fmt: skip
    write_parquet(df, tmp_path / "data" / "NQ" / "1m.parquet")
    (tmp_path / "swing.py").write_text(WF_STRATEGY)


def test_walk_forward_tab_equals_guban_wfo(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from guban.cli import app
    from guban.gui import tab_data
    from guban.gui.handlers import METHODS, State

    emit = lambda *a, **k: None  # noqa: E731
    monkeypatch.chdir(tmp_path)
    _wf_store(tmp_path)
    monkeypatch.setattr(tab_data, "DATA_DIR", tmp_path / "data")
    form = {"symbol": "NQ", "tf": "30m", "session": "rth", "tz": "America/New_York", "start": "2024-01-02", "end": "2024-05-31"}
    main, opt = State(), State()
    main.data = tab_data.load_data(form, emit)
    lay = {"is_months": "1", "oos_months": "1", "step_months": "1", "folds": "3"}
    pre = METHODS["wf.windows"](main, lay, emit)
    assert pre["folds"] == 3 and pre["start"] == "2024-02-01"  # end-aligned: the last fold ends with the data (May 31)
    assert pre["rows"][-1] == ["W3", "2024-04-01 → 2024-04-30", "2024-05-01 → 2024-05-31"]
    with pytest.raises(ValueError, match="not enough data: 5 folds"):
        METHODS["wf.windows"](main, {**lay, "folds": "5"}, emit)
    with pytest.raises(ValueError, match="not enough data"):
        METHODS["wf.windows"](main, {**lay, "start": "2024-02-02"}, emit)  # the last fold would run past the data

    grid = {"up": "1,2,3", "hold": "2,6"}
    r = METHODS["wf.run"](opt, {"data": form, "strategy": {"file": "swing.py"}, "grid": grid, **lay, "min_trades": "1", "workers": "1",
                                "fill_mode": "ohlc"}, emit)  # fmt: skip
    assert r["start"] == "2024-02-01" and len(r["windows"]) == 3 and r["trades"] > 0
    assert all(r["charts"][k] for k in ("equity", "drawdown", "folds")) and "Walk-forward" in r["html"]["section"]
    assert set(r["last_inputs"]) == {"up", "hold"}
    gui = METHODS["wf.save"](opt, {"out": "gui"}, emit)
    assert len(gui["files"]) == 5

    res = CliRunner().invoke(app, ["wfo", "swing.py", "--grid", "up=1,2,3", "--grid", "hold=2,6", "--symbol", "NQ", "--tf", "30m", "--session", "rth",
                                   "--tz", "America/New_York", "--data-dir", "data", "--data-from", "2024-01-02", "--fill-mode", "ohlc",
                                   "--is", "1", "--oos", "1", "--step", "1", "--folds", "3", "--start", "2024-02-01", "--min-trades", "1",
                                   "--workers", "1", "--no-trade-charts", "--out", "cli"])  # fmt: skip
    assert res.exit_code == 0, res.output
    for name in ("trades.csv", "windows.csv", "results.csv"):
        assert (tmp_path / "gui" / name).read_bytes() == (tmp_path / "cli" / name).read_bytes(), name  # R5
    a, b = (json.loads((tmp_path / d / "wfo.json").read_text(encoding="utf-8")) for d in ("gui", "cli"))
    assert a.pop("written_by") == "Guban Studio"
    for j in (a, b):
        j.pop("seconds"), j.pop("workers")
    assert a == b

    METHODS["wf.register"](main, {"strategy": r["strategy"], "tz": r["tz"]}, emit)
    m = METHODS["mc.trade_run"](main, {"source": "walkforward", "mc_sims": "200"}, emit)
    assert m["summary"]["trades"] == r["trades"] and m["source"].startswith("Walk-forward: Swing")


def test_propfirm_tab_equals_guban_propfirm(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from guban.cli import app
    from guban.gui.handlers import METHODS

    emit = lambda *a, **k: None  # noqa: E731
    state, d = _studio("full")
    rules = METHODS["mc.propfirm_rules"](state, {}, emit)
    assert rules["selected"] == "apex_50k_eod" and rules["notes"]
    fields = {x["key"]: x["value"] for x in rules["fields"]}
    assert fields["profit_target"] == "1250" and fields["eod_trailing"] == "true" and fields["sweep"].startswith("0.3, 0.5")
    METHODS["strategy.apply"](state, {"inputs": {}, "fill_mode": "ohlc"}, emit)
    METHODS["backtest.run"](state, {"fill_mode": "ohlc", "mc_sims": "0"}, emit)
    METHODS["backtest.save"](state, {"out": str(tmp_path / "run")}, emit)
    small = {**fields, "sims": "2000", "sweep_sims": "500", "sweep": "0.5, 1"}
    monkeypatch.chdir(tmp_path)  # the tab writes the source's trades.csv under reports/_gui_tmp
    r = METHODS["mc.propfirm_run"](state, {"source": "backtest", "rules": "apex_50k_eod", "fields": small}, emit)
    assert r["edited"] == ["sims", "sweep_sims", "sweep"] and "Pass rate" in r["html"] and 0 <= r["pass_rate"] <= 1
    gui = METHODS["mc.propfirm_save"](state, {"out": str(tmp_path / "gui")}, emit)
    assert len(gui["files"]) == 5
    res = CliRunner().invoke(app, ["propfirm", "--trades", str(tmp_path / "run" / "trades.csv"), "--rules", "apex_50k_eod", "--sims", "2000",
                                   "--sweep-sims", "500", "--sweep", "0.5,1", "--out", str(tmp_path / "cli")])  # fmt: skip
    assert res.exit_code == 0, res.output
    a, b = (json.loads((tmp_path / x / "propfirm.json").read_text(encoding="utf-8")) for x in ("gui", "cli"))
    for j in (a, b):
        j.pop("trades_file"), j.pop("seconds")
    assert a == b  # R8: the same numbers as `guban propfirm` on the Backtest Save's trades.csv
    for name in ("sweep.csv", "degradation.csv", "daily.csv"):
        assert (tmp_path / "gui" / name).read_bytes() == (tmp_path / "cli" / name).read_bytes(), name
    assert "edited in Guban Studio: sims, sweep_sims, sweep" in (tmp_path / "gui" / "propfirm.html").read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="Max trading days must be a whole number"):
        METHODS["mc.propfirm_run"](state, {"source": "backtest", "fields": {**small, "max_trading_days": "2.5"}}, emit)
    with pytest.raises(ValueError, match="min_trading_days .* is more than max_trading_days"):
        METHODS["mc.propfirm_run"](state, {"source": "backtest", "fields": {**small, "min_trading_days": "30"}}, emit)
