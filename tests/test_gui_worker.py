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
    assert "id='monte-carlo'" in r["html"]["monte_carlo"] and "id='evaluation'" in r["html"]["evaluation"]
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
