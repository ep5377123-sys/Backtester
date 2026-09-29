"""MC-layout trades.csv, metrics.json-ready output and the self-contained HTML report (R6)."""

from __future__ import annotations

import re

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.engine import Costs, Strategy, run_backtest
from guban.metrics import compute_metrics
from guban.parity import load_mc_trades, load_our_trades
from guban.report import write_report
from guban.tradelist import MC_TRADE_COLUMNS

MNQ = ContractSpec(symbol="MNQ", name="Micro E-mini Nasdaq-100", point_value=2.0, tick_size=0.25)


def make_bars(n: int, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 20000 + np.cumsum(rng.normal(0, 8, n)).round() * 0.25
    opens = np.r_[closes[0], closes[:-1]]
    idx = pd.date_range("2025-06-09 08:35", periods=n, freq="5min", tz="America/Chicago", name="time")
    return pd.DataFrame({
        "open": opens, "high": np.maximum(opens, closes) + 1, "low": np.minimum(opens, closes) - 1, "close": closes,
        "volume": 1, "session_date": idx.tz_localize(None).normalize(),
    }, index=idx)  # fmt: skip


class Flipper(Strategy):
    inputs = dict(qty=5)

    def on_bar(self):
        if self.current_bar < 4:
            return
        up = self.close[0] > self.close[3]
        if up and self.market_position <= 0:
            self.buy("L", self.p.qty)
        elif not up and self.market_position >= 0:
            self.sell_short("S", self.p.qty)


@pytest.fixture(scope="module")
def result():
    bars = make_bars(600)
    return bars, run_backtest(Flipper(), bars, MNQ, Costs(commission=0.40, slippage=0.04))


@pytest.mark.parametrize("capital", [0, -1.0, float("nan")])
def test_trades_csv_rejects_non_positive_capital(result, tmp_path, capital):
    _, res = result
    with pytest.raises(ValueError, match="capital must be"):
        res.write_trades_csv(tmp_path / "t" / "trades.csv", capital=capital)
    assert not (tmp_path / "t").exists()  # checked before anything is written


def test_trades_csv_has_mc_columns_and_values(result, tmp_path):
    _, res = result
    path = res.write_trades_csv(tmp_path / "trades.csv", capital=1_000_000)
    df = pd.read_csv(path)
    assert list(df.columns[: len(MC_TRADE_COLUMNS)]) == MC_TRADE_COLUMNS
    t, r = res.trades.iloc[0], df.iloc[0]
    assert (r.trade_id, r.order_id, r.contracts, r.signal) == (1, 1, 5, t.entry_name)
    assert df["order_id"].tolist() == (2 * df["trade_id"] - 1).tolist()
    assert r.type == ("EntryLong" if t.direction == "long" else "EntryShort") and r.direction == t.direction.upper()
    serial = (t.entry_time.tz_localize(None) - pd.Timestamp("1899-12-30")) / pd.Timedelta(days=1)
    assert r.excel_date == r.excel_time == pytest.approx(serial, abs=1e-9)
    notional = t.entry_price * 2.0 * 5
    assert r.profit_usd == pytest.approx(t.net_pnl) and r.profit_pct == pytest.approx(t.net_pnl / notional)
    assert r.runup_pct == pytest.approx(t.runup / notional) and r.drawdown_pct == pytest.approx(t.drawdown / notional)
    assert df["cum_profit_pct"].iat[-1] == pytest.approx(res.trades["cum_net_pnl"].iat[-1] / 1_000_000)


def test_trades_csv_reads_back_for_compare(result, tmp_path):
    _, res = result
    path = res.write_trades_csv(tmp_path / "trades.csv")
    ours = load_our_trades(path)
    tr = res.trades
    assert ours["trade_no"].tolist() == tr["trade_no"].tolist()
    assert ours["direction"].tolist() == tr["direction"].tolist()
    assert ours["entry_time"].tolist() == tr["entry_time"].dt.tz_convert("UTC").tolist()
    np.testing.assert_allclose(ours["exit_price"], tr["exit_price"])
    np.testing.assert_allclose(ours["net_pnl"], tr["net_pnl"], atol=1e-6)
    # MC's own reader accepts the file too (entries only, as for an MC CSV export)
    mc = load_mc_trades(path, "America/Chicago")
    assert mc["entry_time"].tolist() == tr["entry_time"].tolist() and mc["qty"].tolist() == tr["qty"].tolist()


def test_report_is_self_contained_and_carries_the_data(result, tmp_path):
    bars, res = result
    m = compute_metrics(res.trades, res.equity, res.equity_low, bars["session_date"])
    meta = {"title": "Flipper · MNQ 5m", "details": {"Bars": len(bars)}, "inputs": {"qty": 5}, "notes": ["a note"]}
    path = write_report(res, bars, tmp_path / "report.html", m, meta)
    text = path.read_text(encoding="utf-8")
    assert "<script src" not in text and "<link" not in text  # nothing fetched: opens offline
    assert "Plotly" in text and len(text) > 1_000_000  # plotly.js inlined
    for anchor in ("id='equity'", "id='drawdown'", "id='viewer'", "id='tlist'", "Performance summary", "Net profit by month", "a note"):
        assert anchor in text
    data = re.search(r"const D = (\{.*?\});</script>", text, re.S).group(1)
    import json

    d = json.loads(data)
    assert len(d["trades"]) == len(res.trades)
    assert len(d["days"]) == len(d["equity"]) == len(d["dd"])
    for t in d["trades"]:  # every trade's candles exist in its window, entry price inside the entry candle
        w = d["windows"][t[14]]
        assert 0 <= t[15] <= t[16] < len(w["dt"])
        lo, hi = (w["base"] + w["l"][t[15]]) * d["tick"], (w["base"] + w["h"][t[15]]) * d["tick"]
        assert lo - 1e-9 <= t[5] <= hi + 1e-9


def test_report_merges_candles_for_long_windows(tmp_path, monkeypatch):
    import guban.report as rep

    monkeypatch.setattr(rep, "MAX_CANDLES", 50)
    bars = make_bars(400)

    class Hold(Strategy):
        def on_bar(self):
            if self.current_bar == 2:
                self.buy("L")
            if self.current_bar == 390:
                self.sell("X")

    res = run_backtest(Hold(), bars, MNQ)
    path = rep.write_report(res, bars, tmp_path / "r.html", compute_metrics(res.trades, res.equity), {})
    import json

    d = json.loads(re.search(r"const D = (\{.*?\});</script>", path.read_text(encoding="utf-8"), re.S).group(1))
    (w,) = d["windows"]
    assert w["step"] > 1 and len(w["dt"]) <= 50
    t = d["trades"][0]
    assert (t[15], t[16]) == (2 // w["step"], 390 // w["step"])  # window starts at bar 0 (entry bar 2 - 24 < 0)
