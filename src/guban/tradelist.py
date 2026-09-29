"""Trade list in MultiCharts' trade-export layout (the TradesToCSV CSV that guban.parity reads).

One row per trade, MC's 17 columns first, with MC's value conventions (checked against Vault Break MNQ.csv):
- order_id: MC numbers orders in sequence, entry then exit, so a trade's entry order is 2 x trade_id - 1.
- type "EntryLong" / "EntryShort", direction "LONG" / "SHORT", signal = the entry order's name.
- excel_date and excel_time both hold the full Excel serial date-time of the entry, in chart time.
- price = entry price; profit_usd, runup_usd, drawdown_usd are net of costs (run-up/drawdown carry only the
  entry-side costs, see guban.engine.backtest).
- profit_pct, runup_pct, drawdown_pct divide by the entry notional (price x point value x contracts);
  cum_profit_pct divides by the account's initial capital (the MC run used $1,000,000).
MC's own file wraps every line in quotes; ours does not (guban.parity reads both).

The engine's columns follow MC's, so nothing is lost: entry_time and exit_time (ISO, with UTC offset),
exit_price, exit_name, gross_pnl, commission, slippage, entry_bar, exit_bar, ambiguous.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

MC_TRADE_COLUMNS = [
    "trade_id", "order_id", "type", "signal", "excel_date", "excel_time", "price", "contracts",
    "profit_usd", "profit_pct", "cum_profit_usd", "cum_profit_pct",
    "runup_usd", "runup_pct", "drawdown_usd", "drawdown_pct", "direction",
]  # fmt: skip
ENGINE_COLUMNS = ["entry_time", "exit_time", "exit_price", "exit_name", "gross_pnl", "commission", "slippage", "entry_bar", "exit_bar", "ambiguous"]
EXCEL_EPOCH = pd.Timestamp("1899-12-30")

# MC-layout column -> engine trade column (guban.engine.backtest.TRADE_COLUMNS)
TO_ENGINE = {
    "trade_id": "trade_no", "contracts": "qty", "price": "entry_price", "signal": "entry_name",
    "profit_usd": "net_pnl", "cum_profit_usd": "cum_net_pnl", "runup_usd": "runup", "drawdown_usd": "drawdown",
}  # fmt: skip


def excel_serial(times: pd.Series) -> pd.Series:
    """Excel serial date-time of each timestamp's wall-clock time in its own zone."""
    wall = times.dt.tz_localize(None) if times.dt.tz is not None else times
    return ((wall - EXCEL_EPOCH) / pd.Timedelta(days=1)).round(10)


def to_mc_layout(trades: pd.DataFrame, point_value: float, capital: float = 1_000_000.0) -> pd.DataFrame:
    """Engine trades (BacktestResult.trades) -> MC trade-export columns followed by ENGINE_COLUMNS."""
    t = trades
    long_ = t["direction"] == "long"
    notional = t["entry_price"] * point_value * t["qty"]
    serial = excel_serial(t["entry_time"]) if len(t) else pd.Series(dtype=float)
    out = pd.DataFrame({
        "trade_id": t["trade_no"],
        "order_id": 2 * t["trade_no"] - 1,
        "type": np.where(long_, "EntryLong", "EntryShort"),
        "signal": t["entry_name"],
        "excel_date": serial,
        "excel_time": serial,
        "price": t["entry_price"],
        "contracts": t["qty"],
        "profit_usd": _usd(t["net_pnl"]),
        "profit_pct": t["net_pnl"] / notional,
        "cum_profit_usd": _usd(t["cum_net_pnl"]),
        "cum_profit_pct": t["cum_net_pnl"] / capital,
        "runup_usd": _usd(t["runup"]),
        "runup_pct": t["runup"] / notional,
        "drawdown_usd": _usd(t["drawdown"]),
        "drawdown_pct": t["drawdown"] / notional,
        "direction": np.where(long_, "LONG", "SHORT"),
    })  # fmt: skip
    for col in ENGINE_COLUMNS:
        out[col] = _usd(t[col]) if col in ("gross_pnl", "commission", "slippage") else t[col]
    return out


def from_mc_layout(df: pd.DataFrame) -> pd.DataFrame:
    """A trades.csv written by to_mc_layout (already read) -> engine trade columns."""
    missing = {"entry_time", "exit_time", "exit_price"} - set(df.columns)
    if missing:
        raise ValueError(f"MC-layout trade list without the engine columns {sorted(missing)}; use parity.load_mc_trades for MC exports")
    out = df.rename(columns=TO_ENGINE)
    out["direction"] = out["direction"].str.lower()
    return out


def _usd(s: pd.Series) -> pd.Series:
    return s.astype(float).round(6)
