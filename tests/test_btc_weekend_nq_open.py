"""The BTC weekend / NQ open study's rules (research/btc_weekend_nq_open.py): price lookups and agreement tests."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "research"))
import btc_weekend_nq_open as study


def _bars() -> pd.DataFrame:
    index = pd.to_datetime(["2026-06-07 18:01", "2026-06-07 18:02", "2026-06-07 18:30"]).tz_localize("America/New_York")
    return pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=index)


def test_price_at_reads_the_last_close_and_refuses_stale_or_future_bars():
    bars = _bars()
    assert study.price_at(bars, bars.index[1]) == 2.0
    assert study.price_at(bars, bars.index[1] + pd.Timedelta(minutes=5)) == 2.0
    assert np.isnan(study.price_at(bars, bars.index[1] + pd.Timedelta(minutes=6)))
    assert np.isnan(study.price_at(bars, bars.index[0] - pd.Timedelta(minutes=1)))


def test_identical_series_agree_fully():
    x = np.random.default_rng(1).normal(size=200)
    t = study.tests(x, x * 2, sims=200)
    assert t["agree"] == 1.0 and t["pearson"] > 0.999 and t["spearman"] > 0.999 and abs(t["beta"] - 2) < 1e-9
    assert t["p_agree"] < 0.01 and t["p_pearson"] < 0.01


def test_independent_series_show_nothing():
    rng = np.random.default_rng(2)
    t = study.tests(rng.normal(size=400), rng.normal(size=400), sims=500)
    assert abs(t["agree"] - t["expected"]) < 0.08 and t["p_pearson"] > 0.01


def test_zero_and_missing_weeks_are_dropped():
    t = study.tests([1.0, -1.0, 0.0, np.nan, 1.0, -2.0], [1.0, -1.0, 5.0, 5.0, 2.0, -1.0], sims=50)
    assert t["n"] == 4 and t["agree"] == 1.0
