import ast
import time
from pathlib import Path

import pandas as pd
import typer

from guban.data import import_store
from guban.data.resample import cache_path, load_bars, parse_timeframe
from guban.optimize import MAX_COMBINATIONS, DataSpec

app = typer.Typer(help="Guban Backtester — CME futures backtesting with MultiCharts parity.")


@app.callback()
def main() -> None:
    pass


@app.command("import")
def import_cmd(
    source: Path = typer.Argument(..., help="Per-contract store folder, e.g. Database/NQ-CME"),
    symbol: str = typer.Option(..., "--symbol", help="Symbol name to store under data/, e.g. NQ"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir"),
    roll: str = typer.Option("volume", "--roll", help="volume (highest volume) or expiry (N days before the 3rd Friday; MC style)"),
    days_before: int = typer.Option(2, "--roll-days-before", help="--roll expiry: days before expiration to switch"),
) -> None:
    """Build a continuous 1-minute series from a per-contract store and save it as Parquet."""
    summary = import_store(source, symbol, data_dir, roll, days_before)
    typer.echo(str(summary))
    typer.echo(f"Wrote {data_dir / symbol / '1m.parquet'}")


@app.command("bars")
def bars_cmd(
    symbol: str = typer.Option(..., "--symbol", help="Imported symbol, e.g. NQ"),
    tf: str = typer.Option("5m", "--tf", help="Timeframe: 1m, 5m, 15m, 1h, 1d ..."),
    session: str = typer.Option("rth", "--session", help="Session template from config/sessions.toml"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir"),
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
        raise typer.BadParameter(str(e)) from e


def _parse_inputs(pairs: list[str], defaults: dict) -> dict:
    out = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        key = key.strip()
        if not sep or key not in defaults:
            raise typer.BadParameter(f"--set {pair!r}: use name=value with one of {sorted(defaults)}")
        try:
            value = ast.literal_eval(raw.strip())
        except (ValueError, SyntaxError):
            value = raw.strip()
        default = defaults[key]
        if isinstance(default, float) and isinstance(value, int) and not isinstance(value, bool):
            value = float(value)
        out[key] = value
    return out


@app.command("run")
def run_cmd(
    strategy_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Python file defining a Strategy subclass"),
    symbol: str = typer.Option(..., "--symbol", help="Imported data symbol, e.g. NQ"),
    contract: str = typer.Option(None, "--contract", help="Contract spec for P&L (default: --symbol), e.g. MNQ"),
    tf: str = typer.Option("5m", "--tf", help="Strategy timeframe"),
    session: str = typer.Option("rth", "--session", help="Session template from config/sessions.toml"),
    history_from: str = typer.Option(None, "--history-from", help="Prepend this symbol's bars from before --symbol's first bar (e.g. NQ for MNQ)"),
    tz: str = typer.Option("America/New_York", "--tz", help="Chart time zone the strategy sees (MC exchange time: America/Chicago)"),
    fill_mode: str = typer.Option("magnifier", "--fill-mode", help="magnifier (1-minute sub-bars) or ohlc"),
    commission: float = typer.Option(0.0, "--commission", help="USD per contract per round trip"),
    slippage: float = typer.Option(0.0, "--slippage", help="USD per contract per round trip (half per market/stop fill)"),
    inputs: list[str] = typer.Option([], "--set", help="Input override name=value (repeatable)"),
    class_name: str = typer.Option(None, "--class", help="Strategy class, when the file defines several"),
    out: Path = typer.Option(None, "--out", help="Output folder (default reports/<strategy>_<symbol>_<tf>_<session>)"),
    capital: float = typer.Option(1_000_000.0, "--capital", help="Initial capital, for trades.csv cum_profit_pct (MC run: 1,000,000)"),
    report: bool = typer.Option(True, "--report/--no-report", help="Write report.html"),
    open_report: bool = typer.Option(False, "--open", help="Open report.html in the browser when done"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir"),
) -> None:
    """Backtest a strategy; write trades.csv (MC layout), metrics.json and report.html."""
    import json
    import webbrowser

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
    out = out or Path("reports") / f"{strategy_file.stem}_{symbol}_{tf}_{session}"
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
            "Data": f"{symbol} {tf}, {session} session" + (f" (+{history_from} history)" if history_from else ""),
            "Contract spec": f"{spec.symbol}: ${spec.point_value:g}/pt, tick {spec.tick_size:g}",
            "Chart time zone": tz,
            "Fill mode": fill_mode,
            "Costs (per contract, round trip)": f"commission ${commission:g}, slippage ${slippage:g}",
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
        webbrowser.open(report_path.resolve().as_uri())


@app.command("optimize")
def optimize_cmd(
    strategy_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Python file defining a Strategy subclass"),
    grid_specs: list[str] = typer.Option(..., "--grid", help="Input range name=start:stop[:step], name=a,b,c or name=v (repeatable; several per flag, space-separated)"),
    symbol: str = typer.Option(..., "--symbol", help="Imported data symbol, e.g. NQ"),
    contract: str = typer.Option(None, "--contract", help="Contract spec for P&L (default: --symbol), e.g. MNQ"),
    tf: str = typer.Option("5m", "--tf", help="Strategy timeframe"),
    session: str = typer.Option("rth", "--session", help="Session template from config/sessions.toml"),
    history_from: str = typer.Option(None, "--history-from", help="Prepend this symbol's bars from before --symbol's first bar"),
    tz: str = typer.Option("America/New_York", "--tz", help="Chart time zone the strategy sees (MC exchange time: America/Chicago)"),
    fill_mode: str = typer.Option("magnifier", "--fill-mode", help="magnifier (1-minute sub-bars) or ohlc"),
    commission: float = typer.Option(0.0, "--commission", help="USD per contract per round trip"),
    slippage: float = typer.Option(0.0, "--slippage", help="USD per contract per round trip (half per market/stop fill)"),
    inputs: list[str] = typer.Option([], "--set", help="Fixed input override name=value (repeatable)"),
    class_name: str = typer.Option(None, "--class", help="Strategy class, when the file defines several"),
    metric: str = typer.Option("net_profit", "--metric", help="Rank by: net_profit, profit_factor, return_on_account, sharpe_daily, max_drawdown_intraday, ..."),
    min_trades: int = typer.Option(30, "--min-trades", help="Rank only runs with at least this many trades"),
    workers: int = typer.Option(None, "--workers", help="Worker processes (default: CPU cores)"),
    yes: bool = typer.Option(False, "--yes", help=f"Run grids larger than {MAX_COMBINATIONS:,} combinations"),
    x: str = typer.Option(None, "--x", help="Heatmap x input (default: first varying grid input)"),
    y: str = typer.Option(None, "--y", help="Heatmap y input (default: second varying grid input)"),
    top: int = typer.Option(50, "--top", help="Rows in the HTML ranked table (results.csv has every run)"),
    out: Path = typer.Option(None, "--out", help="Output folder (default reports/<strategy>_<symbol>_<tf>_<session>_opt)"),
    open_report: bool = typer.Option(False, "--open", help="Open optimize.html in the browser when done"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir"),
) -> None:
    """Grid-search strategy inputs on all CPU cores; write results.csv, optimize.json and optimize.html (heatmap)."""
    import json
    import webbrowser

    from guban.config import get_contract
    from guban.engine import Costs
    from guban.metrics import jsonable
    from guban.optimize import METRICS, TooManyCombinations, combinations, optimize, parse_grid
    from guban.optimize_report import heatmap_axes, write_optimize_report
    from guban.report import fmt_value

    cls = _load_strategy_class(strategy_file, class_name)
    fixed = _parse_inputs(inputs, cls.inputs)
    try:
        grid = parse_grid([s for spec in grid_specs for s in spec.split()], cls.inputs)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--grid") from e
    if metric not in METRICS:
        raise typer.BadParameter(f"use one of {list(METRICS)}", param_hint="--metric")
    both = set(grid) & set(fixed)
    if both:
        raise typer.BadParameter(f"{sorted(both)} are both in --grid and --set")
    try:
        heatmap_axes(grid, x, y)  # check --x / --y before the long run
    except ValueError as e:
        raise typer.BadParameter(str(e)) from e
    n = len(combinations(grid))
    if n > MAX_COMBINATIONS and not yes:
        typer.echo(f"The grid has {n:,} combinations (more than {MAX_COMBINATIONS:,}). Narrow it, or rerun with --yes.", err=True)
        raise typer.Exit(2)
    spec = get_contract(contract or symbol)
    out = out or Path("reports") / f"{strategy_file.stem}_{symbol}_{tf}_{session}_opt"

    t0 = time.perf_counter()
    data = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir))
    bars, _ = data.load()  # also builds any missing bar caches before the workers read them
    typer.echo(f"{cls.__name__} on {symbol} {tf} {session} ({spec.symbol} spec, {fill_mode}, chart tz {tz}): "
               f"{n:,} combinations, {len(bars):,} bars")  # fmt: skip
    step = max(1, n // 10)

    def progress(done: int, total: int) -> None:
        if done % step == 0 or done == total:
            el = time.perf_counter() - t0
            typer.echo(f"  {done:,}/{total:,} runs  {el:,.0f} s" + (f"  (~{el / done * (total - done):,.0f} s left)" if done < total else ""))

    try:
        opt = optimize(strategy_file, grid, spec, class_name=cls.__name__, costs= Costs(commission=commission, slippage=slippage), data=data, fixed=fixed,
                       metric=metric, min_trades=min_trades, workers=workers, confirm=yes, progress=progress)  # fmt: skip
    except TooManyCombinations as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    csv_path = opt.write_csv(out / "results.csv")

    label, kind, _ = METRICS[metric]
    grid_text = " · ".join(f"{k}: {', '.join(map(str, v))}" for k, v in grid.items())
    meta = {
        "title": f"Optimization · {cls.__name__} · {spec.symbol} {tf}",
        "subtitle": f"{bars.index[0]:%Y-%m-%d} → {bars.index[-1]:%Y-%m-%d} · {strategy_file.name}",
        "details": {
            "Data": f"{symbol} {tf}, {session} session" + (f" (+{history_from} history)" if history_from else ""),
            "Contract spec": f"{spec.symbol}: ${spec.point_value:g}/pt, tick {spec.tick_size:g}",
            "Chart time zone": tz,
            "Fill mode": fill_mode,
            "Costs (per contract, round trip)": f"commission ${commission:g}, slippage ${slippage:g}",
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
        webbrowser.open(html_path.resolve().as_uri())


@app.command("wfo")
def wfo_cmd(
    strategy_file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Python file defining a Strategy subclass"),
    grid_specs: list[str] = typer.Option(..., "--grid", help="Input range name=start:stop[:step], name=a,b,c or name=v (repeatable; several per flag, space-separated)"),
    symbol: str = typer.Option(..., "--symbol", help="Imported data symbol, e.g. NQ"),
    contract: str = typer.Option(None, "--contract", help="Contract spec for P&L (default: --symbol), e.g. MNQ"),
    tf: str = typer.Option("5m", "--tf", help="Strategy timeframe"),
    session: str = typer.Option("rth", "--session", help="Session template from config/sessions.toml"),
    history_from: str = typer.Option(None, "--history-from", help="Prepend this symbol's bars from before --symbol's first bar"),
    tz: str = typer.Option("America/New_York", "--tz", help="Chart time zone the strategy sees (MC exchange time: America/Chicago)"),
    fill_mode: str = typer.Option("magnifier", "--fill-mode", help="magnifier (1-minute sub-bars) or ohlc"),
    commission: float = typer.Option(0.0, "--commission", help="USD per contract per round trip"),
    slippage: float = typer.Option(0.0, "--slippage", help="USD per contract per round trip (half per market/stop fill)"),
    inputs: list[str] = typer.Option([], "--set", help="Fixed input override name=value (repeatable)"),
    class_name: str = typer.Option(None, "--class", help="Strategy class, when the file defines several"),
    is_months: int = typer.Option(12, "--is", help="In-sample window, months"),
    oos_months: int = typer.Option(3, "--oos", help="Out-of-sample window, months"),
    step_months: int = typer.Option(None, "--step", help="Months between windows (default: --oos)"),
    anchored: bool = typer.Option(False, "--anchored/--rolling", help="Anchored: every in-sample window starts at --start and grows"),
    start: str = typer.Option(None, "--start", help="First in-sample day, YYYY-MM-DD session date (default: first whole month of data)"),
    metric: str = typer.Option("net_profit", "--metric", help="Pick each in-sample winner by: net_profit, profit_factor, return_on_account, sharpe_daily, ..."),
    min_trades: int = typer.Option(30, "--min-trades", help="Only runs with at least this many trades in the in-sample window can win it"),
    workers: int = typer.Option(None, "--workers", help="Worker processes (default: CPU cores)"),
    yes: bool = typer.Option(False, "--yes", help=f"Run grids larger than {MAX_COMBINATIONS:,} combinations"),
    capital: float = typer.Option(1_000_000.0, "--capital", help="Initial capital, for trades.csv cum_profit_pct"),
    out: Path = typer.Option(None, "--out", help="Output folder (default reports/<strategy>_<symbol>_<tf>_<session>_wfo)"),
    open_report: bool = typer.Option(False, "--open", help="Open wfo.html in the browser when done"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir"),
) -> None:
    """Walk-forward: optimize each in-sample window, trade the winner out of sample; write the stitched result."""
    import json
    import webbrowser

    from guban.config import get_contract
    from guban.engine import Costs
    from guban.metrics import jsonable
    from guban.optimize import METRICS, TooManyCombinations, combinations, parse_grid
    from guban.report import fmt_value
    from guban.wfo import walk_forward
    from guban.wfo_report import write_wfo_report

    cls = _load_strategy_class(strategy_file, class_name)
    fixed = _parse_inputs(inputs, cls.inputs)
    try:
        grid = parse_grid([s for spec in grid_specs for s in spec.split()], cls.inputs)
    except ValueError as e:
        raise typer.BadParameter(str(e), param_hint="--grid") from e
    if metric not in METRICS:
        raise typer.BadParameter(f"use one of {list(METRICS)}", param_hint="--metric")
    both = set(grid) & set(fixed)
    if both:
        raise typer.BadParameter(f"{sorted(both)} are both in --grid and --set")
    n = len(combinations(grid))
    if n > MAX_COMBINATIONS and not yes:
        typer.echo(f"The grid has {n:,} combinations (more than {MAX_COMBINATIONS:,}). Narrow it, or rerun with --yes.", err=True)
        raise typer.Exit(2)
    spec = get_contract(contract or symbol)
    out = out or Path("reports") / f"{strategy_file.stem}_{symbol}_{tf}_{session}_wfo"
    data = DataSpec(symbol, tf, session, tz, history_from, fill_mode, str(data_dir))
    typer.echo(f"{cls.__name__} on {symbol} {tf} {session} ({spec.symbol} spec, {fill_mode}, chart tz {tz}): "
               f"{n:,} combinations, each run once over all the data")  # fmt: skip
    t0 = time.perf_counter()
    step = max(1, n // 10)

    def progress(done: int, total: int) -> None:
        if done % step == 0 or done == total:
            el = time.perf_counter() - t0
            typer.echo(f"  {done:,}/{total:,} runs  {el:,.0f} s" + (f"  (~{el / done * (total - done):,.0f} s left)" if done < total else ""))

    try:
        wf = walk_forward(strategy_file, grid, spec, Costs(commission=commission, slippage=slippage), data=data, fixed=fixed,
                          class_name=cls.__name__, is_months=is_months, oos_months=oos_months, step_months=step_months,
                          anchored=anchored, start=start, metric=metric, min_trades=min_trades, workers=workers, confirm=yes,
                          progress=progress)  # fmt: skip
    except TooManyCombinations as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(2) from e
    except ValueError as e:  # window layout that does not fit the data
        raise typer.BadParameter(str(e)) from e

    bars, s, p = wf.bars, wf.summary, wf.pooled
    trades_path = wf.oos_result().write_trades_csv(out / "trades.csv", capital)
    csv_paths = wf.write_csvs(out)
    label = METRICS[metric][0]
    layout = f"{'anchored' if anchored else 'rolling'}, {is_months} m in-sample / {oos_months} m out-of-sample, step {wf.step_months} m"
    notes = []
    if history_from:
        notes.append(f"{history_from} bars stand in for {symbol} before {symbol}'s first bar.")
    if wf.boundary_trades:
        notes.append(f"{wf.boundary_trades} trade(s) were open across an out-of-sample window boundary where the inputs change: each counts "
                     "whole in its entry window's trades, but the equity curve switches to the next window's inputs at the boundary.")  # fmt: skip
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
            "Data": f"{symbol} {tf}, {session} session" + (f" (+{history_from} history)" if history_from else ""),
            "Contract spec": f"{spec.symbol}: ${spec.point_value:g}/pt, tick {spec.tick_size:g}",
            "Chart time zone": tz,
            "Fill mode": fill_mode,
            "Costs (per contract, round trip)": f"commission ${commission:g}, slippage ${slippage:g}",
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
    typer.echo(f"  {n:,} runs + {int(s['combo'].nunique())} re-runs in {wf.seconds:,.1f} s on {wf.workers} workers")
    typer.echo(f"Wrote {trades_path}, {csv_paths[0]}, {csv_paths[1]}, {out / 'wfo.json'}, {html_path}")
    if open_report:
        webbrowser.open(html_path.resolve().as_uri())


@app.command("compare")
def compare_cmd(
    ours: Path = typer.Option(..., "--ours", exists=True, dir_okay=False, help="Our trades.csv"),
    mc: Path = typer.Option(..., "--mc", exists=True, dir_okay=False, help="MC trade export (.csv or report .xlsx)"),
    contract: str = typer.Option(..., "--contract", help="Contract spec for tick size and point value, e.g. MNQ"),
    tf: str = typer.Option("5m", "--tf", help="Strategy timeframe: entries match within one bar"),
    mc_tz: str = typer.Option("America/Chicago", "--mc-tz", help="Time zone of the MC chart the export came from"),
    start: str = typer.Option(None, "--start", help="Window start (default: the later of the two first entries)"),
    end: str = typer.Option(None, "--end", help="Window end (default: the earlier of the two last entries)"),
    out: Path = typer.Option(None, "--out", help="diff.html path (default: next to --ours)"),
) -> None:
    """Compare our trades with an MC export (S3): match rate, prices within 1 tick, net profit within 3%."""
    from guban.config import get_contract
    from guban.parity import compare_trades, load_mc_trades, load_our_trades, write_diff_html

    spec = get_contract(contract)
    n = parse_timeframe(tf)
    bar = pd.Timedelta(minutes=n) if n else pd.Timedelta(days=1)
    def when(s: str | None) -> pd.Timestamp | None:  # naive dates/times are MC chart time
        if s is None:
            return None
        t = pd.Timestamp(s)
        return t.tz_localize(mc_tz) if t.tzinfo is None else t

    cmp = compare_trades(load_our_trades(ours), load_mc_trades(mc, mc_tz), bar, spec.tick_size, spec.point_value, when(start), when(end))
    path = write_diff_html(cmp, out or ours.parent / "diff.html", f"Parity: {ours.parent.name} vs {mc.name}", mc_tz)
    typer.echo(cmp.summary())
    typer.echo(f"Wrote {path}")
    if not cmp.passed:
        raise typer.Exit(1)
