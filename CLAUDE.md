# Guban Backtester

Python backtester for CME futures that reproduces MultiCharts (MC) results. Full plan: [PLAN.md](PLAN.md).

## Stack & commands
- Python 3.14, managed with uv. Package lives in `src/guban/`.
- Install: `uv sync` · Tests: `uv run pytest` · CLI: `uv run guban --help`
- Deps: pandas, numpy, pyarrow, plotly, typer; pytest (dev).

## Conventions
- Strategies are Python only (no PowerLanguage interpreter). They mirror PowerLanguage semantics:
  orders placed in `on_bar` fill on the NEXT bar; `set_stop_loss`/`set_profit_target` are per-position dollars.
- Bar timestamps are tz-aware America/New_York, labeled by bar CLOSE time (MC convention).
- Default fill mode = 1-minute Bar Magnifier; OHLC-path mode kept for comparison.
- `data/` (Parquet) and `reports/` are generated and git-ignored.

## Data
- Source store: `Database/<SYM>-CME/<YYYYMM contract>/Minute/<YYYYMMDD>.data` (binary, git-ignored, ~42 GB incl. Tick). Format documented in `src/guban/data/importer.py`.
- `uv run guban import Database/NQ-CME --symbol NQ` → `data/NQ/1m.parquet` (unadjusted continuous, volume roll).
- `uv run guban bars --symbol NQ --tf 5m --session rth` → session-aligned bars cached at `data/NQ/5m_rth.parquet` (`guban.data.resample.load_bars`).
- Contract specs and session templates: `config/contracts.toml`, `config/sessions.toml` (loaded by `guban/config.py`).

## External inputs (read-only — never write to these)
- MC strategies (PowerLanguage source): `C:\ProgramData\TS Support\MultiCharts64\StudyServer\Studies\SrcEl\Strategies\`
- MC trade-list exports (parity oracle): `D:\Multi Charts\Performance Reports\CSV Data\*.csv`
- Prop-firm notebook being ported: `D:\Multi Charts\25k Prop Firm Accurate Rules.ipynb`

## Correctness gate
A ported strategy is "done" only when `guban compare` against its MC export shows ≥95% trades matched,
prices within 1 tick, and net profit within 3%.
