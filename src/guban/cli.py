import contextlib
import functools
import os
import re
import sys
import time
import traceback
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pandas as pd
import typer

from guban.data import import_store
from guban.data.resample import cache_path, load_bars, parse_timeframe
from guban.optimize import MAX_COMBINATIONS, DataSpec, _coerce

REPORTS = Path("reports")

app = typer.Typer(
    help="Guban Backtester — CME futures backtesting with MultiCharts parity.",
    epilog="Workflow: import → run → compare → optimize → wfo → propfirm (see README.md). "
    "Exit codes: 0 done, 1 compare FAIL, 2 error (bad input, missing data, strategy exception). "
    "Set GUBAN_DEBUG=1 for full tracebacks.",
    no_args_is_help=True,
)

# ---------------------------------------------------------------------------
# Errors: known failures print one "Error: ..." line (plus the strategy's own frames when a strategy raised) and exit 2.

_EXPECTED = (KeyError, ValueError, OSError, RuntimeError)


def _in_library(filename: str) -> bool:
    path = Path(filename).resolve()
    roots = [Path(__file__).resolve().parent, Path(sys.base_prefix).resolve(), Path(sys.prefix).resolve()]
    return "site-packages" in path.parts or any(path.is_relative_to(r) for r in roots)


_FRAME = re.compile(r'^  File "(?P<file>.+?)", line \d+, in .+\n(?:    .*\n)?', re.M)


def _strategy_frames(e: BaseException | None) -> list[str]:
    """Formatted frames of the strategy's own code (outside guban, the stdlib and site-packages) behind a
    StrategyError in e's cause chain — also when it was raised in an optimize/wfo worker process, whose
    traceback arrives only as text (concurrent.futures' _RemoteTraceback)."""
    from guban.engine import StrategyError

    while e is not None:
        if isinstance(e, StrategyError) and e.__cause__ is not None and e.__cause__.__traceback__ is not None:
            frames = traceback.extract_tb(e.__cause__.__traceback__)
            return traceback.format_list([f for f in frames if not _in_library(f.filename)])
        remote = getattr(e, "tb", None)
        if isinstance(remote, str) and "StrategyError" in remote:
            return list(dict.fromkeys(m.group(0) for m in _FRAME.finditer(remote) if not _in_library(m["file"])))
        e = e.__cause__ or e.__context__
    return []


def _message(e: BaseException) -> str:
    return str(e.args[0]) if isinstance(e, KeyError) and e.args else str(e)  # str(KeyError) adds quotes


def _friendly(fn):
    """Turn the expected failures of a command into a short error message and exit code 2 (GUBAN_DEBUG=1: raise)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _EXPECTED as e:
            if isinstance(e, (typer.Exit, typer.Abort)) or os.environ.get("GUBAN_DEBUG"):
                raise
            typer.echo(f"Error: {_message(e)}", err=True)
            frames = _strategy_frames(e)
            if frames:
                typer.echo("Strategy traceback (most recent call last):", err=True)
                typer.echo("".join(frames).rstrip(), err=True)
                typer.echo("The run stopped there; no results were written.", err=True)
            typer.echo("(Set GUBAN_DEBUG=1 for the full traceback.)", err=True)
            raise typer.Exit(2) from e

    return wrapper


# ---------------------------------------------------------------------------
# Shared options: run, optimize and wfo take the same data, cost and strategy flags with the same meaning.

DATA, COSTS, STRAT, SEARCH, WF, MC, OUTPUT = "Data", "Costs", "Strategy", "Optimization", "Walk-forward", "Monte Carlo", "Output"


def _check_tz(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise typer.BadParameter(f"unknown time zone {value!r}; use an IANA name such as America/Chicago") from e
    return value


def _check_date(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        pd.Timestamp(value)
    except ValueError as e:
        raise typer.BadParameter(f"{value!r} is not a date; use YYYY-MM-DD") from e
    return value


def _costs(spec, commission: float | None, slippage: float | None) -> tuple[float, float, str]:
    """(commission, slippage, where they came from): a cost flag that was not given takes the contract's default."""
    c = spec.commission if commission is None else commission
    s = spec.slippage if slippage is None else slippage
    if commission is None and slippage is None:
        source = "contract default (config/contracts.toml)"
    elif commission is not None and slippage is not None:
        source = "flags"
    else:
        source = "--commission; slippage: contract default" if slippage is None else "--slippage; commission: contract default"
    return c, s, source


STRATEGY_FILE = typer.Argument(..., exists=True, dir_okay=False, help="Python file defining a Strategy subclass")
SYMBOL = typer.Option(..., "--symbol", help="Imported data symbol, e.g. NQ or MNQ", rich_help_panel=DATA)
CONTRACT = typer.Option(None, "--contract", help="Contract spec for P&L (default: --symbol), e.g. MNQ", rich_help_panel=DATA)
TF = typer.Option("5m", "--tf", help="Strategy timeframe: 1m, 5m, 15m, 1h, 1d ...", rich_help_panel=DATA)
SESSION = typer.Option("rth", "--session", help="Session template from config/sessions.toml (MC parity: eth)", rich_help_panel=DATA)
HISTORY_FROM = typer.Option(None, "--history-from", help="Prepend this symbol's bars from before --symbol's first bar (e.g. NQ for MNQ)", rich_help_panel=DATA)
DATA_FROM = typer.Option(None, "--data-from", callback=_check_date, help="Drop bars before this date, YYYY-MM-DD in the chart time zone: the warm-up starts there (MC: the chart's first date)", rich_help_panel=DATA)
TZ = typer.Option("America/New_York", "--tz", callback=_check_tz, help="Chart time zone the strategy sees (MC exchange time: America/Chicago)", rich_help_panel=DATA)
FILL_MODE = typer.Option("magnifier", "--fill-mode", help="magnifier (1-minute sub-bars) or ohlc (path rule on the whole bar)", rich_help_panel=DATA)
DATA_DIR = typer.Option(Path("data"), "--data-dir", help="Folder holding the imported Parquet store", rich_help_panel=DATA)
COMMISSION = typer.Option(None, "--commission", help="USD per contract per round trip (default: the contract's, config/contracts.toml)", rich_help_panel=COSTS)
SLIPPAGE = typer.Option(None, "--slippage", help="USD per contract per round trip, half per market/stop fill, none on limit fills (default: the contract's, 1 tick)", rich_help_panel=COSTS)
SET_INPUTS = typer.Option([], "--set", help="Input override name=value (repeatable); with --grid, a fixed input", rich_help_panel=STRAT)
CLASS_NAME = typer.Option(None, "--class", help="Strategy class, when the file defines several", rich_help_panel=STRAT)
GRID = typer.Option(..., "--grid", help="Input range name=start:stop[:step], name=a,b,c or name=v (repeatable; several per flag, space-separated)", rich_help_panel=SEARCH)
MIN_TRADES = typer.Option(30, "--min-trades", help="Rank only runs with at least this many trades (wfo: in the in-sample window)", rich_help_panel=SEARCH)
WORKERS = typer.Option(None, "--workers", help="Worker processes (default: CPU cores)", rich_help_panel=SEARCH)
YES = typer.Option(False, "--yes", help=f"Run grids larger than {MAX_COMBINATIONS:,} combinations", rich_help_panel=SEARCH)
CAPITAL = typer.Option(1_000_000.0, "--capital", help="Initial capital, for trades.csv cum_profit_pct (MC run: 1,000,000)", rich_help_panel=OUTPUT)
TRADE_CHARTS = typer.Option(True, "--trade-charts/--no-trade-charts", help="Draw one price-chart image per trade into <out>/trades/ for the report's trade viewer (redraw later: guban chart)", rich_help_panel=OUTPUT)


def _out_option(default: str) -> typer.models.OptionInfo:
    return typer.Option(None, "--out", help=f"Output folder (default: reports/{default})", rich_help_panel=OUTPUT)


def _open_option(page: str) -> typer.models.OptionInfo:
    return typer.Option(False, "--open", help=f"Open {page} in the browser when done", rich_help_panel=OUTPUT)


FillMode = Literal["magnifier", "ohlc"]
RUN_ID = "<strategy>_<symbol>_<tf>_<session>"


def run_dir(strategy_file: Path, symbol: str, contract: str | None, tf: str, session: str, suffix: str = "") -> Path:
    """Default output folder: reports/<strategy>_<symbol>_<tf>_<session>[_opt|_wfo]; <symbol> becomes NQ-MNQ when
    --contract differs from --symbol, so those runs do not overwrite each other."""
    sym = symbol if not contract or contract.upper() == symbol.upper() else f"{symbol}-{contract}"
    return REPORTS / f"{strategy_file.stem}_{sym}_{tf}_{session}{suffix}"


def _slug(text: str) -> str:
    return "_".join("".join(ch if ch.isalnum() else " " for ch in text.lower()).split()) or "trades"


def _open(path: Path) -> None:
    import webbrowser

    webbrowser.open(path.resolve().as_uri())


# ---------------------------------------------------------------------------


def _version(value: bool) -> None:
    if value:
        try:
            v = version("guban")
        except PackageNotFoundError:
            v = "unknown"
        typer.echo(f"guban {v}")
        raise typer.Exit()


def _utf8_streams() -> None:
    """Redirected or piped output on Windows gets the ANSI code page (cp1252), which cannot encode the − and → the
    summaries print; write UTF-8 there instead (a console already is)."""
    for stream in (sys.__stdout__, sys.__stderr__):
        if stream is None or not hasattr(stream, "reconfigure") or stream.isatty():
            continue
        if stream.encoding.lower().replace("-", "").replace("_", "") != "utf8":
            stream.reconfigure(encoding="utf-8")


@app.callback()
def main(
    show_version: bool = typer.Option(False, "--version", callback=_version, is_eager=True, help="Show the version and exit."),
) -> None:
    _utf8_streams()


@app.command("import")
@_friendly
def import_cmd(
    symbol: str = typer.Option(..., "--symbol", help="Store symbol to import into data/<SYMBOL>/1m.parquet: NQ, MNQ, ES, MES or BTC"),
    roll: Literal["volume", "expiry"] = typer.Option("volume", "--roll", help="volume: the store's roll calendar (highest-volume contract); expiry: N days before the 3rd Friday (MC continuous contract; equity index only)"),
    days_before: int = typer.Option(2, "--roll-days-before", help="--roll expiry: days before expiration to switch (MC: 2, the Wednesday)"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir", help="Folder for the Parquet store"),
    root: Path = typer.Option(None, "--root", file_okay=False, help="Market-data store folder (default: config/store.toml root, D:\\Market Data)"),
    source: Path = typer.Option(None, "--source", exists=True, file_okay=False, help="Read a DeepCharts per-contract folder instead of the store, e.g. Database/NQ-CME (old import, for comparisons)"),
) -> None:
    """Build a continuous 1-minute series from the shared market-data store (or a DeepCharts folder) and save it as Parquet."""
    if source is not None:
        summary = import_store(source, symbol, data_dir, roll, days_before)
    else:
        from guban.store.read import import_from_store

        summary = import_from_store(symbol, data_dir, root, roll, days_before)
    typer.echo(str(summary))
    typer.echo(f"Wrote {data_dir / symbol / '1m.parquet'}")


@app.command("bars")
@_friendly
def bars_cmd(
    symbol: str = typer.Option(..., "--symbol", help="Imported symbol, e.g. NQ"),
    tf: str = typer.Option("5m", "--tf", help="Timeframe: 1m, 5m, 15m, 1h, 1d ..."),
    session: str = typer.Option("rth", "--session", help="Session template from config/sessions.toml"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir", help="Folder holding the imported Parquet store"),
    rebuild: bool = typer.Option(False, "--rebuild", help="Ignore the cached Parquet file"),
) -> None:
    """Build (or load from cache) session-aligned bars and print a summary."""
    bars = load_bars(symbol, tf, session, data_dir, rebuild=rebuild)
    per_session = bars.groupby("session_date").size()
    typer.echo(f"{symbol} {tf} {session}: {len(bars):,} bars in {len(per_session)} sessions")
    typer.echo(f"  first bar: {bars.index[0]}   last bar: {bars.index[-1]}")
    typer.echo(f"  bars per session: typical {per_session.mode().iat[0]}, min {per_session.min()}, max {per_session.max()}")
    short = per_session[per_session < per_session.mode().iat[0]]
    if len(short):
        typer.echo(f"  short sessions ({len(short)}): " + ", ".join(short.index.strftime("%Y-%m-%d")[:20]) + (" ..." if len(short) > 20 else ""))
    typer.echo(f"Cached at {cache_path(symbol, tf, session, data_dir)}")


def _load_strategy_class(path: Path, class_name: str | None):
    from guban.engine.strategy import load_strategy_class

    try:
        return load_strategy_class(path, class_name)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="STRATEGY_FILE / --class") from e


def _parse_inputs(pairs: list[str], defaults: dict) -> dict:
    out = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or key not in defaults:
            raise typer.BadParameter(f"{pair!r}: use name=value with one of {sorted(defaults)}", param_hint="--set")
        try:  # the input's default type decides: numbers must be numbers, bools true/false, strings stay text
            out[key] = _coerce(raw, defaults[key], pair, what="--set")
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--set") from None
    return out


def _trade_list(nos: list[int], limit: int = 20) -> str:
    """'#3, #17, ...' (the first `limit` trade numbers)."""
    return ", ".join(f"#{n}" for n in nos[:limit]) + (f" and {len(nos) - limit} more" if len(nos) > limit else "")


def _mc_trade_info(trades, bars) -> dict:
    """run_monte_carlo's per-trade inputs: the exit bar's session date as the trading day (the exit time's date when
    bars have no session_date), contracts, side, exit time and USD risk to the initial stop (NaN without one)."""
    if "session_date" in bars.columns:
        days = bars["session_date"].to_numpy()[trades["exit_bar"].to_numpy(int)]
    else:
        days = pd.DatetimeIndex(trades["exit_time"]).date
    return {"days": days, "contracts": trades["qty"].to_numpy(float), "sides": trades["direction"].to_numpy(str), "times": trades["exit_time"],
            "risks": trades["risk_usd"].to_numpy(float)}  # fmt: skip


def _exposure_notes(exposure: dict) -> list[str]:
    """Report notes for guban.metrics.session_exposure: trades held past a scheduled close or across a data gap."""
    from guban.metrics import GAP_NOTE, PAST_CLOSE_NOTE

    notes = []
    if exposure["trades_held_past_close"]:
        notes.append(PAST_CLOSE_NOTE.format(n=exposure["trades_held_past_close"], nos=_trade_list(exposure["past_close_trade_nos"])))
    if exposure["trades_held_across_gap"]:
        notes.append(GAP_NOTE.format(n=exposure["trades_held_across_gap"], nos=_trade_list(exposure["gap_trade_nos"])))
    return notes


def _details(symbol: str, tf: str, session: str, history_from: str | None, spec, tz: str, fill_mode: str, commission: float, slippage: float,
             costs_from: str = "flags", data_from: str | None = None) -> dict:  # fmt: skip
    return {
        "Data": f"{symbol} {tf}, {session} session" + (f" (+{history_from} history)" if history_from else "")
        + (f", from {data_from}" if data_from else ""),
        "Contract spec": f"{spec.symbol}: ${spec.point_value:g}/pt, tick {spec.tick_size:g}",
        "Chart time zone": tz,
        "Fill mode": fill_mode,
        "Costs (per contract, round trip)": f"commission ${commission:g}, slippage ${slippage:g}"
        + ("" if costs_from == "flags" else f" ({costs_from})"),
    }


@app.command("run")
@_friendly
def run_cmd(
    strategy_file: Path = STRATEGY_FILE,
    symbol: str = SYMBOL,
    contract: str = CONTRACT,
    tf: str = TF,
    session: str = SESSION,
    history_from: str = HISTORY_FROM,
    data_from: str = DATA_FROM,
    tz: str = TZ,
    fill_mode: FillMode = FILL_MODE,
    data_dir: Path = DATA_DIR,
    commission: float = COMMISSION,
    slippage: float = SLIPPAGE,
    inputs: list[str] = SET_INPUTS,
    class_name: str = CLASS_NAME,
    capital: float = CAPITAL,
    mc_sims: int = typer.Option(10_000, "--mc-sims", help="Monte Carlo runs per method (resample, reorder, whole days) and EV bootstrap on this run's trades; 0: off", rich_help_panel=MC),
    mc_seed: int = typer.Option(42, "--mc-seed", help="Monte Carlo random seed", rich_help_panel=MC),
    mc_dd_limit: float = typer.Option(None, "--mc-dd-limit", help="Also report the chance the max drawdown reaches this many USD, e.g. 2500", rich_help_panel=MC),
    mc_risk: float = typer.Option(None, "--mc-risk", help="Risk per contract in USD for the Monte Carlo's R, e.g. 400 (default: each trade's own initial stop; no stops at all: the average losing trade per contract)", rich_help_panel=MC),
    mc_oos_from: str = typer.Option(None, "--mc-oos-from", callback=_check_date, help="Also split the EV into in/out of sample: trades exiting from this date (YYYY-MM-DD, chart time zone) are out of sample", rich_help_panel=MC),
    report: bool = typer.Option(True, "--report/--no-report", help="Write report.html", rich_help_panel=OUTPUT),
    trade_charts: bool = TRADE_CHARTS,
    out: Path = _out_option(RUN_ID),
    open_report: bool = _open_option("report.html"),
) -> None:
    """Backtest a strategy; write trades.csv (MC layout), metrics.json and report.html (with a trade Monte Carlo)."""
    import json

    from guban.config import get_contract
    from guban.engine import Costs, StrategyError, run_backtest
    from guban.evaluation import evaluate, summary_line
    from guban.metrics import ROLL_NOTE, backtest_metrics, jsonable, roll_summary, session_exposure
    from guban.montecarlo import MIN_TRADES, check_settings, run_monte_carlo
    from guban.report import fmt_value, write_report

    t0 = time.perf_counter()
    check_settings(mc_sims, mc_seed, mc_dd_limit, mc_risk, mc_oos_from)  # before the data loads
    cls = _load_strategy_class(strategy_file, class_name)
    overrides = _parse_inputs(inputs, cls.inputs)
    try:
        strategy = cls(**overrides)
    except Exception as e:  # the strategy's own __init__ failed
        raise StrategyError(f"{cls.__name__}({', '.join(f'{k}={v!r}' for k, v in overrides.items())}) could not be created: {e!r}") from e
    spec = get_contract(contract or symbol)
    commission, slippage, costs_from = _costs(spec, commission, slippage)
    bars, sub = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir), data_from, tuple(cls.extra_columns)).load()
    if "big_buy_body" in cls.extra_columns:
        from guban.data.bigtrades import describe

        typer.echo(describe(bars, bars["big_buy_body"]))
    t1 = time.perf_counter()
    res = run_backtest(strategy, bars, spec, Costs(commission=commission, slippage=slippage), sub_bars=sub, fill_mode=fill_mode)
    t2 = time.perf_counter()
    out = out or run_dir(strategy_file, symbol, contract, tf, session)
    path = res.write_trades_csv(out / "trades.csv", capital)
    tr = res.trades
    pos = res.open_position
    metrics = backtest_metrics(res, bars)
    rolls = roll_summary(tr, bars)
    exposure = session_exposure(tr, bars)
    mc = None  # MonteCarloResult, or {"skipped": reason}
    if mc_sims:
        if len(tr) >= MIN_TRADES:
            mc = run_monte_carlo(tr["net_pnl"].to_numpy(float), mc_sims, mc_seed, mc_dd_limit, risk=mc_risk, oos_from=mc_oos_from,
                                 **_mc_trade_info(tr, bars))  # fmt: skip
        else:
            mc = {"skipped": f"needs at least {MIN_TRADES} trades; this run has {len(tr)}"}
    evaluation = evaluate(tr, metrics, _mc_trade_info(tr, bars)["days"], mc_sims, mc_seed, mc_risk) if len(tr) else None

    title = f"{cls.__name__} · {spec.symbol} {tf}"
    notes = []
    if history_from:
        notes.append(f"{history_from} bars stand in for {symbol} before {symbol}'s first bar.")
    if rolls["trades_held_across"]:
        notes.append(ROLL_NOTE.format(n=rolls["trades_held_across"], nos=_trade_list(rolls["trade_nos"])))
    notes += _exposure_notes(exposure)
    if res.bars_without_sub_bars:
        notes.append(f"{res.bars_without_sub_bars:,} bars had no 1-minute data and were filled from their own OHLC.")
    if res.bars_with_partial_sub_bars:
        notes.append(f"{res.bars_with_partial_sub_bars:,} bars had 1-minute data that did not reproduce their OHLC (minutes missing) "
                     "and were filled from their own OHLC.")  # fmt: skip
    if pos is not None:
        notes.append(f"A position is still open at the end ({pos.direction:+d} x{pos.qty} from {pos.entry_time:%Y-%m-%d %H:%M}); it is not in the trade statistics.")
    if len(tr) and tr["ambiguous"].any():
        notes.append(f"{int(tr['ambiguous'].sum())} trade(s) have an ambiguous fill: the exit order inside one sub-bar came from the OHLC path rule, not the data.")
    meta = {
        "title": title,
        "subtitle": f"{bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d} · {strategy_file.name}",
        "details": {
            **_details(symbol, tf, session, history_from, spec, tz, fill_mode, commission, slippage, costs_from, data_from),
            "Bars": f"{len(bars):,}",
            "First / last bar": f"{bars.index[0]:%Y-%m-%d %H:%M} / {bars.index[-1]:%Y-%m-%d %H:%M}",
            "Backtest time": f"{t2 - t1:.1f} s",
        },
        "inputs": vars(strategy.p),
        "notes": notes,
    }
    meta_json = {"strategy": cls.__name__, "file": str(strategy_file), "symbol": symbol, "contract": spec.symbol, "tf": tf,
                 "session": session, "tz": tz, "fill_mode": fill_mode, "history_from": history_from,
                 "commission": commission, "slippage": slippage, "costs": costs_from, "data_from": data_from, "capital": capital, "inputs": vars(strategy.p),
                 "first_bar": bars.index[0], "last_bar": bars.index[-1], "bars": len(bars)}  # fmt: skip
    meta_json["contract_rolls"] = rolls
    meta_json["session_exposure"] = exposure
    mc_json = {} if mc is None else {"monte_carlo": mc if isinstance(mc, dict) else mc.summary()}
    ev_json = {} if evaluation is None else {"evaluation": evaluation}
    (out / "metrics.json").write_text(json.dumps(jsonable({"run": meta_json, **metrics, **mc_json, **ev_json}), indent=2, default=str) + "\n", encoding="utf-8")
    report_path = (write_report(res, bars, out / "report.html", metrics, meta, monte_carlo=mc, trade_charts=trade_charts, evaluation=evaluation)
                   if report else None)  # fmt: skip
    t3 = time.perf_counter()
    mc_seconds = mc.seconds if mc is not None and not isinstance(mc, dict) else 0.0

    typer.echo(f"{cls.__name__} on {symbol} {tf} {session} ({spec.symbol} spec, {fill_mode}, chart tz {tz})")
    typer.echo(f"  bars: {len(bars):,}  {bars.index[0]} -> {bars.index[-1]}" + (f"  ({history_from} before {symbol}'s first bar)" if history_from else ""))
    if len(tr):
        a, acc = metrics["all"], metrics["account"]
        pf = "inf" if a["profit_factor"] == float("inf") else f"{a['profit_factor'] or 0:.2f}"
        typer.echo(f"  trades: {len(tr):,}  net profit: {res.net_profit:,.2f}  profit factor: {pf}  "
                   f"max drawdown: {acc['max_drawdown_intraday']:,.2f}  ambiguous fills: {a['ambiguous_fills']}")  # fmt: skip
    else:
        typer.echo("  trades: 0")
    if res.bars_without_sub_bars or res.bars_with_partial_sub_bars:
        typer.echo(f"  bars without (complete) 1-minute data (OHLC fallback): {res.bars_without_sub_bars + res.bars_with_partial_sub_bars}")
    if rolls["trades_held_across"]:
        typer.echo(f"  trades held across a contract roll (unadjusted series): {rolls['trades_held_across']}")
    if exposure["trades_held_past_close"]:
        typer.echo(f"  trades held past their session's scheduled close: {exposure['trades_held_past_close']} "
                   f"({_trade_list(exposure['past_close_trade_nos'], 10)})")  # fmt: skip
    if exposure["trades_held_across_gap"]:
        typer.echo(f"  trades held across a data gap: {exposure['trades_held_across_gap']} ({_trade_list(exposure['gap_trade_nos'], 10)})")
    if pos is not None:
        typer.echo(f"  open position at the end: {pos.direction:+d} x{pos.qty}")
    if isinstance(mc, dict):
        typer.echo(f"  monte carlo: skipped ({mc['skipped']})")
    elif mc is not None:
        rs, ro = mc.method_summary("resample"), mc.method_summary("reorder")
        u = lambda v: fmt_value(v, "usd")  # noqa: E731
        typer.echo(f"  monte carlo ({mc.sims:,} runs, close-to-close DD {u(mc.max_dd)}): "
                   f"resample net P5 {u(rs['net_profit']['p5'])} / P50 {u(rs['net_profit']['p50'])} / P95 {u(rs['net_profit']['p95'])}, "
                   f"max DD P5 {u(rs['max_drawdown']['p5'])}, P(net <= 0) {rs['p_net_le_0']:.1%}; "
                   f"reorder max DD P5 {u(ro['max_drawdown']['p5'])}, P(DD worse than backtest) {ro['p_dd_worse_than_backtest']:.1%}")  # fmt: skip
        if "day" in mc.methods:
            dy = mc.method_summary("day")
            typer.echo(f"  whole days ({mc.days:,} trading days): net P5 {u(dy['net_profit']['p5'])} / P50 {u(dy['net_profit']['p50'])}, "
                       f"max DD P5 {u(dy['max_drawdown']['p5'])}, P(net <= 0) {dy['p_net_le_0']:.1%}")  # fmt: skip
        if mc.dd_limit is not None:
            limit = ", ".join(f"{name} {mc.method_summary(name)['p_dd_reaches_limit']:.1%}" for name in mc.methods)
            typer.echo(f"  P(max DD reaches {u(mc.dd_limit)}): {limit}")
        ev = mc.ev["groups"].get("all")
        if ev:
            r = ev["r"]
            typer.echo(f"  EV per trade: {u(ev['usd']['mean'])} ({mc.ev['ci']:.0%} CI {u(ev['usd']['lo'])} to {u(ev['usd']['hi'])})"
                       + (f", {r['mean']:+.3f} R ({mc.ev['risk_source']}, avg {u(mc.ev['risk_per_contract'])} per contract)" if r else "")
                       + f", P(EV <= 0) {ev['p_ev_le_0']:.1%}")  # fmt: skip
            for g in ("in_sample", "out_of_sample"):
                e = mc.ev["groups"].get(g)
                if e:
                    typer.echo(f"    {g.replace('_', ' ')}: {e['trades']:,} trades, EV {u(e['usd']['mean'])}, P(EV <= 0) {e['p_ev_le_0']:.1%}")
    if evaluation:
        typer.echo(f"  evaluation: {summary_line(evaluation)}")
    mc_time = f" + monte carlo {mc_seconds:.1f} s" if mc_seconds else ""
    typer.echo(f"  time: load {t1 - t0:.1f} s + backtest {t2 - t1:.1f} s{mc_time} + report {t3 - t2 - mc_seconds:.1f} s = {t3 - t0:.1f} s")
    images = f", {out / 'trades'}{os.sep}trade_*.png" if report_path and trade_charts and len(tr) else ""
    typer.echo(f"Wrote {path}, {out / 'metrics.json'}" + (f", {report_path}" if report_path else "") + images)
    if report_path and open_report:
        _open(report_path)


@app.command("compare")
@_friendly
def compare_cmd(
    ours: Path = typer.Option(..., "--ours", exists=True, dir_okay=False, help="Our trades.csv (from guban run or wfo)"),
    mc: Path = typer.Option(..., "--mc", exists=True, dir_okay=False, help="MC trade export (.csv or report .xlsx)"),
    contract: str = typer.Option(..., "--contract", help="Contract spec for tick size and point value, e.g. MNQ"),
    tf: str = typer.Option("5m", "--tf", help="Strategy timeframe: entries match within one bar"),
    mc_tz: str = typer.Option("America/Chicago", "--mc-tz", callback=_check_tz, help="Time zone of the MC chart the export came from"),
    start: str = typer.Option(None, "--start", help="Window start, MC chart time (default: the later of the two first entries)"),
    end: str = typer.Option(None, "--end", help="Window end, MC chart time (default: the earlier of the two last entries)"),
    out: Path = typer.Option(None, "--out", help="Output folder for diff.html (default: the folder of --ours)", rich_help_panel=OUTPUT),
    open_report: bool = _open_option("diff.html"),
) -> None:
    """Compare our trades with an MC export (S3): match rate, prices within 1 tick, net profit within 3%. Exit 1 on FAIL."""
    from guban.config import get_contract
    from guban.parity import compare_trades, load_mc_trades, load_our_trades, write_diff_html

    spec = get_contract(contract)
    n = parse_timeframe(tf)
    bar = pd.Timedelta(minutes=n) if n else pd.Timedelta(days=1)

    def when(s: str | None, flag: str) -> pd.Timestamp | None:  # naive dates/times are MC chart time
        if s is None:
            return None
        try:
            t = pd.Timestamp(s)
        except ValueError as e:
            raise typer.BadParameter(f"{s!r} is not a date/time, e.g. 2025-06-08 or '2025-06-08 09:30'", param_hint=flag) from e
        return t.tz_localize(mc_tz) if t.tzinfo is None else t

    def load(loader, path: Path, flag: str, what: str):
        try:
            return loader(path)
        except (KeyError, ValueError) as e:
            detail = f"missing column {_message(e)!r}" if isinstance(e, KeyError) else _message(e)
            raise typer.BadParameter(f"{path.name} is not {what} ({detail})", param_hint=flag) from e

    ours_tr = load(load_our_trades, ours, "--ours", "a guban trades.csv")
    mc_tr = load(lambda p: load_mc_trades(p, mc_tz), mc, "--mc", "an MC trade export")
    cmp = compare_trades(ours_tr, mc_tr, bar, spec.tick_size, spec.point_value, when(start, "--start"), when(end, "--end"))
    target = out if out is not None and out.suffix.lower() == ".html" else (out or ours.parent) / "diff.html"
    path = write_diff_html(cmp, target, f"Parity: {ours.parent.name} vs {mc.name}", mc_tz)
    typer.echo(cmp.summary())
    typer.echo(f"Wrote {path}")
    if open_report:
        _open(path)
    if not cmp.passed:
        raise typer.Exit(1)


def _parse_search(grid_specs: list[str], inputs: list[str], defaults: dict, metric: str, yes: bool) -> tuple[dict, dict, int]:
    """Validate the optimize/wfo search flags before any data is loaded: (grid, fixed inputs, combinations)."""
    from guban.optimize import METRICS, combinations, parse_grid

    fixed = _parse_inputs(inputs, defaults)
    try:
        grid = parse_grid([s for spec in grid_specs for s in spec.split()], defaults)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--grid") from e
    if metric not in METRICS:
        raise typer.BadParameter(f"use one of {list(METRICS)}", param_hint="--metric")
    both = set(grid) & set(fixed)
    if both:
        raise typer.BadParameter(f"{sorted(both)} are both in --grid and --set", param_hint="--grid / --set")
    n = len(combinations(grid))
    if n > MAX_COMBINATIONS and not yes:
        typer.echo(f"Error: the grid has {n:,} combinations (more than {MAX_COMBINATIONS:,}). Narrow it, or rerun with --yes.", err=True)
        raise typer.Exit(2)
    return grid, fixed, n


def _progress(t0: float, n: int):
    step = max(1, n // 10)

    def progress(done: int, total: int) -> None:
        if done % step == 0 or done == total:
            el = time.perf_counter() - t0
            typer.echo(f"  {done:,}/{total:,} runs  {el:,.0f} s" + (f"  (~{el / done * (total - done):,.0f} s left)" if done < total else ""))

    return progress


@app.command("optimize")
@_friendly
def optimize_cmd(
    strategy_file: Path = STRATEGY_FILE,
    grid_specs: list[str] = GRID,
    symbol: str = SYMBOL,
    contract: str = CONTRACT,
    tf: str = TF,
    session: str = SESSION,
    history_from: str = HISTORY_FROM,
    data_from: str = DATA_FROM,
    tz: str = TZ,
    fill_mode: FillMode = FILL_MODE,
    data_dir: Path = DATA_DIR,
    commission: float = COMMISSION,
    slippage: float = SLIPPAGE,
    inputs: list[str] = SET_INPUTS,
    class_name: str = CLASS_NAME,
    metric: str = typer.Option("net_profit", "--metric", help="Rank by: net_profit, profit_factor, return_on_account, sharpe_daily, max_drawdown_intraday, ...", rich_help_panel=SEARCH),
    min_trades: int = MIN_TRADES,
    workers: int = WORKERS,
    yes: bool = YES,
    x: str = typer.Option(None, "--x", help="Heatmap x input (default: first varying grid input)", rich_help_panel=OUTPUT),
    y: str = typer.Option(None, "--y", help="Heatmap y input (default: second varying grid input)", rich_help_panel=OUTPUT),
    top: int = typer.Option(50, "--top", help="Rows in the HTML ranked table (results.csv has every run)", rich_help_panel=OUTPUT),
    out: Path = _out_option(RUN_ID + "_opt"),
    open_report: bool = _open_option("optimize.html"),
) -> None:
    """Grid-search strategy inputs on all CPU cores; write results.csv, optimize.json and optimize.html (heatmap)."""
    import json

    from guban.config import get_contract
    from guban.engine import Costs
    from guban.metrics import ROLL_NOTE, jsonable, roll_summary
    from guban.optimize import METRICS, TooManyCombinations, optimize
    from guban.optimize_report import heatmap_axes, write_optimize_report
    from guban.report import fmt_value

    cls = _load_strategy_class(strategy_file, class_name)
    grid, fixed, n = _parse_search(grid_specs, inputs, cls.inputs, metric, yes)
    try:
        heatmap_axes(grid, x, y)  # check --x / --y before the long run
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--x / --y") from e
    spec = get_contract(contract or symbol)
    commission, slippage, costs_from = _costs(spec, commission, slippage)
    out = out or run_dir(strategy_file, symbol, contract, tf, session, "_opt")

    t0 = time.perf_counter()
    data = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir), data_from, tuple(cls.extra_columns))
    bars, _ = data.load()  # also builds any missing bar caches before the workers read them
    typer.echo(f"{cls.__name__} on {symbol} {tf} {session} ({spec.symbol} spec, {fill_mode}, chart tz {tz}): "
               f"{n:,} combinations, {len(bars):,} bars")  # fmt: skip
    try:
        opt = optimize(strategy_file, grid, spec, class_name=cls.__name__, costs=Costs(commission=commission, slippage=slippage), data=data, fixed=fixed,
                       metric=metric, min_trades=min_trades, workers=workers, confirm=yes, progress=_progress(t0, n))  # fmt: skip
    except TooManyCombinations as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e
    csv_path = opt.write_csv(out / "results.csv")

    label, kind, _ = METRICS[metric]
    grid_text = " · ".join(f"{k}: {', '.join(map(str, v))}" for k, v in grid.items())
    meta = {
        "title": f"Optimization · {cls.__name__} · {spec.symbol} {tf}",
        "subtitle": f"{bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d} · {strategy_file.name}",
        "details": {
            **_details(symbol, tf, session, history_from, spec, tz, fill_mode, commission, slippage, costs_from, data_from),
            "Ranked by": f"{label}, runs with ≥ {min_trades:,} trades",
            "Grid": grid_text,
        },
        "inputs": fixed,
        "notes": ["Fixed inputs (--set) are shown as chips; every other input is at its default."] if fixed else [],
    }
    html_path = write_optimize_report(opt, out / "optimize.html", meta, x, y, top)
    best = opt.results[opt.results["rank"] == 1]
    summary = {
        "run": {"strategy": cls.__name__, "file": str(strategy_file), "symbol": symbol, "contract": spec.symbol, "tf": tf,
                "session": session, "tz": tz, "fill_mode": fill_mode, "history_from": history_from,
                "commission": commission, "slippage": slippage, "costs": costs_from, "data_from": data_from, "fixed": fixed,
                "first_bar": bars.index[0], "last_bar": bars.index[-1], "bars": len(bars)},
        "grid": grid, "metric": metric, "min_trades": min_trades, "combinations": n, "ranked": int(opt.results["rank"].notna().sum()),
        "workers": opt.workers, "seconds": round(opt.seconds, 2),
        "best": {"inputs": opt.best, **{k: best[k].iat[0] for k in METRICS}} if len(best) else None,
    }  # fmt: skip
    (out / "optimize.json").write_text(json.dumps(jsonable(summary), indent=2, default=str) + "\n", encoding="utf-8")

    typer.echo(f"  {n:,} runs in {opt.seconds:,.1f} s on {opt.workers} workers; {summary['ranked']:,} with >= {min_trades:,} trades")
    for _, row in opt.ranked().head(5).iterrows():
        if pd.isna(row["rank"]):
            break
        ins = ", ".join(f"{k}={row[k]}" for k in grid)
        typer.echo(f"  #{int(row['rank'])}  {ins}  {label.lower()} {fmt_value(row[metric], kind)}  trades {int(row['total_trades']):,}  "
                   f"max drawdown {fmt_value(row['max_drawdown_intraday'], 'usd')}")  # fmt: skip
    typer.echo(f"Wrote {csv_path}, {out / 'optimize.json'}, {html_path}")
    if open_report:
        _open(html_path)


@app.command("wfo")
@_friendly
def wfo_cmd(
    strategy_file: Path = STRATEGY_FILE,
    grid_specs: list[str] = GRID,
    symbol: str = SYMBOL,
    contract: str = CONTRACT,
    tf: str = TF,
    session: str = SESSION,
    history_from: str = HISTORY_FROM,
    data_from: str = DATA_FROM,
    tz: str = TZ,
    fill_mode: FillMode = FILL_MODE,
    data_dir: Path = DATA_DIR,
    commission: float = COMMISSION,
    slippage: float = SLIPPAGE,
    inputs: list[str] = SET_INPUTS,
    class_name: str = CLASS_NAME,
    is_months: int = typer.Option(12, "--is", help="In-sample window, months", rich_help_panel=WF),
    oos_months: int = typer.Option(3, "--oos", help="Out-of-sample window, months", rich_help_panel=WF),
    step_months: int = typer.Option(None, "--step", help="Months between windows (default: --oos)", rich_help_panel=WF),
    anchored: bool = typer.Option(False, "--anchored/--rolling", help="Anchored: every in-sample window starts at --start and grows", rich_help_panel=WF),
    start: str = typer.Option(None, "--start", help="First in-sample day, YYYY-MM-DD session date (default: first whole month of data)", rich_help_panel=WF),
    metric: str = typer.Option("net_profit", "--metric", help="Pick each in-sample winner by: net_profit, profit_factor, return_on_account, sharpe_daily, ...", rich_help_panel=SEARCH),
    min_trades: int = MIN_TRADES,
    workers: int = WORKERS,
    yes: bool = YES,
    capital: float = CAPITAL,
    trade_charts: bool = TRADE_CHARTS,
    out: Path = _out_option(RUN_ID + "_wfo"),
    open_report: bool = _open_option("wfo.html"),
) -> None:
    """Walk-forward: optimize each in-sample window, trade the winner out of sample; write the stitched result."""
    import json

    from guban.config import get_contract
    from guban.engine import Costs
    from guban.metrics import ROLL_NOTE, jsonable, roll_summary, session_exposure
    from guban.optimize import METRICS, TooManyCombinations
    from guban.report import fmt_value
    from guban.wfo import walk_forward
    from guban.wfo_report import write_wfo_report

    cls = _load_strategy_class(strategy_file, class_name)
    grid, fixed, n = _parse_search(grid_specs, inputs, cls.inputs, metric, yes)
    spec = get_contract(contract or symbol)
    commission, slippage, costs_from = _costs(spec, commission, slippage)
    out = out or run_dir(strategy_file, symbol, contract, tf, session, "_wfo")
    data = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir), data_from, tuple(cls.extra_columns))
    typer.echo(f"{cls.__name__} on {symbol} {tf} {session} ({spec.symbol} spec, {fill_mode}, chart tz {tz}): "
               f"{n:,} combinations, each run once per window start")  # fmt: skip
    t0 = time.perf_counter()
    try:
        wf = walk_forward(strategy_file, grid, spec, Costs(commission=commission, slippage=slippage), data=data, fixed=fixed,
                          class_name=cls.__name__, is_months=is_months, oos_months=oos_months, step_months=step_months,
                          anchored=anchored, start=start, metric=metric, min_trades=min_trades, workers=workers, confirm=yes,
                          progress=_progress(t0, n))  # fmt: skip
    except TooManyCombinations as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(2) from e

    bars, s, p = wf.bars, wf.summary, wf.pooled
    trades_path = wf.oos_result().write_trades_csv(out / "trades.csv", capital)
    csv_paths = wf.write_csvs(out)
    label = METRICS[metric][0]
    layout = f"{'anchored' if anchored else 'rolling'}, {is_months} m in-sample / {oos_months} m out-of-sample, step {wf.step_months} m"
    notes = []
    if history_from:
        notes.append(f"{history_from} bars stand in for {symbol} before {symbol}'s first bar.")
    if wf.boundary_trades:
        notes.append(f"{wf.boundary_trades} out-of-sample trade(s) were still open at their window's end and were closed at its last bar's "
                     "close (exit “Window End”): every window starts and ends flat.")  # fmt: skip
    rolls = roll_summary(wf.trades, bars)
    if rolls["trades_held_across"]:
        notes.append(ROLL_NOTE.format(n=rolls["trades_held_across"], nos=_trade_list(rolls["trade_nos"])))
    exposure = session_exposure(wf.trades, bars)
    notes += _exposure_notes(exposure)
    if s["combo"].isna().any():
        notes.append(f"{int(s['combo'].isna().sum())} window(s) had no combination with ≥ {min_trades:,} in-sample trades and sat out (flat).")
    tr = wf.trades
    if len(tr) and tr["ambiguous"].any():
        notes.append(f"{int(tr['ambiguous'].sum())} out-of-sample trade(s) have an ambiguous fill (OHLC path rule inside one sub-bar).")
    notes.append("Fixed inputs (--set) are shown as chips; every other input not in the grid is at its default.")
    meta = {
        "title": f"Walk-forward · {cls.__name__} · {spec.symbol} {tf}",
        "subtitle": f"Out of sample {s['oos_from'].iat[0]:%Y-%m-%d} → {s['oos_to'].iat[-1]:%Y-%m-%d} · {strategy_file.name}",
        "details": {
            **_details(symbol, tf, session, history_from, spec, tz, fill_mode, commission, slippage, costs_from, data_from),
            "Windows": layout,
            "Chosen by": f"{label}, runs with ≥ {min_trades:,} in-sample trades",
            "Grid": " · ".join(f"{k}: {', '.join(map(str, v))}" for k, v in grid.items()),
            "Runs": f"{n:,} over {bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d}, {wf.seconds:,.1f} s on {wf.workers} workers",
        },
        "inputs": fixed,
        "notes": notes,
    }
    html_path = write_wfo_report(wf, out / "wfo.html", meta, trade_charts=trade_charts, workers=workers)
    summary = {
        "run": {"strategy": cls.__name__, "file": str(strategy_file), "symbol": symbol, "contract": spec.symbol, "tf": tf,
                "session": session, "tz": tz, "fill_mode": fill_mode, "history_from": history_from,
                "commission": commission, "slippage": slippage, "costs": costs_from, "data_from": data_from, "fixed": fixed, "capital": capital,
                "first_bar": bars.index[0], "last_bar": bars.index[-1], "bars": len(bars)},
        "grid": grid, "metric": metric, "min_trades": min_trades, "combinations": n,
        "layout": {"anchored": anchored, "is_months": is_months, "oos_months": oos_months, "step_months": wf.step_months, "start": s["is_from"].iat[0]},
        "workers": wf.workers, "seconds": round(wf.seconds, 2),
        "walk_forward_efficiency": p["wfe"], "is_net_per_year": p["is_net_per_year"], "oos_net_per_year": p["oos_net_per_year"],
        "boundary_trades": wf.boundary_trades,
        "contract_rolls": rolls,
        "session_exposure": exposure,
        "windows": wf.summary_records(),
        "oos": wf.metrics,
    }  # fmt: skip
    (out / "wfo.json").write_text(json.dumps(jsonable(summary), indent=2, default=str) + "\n", encoding="utf-8")

    names = list(grid)
    for _, r in s.iterrows():
        head = f"  W{r['window']:<2} IS {r['is_from']:%Y-%m-%d}→{r['is_to']:%Y-%m-%d}  OOS {r['oos_from']:%Y-%m-%d}→{r['oos_to']:%Y-%m-%d}"
        if pd.isna(r["combo"]):
            typer.echo(f"{head}  sat out (no run with >= {min_trades:,} in-sample trades)")
            continue
        ins = ", ".join(f"{k}={r[k]}" for k in names)
        wfe = "n/a" if pd.isna(r["wfe"]) else f"{r['wfe']:.0%}"
        typer.echo(f"{head}  {ins}  IS/yr {fmt_value(r['is_net_per_year'], 'usd')}  OOS {fmt_value(r['oos_net_profit'], 'usd')} "
                   f"({int(r['oos_trades'])} trades)  WFE {wfe}")  # fmt: skip
    a, acc = wf.metrics["all"], wf.metrics["account"]
    typer.echo(f"  out of sample: {a['total_trades']:,} trades  net {fmt_value(a['net_profit'], 'usd')}  "
               f"max drawdown {fmt_value(acc['max_drawdown_intraday'], 'usd')}  net/yr {fmt_value(p['oos_net_per_year'], 'usd')}  "
               f"(in sample {fmt_value(p['is_net_per_year'], 'usd')}/yr)")  # fmt: skip
    typer.echo(f"  walk-forward efficiency: {'n/a' if p['wfe'] is None else format(p['wfe'], '.0%')}"
               + (f"  boundary trades: {wf.boundary_trades}" if wf.boundary_trades else ""))  # fmt: skip
    typer.echo(f"  {n:,} combinations + {int(s['combo'].notna().sum())} out-of-sample window re-runs in {wf.seconds:,.1f} s on {wf.workers} workers")
    typer.echo(f"Wrote {trades_path}, {csv_paths[0]}, {csv_paths[1]}, {out / 'wfo.json'}, {html_path}")
    if open_report:
        _open(html_path)


def _run_bars(run: Path, info: dict, data_dir: Path) -> pd.DataFrame:
    """The bars a run's trade bar numbers index: its data settings reloaded, cut back to the run's bar count when
    sessions were added since; anything else changed means the numbers no longer match."""
    bars, _ = DataSpec(info["symbol"], info["tf"], info["session"], info["tz"], info.get("history_from"), "ohlc", str(data_dir),
                       info.get("data_from")).load()  # fmt: skip
    n, first, last = int(info["bars"]), pd.Timestamp(info["first_bar"]), pd.Timestamp(info["last_bar"])
    if len(bars) < n or bars.index[0] != first or bars.index[n - 1] != last:
        now = f"{len(bars):,} bars {bars.index[0]} → {bars.index[-1]}" if len(bars) else "no bars"
        raise ValueError(f"the bars changed since {run} was written ({n:,} bars {first} → {last} then, {now} now), so its trade bar "
                         "numbers no longer match; run the backtest again to draw its charts")  # fmt: skip
    return bars.iloc[:n]


@app.command("chart")
@_friendly
def chart_cmd(
    run: Path = typer.Argument(..., exists=True, file_okay=False, help="Run folder of guban run or wfo (trades.csv + metrics.json or wfo.json)"),
    trades: list[int] = typer.Option([], "--trade", help="Trade number to draw (repeatable; default: every trade)"),
    data_dir: Path = DATA_DIR,
    workers: int = WORKERS,
    open_image: bool = typer.Option(False, "--open", help="Open the first drawn image when done", rich_help_panel=OUTPUT),
) -> None:
    """Draw trade price-chart images, <run>/trades/trade_<no>.png, the images report.html's trade viewer shows."""
    import json

    from guban.config import get_contract
    from guban.parity import load_our_trades
    from guban.trade_charts import FOLDER, image_name, write_trade_charts

    meta = next((run / name for name in ("metrics.json", "wfo.json") if (run / name).is_file()), None)
    if meta is None or not (run / "trades.csv").is_file():
        raise ValueError(f"{run} is not a run folder: it needs trades.csv and metrics.json (guban run) or wfo.json (guban wfo)")
    info = json.loads(meta.read_text(encoding="utf-8"))["run"]
    tr = load_our_trades(run / "trades.csv")
    if not len(tr):
        raise ValueError(f"{run / 'trades.csv'} has no trades")
    missing = sorted(set(trades) - set(tr["trade_no"].astype(int)))
    if missing:
        raise ValueError(f"{run / 'trades.csv'} has no trade {_trade_list(missing)} (trades {int(tr['trade_no'].min())} to {int(tr['trade_no'].max())})")
    for col in ("entry_time", "exit_time"):
        tr[col] = tr[col].dt.tz_convert(info["tz"])  # titles in the chart time zone, as the run drew them
    t0 = time.perf_counter()
    bars = _run_bars(run, info, data_dir)
    nos = sorted(set(trades)) or None
    n = write_trade_charts(tr, bars, run / FOLDER, get_contract(info["contract"]).tick_size, workers, nos)
    first = run / FOLDER / image_name(nos[0] if nos else int(tr["trade_no"].iat[0]))
    drawn = ", ".join(str(run / FOLDER / image_name(no)) for no in nos) if nos and len(nos) <= 5 else f"{run / FOLDER}{os.sep}trade_*.png"
    typer.echo(f"Drew {n:,} trade chart{'s' if n != 1 else ''} in {time.perf_counter() - t0:.1f} s: {drawn}")
    if open_image:
        _open(first)


@app.command("gui")
@_friendly
def gui_cmd(
    smoke: bool = typer.Option(False, "--smoke", help="Drive the tabs in a hidden window, save a screenshot per step to reports/_gui_smoke/, exit 0 if every step passed"),
) -> None:
    """Open Guban Studio, the desktop GUI (Electron app in gui/; installs Electron on first launch)."""
    import json
    import shutil
    import subprocess

    gui = Path(__file__).resolve().parents[2] / "gui"
    if not (gui / "package.json").is_file():
        raise OSError(f"{gui} has no package.json: Guban Studio runs from the repo's gui/ folder")
    node, npm = shutil.which("node"), shutil.which("npm")
    if node is None or npm is None:
        raise OSError("node/npm not found: install Node.js (https://nodejs.org), then run guban gui again")
    electron = gui / "node_modules" / "electron"
    if not (electron / "package.json").is_file():
        typer.echo("Installing Electron into gui/node_modules (first launch)...")
        if subprocess.run([npm, "install"], cwd=gui).returncode:
            raise RuntimeError(f"npm install failed in {gui}: fix the error above, then run guban gui again")
    if not (electron / "dist").is_dir():
        typer.echo("Downloading the Electron binary (first launch)...")
    # require('electron') is the executable's path; it downloads the binary when it is missing (Electron >= 45)
    found = subprocess.run([node, "-e", "process.stdout.write('\\n' + require('electron'))"], cwd=gui, capture_output=True, text=True)
    exe = Path(found.stdout.strip().splitlines()[-1]) if found.returncode == 0 and found.stdout.strip() else None
    if exe is None or not exe.is_file():
        raise RuntimeError(f"Electron is not installed correctly in {electron}: delete that folder and run guban gui again\n{found.stderr.strip()}")
    env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}  # set in VS Code terminals: Electron would start as plain Node
    result = REPORTS / "_gui_smoke" / "result.json"
    if smoke:
        result.unlink(missing_ok=True)
    code = subprocess.run([str(exe), str(gui), *(["--smoke"] if smoke else [])], env=env).returncode
    if smoke:
        if not result.is_file():
            raise RuntimeError(f"the smoke run wrote no {result} (exit code {code})")
        for r in json.loads(result.read_text(encoding="utf-8"))["results"]:
            typer.echo(f"{'ok  ' if r['ok'] else 'FAIL'} {r['step']:<22} {r['ms']:>6,} ms  {json.dumps(r['detail'], ensure_ascii=False)[:160]}")
        typer.echo(f"Screenshots: {result.parent}")
    raise typer.Exit(code)


# ---------------------------------------------------------------------------
# guban store: the shared market-data store (Phase 14). build and verify share these options.

store_app = typer.Typer(help="Shared market-data store (config/store.toml): build it from DeepCharts, verify it.", no_args_is_help=True)
app.add_typer(store_app, name="store")
STORE = "Store"
STORE_SYMBOLS = typer.Option(None, "--symbol", help="Store symbol, repeatable (default: every symbol in config/store.toml)", rich_help_panel=STORE)
STORE_ROOT = typer.Option(None, "--root", file_okay=False, help="Store folder (default: config/store.toml root)", rich_help_panel=STORE)
STORE_SOURCE = typer.Option(None, "--source", file_okay=False, help="DeepCharts Database folder, opened read-only (default: config/store.toml source)", rich_help_panel=STORE)


def _store_config(root: Path | None, source: Path | None, **overrides):
    from dataclasses import replace

    from guban.store import load_store_config

    cfg = load_store_config()
    given = {k: v for k, v in {"root": root, "source": source, **overrides}.items() if v is not None}
    return replace(cfg, **given)


@contextlib.contextmanager
def _keep_awake():
    """Keep Windows from sleeping while a long build runs (a sleeping PC stretched a 5-minute build to 3 hours)."""
    if sys.platform != "win32":
        yield
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)


def _store_progress(step: int = 500):
    def progress(symbol: str, done: int, total: int) -> None:
        if done % step == 0 or done == total:
            typer.echo(f"  {symbol}: {done:,}/{total:,}")

    return progress


@store_app.command("build")
@_friendly
def store_build_cmd(
    symbols: list[str] = STORE_SYMBOLS,
    root: Path = STORE_ROOT,
    source: Path = STORE_SOURCE,
    rebuild: list[str] = typer.Option([], "--rebuild", help="Build stored sessions again from their current source: SYM:YYYYMMDD, SYM:changed or SYM:all (repeatable)", rich_help_panel=STORE),
    workers: int = WORKERS,
    settle_minutes: float = typer.Option(None, "--settle-minutes", help="Minutes a session file must be unmodified (default: config)", rich_help_panel=STORE),
) -> None:
    """Add every finished session of the symbols in config/store.toml to the write-once store; list skipped and changed sources."""
    from guban.store import build_store

    cfg = _store_config(root, source, settle_minutes=settle_minutes)
    typer.echo(f"Building {cfg.root} from {cfg.source} (read-only)")
    with _keep_awake():
        reports = build_store(cfg, symbols or None, rebuild, workers=workers, progress=_store_progress())
    typer.echo(f"Bars from: {cfg.bars} records (config/store.toml bars)")
    for r in reports:
        typer.echo(f"{r.symbol}: built {r.built:,} sessions ({r.tick_built:,} from ticks, {r.built - r.tick_built:,} from minute records"
                   + (f"; {r.rebuilt:,} of them rebuilt" if r.rebuilt else "") + f"), {r.already:,} already stored, {r.empty:,} empty files, "
                   f"{r.rolls} rolls; {r.files_written:,} files written in {r.seconds:,.0f} s")  # fmt: skip
        if r.folder_missing:
            typer.echo(f"  {cfg.source / cfg.symbols[r.symbol]} is gone: nothing to add; the store keeps its {r.gone:,} sessions")
        elif r.gone:
            typer.echo(f"  {r.gone:,} stored sessions have no source file any more (kept as stored)")
        for key, why in r.skipped:
            typer.echo(f"  skipped {key}: {why}")
        for key, why in r.changed[:20]:
            typer.echo(f"  CHANGED SOURCE {key}: {why} (not applied; --rebuild {r.symbol}:{key[-8:]} or {r.symbol}:changed)")
        if len(r.changed) > 20:
            typer.echo(f"  ... {len(r.changed) - 20:,} more changed sources (--rebuild {r.symbol}:changed rebuilds them all)")


@store_app.command("verify")
@_friendly
def store_verify_cmd(
    symbols: list[str] = STORE_SYMBOLS,
    root: Path = STORE_ROOT,
    source: Path = STORE_SOURCE,
) -> None:
    """Check the stored files; minute-built sessions must equal their source, tick-built ones meet SR3. Exit 1 on FAIL."""
    from guban.store import verify_store
    from guban.store.build import OHLC_TARGET

    cfg = _store_config(root, source)
    results = verify_store(cfg, symbols or None, progress=_store_progress(200))
    for v in results:
        verdict = "PASS" if v.passed else "FAIL"
        typer.echo(f"{v.symbol}: {verdict}  {v.sessions:,} sessions: {v.minute_sessions:,} minute-built equal their source, "
                   f"{v.tick_sessions:,} tick-built (equal volume {v.volume_match:.4%} of {v.minutes:,} minutes, target {v.volume_target:.0%}; "
                   f"equal OHLC {v.ohlc_match:.4%}, target {OHLC_TARGET:.1%}), {v.not_compared:,} not compared (source gone or changed); "
                   f"{v.sources_gone:,} source files gone")  # fmt: skip
        for p in v.problems[:20]:
            typer.echo(f"  problem: {p}")
        for c in v.changed[:20]:
            typer.echo(f"  changed source: {c}")
    if not all(v.passed for v in results):
        raise typer.Exit(1)


def _floats(text: str | None, flag: str) -> tuple[float, ...] | None:
    if text is None:
        return None
    try:
        return tuple(float(v) for v in text.replace(",", " ").split())
    except ValueError as e:
        raise typer.BadParameter("use numbers separated by commas, e.g. 0.5,1,1.5", param_hint=flag) from e


@app.command("propfirm")
@_friendly
def propfirm_cmd(
    trades: Path = typer.Option(..., "--trades", exists=True, dir_okay=False, help="Trade list: guban trades.csv, or an MC trade export (.csv / .xlsx)"),
    rules: str = typer.Option("apex_50k_eod", "--rules", help="Rules name in config/propfirm/ (or a .toml path)"),
    size: float = typer.Option(None, "--size", help="Multiplier on the trade file's daily P&L (rules file default: 1.0)", rich_help_panel="Simulation"),
    sims: int = typer.Option(None, "--sims", help="Simulations in the main run", rich_help_panel="Simulation"),
    sweep_sims: int = typer.Option(None, "--sweep-sims", help="Simulations per sweep size and per degradation scenario", rich_help_panel="Simulation"),
    sweep: str = typer.Option(None, "--sweep", help="Sizes to sweep, e.g. 0.5,1,1.5 (empty string: no sweep)", rich_help_panel="Simulation"),
    seed: int = typer.Option(None, "--seed", help="Random seed", rich_help_panel="Simulation"),
    commission: float = typer.Option(None, "--commission", help="USD per contract per trade, deducted on top of the file's (already net) profit", rich_help_panel=COSTS),
    slippage: float = typer.Option(None, "--slippage", help="USD per contract per trade, deducted on top of the file's (already net) profit", rich_help_panel=COSTS),
    risk: float = typer.Option(None, "--risk", help="1R in USD per contract, for R-multiples", rich_help_panel="Simulation"),
    out: Path = _out_option("<trades>_<rules>"),
    open_report: bool = _open_option("propfirm.html"),
) -> None:
    """Prop-firm evaluation Monte Carlo on a trade list: pass rate, degradation scenarios and a size sweep."""
    import json

    from guban.metrics import jsonable
    from guban.propfirm import FAIL_DD, FAIL_TIME, load_setup, load_trades, run_propfirm
    from guban.propfirm.report import write_propfirm_report

    try:
        setup = load_setup(rules)
    except FileNotFoundError as e:
        raise typer.BadParameter(str(e), param_hint="--rules") from e
    setup = setup.with_overrides(size=size, sims=sims, sweep_sims=sweep_sims, sweep=_floats(sweep, "--sweep"), seed=seed,
                                 commission=commission, slippage=slippage, risk_per_contract=risk)  # fmt: skip
    try:
        tr = load_trades(trades)
        res = run_propfirm(tr, setup, progress=lambda msg: typer.echo(f"  {msg}"))
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    stem = trades.parent.name if trades.stem == "trades" else trades.stem
    out = out or REPORTS / f"{_slug(stem)}_{setup.key}"
    out.mkdir(parents=True, exist_ok=True)

    r, t, m, s = setup.rules, res.stats, res.main.summary(), res.summary()
    res.daily.to_csv(out / "daily.csv", index=False, date_format="%Y-%m-%d")
    res.scenario_table().to_csv(out / "degradation.csv", index=False)
    res.sweep_table().to_csv(out / "sweep.csv", index=False)
    (out / "propfirm.json").write_text(json.dumps(jsonable({"trades_file": str(trades), **res.to_json()}), indent=2, default=str) + "\n", encoding="utf-8")
    c = t["contracts"]
    meta = {
        "title": f"Prop-firm Monte Carlo · {r.name}",
        "subtitle": f"{trades.name} · {t['first']:%Y-%m-%d} → {t['last']:%Y-%m-%d} · {t['trades']:,} trades",
        "details": {
            "Rules": f"{setup.key}.toml",
            "Profit target": f"${r.profit_target:,.0f}",
            "Max drawdown": f"${r.max_drawdown:,.0f} ({'end-of-day trailing' if r.eod_trailing else 'trailing'})",
            "Deadline": f"{r.max_trading_days} trading days" + (f", at least {r.min_trading_days}" if r.min_trading_days else ""),
            "Daily loss limit": f"${r.daily_loss_limit:,.0f}" if r.daily_loss_limit > 0 else "none",
            "Account size (not simulated)": f"${r.account_size:,.0f}",
            "Size": f"{setup.size:g}× the file" + (f" ({setup.size * c:g} contract{'' if setup.size * c == 1 else 's'})" if c is not None else ""),
            "Added costs (per contract, per trade)": f"commission ${setup.commission:g}, slippage ${setup.slippage:g}",
            "Simulations": f"{setup.sims:,} main, {setup.sweep_sims:,} per scenario and sweep size, seed {setup.seed}",
            "Run time": f"{res.seconds:,.1f} s",
        },
        "notes": res.notes,
    }
    html_path = write_propfirm_report(res, out / "propfirm.html", meta)

    lo, hi = m["pass_rate_ci"]
    typer.echo(f"{r.name} on {trades.name}: {t['trades']:,} trades, {len(res.daily):,} weekdays ({int(res.daily['is_trade_day'].sum()):,} with trades)")
    typer.echo(f"  target ${r.profit_target:,.0f}  max drawdown ${r.max_drawdown:,.0f}  deadline {r.max_trading_days} days  size {setup.size:g}x  "
               f"added costs ${setup.commission + setup.slippage:,.2f}/contract")  # fmt: skip
    typer.echo(f"  win rate {t['win_rate_net']:.1%}  avg trade ${t['avg_trade_net']:,.2f}  EV {t['ev_r']:.4f} R  P(EV <= 0) {t['p_ev_usd_le_0']:.3f}")
    typer.echo(f"  PASS RATE {m['pass_rate']:.1%} [95% CI {lo:.1%} - {hi:.1%}]  of {m['n_sims']:,}: passed {m['passed']:,}  fail DD {m['fail_dd']:,}  "
               f"fail daily {m['fail_daily']:,}  fail time {m['fail_time']:,}")  # fmt: skip
    if m["avg_days_to_pass"] is not None:
        typer.echo(f"  days to pass avg {m['avg_days_to_pass']:.1f} median {m['median_days_to_pass']:.1f}  max DD avg ${m['avg_max_dd']:,.0f} "
                   f"P95 ${m['p95_max_dd']:,.0f}  expected attempts {s['expected_attempts']:.1f}  verdict {s['verdict']}")  # fmt: skip
    for sc, sr in res.scenarios:
        typer.echo(f"  {sc.label:<32} pass {sr.pass_rate:6.1%}  fail DD {sr.rate(FAIL_DD):6.1%}")
    if res.sweep:
        typer.echo("  size  " + " ".join(f"{sr.size:>6g}" for sr in res.sweep))
        typer.echo("  pass  " + " ".join(f"{sr.pass_rate:>6.1%}" for sr in res.sweep))
        typer.echo("  DD    " + " ".join(f"{sr.rate(FAIL_DD):>6.1%}" for sr in res.sweep))
        typer.echo("  time  " + " ".join(f"{sr.rate(FAIL_TIME):>6.1%}" for sr in res.sweep))
        best = res.best_size
        typer.echo(f"  best size: {best.size:g}x -> {best.pass_rate:.1%}" if best else "  best size: none passed")
    typer.echo(f"  {res.seconds:,.1f} s")
    typer.echo(f"Wrote {out / 'propfirm.json'}, {out / 'sweep.csv'}, {out / 'degradation.csv'}, {out / 'daily.csv'}, {html_path}")
    if open_report:
        _open(html_path)
