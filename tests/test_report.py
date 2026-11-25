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
    }, index=idx)


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
    assert not (tmp_path / "t").exists()


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
    mc = load_mc_trades(path, "America/Chicago")
    assert mc["entry_time"].tolist() == tr["entry_time"].tolist() and mc["qty"].tolist() == tr["qty"].tolist()


def test_trades_csv_ends_with_the_risk_columns_and_old_files_read_without_them(result, tmp_path):
    from guban.tradelist import RISK_COLUMNS

    _, res = result
    df = pd.read_csv(res.write_trades_csv(tmp_path / "trades.csv"))
    assert list(df.columns[-4:]) == RISK_COLUMNS == ["risk_usd", "reward_usd", "planned_rr", "r_multiple"]
    df.drop(columns=RISK_COLUMNS).to_csv(tmp_path / "old.csv", index=False)
    old = load_our_trades(tmp_path / "old.csv")
    assert len(old) == len(res.trades) and old[RISK_COLUMNS].isna().all(axis=None)


PNG = b"\x89PNG\r\n\x1a\n"


def _data(text: str) -> dict:
    import json

    return json.loads(re.search(r"const D = (\{.*?\});</script>", text, re.S).group(1))


def _report(result, path, **kw):
    bars, res = result
    m = compute_metrics(res.trades, res.equity, res.equity_low, bars["session_date"])
    meta = {"title": "Flipper · MNQ 5m", "details": {"Bars": len(bars)}, "inputs": {"qty": 5}, "notes": ["a note"]}
    return write_report(res, bars, path, m, meta, **kw)


def test_report_is_self_contained_with_image_charts(result, tmp_path):
    _, res = result
    text = _report(result, tmp_path / "report.html").read_text(encoding="utf-8")
    assert "<script src" not in text and "<link" not in text
    assert "Plotly" not in text and "prefers-color-scheme" not in text
    for anchor in ("id='equity'", "id='drawdown'", "id='viewer'", "id='tlist'", "Performance summary", "Net profit by month", "a note"):
        assert anchor in text
    assert text.count("src='data:image/png;base64,") == 2
    d = _data(text)
    assert len(d["trades"]) == len(res.trades) and d["img"] == "trades/"
    assert [t[0] for t in d["trades"]] == res.trades["trade_no"].tolist()
    files = sorted(p.name for p in (tmp_path / "trades").iterdir())
    assert files == sorted(f"trade_{n}.png" for n in res.trades["trade_no"])
    assert (tmp_path / "trades" / files[0]).read_bytes()[:8] == PNG


class Hold(Strategy):
    """One long trade, bar 2 to bar 390."""

    def on_bar(self):
        if self.current_bar == 2:
            self.buy("L")
        if self.current_bar == 390:
            self.sell("X")


def test_trade_images_replace_old_ones_and_can_be_left_out(tmp_path):
    bars = make_bars(400)
    one = (bars, run_backtest(Hold(), bars, MNQ))
    (tmp_path / "a" / "trades").mkdir(parents=True)
    (tmp_path / "a" / "trades" / "trade_99999.png").write_bytes(b"old")
    _report(one, tmp_path / "a" / "report.html")
    assert [p.name for p in (tmp_path / "a" / "trades").iterdir()] == ["trade_1.png"]
    text = _report(one, tmp_path / "b" / "report.html", trade_charts=False).read_text(encoding="utf-8")
    assert not (tmp_path / "b" / "trades").exists() and _data(text)["img"] == "trades/"
    assert "id='noimg'" in text and "guban chart" in text and len(_data(text)["trades"]) == 1
    _report(one, tmp_path / "a" / "report.html", trade_charts=False)
    assert not list((tmp_path / "a" / "trades").iterdir())


def test_process_pool_draws_the_same_images(result, tmp_path, monkeypatch):
    import guban.trade_charts as tc

    bars, res = result
    tr = res.trades.iloc[:12]
    assert tc.write_trade_charts(tr, bars, tmp_path / "one", 0.25, workers=1) == 12
    monkeypatch.setattr(tc, "PARALLEL_MIN", 2)
    assert tc.write_trade_charts(tr, bars, tmp_path / "pool", 0.25, workers=2) == 12
    for p in (tmp_path / "one").iterdir():
        assert (tmp_path / "pool" / p.name).read_bytes() == p.read_bytes()


def test_trade_payloads_hold_each_trade_in_its_candles(result):
    from guban.trade_charts import trade_payloads

    bars, res = result
    payloads = trade_payloads(res.trades, bars, 0.25)
    assert [p["no"] for p in payloads] == res.trades["trade_no"].tolist()
    for p in payloads:
        for _, ei, ep, xi, xp, _ in [p["trade"], *p["others"]]:
            assert 0 <= ei <= xi < len(p["o"])
            assert p["l"][ei] - 1e-9 <= ep <= p["h"][ei] + 1e-9 and p["l"][xi] - 1e-9 <= xp <= p["h"][xi] + 1e-9
        assert f"@ {p['trade'][2]:,.2f}" in p["title"]
    assert any(p["others"] for p in payloads)


def test_long_trades_merge_candles(tmp_path, monkeypatch):
    import guban.trade_charts as tc

    monkeypatch.setattr(tc, "MAX_CANDLES", 50)
    bars = make_bars(400)
    res = run_backtest(Hold(), bars, MNQ)
    (p,) = tc.trade_payloads(res.trades, bars, 0.25)
    assert p["step"] > 1 and len(p["o"]) <= 50 and "bars per candle" in p["title"]
    assert p["trade"][1] == 2 // p["step"] and p["trade"][3] == 390 // p["step"]
    assert tc.render_trade(p, tmp_path / "t.png").read_bytes()[:8] == PNG


def test_plot_lines_follow_the_candles_and_change_the_image(tmp_path, monkeypatch):
    import guban.trade_charts as tc

    monkeypatch.setattr(tc, "MAX_CANDLES", 50)
    bars = make_bars(400)
    res = run_backtest(Hold(), bars, MNQ)
    mid = bars["close"].to_numpy() + 50.0
    mid[:10] = np.nan
    (p,) = tc.trade_payloads(res.trades, bars, 0.25, lines={"mid": mid})
    last = np.r_[np.arange(0, 400, p["step"])[1:], 400] - 1
    np.testing.assert_array_equal(p["lines"]["mid"], mid[last])
    plain = tc.render_trade(tc.trade_payloads(res.trades, bars, 0.25)[0], tmp_path / "plain.png").read_bytes()
    lined = tc.render_trade(p, tmp_path / "lined.png").read_bytes()
    assert lined[:8] == PNG and lined != plain
    with pytest.raises(ValueError, match="plot line 'mid' has 9 values"):
        tc.trade_payloads(res.trades, bars, 0.25, lines={"mid": mid[:9]})


def test_price_decimals_show_the_tick():
    from guban.trade_charts import _decimals

    assert (_decimals(0.25), _decimals(0.01), _decimals(5.0), _decimals(0.1)) == (2, 2, 0, 1)


def test_monte_carlo_section_has_every_method_ev_and_horizons(result, tmp_path):
    from guban.montecarlo import run_monte_carlo

    bars, res = result
    tr = res.trades
    days = bars["session_date"].to_numpy()[tr["exit_bar"].to_numpy(int)]
    mc = run_monte_carlo(tr["net_pnl"].to_numpy(float), 500, 1, 200.0, days=days, contracts=tr["qty"].to_numpy(float),
                         sides=tr["direction"].to_numpy(str))
    assert mc.days == 3
    text = _report(result, tmp_path / "report.html", monte_carlo=mc, trade_charts=False).read_text(encoding="utf-8")
    for name in ("resample", "reorder", "day"):
        assert f"id='mc-band-{name}'" in text and f"id='mc-dd-{name}'" in text
    assert "id='mc-net-resample'" in text and "id='mc-net-day'" in text and "id='mc-net-reorder'" not in text
    assert "Expected value per trade" in text and "Horizons" in text and "Max drawdown depth" in text and "Max DD reaches 200.00" in text
    one_day = run_monte_carlo(tr["net_pnl"].to_numpy(float), 50, 1, days=np.zeros(len(tr)))
    text = _report(result, tmp_path / "one.html", monte_carlo=one_day, trade_charts=False).read_text(encoding="utf-8")
    assert "id='mc-band-day'" not in text and "Whole days method skipped" in text
