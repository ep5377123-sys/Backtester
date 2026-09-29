"""Bar series with PowerLanguage-style lookback: s[0] is the current bar, s[1] the bar before, and so on."""

from __future__ import annotations

from typing import Any


class Clock:
    """Current bar index shared by every series of one backtest."""

    __slots__ = ("i",)

    def __init__(self) -> None:
        self.i = -1


class Series:
    """Read-only view of a per-bar column, indexed backwards from the current bar."""

    __slots__ = ("_values", "_clock", "name")

    def __init__(self, values: list, clock: Clock, name: str = "") -> None:
        self._values = values
        self._clock = clock
        self.name = name

    def __getitem__(self, n: int) -> Any:
        if n < 0:
            raise IndexError(f"{self.name}[{n}]: negative offsets would read future bars")
        j = self._clock.i - n
        if j < 0:
            raise IndexError(
                f"{self.name}[{n}] reaches before the first bar (current bar {self._clock.i + 1}); "
                "guard lookbacks with self.current_bar (MC MaxBarsBack)"
            )
        return self._values[j]

    @property
    def value(self) -> Any:
        """The current bar's value, same as s[0]."""
        return self[0]

    def __len__(self) -> int:
        """Bars available so far (current bar included)."""
        return self._clock.i + 1

    def __repr__(self) -> str:
        cur = self._values[self._clock.i] if self._clock.i >= 0 else None
        return f"<Series {self.name} bar={self._clock.i + 1} value={cur!r}>"


class VarSeries(Series):
    """A strategy variable with lookback. It keeps last bar's value until assigned, like a PowerLanguage variable."""

    __slots__ = ()

    @Series.value.setter
    def value(self, v: Any) -> None:
        self._values[self._clock.i] = v

    def _carry(self, i: int) -> None:
        if i > 0:
            self._values[i] = self._values[i - 1]
