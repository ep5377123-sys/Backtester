"""Contract specifications, session templates and exchange calendars, loaded from config/*.toml."""

from __future__ import annotations

import math
import numbers
import tomllib
from dataclasses import dataclass
from datetime import date, time
from functools import cache
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load(name: str, config_dir: Path | None) -> dict:
    path = Path(config_dir or CONFIG_DIR) / name
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    point_value: float
    tick_size: float
    calendar: str | None = None
    commission: float = 0.0
    slippage: float = 0.0

    def __post_init__(self) -> None:
        for name in ("point_value", "tick_size", "commission", "slippage"):
            v = getattr(self, name)
            low_ok = v >= 0 if name in ("commission", "slippage") else v > 0
            if isinstance(v, bool) or not isinstance(v, numbers.Real) or not math.isfinite(v) or not low_ok:
                raise ValueError(f"contract {self.symbol}: {name} must be a finite number {'>=' if name in ('commission', 'slippage') else '>'} 0, "
                                 f"got {v!r} (check config/contracts.toml)")

    @property
    def tick_value(self) -> float:
        return self.point_value * self.tick_size

    def round_to_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 10)

    def pnl(
        self,
        entry: float,
        exit: float,
        contracts: int,
        direction: int = 1,
        commission: float = 0.0,
        slippage: float = 0.0,
    ) -> float:
        """Net dollar P&L of a round trip: points x point value x contracts - costs.

        direction: 1 long, -1 short. commission and slippage are USD per contract per round trip.
        """
        if direction not in (1, -1):
            raise ValueError(f"direction must be 1 or -1, got {direction}")
        gross = (exit - entry) * direction * self.point_value * contracts
        return gross - (commission + slippage) * contracts


@dataclass(frozen=True)
class Session:
    key: str
    name: str
    start: time
    end: time

    @property
    def overnight(self) -> bool:
        return self.end <= self.start

    @property
    def start_min(self) -> int:
        return self.start.hour * 60 + self.start.minute

    @property
    def end_min(self) -> int:
        return self.end.hour * 60 + self.end.minute

    @property
    def length_min(self) -> int:
        return (self.end_min - self.start_min) % 1440 or 1440


@cache
def _contracts(config_dir: Path | None) -> dict[str, ContractSpec]:
    return {sym: ContractSpec(symbol=sym, **v) for sym, v in _load("contracts.toml", config_dir).items()}


@cache
def _sessions(config_dir: Path | None) -> dict[str, Session]:
    return {
        key: Session(key=key, name=v["name"], start=time.fromisoformat(v["start"]), end=time.fromisoformat(v["end"]))
        for key, v in _load("sessions.toml", config_dir).items()
    }


def get_contract(symbol: str, config_dir: Path | None = None) -> ContractSpec:
    specs = _contracts(config_dir)
    if symbol.upper() not in specs:
        raise KeyError(f"Unknown symbol {symbol!r}; known: {', '.join(specs)} (edit config/contracts.toml)")
    return specs[symbol.upper()]


def get_session(key: str, config_dir: Path | None = None) -> Session:
    sessions = _sessions(config_dir)
    if key.lower() not in sessions:
        raise KeyError(f"Unknown session {key!r}; known: {', '.join(sessions)} (edit config/sessions.toml)")
    return sessions[key.lower()]


@cache
def _calendars(config_dir: Path | None) -> dict[str, dict[date, time]]:
    out = {}
    for key, v in _load("calendar.toml", config_dir).items():
        closes = {}
        for d, t in v.get("early_close", {}).items():
            try:
                closes[date.fromisoformat(d)] = time.fromisoformat(t)
            except (TypeError, ValueError) as e:
                raise ValueError(f"config/calendar.toml [{key}.early_close]: bad entry {d!r} = {t!r}; use \"YYYY-MM-DD\" = \"HH:MM\"") from e
        out[key] = closes
    return out


def get_early_closes(calendar: str | None, config_dir: Path | None = None) -> dict[date, time]:
    """Scheduled early closes of exchange calendar `calendar`: trading date -> Eastern halt time ({} for None)."""
    if calendar is None:
        return {}
    calendars = _calendars(config_dir)
    if calendar not in calendars:
        raise KeyError(f"Unknown calendar {calendar!r}; known: {', '.join(calendars)} (edit config/calendar.toml)")
    return calendars[calendar]
