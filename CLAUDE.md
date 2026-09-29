# Guban Backtester

Python backtester for CME futures that reproduces MultiCharts (MC) results. Full plan: [PLAN.md](PLAN.md).

## File access rule (user, 2026-09-28) — highest priority
- Read ONLY the file(s) the user explicitly designates for the current task (e.g. "work on phase N of PLAN.md" → read PLAN.md only).
- Before reading ANY other file (source, tests, configs, data, external inputs), stop and ask the user for permission: name the exact file(s) and say why the task cannot be done without them. Ask only when it is genuinely necessary.
- This covers every way of reading contents: Read, Grep, shell `cat`/`type`/`Get-Content`, Python scripts that open files, and subagents.
- Files the user tells you to edit count as designated. Files you create in this session may be re-read.

## Stack & commands
- Python 3.14, managed with uv. Package lives in `src/guban/`.
- Install: `uv sync` · Tests: `uv run pytest` · CLI: `uv run guban --help`
- Deps: pandas, numpy, pyarrow, plotly, typer; pytest (dev).

## Conventions
- Strategies are Python only (no PowerLanguage interpreter). They mirror PowerLanguage semantics:
  orders placed in `on_bar` fill on the NEXT bar; `set_stop_loss`/`set_profit_target` are per-position dollars.
- Bar timestamps are tz-aware America/New_York, labeled by bar CLOSE time (MC convention).
- Default fill mode = 1-minute Bar Magnifier; OHLC-path mode kept for comparison.
- Engine: `guban.engine` — subclass `Strategy`, run with `run_backtest(strategy, bars, spec, Costs(...))`.
  No pyramiding; opposite entry reverses; costs are USD per contract per round trip (MC style). Details in PLAN.md "Phase 3 results".
- Intrabar fills: pass `sub_bars=load_bars(sym, "1m", session)` for magnifier mode (else OHLC path). Stop/limit orders via
  `at="stop"|"limit", price=`; `set_stop_loss`/`set_profit_target` persist until set to None. Details in PLAN.md "Phase 4 results".
- `data/` (Parquet) and `reports/` are generated and git-ignored.
- MC charts for CME are in exchange time: run MC-parity backtests with `--session eth --tz America/Chicago` (MC Time/Date/input times are Central). Import with `--roll expiry` to match MC's continuous contract (rolls Wednesday of expiration week).
- `guban run <strategy.py> ...` writes `reports/<run>/trades.csv` (MC trade-export layout + engine columns, `guban/tradelist.py`), `metrics.json` (`guban/metrics.py`) and a self-contained `report.html` (`guban/report.py`; `--open`, `--no-report`); `guban compare --ours ... --mc <MC csv|xlsx> --contract MNQ` prints S3 stats and writes `diff.html` (`guban/parity.py`). Vault Break parity command: PLAN.md "Phase 5 results".
- MC costs: `Costs(commission, slippage)` USD per contract per round trip; slippage is charged half per market/stop fill, none on limit fills (MC behavior).

## Data
- Source store: `Database/<SYM>-CME/<YYYYMM contract>/Minute/<YYYYMMDD>.data` (binary, git-ignored, ~42 GB incl. Tick). Format documented in `src/guban/data/importer.py`.
- `uv run guban import Database/NQ-CME --symbol NQ` → `data/NQ/1m.parquet` (unadjusted continuous, volume roll).
- `uv run guban bars --symbol NQ --tf 5m --session rth` → session-aligned bars cached at `data/NQ/5m_rth.parquet` (`guban.data.resample.load_bars`).
- Contract specs and session templates: `config/contracts.toml`, `config/sessions.toml` (loaded by `guban/config.py`).

## External inputs (read-only — never write to these)
- MC strategies (PowerLanguage source): `C:\ProgramData\TS Support\MultiCharts64\StudyServer\Studies\SrcEl\Strategies\`
- MC trade-list exports (parity oracle): `D:\Multi Charts\Performance Reports\CSV Data\*.csv`
- Prop-firm notebook being ported: `D:\Multi Charts\25k Prop Firm Accurate Rules.ipynb`

## Git
- Commit after every completed PLAN.md phase (tests green, phase checkbox + results section updated), one commit per phase.

## Correctness gate
A ported strategy is "done" only when `guban compare` against its MC export shows ≥95% trades matched,
prices within 1 tick, and net profit within 3%.
