import math

import numpy as np
import pandas as pd
import pytest

from guban.evaluation import evaluate, summary_line
from guban.metrics import downside_deviation
from guban.report import _evaluation_html


def _trades(net, r, risk=None, qty=None, years=None, exits=None):
    n = len(net)
    years = years or [2024] * n
    return pd.DataFrame({
        "net_pnl": np.asarray(net, float),
        "r_multiple": np.asarray(r, float),
        "risk_usd": np.asarray(risk if risk is not None else [100.0] * n, float),
        "qty": np.asarray(qty if qty is not None else [1] * n, float),
        "exit_time": pd.to_datetime([f"{y}-03-{i % 28 + 1:02d} 15:00" for i, y in enumerate(years)]).tz_localize("America/New_York"),
        "exit_name": exits or ["TP" if x > 0 else "SL" for x in net],
    })


def _metrics(trades, sharpe=1.5, sortino=2.0, days=10):
    net = trades["net_pnl"].to_numpy(float)
    c = np.r_[0.0, np.cumsum(net)]
    gl = -net[net < 0].sum()
    return {
        "all": {"profit_factor": net[net > 0].sum() / gl if gl else math.inf},
        "account": {"sharpe_daily": sharpe, "sortino_daily": sortino, "max_drawdown_close_to_close": float((c - np.maximum.accumulate(c)).min()),
                    "max_drawdown_intraday": float((c - np.maximum.accumulate(c)).min()) - 10, "trading_days": days},
    }


def test_downside_deviation_is_the_rms_of_the_losses_over_every_period():
    assert downside_deviation(np.array([2.0, -3.0, 0.0, -4.0])) == pytest.approx(math.sqrt((9 + 16) / 4))
    assert downside_deviation(np.array([1.0, 2.0])) == 0.0


def test_as_run_and_equal_risk_figures():
    tr = _trades([200, -100, -300, 150], [2.0, -1.0, -1.0, 0.5], risk=[100, 150, 250, 300], years=[2023, 2023, 2024, 2024])
    days = tr["exit_time"].dt.date.to_numpy()
    ev = evaluate(tr, _metrics(tr), days, sims=500, seed=1)
    a = ev["as_run"]
    assert ev["r_usd"] == 200.0
    assert a["max_dd_r"] == pytest.approx(400 / 200) and a["pass"]["max_dd_r"] is True
    assert a["pass"]["sharpe"] is True and a["passed"] == 3
    e = ev["equal_risk"]
    assert e["profit_factor"] == pytest.approx(2.5 / 2.0) and e["max_dd_r"] == pytest.approx(2.0) and e["net_r"] == pytest.approx(0.5)
    d = np.r_[[2.0, -1.0, -1.0, 0.5], np.zeros(6)]
    assert e["sharpe"] == pytest.approx(d.mean() / d.std(ddof=1) * math.sqrt(252))
    assert e["sortino"] == pytest.approx(d.mean() / downside_deviation(d) * math.sqrt(252))
    mc = e["monte_carlo"]
    assert mc["max_dd_r"]["p50"] <= mc["max_dd_r"]["p95"] <= mc["max_dd_r"]["p99"] and 0 <= mc["p_dd_over_limit"] <= 1
    assert evaluate(tr, _metrics(tr), days, sims=500, seed=1)["equal_risk"]["monte_carlo"] == mc
    assert [g["group"] for g in ev["by_year"]] == ["2023", "2024"] and [g["trades"] for g in ev["by_year"]] == [2, 2]
    assert {g["group"] for g in ev["by_exit"]} == {"TP", "SL"}
    assert "as run 3/4" in summary_line(ev) and "equal risk" in summary_line(ev)
    page = _evaluation_html(ev)
    assert "<section id='evaluation'>" in page and "By stop width" in page and "By exit year" in page


def test_mc_risk_sets_the_as_run_r():
    tr = _trades([100, -100, 50], [1, -1, 0.5], qty=[2, 2, 2])
    ev = evaluate(tr, _metrics(tr), tr["exit_time"].dt.date.to_numpy(), sims=0, risk=50)
    assert ev["r_usd"] == 100.0 and "--mc-risk" in ev["r_source"] and "monte_carlo" not in ev["equal_risk"]


def test_no_stops_means_no_r_figures():
    tr = _trades([100, -50, 30], [np.nan] * 3, risk=[np.nan] * 3)
    ev = evaluate(tr, _metrics(tr), tr["exit_time"].dt.date.to_numpy(), sims=100)
    assert ev["r_usd"] is None and ev["equal_risk"] is None and ev["by_stop_width"] == []
    assert ev["as_run"]["max_dd_r"] is None and ev["as_run"]["pass"]["max_dd_r"] is None
    assert "n/a" in summary_line(ev) and "<section id='evaluation'>" in _evaluation_html(ev)


def test_no_trades_means_no_evaluation():
    tr = _trades([], [])
    assert evaluate(tr, _metrics(tr), [], sims=10) is None
