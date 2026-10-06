"""Volume profiles: volume per price tick, with the POC and the value area.

- From trades (exact): each trade's size at its price.
- From 1-minute bars (approximate): each bar's volume spread evenly over every tick from its low to its high.
POC = the price with the most volume; ties go to the price nearest the middle of the profile's range (then the lower
one). Value area = grown one tick at a time from the POC, adding the side (above or below) with more volume (above
on a tie), until it holds `pct` of the volume.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Profile:
    base: int  # lowest price, in ticks
    volume: np.ndarray  # volume per tick from `base` up
    tick_size: float

    def price(self, i: int) -> float:
        return round((self.base + int(i)) * self.tick_size, 10)

    @property
    def total(self) -> float:
        return float(self.volume.sum())

    @property
    def low(self) -> float:
        return self.price(0)

    @property
    def high(self) -> float:
        return self.price(len(self.volume) - 1)

    def poc_index(self) -> int:
        v = self.volume
        top = np.flatnonzero(v == v.max())
        if top.size == 1:
            return int(top[0])
        mid = (len(v) - 1) / 2
        return int(top[np.argmin(np.abs(top - mid))])  # argmin keeps the first (lower) of two equal distances

    def poc(self) -> float:
        return self.price(self.poc_index())

    def value_area(self, pct: float = 0.70) -> tuple[float, float]:
        """(value area low, value area high)."""
        v, n = self.volume, len(self.volume)
        lo = hi = self.poc_index()
        acc, target = float(v[lo]), pct * self.total
        while acc < target and (lo > 0 or hi < n - 1):
            up = v[hi + 1] if hi < n - 1 else -1.0
            dn = v[lo - 1] if lo > 0 else -1.0
            if up >= dn:
                hi += 1
                acc += up
            else:
                lo -= 1
                acc += dn
        return self.price(lo), self.price(hi)


def from_volume(price_tick: np.ndarray, volume: np.ndarray, tick_size: float) -> Profile:
    """A profile from (price in ticks, volume) pairs; repeated prices are summed."""
    idx = np.asarray(price_tick, np.int64)
    if idx.size == 0:
        raise ValueError("empty profile: no volume")
    base = int(idx.min())
    return Profile(base, np.bincount(idx - base, weights=np.asarray(volume, float)), tick_size)


def from_trades(prices: np.ndarray, sizes: np.ndarray, tick_size: float) -> Profile:
    return from_volume(np.rint(np.asarray(prices, float) / tick_size), sizes, tick_size)


def from_bars(low: np.ndarray, high: np.ndarray, volume: np.ndarray, tick_size: float) -> Profile:
    """Each bar's volume spread evenly over its ticks low..high (inclusive)."""
    lo = np.rint(np.asarray(low, float) / tick_size).astype(np.int64)
    hi = np.rint(np.asarray(high, float) / tick_size).astype(np.int64)
    vol = np.asarray(volume, float)
    keep = vol > 0
    lo, hi, vol = lo[keep], hi[keep], vol[keep]
    if lo.size == 0:
        raise ValueError("empty profile: no volume")
    base, top = int(lo.min()), int(hi.max())
    per_tick = vol / (hi - lo + 1)
    diff = np.zeros(top - base + 2)
    np.add.at(diff, lo - base, per_tick)
    np.add.at(diff, hi - base + 1, -per_tick)
    return Profile(base, np.round(np.cumsum(diff)[:-1], 9), tick_size)  # rounding drops cumsum noise, so ties stay ties
