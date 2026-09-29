import ast
import importlib.util
import inspect
import time
from pathlib import Path

import pandas as pd
import typer

from guban.data import import_store
from guban.data.resample import cache_path, load_bars, parse_timeframe, splice_history

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
    from guban.engine import Strategy

    spec = importlib.util.spec_from_file_location(f"guban_user_strategy_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise typer.BadParameter(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    found = {
        name: obj for name, obj in vars(module).items()
        if inspect.isclass(obj) and issubclass(obj, Strategy) and obj is not Strategy and obj.__module__ == module.__name__
    }  # fmt: skip
    if class_name:
        if class_name not in found:
            raise typer.BadParameter(f"{path} has no Strategy subclass {class_name!r}; found {sorted(found)}")
        return found[class_name]
    if len(found) != 1:
        raise typer.BadParameter(f"{path} defines {len(found)} Strategy subclasses {sorted(found)}; pick one with --class")
    return next(iter(found.values()))


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
    from guban.metrics import compute_metrics, jsonable
    from guban.report import write_report

    t0 = time.perf_counter()
    cls = _load_strategy_class(strategy_file, class_name)
    strategy = cls(**_parse_inputs(inputs, cls.inputs))
    spec = get_contract(contract or symbol)
    bars = load_bars(symbol, tf, session, data_dir, tz=tz)
    sub = load_bars(symbol, "1m", session, data_dir, tz=tz) if fill_mode == "magnifier" else None
    if history_from:
        bars = splice_history(bars, load_bars(history_from, tf, session, data_dir, tz=tz))
        if sub is not None:
            sub = splice_history(sub, load_bars(history_from, "1m", session, data_dir, tz=tz))
    t1 = time.perf_counter()
    res = run_backtest(strategy, bars, spec, Costs(commission=commission, slippage=slippage), sub_bars=sub, fill_mode=fill_mode)
    t2 = time.perf_counter()
    out = out or Path("reports") / f"{strategy_file.stem}_{symbol}_{tf}_{session}"
    path = res.write_trades_csv(out / "trades.csv", capital)
    tr = res.trades
    pos = res.open_position
    open_pnl = res.equity.iat[-1] - res.net_profit if pos is not None else 0.0
    metrics = compute_metrics(tr, res.equity, res.equity_low, bars["session_date"], open_pnl)

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
