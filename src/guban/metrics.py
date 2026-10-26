"""Performance metrics: the core of MC's Strategy Performance Report (R6).

Trade figures are computed from net trade P&L (after commission and slippage), as MC does: gross profit is
the sum of the winning trades' net profit, gross loss the sum of the losing trades'. A trade with zero net
is "even": neither a winner nor a loser, and it ends a winning or losing streak.

Drawdowns (always <= 0):
- close-to-close: peak-to-valley of closed-trade equity (cumulative net P&L after each trade exit).
- intraday peak-to-valley: the deepest drop of equity below its running peak (from 0), with equity followed
  point by point along the intrabar path (closed net + open gross). Per bar it is the deeper of the bar's worst
  equity (BacktestResult.equity_low) minus the highest equity any earlier bar reached (equity_high), and the
  drop below a peak reached earlier inside the bar itself (bar_drawdown). So $0 -> +$10 -> -$10 in one bar is
  -$20. Without the intrabar series it falls back to bar-close equity. Return on account = net profit /
  |intraday max drawdown| (MC's definition).
Sharpe (daily): mean / standard deviation (ddof 1) of the daily change in equity at each session's last bar,
times sqrt(252), with no risk-free rate. Every session in the data counts, traded or not.
Sortino (daily): the same mean over the downside deviation (root mean square of the losing days, target 0, every
session counted), times sqrt(252).
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
    ("avg_r_multiple", "Avg trade R multiple (net ÷ risk to the initial stop)", "ratio"),
    ("avg_planned_rr", "Avg planned reward : risk (target ÷ stop at entry)", "ratio"),
    ("trades_with_stop", "Trades with a stop (in the R figures)", "int"),
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
    ("sortino_daily", "Sortino ratio (daily, annualized)", "ratio"),
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
    nan = np.full(n, np.nan)
    r = trades["r_multiple"].to_numpy(float) if "r_multiple" in trades else nan  # Phase 17; absent in MC exports
    rr = trades["planned_rr"].to_numpy(float) if "planned_rr" in trades else nan
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
        "avg_r_multiple": float(np.nanmean(r)) if np.isfinite(r).any() else None,
        "avg_planned_rr": float(np.nanmean(rr)) if np.isfinite(rr).any() else None,
        "trades_with_stop": int(np.isfinite(r).sum()),
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


def intraday_drawdown(
    equity: pd.Series,
    equity_low: pd.Series | None = None,
    equity_high: pd.Series | None = None,
    bar_drawdown: pd.Series | None = None,
) -> pd.Series:
    """Per-bar peak-to-valley drawdown (<= 0): min(bar's worst equity - highest equity of the earlier bars (from 0),
    drop inside the bar below its own earlier peak, 0). Missing intrabar series fall back to bar-close equity."""
    eq = equity.to_numpy(float)
    low = eq if equity_low is None else np.minimum(equity_low.to_numpy(float), eq)
    high = eq if equity_high is None else np.maximum(equity_high.to_numpy(float), eq)
    peak_before = np.maximum(np.maximum.accumulate(np.r_[0.0, high[:-1]]), 0.0) if len(eq) else eq
    dd = np.minimum(low - peak_before, 0.0)
    if bar_drawdown is not None:
        dd = np.minimum(dd, bar_drawdown.to_numpy(float))
    return pd.Series(dd, index=equity.index, name="drawdown")


def session_keys(index: pd.DatetimeIndex, session_dates=None) -> pd.DatetimeIndex:
    """Session date of each bar: `session_dates` when given, else the bar's calendar date."""
    return pd.DatetimeIndex(pd.to_datetime(np.asarray(index.date if session_dates is None else session_dates)))


def daily_equity(equity: pd.Series, session_dates=None) -> pd.Series:
    """Equity at each session's last bar, indexed by session date."""
    return equity.groupby(session_keys(equity.index, session_dates)).last()


def downside_deviation(pnl: np.ndarray) -> float:
    """Root mean square of the losses (gains count as 0) over every period: the Sortino denominator, target 0."""
    return float(np.sqrt(np.mean(np.minimum(pnl, 0.0) ** 2))) if len(pnl) else 0.0


def compute_metrics(
    trades: pd.DataFrame,
    equity: pd.Series,
    equity_low: pd.Series | None = None,
    session_dates=None,
    open_position_pnl: float = 0.0,
    equity_high: pd.Series | None = None,
    bar_drawdown: pd.Series | None = None,
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
    dd = intraday_drawdown(equity, equity_low, equity_high, bar_drawdown)
    j = int(np.argmin(dd.to_numpy())) if len(dd) else 0
    max_dd = float(dd.iat[j]) if len(dd) else 0.0
    daily = daily_equity(equity, session_dates)
    pnl = daily.diff().fillna(daily.iloc[0]).to_numpy(float) if len(daily) else np.array([])
    sd = float(pnl.std(ddof=1)) if len(pnl) > 1 else 0.0
    down = downside_deviation(pnl)
    net = out["all"]["net_profit"]
    out["account"] = {
        "max_drawdown_intraday": max_dd,
        "max_drawdown_intraday_time": dd.index[j] if max_dd < 0 else None,
        "max_drawdown_close_to_close": float(ctc[k]),
        "max_drawdown_close_to_close_time": trades["exit_time"].iat[k - 1] if ctc[k] < 0 else None,
        "return_on_account": net / -max_dd if max_dd < 0 else None,
        "sharpe_daily": float(pnl.mean() / sd * math.sqrt(252)) if sd > 0 else None,
        "sortino_daily": float(pnl.mean() / down * math.sqrt(252)) if down > 0 else None,
        "trading_days": int(len(daily)),
        "open_position_pnl": float(open_position_pnl),
    }
    return out


def backtest_metrics(result, bars: pd.DataFrame) -> dict:
    """compute_metrics for a BacktestResult run on `bars` (the frame passed to run_backtest)."""
    open_pnl = result.equity.iat[-1] - result.net_profit if result.open_position is not None else 0.0
    sessions = bars["session_date"] if "session_date" in bars.columns else None
    return compute_metrics(
        result.trades, result.equity, result.equity_low, sessions, open_pnl,
        equity_high=getattr(result, "equity_high", None), bar_drawdown=getattr(result, "bar_drawdown", None),
    )  # fmt: skip


def window_metrics(result, bars: pd.DataFrame, lo: int, hi: int) -> dict:
    """compute_metrics for the trading window [lo, hi) of a run on `bars` made with trade_from=lo and hi in
    window_ends: flat at the window's start, and a position still open at its end closed at bar hi-1's close
    ("Window End"). Equals backtest_metrics of run_backtest(..., bars[:hi], trade_from=lo, flatten_at_end=True) over
    those bars, so nothing after the window's end (not even an open trade's eventual exit) reaches the score."""
    return backtest_metrics(result.window(lo, hi), bars.iloc[lo:hi])


def empty_metrics() -> dict:
    """compute_metrics of a window with no bars: no trades, no drawdown."""
    from guban.engine.backtest import TRADE_COLUMNS

    return compute_metrics(pd.DataFrame(columns=TRADE_COLUMNS), pd.Series([], index=pd.DatetimeIndex([], tz="UTC"), dtype=float))


def contract_rolls(bars: pd.DataFrame) -> list[tuple[pd.Timestamp, str, str]]:
    """(first bar, from contract, to contract) of every contract switch in `bars` (none without a contract column)."""
    if "contract" not in bars.columns or not len(bars):
        return []
    c = bars["contract"].astype(str).to_numpy()
    return [(bars.index[k], c[k - 1], c[k]) for k in np.flatnonzero(c[1:] != c[:-1]) + 1]


def held_across_roll(trades: pd.DataFrame, bars: pd.DataFrame) -> pd.Series:
    """Per trade: True when a contract switch lies between its entry and exit bars (bar numbers index `bars`). The
    continuous series is unadjusted, so such a trade's P&L includes the price gap between the two contracts."""
    if "contract" not in bars.columns or not len(trades):
        return pd.Series(False, index=trades.index, dtype=bool)
    c = bars["contract"].astype(str).to_numpy()
    roll_no = np.r_[0, np.cumsum(c[1:] != c[:-1])]
    entry, exit_ = trades["entry_bar"].to_numpy(int), trades["exit_bar"].to_numpy(int)
    return pd.Series(roll_no[exit_] != roll_no[entry], index=trades.index)


def roll_summary(trades: pd.DataFrame, bars: pd.DataFrame) -> dict:
    """{"count", "rolls": [{"at", "from", "to"}], "trades_held_across": n, "trade_nos": [...]} for metrics.json."""
    held = held_across_roll(trades, bars)
    return {
        "count": len(rolls := contract_rolls(bars)),
        "rolls": [{"at": t, "from": a, "to": b} for t, a, b in rolls],
        "trades_held_across": int(held.sum()),
        "trade_nos": [int(n) for n in trades.loc[held, "trade_no"]] if len(trades) else [],
    }


ROLL_NOTE = ("{n} trade(s) were held across a contract roll. The continuous series is unadjusted, so the price gap "
             "between the two contracts is part of their P&L: {nos}.")


def data_gaps(bars: pd.DataFrame) -> list[int]:
    """Bar numbers k where strategy bars are missing just before bar k: bars k-1 and k are in one session but more
    than one bar length apart (bar length: the most common spacing inside sessions), or bar k-1 is the last bar of a
    session that is scheduled to close more than one bar length later (bars' session_close column). A scheduled early
    close is not a gap. Without a session_date column every pair of bars counts as one session."""
    n = len(bars)
    if n < 2:
        return []
    t = bars.index.as_unit("ns").asi8
    step = np.diff(t)
    sess = pd.to_datetime(bars["session_date"]).to_numpy() if "session_date" in bars.columns else np.zeros(n)
    same = sess[1:] == sess[:-1]
    if not same.any():
        return []
    bar_len = pd.Series(step[same]).mode().iat[0]
    gap = same & (step > bar_len)
    if "session_close" in bars.columns:
        close = pd.DatetimeIndex(bars["session_close"]).as_unit("ns").asi8
        gap |= ~same & (close[:-1] - t[:-1] > bar_len)  # the session's data stopped before its scheduled close
    return (np.flatnonzero(gap) + 1).tolist()


def session_exposure(trades: pd.DataFrame, bars: pd.DataFrame) -> dict:
    """Trades held past the scheduled close of the session they were entered in (bars' session_close; they carry the
    reopen gap), and trades held across a data gap (data_gaps), for metrics.json: {"trades_held_past_close",
    "past_close_trade_nos", "data_gaps": [{"after", "before"}], "trades_held_across_gap", "gap_trade_nos"}."""
    gaps = data_gaps(bars)
    past = across = pd.Series(False, index=trades.index, dtype=bool)
    if len(trades):
        entry, exit_ = trades["entry_bar"].to_numpy(int), trades["exit_bar"].to_numpy(int)
        if "session_close" in bars.columns:
            close = pd.DatetimeIndex(bars["session_close"])[entry]
            past = pd.Series(np.asarray(pd.DatetimeIndex(trades["exit_time"]) > close), index=trades.index)
        if gaps:
            k = np.asarray(gaps)
            across = pd.Series([bool(((e < k) & (k <= x)).any()) for e, x in zip(entry, exit_)], index=trades.index)
    nos = trades["trade_no"] if len(trades) else pd.Series([], dtype=int)
    return {
        "trades_held_past_close": int(past.sum()),
        "past_close_trade_nos": [int(n) for n in nos[past]],
        "data_gaps": [{"after": bars.index[k - 1], "before": bars.index[k]} for k in gaps],
        "trades_held_across_gap": int(across.sum()),
        "gap_trade_nos": [int(n) for n in nos[across]],
    }


PAST_CLOSE_NOTE = ("{n} trade(s) were held past the scheduled close of the session they were entered in (the session "
                   "template's end, or an early close in config/calendar.toml), through the reopen gap: {nos}.")
GAP_NOTE = ("{n} trade(s) were held across missing bars (a data gap inside a session, or a session whose data stops "
            "before its scheduled close); the strategy could not act until data resumed: {nos}.")


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
