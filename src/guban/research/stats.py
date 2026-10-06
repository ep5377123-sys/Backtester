"""Conditional-rate tests: does an outcome happen more often after a signal than on an average observation?

- base rate: P(hit) over every observation.
- conditional rate: P(hit | signal).
- p-value: one-sided exact binomial, P(X >= hits) for X ~ Binomial(n signal observations, base rate), i.e. how likely
  a rate this high is by chance if the signal carried no information.
- verdict: REAL only if the gap (conditional - base) >= min_gap, p < alpha, and the gap is > 0 in every period.
Standard library + numpy only.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p), exact (log-space sum)."""
    if not 0 <= p <= 1:
        raise ValueError(f"p must be in [0, 1], got {p}")
    if k <= 0:
        return 1.0
    if k > n:
        return 0.0
    if p == 0:
        return 0.0
    if p == 1:
        return 1.0
    lp, lq = math.log(p), math.log1p(-p)
    lc = math.lgamma(n + 1)
    terms = [lc - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * lp + (n - i) * lq for i in range(k, n + 1)]
    m = max(terms)
    return min(1.0, math.exp(m) * math.fsum(math.exp(t - m) for t in terms))


@dataclass(frozen=True)
class RateTest:
    label: str
    n: int  # observations with the signal
    hits: int
    rate: float  # hits / n
    base_n: int  # all observations
    base_hits: int
    base_rate: float
    gap: float  # rate - base_rate
    p_value: float  # one-sided exact binomial against base_rate

    def to_dict(self) -> dict:
        return asdict(self)


def rate_test(hit, signal, label: str = "") -> RateTest:
    """hit, signal: boolean arrays over the same observations."""
    hit, signal = np.asarray(hit, bool), np.asarray(signal, bool)
    if hit.shape != signal.shape:
        raise ValueError(f"hit and signal differ in length: {hit.shape} vs {signal.shape}")
    base_n, base_hits = int(hit.size), int(hit.sum())
    n, hits = int(signal.sum()), int((hit & signal).sum())
    base = base_hits / base_n if base_n else float("nan")
    rate = hits / n if n else float("nan")
    p = binom_sf(hits, n, base) if n and base_n else float("nan")
    return RateTest(label, n, hits, rate, base_n, base_hits, base, rate - base, p)


def by_period(hit, signal, period) -> pd.DataFrame:
    """rate_test per period (base rate taken within the period). Columns: n, hits, rate, base_n, base_rate, gap, p_value."""
    hit, signal, period = np.asarray(hit, bool), np.asarray(signal, bool), np.asarray(period)
    rows = {}
    for key in sorted(set(period.tolist())):
        m = period == key
        t = rate_test(hit[m], signal[m])
        rows[key] = {k: getattr(t, k) for k in ("n", "hits", "rate", "base_n", "base_rate", "gap", "p_value")}
    return pd.DataFrame.from_dict(rows, orient="index").rename_axis("period")


@dataclass(frozen=True)
class Verdict:
    real: bool
    checks: list[tuple[str, bool, str]]  # (rule, passed, detail)

    def to_dict(self) -> dict:
        return {"real": self.real, "checks": [{"rule": r, "passed": ok, "detail": d} for r, ok, d in self.checks]}


def verdict(test: RateTest, periods: pd.DataFrame, min_gap: float = 0.03, alpha: float = 0.05) -> Verdict:
    """The pre-registered rule: gap >= min_gap, p < alpha, and gap > 0 in every period of `periods` (by_period)."""
    neg = [str(k) for k, g in periods["gap"].items() if not g > 0]
    checks = [
        (f"gap >= {min_gap * 100:g} pp", bool(test.gap >= min_gap), f"{test.gap * 100:+.2f} pp"),
        (f"p < {alpha:g}", bool(test.p_value < alpha), f"p = {test.p_value:.4g}"),
        ("gap > 0 in every period", not neg, "all positive" if not neg else "not positive in " + ", ".join(neg)),
    ]
    return Verdict(all(ok for _, ok, _ in checks), checks)
