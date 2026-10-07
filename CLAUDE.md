# Guban Backtester

Python backtester for CME futures that reproduces MultiCharts (MC) results. Full plan: [PLAN.md](PLAN.md).

## File access rule (user, 2026-09-28) — highest priority
- Read ONLY the file(s) the user explicitly designates for the current task (e.g. "work on phase N of PLAN.md" → read PLAN.md only).
- Before reading ANY other file (source, tests, configs, data, external inputs), stop and ask the user for permission: name the exact file(s) and say why the task cannot be done without them. Ask only when it is genuinely necessary.
- This covers every way of reading contents: Read, Grep, shell `cat`/`type`/`Get-Content`, Python scripts that open files, and subagents.
- Files the user tells you to edit count as designated. Files you create in this session may be re-read.
- **Exception — bug fixes (user, 2026-09-29):** when fixing a bug (one the user reports, or one found while working), read whatever files are required to fix it without asking, and don't report which files you read — just fix it. The rule above still applies to all other (non-bug-fix) work.

## Running a backtest (user, 2026-09-29)
- When the user says "run a backtest" (or similar), invoke the `run-backtest` skill (`.claude/skills/run-backtest/SKILL.md`):
  ask which strategy (Glob `strategies/*.py`, names only), run `guban run` on NQ with the MC-parity flags and `--open`
  so `report.html` opens in the browser, then summarize the results. Don't run propfirm/compare/optimize/wfo unless asked.
- Command guide for every guban command: [USE.md](USE.md).

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
- `at="close"` = MC `this bar on close`: fills at the placing bar's close right after `on_bar()` (market fill for costs; exits
  cover it from the next bar). `self.big_point_value` = MC BigPointValue. IVB port + its MC audit: PLAN.md "IVB parity audit".
- `tests/test_ledger.py` pins trade-ledger integrity (every signal fills once, every fill is one row, contracts add up,
  both fill modes). Any change to order handling or the ledger must keep it green.
- Intrabar fills: pass `sub_bars=load_bars(sym, "1m", session)` for magnifier mode (else OHLC path). Stop/limit orders via
  `at="stop"|"limit", price=`; `set_stop_loss`/`set_profit_target` persist until set to None. Details in PLAN.md "Phase 4 results".
- `data/` (Parquet) and `reports/` are generated and git-ignored.
- MC charts for CME are in exchange time: run MC-parity backtests with `--session eth --tz America/Chicago` (MC Time/Date/input times are Central). Import with `--roll expiry` to match MC's continuous contract (rolls Wednesday of expiration week).
- `guban run <strategy.py> ...` writes `reports/<run>/trades.csv` (MC trade-export layout + engine columns, `guban/tradelist.py`), `metrics.json` (`guban/metrics.py`) and a self-contained `report.html` (`guban/report.py`; `--open`, `--no-report`); `guban compare --ours ... --mc <MC csv|xlsx> --contract MNQ` prints S3 stats and writes `diff.html` (`guban/parity.py`). Vault Break parity command: PLAN.md "Phase 5 results".
- `guban run` also runs a trade Monte Carlo by default (`guban/montecarlo.py`, Phase 11): resample (with replacement) and reorder (shuffle) the run's `net_pnl`, side by side in `report.html` (`write_report(..., monte_carlo=)`) and `metrics.json` `monte_carlo`. `--mc-sims` (10000, 0 = off), `--mc-seed` (42; reorder uses seed+1), `--mc-dd-limit`. Drawdowns are close-to-close (compute_metrics' definition); output is deterministic for a seed and independent of the batch size — keep it that way. Separate from `propfirm`.
- `guban optimize <strategy.py> --grid "a=0.4:1.0:0.1 b=1:4" [run's data/cost flags]` grid-searches inputs on a process pool (`guban/optimize.py`, `guban/optimize_report.py`) and writes `reports/<run>_opt/results.csv`, `optimize.json`, `optimize.html` (heatmap); `--metric`, `--min-trades` (default 30), `--workers`, `--x/--y`; >5,000 combinations need `--yes`. Strategy classes must be top-level in their file (workers re-import them). Details in PLAN.md "Phase 7 results".
- `guban wfo <strategy.py> --grid ... [run's data/cost flags]` walk-forwards inputs (`guban/wfo.py`, `guban/wfo_report.py`): `--is 12 --oos 3 --step 3` months, `--rolling/--anchored`, `--start`; writes `reports/<run>_wfo/trades.csv` (stitched OOS), `windows.csv`, `results.csv`, `wfo.json`, `wfo.html`. Every IS/OOS window is its own trading window: warm-up from the first bar, no fills before the window (`run_backtest(trade_from=)`), a position open at its end closed as "Window End"; windows sharing a start share one run (`BacktestResult.window`). The engine must stay causal: no strategy/engine code may look at future bars. Details in PLAN.md "Phase 8 results".
- `guban propfirm --trades <trades.csv | MC csv/xlsx> --rules apex_50k_eod` runs the prop-firm evaluation Monte Carlo (`guban/propfirm/`, rules + sim settings in `config/propfirm/<rules>.toml`) and writes `reports/<trades>_<rules>/propfirm.json`, `sweep.csv`, `degradation.csv`, `daily.csv`, `propfirm.html`. It is a bit-exact port of the notebook (same RNG draw order) — keep it that way; notebook quirks (costs added on top of net profit, zero-trade weekdays in the pool) are ported as written and noted in the report. Details in PLAN.md "Phase 9 results".
- Scheduled early closes: `config/calendar.toml` (CME equity; add each new year's dates) → `session_close` column from `load_bars` (not cached) → `Strategy.session_close` series. Vault Break flattens 10 min before an early close; MC-parity runs need `--set flatten_early_close=false` plus the Central inputs `earliest_entry=945 latest_entry=1500`. `run`/`wfo` report trades held past a session's scheduled close or across a data gap (`metrics.session_exposure`).
- MC costs: `Costs(commission, slippage)` USD per contract per round trip; slippage is charged half per market/stop fill, none on limit fills (MC behavior).
- CLI conventions (`guban/cli.py`, Phase 10): `run`/`optimize`/`wfo` share module-level option objects (`SYMBOL`, `TZ`, `FILL_MODE`, ... — add new shared flags there, not per command; `tests/test_cli.py` checks they stay identical). `--out` is always a folder; default folders come from `run_dir()`. Commands are wrapped in `@_friendly`: expected failures (KeyError/ValueError/OSError/RuntimeError incl. `StrategyError`) print `Error: ...` (+ the strategy's own frames, also from worker processes) and exit 2; `compare` FAIL exits 1; `GUBAN_DEBUG=1` re-raises. Library code should raise those types with a message that names the fix. User docs: README.md.

- Statistical studies (Phase 13) run outside the engine and may look ahead: reusable core in `guban/research/` (`days`: contract
  + RTH window per trading day from the store; `profile`: POC/value area from trades or 1-min bars; `stats`: base vs conditional
  rate, exact one-sided binomial, per-year gaps, `verdict`; `report`: study HTML); each study is a script in `research/`
  (first: `research/poc_shift.py` → `reports/poc_shift/`). Tick store reader + RTH volume-at-price cache: `guban/data/ticks.py`
  (57-byte records; some sessions are defective, so keep a day only if its RTH tick volume matches the minute volume within 2%).
  A `guban study` command is parked until a second study exists.

## Data
- Source store: `Database/<SYM>-CME/<YYYYMM contract>/Minute/<YYYYMMDD>.data` (binary, git-ignored, ~42 GB incl. Tick). It comes from **DeepCharts (dxFeed data)**, not MultiCharts (user, 2026-09-29) — so small price differences vs MC's feed are expected. Format documented in `src/guban/data/importer.py`.
- `uv run guban import Database/NQ-CME --symbol NQ` → `data/NQ/1m.parquet` (unadjusted continuous; volume roll decided from the previous session's volume).
- `uv run guban bars --symbol NQ --tf 5m --session rth` → session-aligned bars cached at `data/NQ/5m_rth.parquet` (`guban.data.resample.load_bars`). Caches are fingerprinted (source, timeframe, session, `CACHE_VERSION` — bump it when `resample()` output changes); write Parquet only via `guban/data/parquet.py` (atomic).
- Contract specs and session templates: `config/contracts.toml`, `config/sessions.toml` (loaded by `guban/config.py`).

## External inputs (read-only — never write to these)
- MC strategies (PowerLanguage source): `C:\ProgramData\TS Support\MultiCharts64\StudyServer\Studies\SrcEl\Strategies\`
- MC trade-list exports (parity oracle): `D:\Multi Charts\Performance Reports\CSV Data\*.csv` (the user converts MC's Strategy Performance Report Excel export to this CSV; guban also reads the `.xlsx` List of Trades directly)
- Prop-firm notebook being ported: `D:\Multi Charts\25k Prop Firm Accurate Rules.ipynb`
- DeepCharts live folder `D:\Deepchart\Database` (still being written by the app): only `guban store build` reads it.

## Shared market-data store (Phase 14)
- `D:\Market Data\` is shared with the Research Pipeline (`D:\Research Pipeline`): write-once, read-only Parquet per
  contract session (`<SYM>/{minute,tick}/<contract>/<YYYYMMDD>.parquet`), `manifest.csv`, per-symbol `quality.csv` and
  `roll_calendar.csv`; schema + rules in its `README.md` (generated from `guban/store/build.py`). Never edit or
  delete files there by hand; `guban store build [--symbol ..] [--rebuild SYM:YYYYMMDD]` adds finished sessions and
  reports changed sources, `guban store verify` checks SR3. Settings: `config/store.toml`. Code: `guban/store/`
  (`bars.py` = per-session rules, tested in `tests/test_store.py`). Changing a bar rule means rebuilding the
  affected sessions (`--rebuild`) — say so before doing it, the pipeline reads the same files.

## Git
- Commit after every completed PLAN.md phase (tests green, phase checkbox + results section updated), one commit per phase.

## New strategies (user, 2026-10-05)
- The user's existing MC strategies are disregarded completely: don't port them or suggest porting them. New strategies
  are written fresh in Python (`strategies/*.py`) and run with `guban run`; they have no MC export, so the correctness
  gate below doesn't apply to them. Verify them with the ledger checks instead (signals vs trades, `tests/test_ledger.py`).

## Correctness gate
A ported strategy is "done" only when `guban compare` against its MC export shows ≥95% trades matched,
prices within 1 tick, and net profit within 3%. Exception: IVB (`strategies/ivb.py`) — the user waived MC parity
(2026-10-05); it is accepted on the ledger-integrity check (PLAN.md Phase 12).
