import ast
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
from guban.optimize import MAX_COMBINATIONS, DataSpec

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

DATA, COSTS, STRAT, SEARCH, WF, OUTPUT = "Data", "Costs", "Strategy", "Optimization", "Walk-forward", "Output"


def _check_tz(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as e:
        raise typer.BadParameter(f"unknown time zone {value!r}; use an IANA name such as America/Chicago") from e
    return value


STRATEGY_FILE = typer.Argument(..., exists=True, dir_okay=False, help="Python file defining a Strategy subclass")
SYMBOL = typer.Option(..., "--symbol", help="Imported data symbol, e.g. NQ or MNQ", rich_help_panel=DATA)
CONTRACT = typer.Option(None, "--contract", help="Contract spec for P&L (default: --symbol), e.g. MNQ", rich_help_panel=DATA)
TF = typer.Option("5m", "--tf", help="Strategy timeframe: 1m, 5m, 15m, 1h, 1d ...", rich_help_panel=DATA)
SESSION = typer.Option("rth", "--session", help="Session template from config/sessions.toml (MC parity: eth)", rich_help_panel=DATA)
HISTORY_FROM = typer.Option(None, "--history-from", help="Prepend this symbol's bars from before --symbol's first bar (e.g. NQ for MNQ)", rich_help_panel=DATA)
TZ = typer.Option("America/New_York", "--tz", callback=_check_tz, help="Chart time zone the strategy sees (MC exchange time: America/Chicago)", rich_help_panel=DATA)
FILL_MODE = typer.Option("magnifier", "--fill-mode", help="magnifier (1-minute sub-bars) or ohlc (path rule on the whole bar)", rich_help_panel=DATA)
DATA_DIR = typer.Option(Path("data"), "--data-dir", help="Folder holding the imported Parquet store", rich_help_panel=DATA)
COMMISSION = typer.Option(0.0, "--commission", help="USD per contract per round trip", rich_help_panel=COSTS)
SLIPPAGE = typer.Option(0.0, "--slippage", help="USD per contract per round trip (half per market/stop fill, none on limit fills)", rich_help_panel=COSTS)
SET_INPUTS = typer.Option([], "--set", help="Input override name=value (repeatable); with --grid, a fixed input", rich_help_panel=STRAT)
CLASS_NAME = typer.Option(None, "--class", help="Strategy class, when the file defines several", rich_help_panel=STRAT)
GRID = typer.Option(..., "--grid", help="Input range name=start:stop[:step], name=a,b,c or name=v (repeatable; several per flag, space-separated)", rich_help_panel=SEARCH)
MIN_TRADES = typer.Option(30, "--min-trades", help="Rank only runs with at least this many trades (wfo: in the in-sample window)", rich_help_panel=SEARCH)
WORKERS = typer.Option(None, "--workers", help="Worker processes (default: CPU cores)", rich_help_panel=SEARCH)
YES = typer.Option(False, "--yes", help=f"Run grids larger than {MAX_COMBINATIONS:,} combinations", rich_help_panel=SEARCH)
CAPITAL = typer.Option(1_000_000.0, "--capital", help="Initial capital, for trades.csv cum_profit_pct (MC run: 1,000,000)", rich_help_panel=OUTPUT)


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
    source: Path = typer.Argument(..., exists=True, file_okay=False, help="Per-contract store folder, e.g. Database/NQ-CME"),
    symbol: str = typer.Option(..., "--symbol", help="Symbol name to store under data/, e.g. NQ"),
    roll: Literal["volume", "expiry"] = typer.Option("volume", "--roll", help="volume: highest-volume contract; expiry: N days before the 3rd Friday (MC continuous contract)"),
    days_before: int = typer.Option(2, "--roll-days-before", help="--roll expiry: days before expiration to switch (MC: 2, the Wednesday)"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir", help="Folder for the Parquet store"),
) -> None:
    """Build a continuous 1-minute series from a per-contract store and save it as Parquet."""
    summary = import_store(source, symbol, data_dir, roll, days_before)
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
        try:
            value = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            value = raw.strip()
        default = defaults[key]
        if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        out[key] = value
    return out


def _details(symbol: str, tf: str, session: str, history_from: str | None, spec, tz: str, fill_mode: str, commission: float, slippage: float) -> dict:
    return {
        "Data": f"{symbol} {tf}, {session} session" + (f" (+{history_from} history)" if history_from else ""),
        "Contract spec": f"{spec.symbol}: ${spec.point_value:g}/pt, tick {spec.tick_size:g}",
        "Chart time zone": tz,
        "Fill mode": fill_mode,
        "Costs (per contract, round trip)": f"commission ${commission:g}, slippage ${slippage:g}",
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
    tz: str = TZ,
    fill_mode: FillMode = FILL_MODE,
    data_dir: Path = DATA_DIR,
    commission: float = COMMISSION,
    slippage: float = SLIPPAGE,
    inputs: list[str] = SET_INPUTS,
    class_name: str = CLASS_NAME,
    capital: float = CAPITAL,
    report: bool = typer.Option(True, "--report/--no-report", help="Write report.html", rich_help_panel=OUTPUT),
    out: Path = _out_option(RUN_ID),
    open_report: bool = _open_option("report.html"),
) -> None:
    """Backtest a strategy; write trades.csv (MC layout), metrics.json and report.html."""
    import json

    from guban.config import get_contract
    from guban.engine import Costs, run_backtest
    from guban.metrics import backtest_metrics, jsonable
    from guban.report import write_report

    t0 = time.perf_counter()
    cls = _load_strategy_class(strategy_file, class_name)
    strategy = cls(**_parse_inputs(inputs, cls.inputs))
    spec = get_contract(contract or symbol)
    bars, sub = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir)).load()
    t1 = time.perf_counter()
    res = run_backtest(strategy, bars, spec, Costs(commission=commission, slippage=slippage), sub_bars=sub, fill_mode=fill_mode)
    t2 = time.perf_counter()
    out = out or run_dir(strategy_file, symbol, contract, tf, session)
    path = res.write_trades_csv(out / "trades.csv", capital)
    tr = res.trades
    pos = res.open_position
    metrics = backtest_metrics(res, bars)

    title = f"{cls.__name__} · {spec.symbol} {tf}"
    notes = []
    if history_from:
        notes.append(f"{history_from} bars stand in for {symbol} before {symbol}'s first bar.")
    if res.bars_without_sub_bars:
        notes.append(f"{res.bars_without_sub_bars:,} bars had no 1-minute data and were filled from their own OHLC.")
    if pos is not None:
        notes.append(f"A position is still open at the end ({pos.direction:+d} x{pos.qty} from {pos.entry_time:%Y-%m-%d %H:%M}); it is not in the trade statistics.")
    if len(tr) and tr["ambiguous"].any():
        notes.append(f"{int(tr['ambiguous'].sum())} trade(s) have an ambiguous fill: the exit order inside one sub-bar came from the OHLC path rule, not the data.")
    meta = {
        "title": title,
        "subtitle": f"{bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d} · {strategy_file.name}",
        "details": {
            **_details(symbol, tf, session, history_from, spec, tz, fill_mode, commission, slippage),
            "Bars": f"{len(bars):,}",
            "First / last bar": f"{bars.index[0]:%Y-%m-%d %H:%M} / {bars.index[-1]:%Y-%m-%d %H:%M}",
            "Backtest time": f"{t2 - t1:.1f} s",
        },
        "inputs": vars(strategy.p),
        "notes": notes,
    }
    meta_json = {"strategy": cls.__name__, "file": str(strategy_file), "symbol": symbol, "contract": spec.symbol, "tf": tf,
                 "session": session, "tz": tz, "fill_mode": fill_mode, "history_from": history_from,
                 "commission": commission, "slippage": slippage, "capital": capital, "inputs": vars(strategy.p),
                 "first_bar": bars.index[0], "last_bar": bars.index[-1], "bars": len(bars)}  # fmt: skip
    (out / "metrics.json").write_text(json.dumps(jsonable({"run": meta_json, **metrics}), indent=2, default=str) + "\n", encoding="utf-8")
    report_path = write_report(res, bars, out / "report.html", metrics, meta) if report else None
    t3 = time.perf_counter()

    typer.echo(f"{cls.__name__} on {symbol} {tf} {session} ({spec.symbol} spec, {fill_mode}, chart tz {tz})")
    typer.echo(f"  bars: {len(bars):,}  {bars.index[0]} -> {bars.index[-1]}" + (f"  ({history_from} before {symbol}'s first bar)" if history_from else ""))
    if len(tr):
        a, acc = metrics["all"], metrics["account"]
        pf = "inf" if a["profit_factor"] == float("inf") else f"{a['profit_factor'] or 0:.2f}"
        typer.echo(f"  trades: {len(tr):,}  net profit: {res.net_profit:,.2f}  profit factor: {pf}  "
                   f"max drawdown: {acc['max_drawdown_intraday']:,.2f}  ambiguous fills: {a['ambiguous_fills']}")  # fmt: skip
    else:
        typer.echo("  trades: 0")
    if res.bars_without_sub_bars:
        typer.echo(f"  bars without 1-minute data (OHLC fallback): {res.bars_without_sub_bars}")
    if pos is not None:
        typer.echo(f"  open position at the end: {pos.direction:+d} x{pos.qty}")
    typer.echo(f"  time: load {t1 - t0:.1f} s + backtest {t2 - t1:.1f} s + report {t3 - t2:.1f} s = {t3 - t0:.1f} s")
    typer.echo(f"Wrote {path}, {out / 'metrics.json'}" + (f", {report_path}" if report_path else ""))
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
    from guban.metrics import jsonable
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
    out = out or run_dir(strategy_file, symbol, contract, tf, session, "_opt")

    t0 = time.perf_counter()
    data = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir))
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
            **_details(symbol, tf, session, history_from, spec, tz, fill_mode, commission, slippage),
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
                "commission": commission, "slippage": slippage, "fixed": fixed,
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
    out: Path = _out_option(RUN_ID + "_wfo"),
    open_report: bool = _open_option("wfo.html"),
) -> None:
    """Walk-forward: optimize each in-sample window, trade the winner out of sample; write the stitched result."""
    import json

    from guban.config import get_contract
    from guban.engine import Costs
    from guban.metrics import jsonable
    from guban.optimize import METRICS, TooManyCombinations
    from guban.report import fmt_value
    from guban.wfo import walk_forward
    from guban.wfo_report import write_wfo_report

    cls = _load_strategy_class(strategy_file, class_name)
    grid, fixed, n = _parse_search(grid_specs, inputs, cls.inputs, metric, yes)
    spec = get_contract(contract or symbol)
    out = out or run_dir(strategy_file, symbol, contract, tf, session, "_wfo")
    data = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir))
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
            **_details(symbol, tf, session, history_from, spec, tz, fill_mode, commission, slippage),
            "Windows": layout,
            "Chosen by": f"{label}, runs with ≥ {min_trades:,} in-sample trades",
            "Grid": " · ".join(f"{k}: {', '.join(map(str, v))}" for k, v in grid.items()),
            "Runs": f"{n:,} over {bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d}, {wf.seconds:,.1f} s on {wf.workers} workers",
        },
        "inputs": fixed,
        "notes": notes,
    }
    html_path = write_wfo_report(wf, out / "wfo.html", meta)
    summary = {
        "run": {"strategy": cls.__name__, "file": str(strategy_file), "symbol": symbol, "contract": spec.symbol, "tf": tf,
                "session": session, "tz": tz, "fill_mode": fill_mode, "history_from": history_from,
                "commission": commission, "slippage": slippage, "fixed": fixed, "capital": capital,
                "first_bar": bars.index[0], "last_bar": bars.index[-1], "bars": len(bars)},
        "grid": grid, "metric": metric, "min_trades": min_trades, "combinations": n,
        "layout": {"anchored": anchored, "is_months": is_months, "oos_months": oos_months, "step_months": wf.step_months, "start": s["is_from"].iat[0]},
        "workers": wf.workers, "seconds": round(wf.seconds, 2),
        "walk_forward_efficiency": p["wfe"], "is_net_per_year": p["is_net_per_year"], "oos_net_per_year": p["oos_net_per_year"],
        "boundary_trades": wf.boundary_trades,
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
