"""Research core (Phase 13): volume profiles, conditional-rate statistics, the verdict rule, the study report."""

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
import poc_shift  # noqa: E402

# --- profiles --------------------------------------------------------------------------------------------------


def test_poc_is_the_price_with_most_volume():
    p = from_trades([100.0, 100.25, 100.25, 100.5], [5, 4, 4, 1], 0.25)
    assert p.poc() == 100.25
    assert (p.low, p.high, p.total) == (100.0, 100.5, 14)


def test_poc_tie_goes_to_the_middle_of_the_range_then_lower():
    # ticks 0..8, equal peaks at 1 and 5: the middle is 4, so 5 wins
    p = from_volume([0, 1, 5, 8], [1, 9, 9, 1], 0.25)
    assert p.poc() == 5 * 0.25
    # equal peaks equidistant from the middle (2 and 6 around 4): the lower one
    p = from_volume([0, 2, 6, 8], [1, 9, 9, 1], 0.25)
    assert p.poc() == 2 * 0.25


def test_value_area_grows_toward_more_volume():
    # ticks 0..6 volumes 1 2 3 10 6 2 1 (25 total, 70% = 17.5): POC 3, add 4 (6>3) -> 16, add 2 (3>2) -> 19
    p = from_volume(np.arange(7), [1, 2, 3, 10, 6, 2, 1], 1.0)
    assert p.value_area() == (2.0, 4.0)
    # a single-price profile
    assert from_volume([4], [10], 0.25).value_area() == (1.0, 1.0)


def test_bar_profile_spreads_volume_evenly():
    # bar 1: 100.00..100.50 (3 ticks) vol 30; bar 2: 100.50 only, vol 5
    p = from_bars([100.0, 100.5], [100.5, 100.5], [30, 5], 0.25)
    assert p.volume.tolist() == [10.0, 10.0, 15.0]
    assert p.total == 35 and p.poc() == 100.5


def test_empty_profile_raises():
    with pytest.raises(ValueError, match="empty"):
        from_bars([1.0], [2.0], [0], 0.25)


# --- statistics ------------------------------------------------------------------------------------------------


def test_binom_sf_matches_hand_values():
    assert binom_sf(8, 10, 0.5) == pytest.approx(56 / 1024)  # (45 + 10 + 1) / 1024 = 0.0547
    assert binom_sf(0, 10, 0.3) == 1.0
    assert binom_sf(11, 10, 0.3) == 0.0
    assert binom_sf(3, 3, 0.2) == pytest.approx(0.008)
    total = sum(math.comb(20, i) * 0.6**i * 0.4 ** (20 - i) for i in range(13, 21))
    assert binom_sf(13, 20, 0.6) == pytest.approx(total)


def test_binom_sf_large_n_stays_finite():
    p = binom_sf(600, 1100, 0.5)
    assert 0 < p < 0.01


def test_rate_test_counts_and_gap():
    hit = np.array([1, 1, 1, 0, 1, 0, 0, 0, 1, 0], bool)  # base 5/10
    sig = np.array([1, 1, 1, 1, 0, 0, 0, 0, 0, 0], bool)  # 3/4 hits with the signal
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
    v = _run(("a", 35, 50, 225, 450), ("b", 35, 50, 225, 450))  # 70% vs 52% in both periods
    assert v.real and all(ok for _, ok, _ in v.checks)
    # overall gap and p pass, but period b's gap is negative: rule 3 fails alone
    v2 = _run(("a", 50, 50, 225, 450), ("b", 24, 50, 225, 450))
    assert not v2.real and [ok for _, ok, _ in v2.checks] == [True, True, False]
    assert "b" in v2.checks[2][2]


def test_verdict_small_gap_fails():
    hit = np.r_[np.ones(52), np.zeros(48), np.ones(500), np.zeros(500)].astype(bool)
    sig = np.r_[np.ones(100), np.zeros(1000)].astype(bool)
    v = verdict(rate_test(hit, sig), by_period(hit, sig, np.zeros(1100)))
    assert not v.real and v.checks[0][1] is False


# --- study logic (research/poc_shift.py) -----------------------------------------------------------------------


def _days(pocs, contracts=None, clean=None):
    n = len(pocs)
    idx = pd.bdate_range("2024-03-04", periods=n)
    return pd.DataFrame({
        "contract": contracts or ["202406"] * n, "tick_clean": clean or [True] * n,
        "tick_poc": pocs, "tick_val": np.array(pocs) - 5, "tick_vah": np.array(pocs) + 5,
        "tick_open": 100.0, "tick_close": 101.0,
    }, index=idx)  # fmt: skip


def test_triples_signal_and_outcome():
    t, drops = poc_shift.build_triples(_days([100, 110, 120, 115, 115]), "tick")
    assert t["signal"].tolist() == [1, 1, -1]
    assert t["outcome"].tolist() == [1, -1, 0]
    assert t["signal_shift"].tolist() == [10, 10, -5] and drops == {"unclean day": 0, "contract roll": 0}


def test_triples_drop_unclean_days_without_bridging():
    t, drops = poc_shift.build_triples(_days([100, 110, 120, 130, 140], clean=[True, True, False, True, True]), "tick")
    assert t.empty and drops["unclean day"] == 3  # every triple touches day 3; days 2 and 4 are never joined


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


# --- report ----------------------------------------------------------------------------------------------------


def test_study_report_writes_a_page(tmp_path):
    frame = pd.DataFrame({"n": [1, 2]}, index=pd.Index(["a", "b"], name="k"))
    path = write_study_report(
        tmp_path / "r.html", {"title": "T <x>", "subtitle": "s", "details": {"d": 1}},
        verdicts=[{"label": "Up", "real": False, "headline": "h", "checks": [{"rule": "r", "passed": False, "detail": "d"}]}],
        charts=[{"id": "c", "title": "C", "x": ["2024"], "series": [{"name": "s", "y": [1.0]}], "ref": 0}],
        tables=[{"title": "Tab", "frame": frame}], notes=["n"],
    )  # fmt: skip
    text = path.read_text(encoding="utf-8")
    assert "T &lt;x&gt;" in text and "NOT REAL" in text and "<td class='l'>b</td>" in text and "Plotly" in text
