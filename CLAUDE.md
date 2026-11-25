# Guban Backtester

Python backtester for CME futures (NQ, MNQ, ES, MES, BTC). History and design notes: [PLAN.md](PLAN.md); command guide: [USE.md](USE.md); user docs: README.md.

## Working rules (user)
- Don't ask permission to read files or run commands, but read and run only what the task needs (no exploratory sweeps).
- No comments in code (`#`, `//`, `/* */`); docstrings are fine. Make code readable through names and structure instead.
- Don't write markdown files nobody asked for. Update existing docs only when the change makes them wrong.
- "Run a backtest" → the `run-backtest` skill. Always run in Eastern time (`--tz America/New_York`).
- Commit after every completed PLAN.md phase (tests green, phase checkbox + results section updated), one commit per phase.

## Stack
- Python 3.14 + uv, package in `src/guban/`. `uv sync` · `uv run pytest` · `uv run guban --help`.
- Charts are matplotlib images (`guban/charts.py`), no plotly. `data/` and `reports/` are generated and git-ignored.
- GUI: `guban gui` = Electron app in `gui/` (plain JS) driving the Python worker `guban.gui.worker` (`guban/gui/tab_*.py`).
  Verify GUI changes with `uv run guban gui --smoke` and `tests/test_gui_worker.py`; GUI results must equal the CLI's.

## Engine semantics
- Strategies are Python (`strategies/*.py`, top-level classes so worker processes can re-import them), PowerLanguage-style:
  orders placed in `on_bar` fill on the next bar; `at="close"` fills at the placing bar's close; `at="stop"|"limit"|"touch"`;
  `set_stop_loss`/`set_profit_target` are per-position USD and persist until set to None. No pyramiding; an opposite entry reverses.
- Bars are tz-aware, labeled by close time. Default fills use the 1-minute bar magnifier (`sub_bars=`), else the OHLC path.
- Costs are USD per contract per round trip; slippage is charged half per market/stop fill, none on limit fills.
- The engine must stay causal: no strategy or engine code may look at future bars. Research scripts (`research/`,
  `guban/research/`) run outside the engine and may look ahead.
- New strategies are verified with the ledger checks (`tests/test_ledger.py`); any order-handling or ledger change keeps it green.
  The user's old MC strategies are disregarded: don't port them or suggest porting them.

## Invariants to keep
- `run`/`optimize`/`wfo` share the module-level option objects in `guban/cli.py`; add shared flags there (`tests/test_cli.py`).
  Library code raises KeyError/ValueError/OSError/RuntimeError with a message that names the fix (`@_friendly` prints it, exit 2).
- `guban propfirm` is a bit-exact port of the notebook (same RNG draw order), quirks included.
- Monte Carlo and trade-chart output is deterministic for a seed and independent of batch size.
- Bar caches are fingerprinted: bump `CACHE_VERSION` when `resample()` output changes; write Parquet only via `guban/data/parquet.py`.
- `config/calendar.toml` lists scheduled early closes per year; add each new year's dates.

## Data
- Market data: DeepCharts (`D:\Deepchart\Database`, read only by `guban store build`) → the shared store `D:\Market Data\`
  (also read by `D:\Research Pipeline`). The store is write-once: never edit or delete its files by hand, and say so before
  a `--rebuild`, since the pipeline reads the same files. Bar rules: `guban/store/bars.py`, settings `config/store.toml`.
- Strategies use 1-minute bars; use ticks only when a strategy needs them, and only sessions with `quality.csv` `tick_ok`
  (research also requires RTH tick volume within 2% of minute volume).
- Read-only external inputs: MC trade exports in `D:\Multi Charts\Performance Reports\CSV Data\`, the prop-firm notebook in
  `D:\Multi Charts\`.
