"""Performance metrics: the core of MC's Strategy Performance Report (R6).

Trade figures are computed from net trade P&L (after commission and slippage), as MC does: gross profit is
the sum of the winning trades' net profit, gross loss the sum of the losing trades'. A trade with zero net
is "even": neither a winner nor a loser, and it ends a winning or losing streak.

Drawdowns (always <= 0):
- close-to-close: peak-to-valley of closed-trade equity (cumulative net P&L after each trade exit).
- intraday peak-to-valley: the peak is the highest bar-close equity before the bar, the valley is the bar's
  worst equity along the intrabar path (BacktestResult.equity_low: closed net + open gross at the adverse
  extreme). Return on account = net profit / |intraday max drawdown| (MC's definition).
Sharpe (daily): mean / standard deviation (ddof 1) of the daily change in equity at each session's last bar,
times sqrt(252), with no risk-free rate. Every session in the data counts, traded or not.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADE_STATS = [
    ("net_profit", "Total net profit", "usd"),
    ("gross_profit", "Gross profit", "usd"),
    ("gross_loss", "Gross loss", "usd"),
    ("profit_factor", "Profit factor", "ratio"),
    ("total_trades", "Total number of trades", "int"),
    ("percent_profitable", "Percent profitable", "pct"),
    ("winning_trades", "Winning trades", "int"),
    ("losing_trades", "Losing trades", "int"),
    ("even_trades", "Even trades", "int"),
    ("avg_trade", "Avg trade net profit", "usd"),
    ("avg_winning_trade", "Avg winning trade", "usd"),
    ("avg_losing_trade", "Avg losing trade", "usd"),
    ("ratio_avg_win_avg_loss", "Ratio avg win : avg loss", "ratio"),
    ("largest_winning_trade", "Largest winning trade", "usd"),
    ("largest_losing_trade", "Largest losing trade", "usd"),
    ("max_consecutive_winners", "Max consecutive winning trades", "int"),
    ("max_consecutive_losers", "Max consecutive losing trades", "int"),
    ("avg_bars_in_trade", "Avg bars in trade", "num"),
    ("total_commission", "Total commission", "usd"),
    ("total_slippage", "Total slippage", "usd"),
    ("ambiguous_fills", "Ambiguous fills", "int"),
]  # (key, label, format)
ACCOUNT_STATS = [
    ("max_drawdown_intraday", "Max strategy drawdown (intraday peak to valley)", "usd"),
    ("max_drawdown_intraday_time", "  reached at", "time"),
    ("max_drawdown_close_to_close", "Max close-to-close drawdown (closed trades)", "usd"),
    ("max_drawdown_close_to_close_time", "  reached at", "time"),
    ("return_on_account", "Return on account", "pct"),
    ("sharpe_daily", "Sharpe ratio (daily, annualized)", "ratio"),
    ("trading_days", "Trading days", "int"),
    ("open_position_pnl", "Open position P/L", "usd"),
]


def trade_stats(trades: pd.DataFrame) -> dict:
    """MC trade statistics for one set of trades (engine trade columns)."""
    net = trades["net_pnl"].to_numpy(float)
    n = len(net)
    win, loss = net > 0, net < 0
    gp, gl = float(net[win].sum()), float(net[loss].sum())
    avg_win = float(net[win].mean()) if win.any() else 0.0
    avg_loss = float(net[loss].mean()) if loss.any() else 0.0
    return {
        "net_profit": float(net.sum()),
        "gross_profit": gp,
        "gross_loss": gl,
        "profit_factor": gp / -gl if gl else (math.inf if gp else None),
        "total_trades": n,
        "percent_profitable": float(win.sum() / n) if n else 0.0,
        "winning_trades": int(win.sum()),
        "losing_trades": int(loss.sum()),
        "even_trades": int(n - win.sum() - loss.sum()),
        "avg_trade": float(net.mean()) if n else 0.0,
        "avg_winning_trade": avg_win,
        "avg_losing_trade": avg_loss,
        "ratio_avg_win_avg_loss": avg_win / -avg_loss if avg_loss else (math.inf if avg_win else None),
        "largest_winning_trade": float(net.max()) if win.any() else 0.0,
        "largest_losing_trade": float(net.min()) if loss.any() else 0.0,
        "max_consecutive_winners": _longest_run(win),
        "max_consecutive_losers": _longest_run(loss),
        "avg_bars_in_trade": float((trades["exit_bar"] - trades["entry_bar"]).mean()) if n else 0.0,
        "total_commission": float(trades["commission"].sum()),
        "total_slippage": float(trades["slippage"].sum()),
        "ambiguous_fills": int(trades["ambiguous"].sum()) if n else 0,
    }


def _longest_run(flags: np.ndarray) -> int:
    best = run = 0
    for f in flags:
        run = run + 1 if f else 0
        best = max(best, run)
    return best


def intraday_drawdown(equity: pd.Series, equity_low: pd.Series | None = None) -> pd.Series:
    """Per-bar drawdown (<= 0): the bar's worst equity minus the highest bar-close equity before it (from 0)."""
    eq = equity.to_numpy(float)
    low = eq if equity_low is None else np.minimum(equity_low.to_numpy(float), eq)
    peak_before = np.maximum.accumulate(np.r_[0.0, eq[:-1]]) if len(eq) else eq
    peak_before = np.maximum(peak_before, 0.0)
    return pd.Series(np.minimum(low - peak_before, 0.0), index=equity.index, name="drawdown")


def session_keys(index: pd.DatetimeIndex, session_dates=None) -> pd.DatetimeIndex:
    """Session date of each bar: `session_dates` when given, else the bar's calendar date."""
    return pd.DatetimeIndex(pd.to_datetime(np.asarray(index.date if session_dates is None else session_dates)))


def daily_equity(equity: pd.Series, session_dates=None) -> pd.Series:
    """Equity at each session's last bar, indexed by session date."""
    return equity.groupby(session_keys(equity.index, session_dates)).last()


def compute_metrics(
    trades: pd.DataFrame,
    equity: pd.Series,
    equity_low: pd.Series | None = None,
    session_dates=None,
    open_position_pnl: float = 0.0,
) -> dict:
    """{"all", "long", "short": trade_stats, "account": drawdowns, return on account, Sharpe, ...}."""
    out = {
        "all": trade_stats(trades),
        "long": trade_stats(trades[trades["direction"] == "long"]),
        "short": trade_stats(trades[trades["direction"] == "short"]),
    }
    closed = np.r_[0.0, trades["cum_net_pnl"].to_numpy(float)]
    ctc = closed - np.maximum.accumulate(closed)
    k = int(np.argmin(ctc))
    dd = intraday_drawdown(equity, equity_low)
    j = int(np.argmin(dd.to_numpy())) if len(dd) else 0
    max_dd = float(dd.iat[j]) if len(dd) else 0.0
    daily = daily_equity(equity, session_dates)
    pnl = daily.diff().fillna(daily.iloc[0]).to_numpy(float) if len(daily) else np.array([])
    sd = float(pnl.std(ddof=1)) if len(pnl) > 1 else 0.0
    net = out["all"]["net_profit"]
    out["account"] = {
        "max_drawdown_intraday": max_dd,
        "max_drawdown_intraday_time": dd.index[j] if max_dd < 0 else None,
        "max_drawdown_close_to_close": float(ctc[k]),
        "max_drawdown_close_to_close_time": trades["exit_time"].iat[k - 1] if ctc[k] < 0 else None,
        "return_on_account": net / -max_dd if max_dd < 0 else None,
        "sharpe_daily": float(pnl.mean() / sd * math.sqrt(252)) if sd > 0 else None,
        "trading_days": int(len(daily)),
        "open_position_pnl": float(open_position_pnl),
    }
    return out


def jsonable(obj):
    """Metrics (or any nested dict/list) -> JSON-safe values: inf/NaN -> None, timestamps -> ISO strings."""
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return float(obj) if math.isfinite(obj) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj
