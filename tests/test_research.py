"""Research core (Phase 13): volume profiles, conditional-rate statistics, the verdict rule, the study report."""

import datetime as dt
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from guban.research.profile import from_bars, from_trades, from_volume
from guban.research.report import write_study_report
from guban.research.stats import binom_sf, by_period, rate_test, verdict

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import poc_shift


def test_poc_is_the_price_with_most_volume():
    p = from_trades([100.0, 100.25, 100.25, 100.5], [5, 4, 4, 1], 0.25)
    assert p.poc() == 100.25
    assert (p.low, p.high, p.total) == (100.0, 100.5, 14)


def test_poc_tie_goes_to_the_middle_of_the_range_then_lower():
    p = from_volume([0, 1, 5, 8], [1, 9, 9, 1], 0.25)
    assert p.poc() == 5 * 0.25
    p = from_volume([0, 2, 6, 8], [1, 9, 9, 1], 0.25)
    assert p.poc() == 2 * 0.25


def test_value_area_grows_toward_more_volume():
    p = from_volume(np.arange(7), [1, 2, 3, 10, 6, 2, 1], 1.0)
    assert p.value_area() == (2.0, 4.0)
    assert from_volume([4], [10], 0.25).value_area() == (1.0, 1.0)


def test_bar_profile_spreads_volume_evenly():
    p = from_bars([100.0, 100.5], [100.5, 100.5], [30, 5], 0.25)
    assert p.volume.tolist() == [10.0, 10.0, 15.0]
    assert p.total == 35 and p.poc() == 100.5


def test_empty_profile_raises():
    with pytest.raises(ValueError, match="empty"):
        from_bars([1.0], [2.0], [0], 0.25)



def test_binom_sf_matches_hand_values():
    assert binom_sf(8, 10, 0.5) == pytest.approx(56 / 1024)
    assert binom_sf(0, 10, 0.3) == 1.0
    assert binom_sf(11, 10, 0.3) == 0.0
    assert binom_sf(3, 3, 0.2) == pytest.approx(0.008)
    total = sum(math.comb(20, i) * 0.6**i * 0.4 ** (20 - i) for i in range(13, 21))
    assert binom_sf(13, 20, 0.6) == pytest.approx(total)


def test_binom_sf_large_n_stays_finite():
    p = binom_sf(600, 1100, 0.5)
    assert 0 < p < 0.01


def test_rate_test_counts_and_gap():
    hit = np.array([1, 1, 1, 0, 1, 0, 0, 0, 1, 0], bool)
    sig = np.array([1, 1, 1, 1, 0, 0, 0, 0, 0, 0], bool)
    t = rate_test(hit, sig, "x")
    assert (t.n, t.hits, t.base_n, t.base_hits) == (4, 3, 10, 5)
    assert t.rate == 0.75 and t.base_rate == 0.5 and t.gap == 0.25
    assert t.p_value == pytest.approx(binom_sf(3, 4, 0.5))


def _period(name, sig_hits, sig_n, other_hits, other_n):
    hit = np.r_[np.ones(sig_hits), np.zeros(sig_n - sig_hits), np.ones(other_hits), np.zeros(other_n - other_hits)].astype(bool)
    sig = np.r_[np.ones(sig_n), np.zeros(other_n)].astype(bool)
    return hit, sig, np.array([name] * (sig_n + other_n))


def _run(*periods):
    hit, sig, per = (np.concatenate(x) for x in zip(*(_period(*p) for p in periods)))
    return verdict(rate_test(hit, sig), by_period(hit, sig, per))


def test_verdict_rule():
    v = _run(("a", 35, 50, 225, 450), ("b", 35, 50, 225, 450))
    assert v.real and all(ok for _, ok, _ in v.checks)
    v2 = _run(("a", 50, 50, 225, 450), ("b", 24, 50, 225, 450))
    assert not v2.real and [ok for _, ok, _ in v2.checks] == [True, True, False]
    assert "b" in v2.checks[2][2]


def test_verdict_small_gap_fails():
    hit = np.r_[np.ones(52), np.zeros(48), np.ones(500), np.zeros(500)].astype(bool)
    sig = np.r_[np.ones(100), np.zeros(1000)].astype(bool)
    v = verdict(rate_test(hit, sig), by_period(hit, sig, np.zeros(1100)))
    assert not v.real and v.checks[0][1] is False



def _days(pocs, contracts=None, clean=None):
    n = len(pocs)
    idx = pd.bdate_range("2024-03-04", periods=n)
    return pd.DataFrame({
        "contract": contracts or ["202406"] * n, "tick_clean": clean or [True] * n,
        "tick_poc": pocs, "tick_val": np.array(pocs) - 5, "tick_vah": np.array(pocs) + 5,
        "tick_open": 100.0, "tick_close": 101.0,
    }, index=idx)


def test_triples_signal_and_outcome():
    t, drops = poc_shift.build_triples(_days([100, 110, 120, 115, 115]), "tick")
    assert t["signal"].tolist() == [1, 1, -1]
    assert t["outcome"].tolist() == [1, -1, 0]
    assert t["signal_shift"].tolist() == [10, 10, -5] and drops == {"unclean day": 0, "contract roll": 0}


def test_triples_drop_unclean_days_without_bridging():
    t, drops = poc_shift.build_triples(_days([100, 110, 120, 130, 140], clean=[True, True, False, True, True]), "tick")
    assert t.empty and drops["unclean day"] == 3


def test_triples_drop_contract_rolls():
    t, drops = poc_shift.build_triples(_days([100, 110, 120, 130], contracts=["202403", "202403", "202406", "202406"]), "tick")
    assert t.empty and drops["contract roll"] == 2


def test_va_relation():
    r = poc_shift.va_relation
    assert r(111, 120, 100, 110) == "higher" and r(90, 99, 100, 110) == "lower"
    assert r(102, 108, 100, 110) == "inside" and r(95, 115, 100, 110) == "outside"
    assert r(105, 115, 100, 110) == "overlapping higher" and r(95, 105, 100, 110) == "overlapping lower"


def test_small_first_year_is_pooled():
    dates = pd.Series(pd.to_datetime(["2023-12-01"] * 10 + ["2024-01-02"] * 100 + ["2025-01-02"] * 100))
    p = poc_shift.periods_for(dates)
    assert set(p[:110]) == {"2023-2024"} and set(p[110:]) == {"2025"}



def test_study_report_writes_a_page(tmp_path):
    frame = pd.DataFrame({"n": [1, 2]}, index=pd.Index(["a", "b"], name="k"))
    path = write_study_report(
        tmp_path / "r.html", {"title": "T <x>", "subtitle": "s", "details": {"d": 1}},
        verdicts=[{"label": "Up", "real": False, "headline": "h", "checks": [{"rule": "r", "passed": False, "detail": "d"}]}],
        charts=[{"id": "c", "title": "C", "x": ["2024"], "series": [{"name": "s", "y": [1.0]}], "ref": 0}],
        tables=[{"title": "Tab", "frame": frame}], notes=["n"],
    )
    text = path.read_text(encoding="utf-8")
    assert "T &lt;x&gt;" in text and "NOT REAL" in text and "<td class='l'>b</td>" in text
    assert "<img class='chart' src='data:image/png;base64," in text and "id='c'" in text and "Plotly" not in text



def test_trading_days_and_minute_bars_from_the_store(tmp_path):
    from guban.research.days import minute_bars, trading_days
    from guban.store.build import QUALITY_COLUMNS

    root = tmp_path / "Market Data"
    sym = root / "NQ"
    rows = []
    for s, ok in (("20250804", True), ("20250805", False), ("20250806", None)):
        d = pd.Timestamp(s) + pd.Timedelta(days=1)
        ts = pd.date_range(d + pd.Timedelta("9h29min"), d + pd.Timedelta("16h"), freq="1min", tz="America/New_York").tz_convert("UTC")
        df = pd.DataFrame({"ts_utc": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 3, "ask_vol": 1, "bid_vol": 2,
                           "contract": "202509", "source": "minute"})
        (sym / "minute" / "202509").mkdir(parents=True, exist_ok=True)
        df.to_parquet(sym / "minute" / "202509" / f"{s}.parquet", index=False)
        rows.append({"session": s, "trade_date": f"{d:%Y-%m-%d}", "contract": "202509", "minute_volume": 3 * len(ts), "bars": len(ts),
                     "bar_source": "minute", "tick_ok": ok, "front": True, "thin": False})
    pd.DataFrame(rows).reindex(columns=QUALITY_COLUMNS).to_csv(sym / "quality.csv", index=False)
    days = trading_days(sym, "NQ")
    assert list(days.index.strftime("%Y-%m-%d")) == ["2025-08-05", "2025-08-06", "2025-08-07"]
    assert days["tick_ok"].tolist() == [True, False, False]
    assert (days["contract"] == "202509").all() and days["session_file"].tolist() == ["20250804", "20250805", "20250806"]
    mb = minute_bars(sym, days.iloc[0])
    assert mb.index[0] == pd.Timestamp("2025-08-05 09:31") and mb.index[-1] == pd.Timestamp("2025-08-05 16:00")
    assert mb.index.tz is None and len(mb) == 390 and mb["volume"].sum() == 3 * 390


from guban.research.ranges import (
    AMBIGUOUS, EXTEND, FADE, RANGE_PER_SIGMA, UNRESOLVED, UNTOUCHED, monthly_refits, race, remaining_share,
    touch_prob, variance_shares,
)
from guban.research.vx import constant_maturity, expiry, vx_before


def test_touch_prob_is_the_reflection_principle():
    assert touch_prob(1.0, 1.0) == pytest.approx(0.31731, abs=1e-5)
    assert touch_prob(2.0, 1.0) == pytest.approx(0.04550, abs=1e-5)
    assert touch_prob(0.0, 1.0) == 1.0 and touch_prob(-0.5, 1.0) == 1.0
    assert touch_prob(0.1, 0.0) == 0.0
    assert RANGE_PER_SIGMA == pytest.approx(1.5958, abs=1e-4)


def test_touch_prob_matches_a_simulated_random_walk():
    rng = np.random.default_rng(7)
    paths = np.cumsum(rng.normal(0, 1 / math.sqrt(2000), (4000, 2000)), axis=1)
    hit = (paths.max(axis=1) >= 1.0).mean()
    assert hit == pytest.approx(touch_prob(1.0, 1.0), abs=0.025)
    assert (paths.max(axis=1) - paths.min(axis=1)).mean() == pytest.approx(RANGE_PER_SIGMA, abs=0.06)


def test_race_outcomes():
    hi = np.array([1.0, 2.0, 2.2, 1.5, 1.0])
    lo = np.array([0.5, 1.5, 1.9, 1.4, 0.4])
    assert race(hi, lo, 2.0, 0.5, 1) == (FADE, 1, 3)
    assert race(hi, lo, 2.0, 0.15, 1) == (EXTEND, 1, 2)
    assert race(hi, lo, 1.9, 0.05, 1) == (EXTEND, 1, 1)
    assert race(hi, lo, 3.0, 0.1, 1)[0] == UNTOUCHED
    assert race(hi, lo, 2.0, 5.0, 1) == (UNRESOLVED, 1, -1)
    assert race(np.array([1.0, 1.2, 3.0]), np.array([0.9, 0.95, 0.0]), 1.0, 0.5, 1) == (AMBIGUOUS, 0, 2)


def test_race_lower_side_mirrors_the_upper():
    hi = np.array([1.0, 2.0, 2.2, 1.5, 1.0])
    lo = np.array([0.5, 1.5, 1.9, 1.4, 0.4])
    assert race(-lo, -hi, -2.0, 0.5, -1) == race(hi, lo, 2.0, 0.5, 1)
    assert race(hi, lo, 0.5, 0.5, -1) == (FADE, 0, 1)


def test_variance_clock():
    r = np.array([[1.0, 1.0, 0.0, 0.0], [2.0, 0.0, 0.0, 2.0], [0.0, 0.0, 0.0, 0.0]])
    s = variance_shares(r)
    assert s.tolist() == [0.5, 0.25, 0.0, 0.25]
    assert remaining_share(s).tolist() == [0.5, 0.25, 0.25, 0.0]


def test_monthly_refits_train_only_on_earlier_months():
    dates = pd.bdate_range("2024-01-01", "2024-04-30")
    usable = np.ones(len(dates), bool)
    usable[3] = False
    seen = []
    for start, train, test in monthly_refits(dates, usable, 30):
        assert dates[train].max() < start <= dates[test].min()
        assert not train[3] and not test[3]
        assert (dates[test].to_period("M") == start.to_period("M")).all()
        seen.append(start.month)
    assert seen == [3, 4]


def test_vx_expiry_rule():
    assert expiry(2024, 3) == dt.date(2024, 3, 20)
    assert expiry(2024, 4) == dt.date(2024, 4, 17)
    assert expiry(2023, 12) == dt.date(2023, 12, 20)
    assert all(expiry(y, m).weekday() == 2 for y in range(2019, 2027) for m in range(1, 13))


def test_vx_constant_maturity_and_before():
    d = pd.Timestamp("2024-03-01")
    bars = pd.DataFrame({"date": [d, d, d], "symbol": ["VX/H4", "VX/J4", "VX/K4"], "close": [14.0, 16.0, 17.0],
                         "expiry": pd.to_datetime(["2024-03-20", "2024-04-17", "2024-05-22"])})
    cm = constant_maturity(bars)
    t1, t2 = 19, 47
    w = (t2 - 30) / (t2 - t1)
    assert cm.loc[d, "vx"] == pytest.approx(w * 14 + (1 - w) * 16) and cm.loc[d, "t1"] == t1
    late = bars.assign(date=pd.Timestamp("2024-02-15"))
    assert constant_maturity(late).iloc[0]["vx"] == 14.0
    s = pd.Series([1.0, 2.0], index=pd.to_datetime(["2024-03-03", "2024-03-04"]))
    out = vx_before(s, pd.DatetimeIndex(["2024-03-03", "2024-03-04", "2024-03-06"]))
    assert np.isnan(out.iloc[0]) and out.iloc[1:].tolist() == [1.0, 2.0]
