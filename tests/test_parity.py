"""Parity harness (R7): MC export loaders, trade matching, S3 checks, diff.html; plus the Vault Break port."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guban.config import ContractSpec
from guban.data.resample import splice_history
from guban.engine import Costs, run_backtest
from guban.parity import compare_trades, load_mc_trades, load_our_trades, write_diff_html

CT, ET = "America/Chicago", "America/New_York"
MNQ = ContractSpec(symbol="MNQ", name="Micro E-mini Nasdaq-100", point_value=2.0, tick_size=0.25)
BAR = pd.Timedelta("5min")

MC_CSV = """\
"trade_id,order_id,type,signal,excel_date,excel_time,price,contracts,profit_usd,profit_pct,cum_profit_usd,cum_profit_pct,runup_usd,runup_pct,drawdown_usd,drawdown_pct,direction"
"1,1,EntryLong,VB_Long,44216.4097222222,44216.4097222222,13235.75,5,497.9,0.0037,497.9,0.0005,498.9,0.0037,-306.1,-0.0023,LONG"
"2,3,EntryLong,VB_Long,44216.5243055555,44216.5243055555,13277.5,5,147.8,0.0011,645.7,0.0006,496.4,0.0037,-128.6,-0.0009,LONG"
"""


def ours_frame(rows) -> pd.DataFrame:
    """Our trades: (entry CT, entry price, exit CT, exit price, exit name, commission, slippage, net)."""
    out = []
    for k, (et, ep, xt, xp, xn, com, slip, net) in enumerate(rows, 1):
        out.append({
            "trade_no": k, "direction": "long", "qty": 5,
            "entry_time": pd.Timestamp(et, tz=CT), "entry_price": ep, "entry_name": "VB_Long",
            "exit_time": pd.Timestamp(xt, tz=CT), "exit_price": xp, "exit_name": xn,
            "commission": com, "slippage": slip, "net_pnl": net, "ambiguous": False,
        })  # fmt: skip
    return pd.DataFrame(out)


def test_load_mc_csv_strips_line_quotes_and_decodes_excel_time_in_chart_zone(tmp_path):
    f = tmp_path / "mc.csv"
    f.write_text(MC_CSV)
    mc = load_mc_trades(f, CT)
    assert list(mc["entry_time"].dt.strftime("%Y-%m-%d %H:%M %Z")) == ["2021-01-20 09:50 CST", "2021-01-20 12:35 CST"]
    assert list(mc["entry_price"]) == [13235.75, 13277.5]
    assert list(mc["net_pnl"]) == [497.9, 147.8]
    assert mc["exit_price"].isna().all() and list(mc["qty"]) == [5, 5] and list(mc["direction"]) == ["long", "long"]


def test_load_mc_xlsx_pairs_entry_and_exit_rows(tmp_path):
    pytest.importorskip("openpyxl")
    f = tmp_path / "report.xlsx"
    header = ["Trade #", "Order #", "Type", "Signal", "Date", "Time", "Price", "Contracts", "Profit ($)"]
    t = pd.Timestamp
    rows = [
        ["List of Trades"] + [None] * 8,
        [None] * 9,
        header,
        [1, 1, "EntryLong", "VB_Long", t("2021-01-20 09:50"), t("2021-01-20 09:50"), 13235.75, 5, 497.9],
        [None, 2, "ExitLong", "Profit Target", t("2021-01-20 12:30"), t("2021-01-20 12:30"), 13285.75, 5, None],
        [2, 3, "EntryLong", "VB_Long", t("2021-01-20 12:35"), t("2021-01-20 12:35"), 13277.5, 5, 147.8],
        [None, 4, "ExitLong", "VB_TimeX", t("2021-01-20 15:55"), t("2021-01-20 15:55"), 13292.5, 5, None],
    ]
    pd.DataFrame(rows).to_excel(f, sheet_name="List of Trades", header=False, index=False)
    mc = load_mc_trades(f, CT)
    assert list(mc["exit_name"]) == ["Profit Target", "VB_TimeX"]
    assert list(mc["exit_price"]) == [13285.75, 13292.5]
    assert list(mc["exit_time"].dt.strftime("%H:%M")) == ["12:30", "15:55"]
    assert list(mc["net_pnl"]) == [497.9, 147.8]


def test_compare_matches_within_one_bar_and_implies_csv_exit_prices(tmp_path):
    f = tmp_path / "mc.csv"
    f.write_text(MC_CSV)
    mc = load_mc_trades(f, CT)
    ours = ours_frame([
        # matched exactly: exit implied from MC net 497.9 + our costs 2.1 -> 50 pts
        ("2021-01-20 09:50", 13235.75, "2021-01-20 12:30", 13285.75, "Profit Target", 2.0, 0.1, 497.9),
        # one bar late and 2 ticks off on entry -> matched, price mismatch
        ("2021-01-20 12:40", 13278.0, "2021-01-20 15:55", 13292.5, "VB_TimeX", 2.0, 0.2, 142.8),
        # no MC counterpart
        ("2021-01-20 14:00", 13300.0, "2021-01-20 15:55", 13292.5, "VB_TimeX", 2.0, 0.2, -77.2),
    ])  # fmt: skip
    window = dict(start=pd.Timestamp("2021-01-01", tz=CT), end=pd.Timestamp("2021-01-21", tz=CT))
    c = compare_trades(ours, mc, BAR, MNQ.tick_size, MNQ.point_value, **window)
    assert len(c.matched) == 2 and c.match_rate == 1.0 and c.implied_exits
    first, second = c.matched.itertuples()
    assert first.mc_exit == 13285.75 and first.price_ok
    assert second.mc_exit == pytest.approx(13292.5) and not second.entry_ok and not second.price_ok
    assert list(c.ours_only["trade_no"]) == [3]
    assert c.net_ours == pytest.approx(497.9 + 142.8 - 77.2) and c.net_mc == pytest.approx(645.7)
    checks = c.checks
    assert list(checks.values()) == [True, False, False] and not c.passed
    html_path = write_diff_html(c, tmp_path / "diff.html", tz=CT)
    text = html_path.read_text(encoding="utf-8")
    assert "Price mismatches (1)" in text and "Our trades with no match (1)" in text and "S3 FAILED" in text


def test_compare_does_not_match_outside_one_bar_or_across_directions():
    mc = ours_frame([("2024-01-02 10:00", 100.0, "2024-01-02 10:30", 101.0, "X", 0, 0, 10.0)])
    ours = ours_frame([
        ("2024-01-02 10:10", 100.0, "2024-01-02 10:30", 101.0, "X", 0, 0, 10.0),  # two bars late
        ("2024-01-02 10:05", 100.0, "2024-01-02 10:30", 101.0, "X", 0, 0, 10.0),  # one bar late, but short
    ])  # fmt: skip
    ours.loc[1, "direction"] = "short"
    c = compare_trades(ours, mc, BAR, 0.25, 2.0, start=pd.Timestamp("2024-01-01", tz=CT), end=pd.Timestamp("2024-01-03", tz=CT))
    assert c.matched.empty and len(c.mc_only) == 1 and len(c.ours_only) == 2


def test_our_trades_csv_round_trips_through_load_our_trades(tmp_path):
    ours = ours_frame([("2024-01-02 10:00", 100.0, "2024-01-02 10:30", 101.0, "X", 0, 0, 10.0)])
    ours.to_csv(tmp_path / "trades.csv", index=False)
    back = load_our_trades(tmp_path / "trades.csv")
    assert back.at[0, "entry_time"] == ours.at[0, "entry_time"]


def test_splice_history_prepends_only_earlier_bars_in_the_main_zone():
    idx = pd.date_range("2025-06-09 09:35", periods=3, freq="5min", tz=ET)
    main = pd.DataFrame({"close": [1.0, 2.0, 3.0], "contract": pd.Categorical(["202506"] * 3)}, index=idx).tz_convert(CT)
    hist_idx = pd.date_range("2025-06-09 09:25", periods=4, freq="5min", tz=ET)
    hist = pd.DataFrame({"close": [8.0, 9.0, 10.0, 11.0], "contract": ["202506"] * 4}, index=hist_idx)
    out = splice_history(main, hist)
    assert list(out["close"]) == [8.0, 9.0, 1.0, 2.0, 3.0]
    assert str(out.index.tz) == CT and out.index.is_monotonic_increasing


# --- Vault Break port ----------------------------------------------------------------------------------


def _vault_break():
    path = Path(__file__).resolve().parents[1] / "strategies" / "vault_break.py"
    spec = importlib.util.spec_from_file_location("vault_break_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.VaultBreak


def test_vault_break_enters_next_bar_after_breakout_and_exits_at_target():
    # One CT day of 5m bars from 23:55 the evening before; flat at 100 until the 09:45 bar closes at 110.
    idx = pd.date_range("2024-03-04 23:55", "2024-03-05 10:30", freq="5min", tz=CT)
    o = np.full(len(idx), 100.0)
    h, l, c = o + 0.5, o - 0.5, o.copy()
    k = idx.get_loc(pd.Timestamp("2024-03-05 09:45", tz=CT))
    h[k], c[k] = 110.5, 110.0  # breakout: 110 > NoiseUp (100 + 0.6 x 10) and > VWAP
    o[k + 1 :], h[k + 1 :], l[k + 1 :], c[k + 1 :] = 110.0, 110.5, 109.5, 110.0
    j = idx.get_loc(pd.Timestamp("2024-03-05 10:05", tz=CT))
    h[j] = 121.0  # the 10-point target (200 USD / $20 per point) is hit here
    bars = pd.DataFrame({"open": o, "high": h, "low": l, "close": c, "volume": 1}, index=idx)
    nq = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
    VaultBreak = _vault_break()
    strat = VaultBreak(take_profit_usd=200, stop_loss_usd=400, atr_seed_sum=150.0, atr_seed_count=15, sess_count_seed=15)
    res = run_backtest(strat, bars, nq, Costs())
    (t,) = res.trades.itertuples()
    assert t.entry_time == pd.Timestamp("2024-03-05 09:50", tz=CT) and t.entry_price == 110.0
    assert (t.exit_time, t.exit_name, t.exit_price) == (pd.Timestamp("2024-03-05 10:05", tz=CT), "Profit Target", 120.0)
    assert strat.daily_atr == 10.0
    assert strat.d_trades == 2 and res.open_position is not None  # flat after the target, still above NoiseUp: re-entry


def test_vault_break_without_seeds_waits_for_daily_atr_history():
    idx = pd.date_range("2024-03-04 23:55", "2024-03-05 10:30", freq="5min", tz=CT)
    bars = pd.DataFrame({"open": 100.0, "high": 200.0, "low": 50.0, "close": 150.0, "volume": 1}, index=idx)
    res = run_backtest(_vault_break()(), bars, MNQ, Costs())
    assert res.trades.empty  # SessCount < DailyATRLen + 1: literal port never trades on day one
