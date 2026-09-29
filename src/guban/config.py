"""Contract specifications and session templates, loaded from config/*.toml."""

from __future__ import annotations

import math
import numbers
import tomllib
from dataclasses import dataclass
from datetime import time
from functools import cache
from pathlib import Path

# src/guban/config.py -> repo root / config
CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


def _load(name: str, config_dir: Path | None) -> dict:
    path = Path(config_dir or CONFIG_DIR) / name
    with open(path, "rb") as f:
        return tomllib.load(f)


@dataclass(frozen=True)
class ContractSpec:
    symbol: str
    name: str
    point_value: float  # USD per 1.0 point per contract
    tick_size: float

    def __post_init__(self) -> None:
        for name in ("point_value", "tick_size"):
            v = getattr(self, name)
            if isinstance(v, bool) or not isinstance(v, numbers.Real) or not math.isfinite(v) or v <= 0:
                raise ValueError(f"contract {self.symbol}: {name} must be a finite number > 0, got {v!r} (check config/contracts.toml)")

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
