from pathlib import Path

import typer

from guban.data import import_store
from guban.data.resample import cache_path, load_bars

app = typer.Typer(help="Guban Backtester — CME futures backtesting with MultiCharts parity.")


@app.callback()
def main() -> None:
    pass


@app.command("import")
def import_cmd(
    source: Path = typer.Argument(..., help="Per-contract store folder, e.g. Database/NQ-CME"),
    symbol: str = typer.Option(..., "--symbol", help="Symbol name to store under data/, e.g. NQ"),
    data_dir: Path = typer.Option(Path("data"), "--data-dir"),
) -> None:
    """Build a continuous 1-minute series from a per-contract store and save it as Parquet."""
    summary = import_store(source, symbol, data_dir)
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
