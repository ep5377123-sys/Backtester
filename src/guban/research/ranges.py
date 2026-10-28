"""Range math for the range-band study (Phase 21): random-walk touch probabilities, the intraday variance clock,
level races on 1-minute bars, monthly walk-forward refits. Prices are in log terms throughout (ln price), so a
distance d is a fraction of price (0.01 = 1%).

Random walk (driftless Brownian motion, volatility sigma over the time left):
- P(the path reaches a level `dist` above the start) = 2 * (1 - Phi(dist / sigma))   (reflection principle)
- E[high - low over the window] = 2 * sqrt(2 / pi) * sigma ~= 1.596 * sigma           (RANGE_PER_SIGMA)
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np
import pandas as pd

RANGE_PER_SIGMA = 2 * math.sqrt(2 / math.pi)

# race outcomes
FADE, EXTEND, AMBIGUOUS, UNRESOLVED, UNTOUCHED = 1, 0, -1, -2, -3
OUTCOME_NAMES = {FADE: "fade", EXTEND: "extend", AMBIGUOUS: "ambiguous", UNRESOLVED: "unresolved", UNTOUCHED: "untouched"}


_erfc = np.vectorize(math.erfc, otypes=[float])


def norm_sf(x) -> np.ndarray:
    """1 - Phi(x), elementwise."""
    return 0.5 * _erfc(np.asarray(x, float) / math.sqrt(2))


def touch_prob(dist, sigma):
    """P(a driftless random walk with volatility `sigma` reaches a level `dist` away), 1 where dist <= 0."""
    dist, sigma = np.asarray(dist, float), np.asarray(sigma, float)
    with np.errstate(divide="ignore", invalid="ignore"):
        p = np.minimum(1.0, 2 * norm_sf(np.where(sigma > 0, dist / sigma, np.inf)))
    return np.where(dist <= 0, 1.0, p)


def variance_shares(returns: np.ndarray) -> np.ndarray:
    """Average share of a session's variance per bar slot. returns: (days, slots) log returns (NaN = no bar). Each day
    is normalized to sum 1 first, so a wild day weighs no more than a quiet one; days with no variance are skipped."""
    r2 = np.nan_to_num(np.asarray(returns, float)) ** 2
    tot = r2.sum(axis=1)
    ok = tot > 0
    if not ok.any():
        raise ValueError("variance_shares: no day with nonzero variance")
    return (r2[ok] / tot[ok, None]).mean(axis=0)


def remaining_share(shares: np.ndarray) -> np.ndarray:
    """remaining[i] = share of variance in slots after slot i (remaining[-1] == 0)."""
    c = np.cumsum(shares[::-1])[::-1]
    return np.append(c[1:], 0.0) / shares.sum()


def race(log_high: np.ndarray, log_low: np.ndarray, level: float, y: float, side: int) -> tuple[int, int, int]:
    """From the first bar that reaches `level` (side +1: high >= level, -1: low <= level), which comes first: a move
    back through the level by `y` (FADE) or on beyond it by `y` (EXTEND)? The touching bar counts only for EXTEND (its
    other extreme may come before the touch); a later bar reaching both is AMBIGUOUS; neither by the last bar is
    UNRESOLVED; no touch is UNTOUCHED. Returns (outcome, touch index, resolve index; -1 when none)."""
    if side < 0:
        log_high, log_low, level = -np.asarray(log_low), -np.asarray(log_high), -level
    hit = np.flatnonzero(log_high >= level)
    if not len(hit):
        return UNTOUCHED, -1, -1
    i = int(hit[0])
    if log_high[i] >= level + y:
        return EXTEND, i, i
    ext = log_high[i + 1 :] >= level + y
    fade = log_low[i + 1 :] <= level - y
    either = np.flatnonzero(ext | fade)
    if not len(either):
        return UNRESOLVED, i, -1
    j = int(either[0])
    code = AMBIGUOUS if ext[j] and fade[j] else (FADE if fade[j] else EXTEND)
    return code, i, i + 1 + j


def monthly_refits(dates: pd.DatetimeIndex, usable: np.ndarray, min_train: int) -> Iterator[tuple[pd.Timestamp, np.ndarray, np.ndarray]]:
    """Expanding walk-forward by calendar month: yields (month start, train mask, test mask). Train = usable rows dated
    before the month (at least `min_train` of them); test = the month's usable rows. Causal by construction."""
    dates = pd.DatetimeIndex(dates)
    usable = np.asarray(usable, bool)
    months = dates.to_period("M")
    for m in months.unique().sort_values():
        start = m.to_timestamp()
        train = usable & (dates < start)
        if train.sum() < min_train:
            continue
        test = usable & (months == m)
        if test.any():
            yield start, train, test


def ols(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Least-squares coefficients of y on x (x already holds the constant column)."""
    beta, *_ = np.linalg.lstsq(np.asarray(x, float), np.asarray(y, float), rcond=None)
    return beta
