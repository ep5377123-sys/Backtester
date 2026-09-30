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
from guban.parity import _match, compare_trades, load_mc_trades, load_our_trades, write_diff_html

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


def _at(*rows):
    """Trades at (CT time, net) as ours_frame rows."""
    return ours_frame([(t, 100.0, t, 101.0, "X", 0, 0, net) for t, net in rows])


DAY = dict(start=pd.Timestamp("2024-01-02 10:00", tz=CT), end=pd.Timestamp("2024-01-02 16:00", tz=CT))


def test_slack_trades_can_match_but_never_count_in_totals():
    # FIX.md #4 repro: our $100 trade one bar before the window used to inflate our net to 110
    mc = _at(("2024-01-02 10:00", 10.0))
    ours = _at(("2024-01-02 09:57", 100.0), ("2024-01-02 10:00", 10.0))
    c = compare_trades(ours, mc, BAR, 0.25, 2.0, **DAY)
    assert (c.n_mc, c.n_ours, len(c.matched)) == (1, 1, 1)
    assert (c.net_mc, c.net_ours, c.net_diff) == (10.0, 10.0, 0.0)
    assert c.ours_only.empty  # the slack trade is outside the window: not listed as ours-only either
    # an edge MC trade still finds its partner one bar outside the window, and our partnered trade is not "ours-only"
    c = compare_trades(_at(("2024-01-02 09:57", 10.0)), mc, BAR, 0.25, 2.0, **DAY)
    assert len(c.matched) == 1 and c.n_ours == 0 and c.net_ours == 0.0 and c.ours_only.empty


def test_matching_is_optimal_not_greedy():
    # FIX.md #4 repro: greedy pairs MC 10:00 with ours 10:02 (nearest) and strands MC 10:05; both can match
    mc = _at(("2024-01-02 10:00", 1.0), ("2024-01-02 10:05", 1.0))
    ours = _at(("2024-01-02 09:55", 1.0), ("2024-01-02 10:02", 1.0))
    c = compare_trades(ours, mc, BAR, 0.25, 2.0, start=pd.Timestamp("2024-01-02 09:00", tz=CT), end=DAY["end"])
    assert list(zip(c.matched["mc_trade"], c.matched["our_trade"])) == [(1, 1), (2, 2)]
    assert c.match_rate == 1.0 and c.mc_only.empty and c.ours_only.empty


def _brute(ta, tb, tol):
    """Best (pairs, -total |dt|) over every one-to-one pairing, by exhaustive search."""
    if not ta:
        return (0, 0)
    best = _brute(ta[1:], tb, tol)  # leave ta[0] unmatched
    for k, t in enumerate(tb):
        if abs(ta[0] - t) <= tol:
            c, cost = _brute(ta[1:], tb[:k] + tb[k + 1 :], tol)
            best = max(best, (c + 1, cost - abs(ta[0] - t)))
    return best


def test_matching_equals_exhaustive_search_on_random_lists():
    rng = np.random.default_rng(7)
    base = pd.Timestamp("2024-01-02 10:00")  # ours_frame localizes to CT
    for _ in range(300):
        na, nb = rng.integers(0, 6, 2)
        ta = sorted(rng.integers(0, 25, na).tolist())
        tb = sorted(rng.integers(0, 25, nb).tolist())
        mc = _at(*[(base + pd.Timedelta(minutes=t), 1.0) for t in ta]) if na else _at(("2024-01-02 23:00", 1.0)).iloc[:0]
        ours = _at(*[(base + pd.Timedelta(minutes=t), 1.0) for t in tb]) if nb else mc.iloc[:0]
        pairs = _match(ours, mc, BAR)
        assert len({i for i, _ in pairs}) == len({j for _, j in pairs}) == len(pairs)
        assert all(abs(ta[i] - tb[j]) <= 5 for i, j in pairs)
        got = (len(pairs), -sum(abs(ta[i] - tb[j]) for i, j in pairs))
        assert got == _brute(ta, tb, 5), (ta, tb)


def test_empty_inputs_and_zero_net():
    mc = _at(("2024-01-02 10:00", 10.0), ("2024-01-02 11:00", -5.0))
    none = mc.iloc[:0]
    c = compare_trades(none, mc, BAR, 0.25, 2.0)  # FIX.md #4: every MC trade is listed as unmatched
    assert (c.n_mc, c.n_ours, len(c.mc_only), c.match_rate) == (2, 0, 2, 0.0) and not c.passed
    assert "note: we have no trades in the window" in c.summary()
    c = compare_trades(mc, none, BAR, 0.25, 2.0)
    assert (c.n_mc, c.n_ours, len(c.ours_only)) == (0, 2, 2) and not c.passed
    with pytest.raises(ValueError, match="both trade lists are empty"):
        compare_trades(none, none, BAR, 0.25, 2.0)
    assert compare_trades(none, none, BAR, 0.25, 2.0, **DAY).n_mc == 0
    with pytest.raises(ValueError, match="do not overlap"):
        compare_trades(_at(("2024-01-03 10:00", 1.0)), mc, BAR, 0.25, 2.0)
    flat = _at(("2024-01-02 10:00", 5.0), ("2024-01-02 11:00", -5.0))
    c = compare_trades(flat, flat, BAR, 0.25, 2.0)
    assert c.net_mc == 0 and c.net_diff == 0.0 and c.passed  # FIX.md #4: was inf
    c = compare_trades(_at(("2024-01-02 10:00", 5.0), ("2024-01-02 11:00", 1.0)), flat, BAR, 0.25, 2.0)
    assert c.net_diff == float("inf") and not c.passed


def test_compare_rejects_naive_times():
    mc = _at(("2024-01-02 10:00", 10.0))
    naive = mc.assign(entry_time=mc["entry_time"].dt.tz_localize(None))
    with pytest.raises(ValueError, match="entry_time must be a tz-aware datetime column"):
        compare_trades(naive, mc, BAR, 0.25, 2.0)
    with pytest.raises(ValueError, match="missing columns"):
        compare_trades(mc.drop(columns="net_pnl"), mc, BAR, 0.25, 2.0)


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
    strat = VaultBreak(
        earliest_entry=945,  # the MC chart's CT input (the strategy default is now Eastern)
        take_profit_usd=200, stop_loss_usd=400, atr_seed_sum=150.0, atr_seed_count=15, sess_count_seed=15,
    )
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


NQ_SPEC = ContractSpec(symbol="NQ", name="E-mini Nasdaq-100", point_value=20.0, tick_size=0.25)
SEEDS = dict(take_profit_usd=200, stop_loss_usd=400, atr_seed_sum=150.0, atr_seed_count=15, sess_count_seed=15)


def _et_day(day: str, breakout: str, last: str, close: str) -> pd.DataFrame:
    """ET 5m bars from 23:55 the evening before to `last`: flat at 100 until the `breakout` bar closes at 110 (above
    NoiseUp = 100 + 0.6 x 10 and VWAP), flat at 110 after it (no target or stop is reached). Session closes at `close`."""
    idx = pd.date_range(pd.Timestamp(day) - pd.Timedelta("5min"), f"{day} {last}", freq="5min", tz=ET)
    o = np.full(len(idx), 100.0)
    k = idx.get_loc(pd.Timestamp(f"{day} {breakout}", tz=ET))
    o[k + 1 :] = 110.0
    c = o.copy()
    c[k] = 110.0
    bars = pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5, "close": c, "volume": 1}, index=idx)
    bars["session_close"] = pd.Timestamp(f"{day} {close}", tz=ET)
    return bars


def test_vault_break_eastern_defaults_enter_after_a_1045_signal_and_flatten_at_1550():
    # FIX.md follow-up B: the default schedule is Eastern (EarliestEntry 1045, FlattenTime 1550)
    res = run_backtest(_vault_break()(**SEEDS), _et_day("2024-03-05", "10:45", "16:30", "17:00"), NQ_SPEC, Costs())
    (t,) = res.trades.itertuples()
    assert (t.entry_time, t.entry_price) == (pd.Timestamp("2024-03-05 10:50", tz=ET), 110.0)  # next bar's open
    # the 15:50 bar sends the exit; it fills at the next bar's open (wall-clock 15:50), labeled 15:55
    assert (t.exit_time, t.exit_name, t.exit_price) == (pd.Timestamp("2024-03-05 15:55", tz=ET), "VB_TimeX", 110.0)


@pytest.mark.parametrize("close, exit_label", [("13:15", "13:10"), ("13:00", "12:55")])
def test_vault_break_flattens_before_a_scheduled_early_close(close, exit_label):
    # FIX.md follow-up A: 2024-07-03 / 2024-12-24 held to the reopen; now flat at close - 10 min
    bars = _et_day("2024-12-24", "12:00", close, close)
    res = run_backtest(_vault_break()(**SEEDS), bars, NQ_SPEC, Costs())
    (t,) = res.trades.itertuples()
    assert t.entry_time == pd.Timestamp("2024-12-24 12:05", tz=ET)
    assert (t.exit_time, t.exit_name) == (pd.Timestamp(f"2024-12-24 {exit_label}", tz=ET), "VB_TimeX")
    assert res.open_position is None
    # the literal .el (flatten_early_close=False) is still long at the halt
    res = run_backtest(_vault_break()(flatten_early_close=False, **SEEDS), bars, NQ_SPEC, Costs())
    assert res.trades.empty and res.open_position is not None


def test_vault_break_last_entry_moves_with_an_early_close():
    # 13:15 close: flatten 13:05, last entry 12:45 (LatestEntry keeps its 20 min before FlattenTime)
    assert run_backtest(_vault_break()(**SEEDS), _et_day("2024-07-03", "12:50", "13:15", "13:15"), NQ_SPEC, Costs()).trades.empty
    res = run_backtest(_vault_break()(**SEEDS), _et_day("2024-07-03", "12:45", "13:15", "13:15"), NQ_SPEC, Costs())
    assert res.trades["entry_time"].tolist() == [pd.Timestamp("2024-07-03 12:50", tz=ET)]


def test_vault_break_data_gap_exits_after_data_resumes_and_is_reported():
    # FIX.md follow-up A, 2026-05-14: the feed stops at 14:25 and resumes at 18:05 (next session). The gap is not
    # known in advance, so VB_TimeX goes out on the first bar after it and fills on the next; the trade is reported.
    from guban.metrics import session_exposure

    day = _et_day("2026-05-14", "12:00", "14:25", "17:00")
    after = pd.date_range("2026-05-14 18:05", periods=3, freq="5min", tz=ET)
    bars = pd.concat([day, pd.DataFrame({"open": 110.0, "high": 110.5, "low": 109.5, "close": 110.0, "volume": 1,
                                         "session_close": pd.Timestamp("2026-05-15 17:00", tz=ET)}, index=after)])  # fmt: skip
    bars["session_date"] = np.where(bars.index < after[0], pd.Timestamp("2026-05-14"), pd.Timestamp("2026-05-15"))
    res = run_backtest(_vault_break()(**SEEDS), bars, NQ_SPEC, Costs())
    (t,) = res.trades.itertuples()
    assert (t.exit_time, t.exit_name) == (pd.Timestamp("2026-05-14 18:10", tz=ET), "VB_TimeX")
    e = session_exposure(res.trades, bars)
    assert (e["past_close_trade_nos"], e["gap_trade_nos"]) == ([1], [1])


def test_session_close_series_is_in_chart_time_and_must_be_tz_aware():
    bars = _et_day("2024-12-24", "12:00", "13:15", "13:15").tz_convert(CT)
    strat = _vault_break()(**SEEDS)
    run_backtest(strat, bars, NQ_SPEC, Costs())
    assert strat.session_close[0] == pd.Timestamp("2024-12-24 12:15", tz=CT) and str(strat.session_close[0].tz) == CT
    with pytest.raises(ValueError, match="session_close"):
        run_backtest(_vault_break()(), bars.assign(session_close=bars["session_close"].dt.tz_localize(None)), NQ_SPEC, Costs())
