# Plan: Guban Backtester — a Python backtester for CME futures that reproduces MultiCharts results

One-line goal: You can write a futures strategy in Python, backtest it on multi-year 1-minute CME data exported from MultiCharts, get a MultiCharts-style performance report in the browser, optimize and walk-forward its inputs, and estimate prop-firm pass rates. The same strategy ported from MultiCharts produces the same trades MultiCharts did.

## Classification
Track: From scratch. The project folder `D:\Guban Backtester` is empty (verified: ran Get-ChildItem -Force -Recurse, no output). Parked secondary asks: multi-symbol portfolio backtesting (Q7), desktop/web GUI (Q1), running PowerLanguage `.el` files directly (Q5).

## Interview Ledger
- Q1 form factor → Python engine + CLI + interactive HTML reports (accepted recommended)
- Q2 data source → own exported files (accepted recommended)
- Q3 sample file → path offered but not supplied
- Q4 file path → "elsewhere on my computer"; found by read-only disk search instead
- Q5 strategy language → "Python for everything" (user)
- Q6 history source → user will export data from MultiCharts (user)
- Q7 v1 features → optimizer + walk-forward + prop-firm Monte Carlo; portfolio parked (user)
- Q8 checkpoint: MC Bar Magnifier → ON, resolution 1-min or tick (user)
- Q9 approval → via plan approval
Total: 9 of 14.

## Goal & Success Criteria
- S1. `guban import <MC export>` turns a MultiCharts 1-minute export (NQ/MNQ, Jan 2021 onward) into a local Parquet store and prints its date range, bar count, and any session gaps.
- S2. `guban run strategies/vault_break.py --symbol MNQ --tf 5m` finishes a 2021–2026 backtest in **≤ 15 s** on this PC and opens a self-contained HTML report with: summary metrics, equity curve, drawdown chart, trade list, and a price chart with trade markers.
- S3. **Parity:** `guban compare` of the Python Vault Break port against `D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv` over the same dates meets all three:
  - ≥ 95% of MC trades matched on entry time and direction.
  - Matched trades' entry and exit prices within 1 tick.
  - Total net profit within 3%.
  - Every mismatch is listed in a diff report.
- S4. `guban optimize` runs a grid search on all CPU cores and writes a ranked results table plus a 2-D heatmap to HTML.
- S5. `guban wfo` runs rolling in-sample/out-of-sample windows and reports the stitched out-of-sample equity curve and walk-forward efficiency.
- S6. `guban propfirm` reproduces the Apex EOD trailing-drawdown Monte Carlo from `25k Prop Firm Accurate Rules.ipynb`, run directly on a backtest's trades. With the same trade CSV, seed, and inputs, its pass rate is within ±1 percentage point of the notebook's.

## Current State
- The project folder is empty and not a git repo (verified: Get-ChildItem; environment reports "Is a git repository: false").
- Python 3.14.4, uv 0.11.7, and git 2.54.0 are installed (verified: ran `python --version`, `uv --version`, `git --version`).
- MultiCharts 16 is in use (verified: `D:\Multi Charts\MultiCharts_16_Release_7.exe`). Its bar history lives in `C:\ProgramData\TS Support\MultiCharts64\Databases\TSSTORAGE.GDB` (136 MB, Firebird; verified: Get-ChildItem). Python will not read it directly; the user will export from MultiCharts (user).
- The user has ~30 PowerLanguage strategies in `C:\ProgramData\TS Support\MultiCharts64\StudyServer\Studies\SrcEl\Strategies\`, edited through Sep 2026 (verified: Get-ChildItem). Examples: `sc_Vaultc20Break.el`, `sb_VWAPb20GOAT.el`, and the IVB family.
- Vault Break (verified: read `sc_Vaultc20Break.el`):
  - Setup: 5-min NQ, RTH 09:30–16:00 ET, long only.
  - Entry: when Close > SessOpen + NoiseMult×DailyATR and Close > VWAP, between 09:45 and 15:00, at most 3 trades a day.
  - Exits: `SetProfitTarget` / `SetStopLoss` in dollars, a VWAP-cross exit, and a flatten at 15:50.
  - Language features used: `Inputs`, `intrabarpersist` variables, `Date <> Date[1]`, `Buy … next bar at market`, `MarketPosition`.
- MultiCharts trade-list exports sit in `D:\Multi Charts\Performance Reports\CSV Data\*.csv` (verified: Get-Content).
  - Columns: `trade_id,order_id,type,signal,excel_date,excel_time,price,contracts,profit_usd,profit_pct,cum_profit_usd,cum_profit_pct,runup_usd,runup_pct,drawdown_usd,drawdown_pct,direction`.
  - Earliest trades are Jan 2021 (excel date 44201).
  - Times are Eastern chart time: the first Vault Break MNQ entry decodes to 2021-01-20 09:50 (verified: computed 0.40972×24h from that file's row 1).
  - Profits appear to be net of costs: 5 contracts × 50 pts × $2 = $500 gross vs `profit_usd` 497.9 (verified: row 1 of `Vault Break MNQ.csv`; the inference is ~$2.10 per trade in costs).
- One existing bar CSV format: `timestamp(ISO, UTC),open,high,low,close,volume`, MNQ 1-min, 2026-03-29 → 2026-04-24 only (verified: `D:\GitHub\we_model_code\data\MNQ_1m.csv`). This is not the history source.
- A prop-firm Monte Carlo already exists as a Colab notebook: `D:\Multi Charts\25k Prop Firm Accurate Rules.ipynb` (verified: parsed the notebook).
  - It covers Apex EOD trailing drawdown, commission of $2.25 and slippage of $5 per NQ contract, a bootstrap of daily P&L, a position-size sweep, and degradation scenarios.
- **Housekeeping flag:** `we_model_code` and `trading-bot` each exist in three places (`D:\GitHub`, `D:\Users\ep537\OneDrive\Documents\GitHub`, `C:\Users\ep537\OneDrive\Documents\GitHub`) (verified: file search). This plan does not touch them, and nothing is copied from them into this repo.

## Scope (v1)
1. Importer for MultiCharts QuoteManager 1-minute ASCII exports. Output is a Parquet store per symbol, with a time-zone-normalized index (exchange time, America/New_York for display).
2. Contract spec table and RTH/ETH session definitions. A resampler builds N-minute and daily bars from 1-minute data, aligned to session start.
3. Bar-by-bar event-driven engine with a Python Strategy API that mirrors PowerLanguage behavior. Fill simulation has two modes: 1-minute Bar Magnifier (default) and OHLC-path.
4. Commission and slippage model.
5. Metrics (MultiCharts Strategy Performance Report core set), a self-contained HTML report, and a trade CSV in MultiCharts' export column layout.
6. Parity harness (`guban compare`) against MultiCharts trade exports.
7. Parameter grid optimizer, parallel across processes.
8. Walk-forward analysis.
9. Prop-firm Monte Carlo (port of the notebook).
10. One ported strategy: Vault Break. It is the parity reference.

## Out of Scope & Parked Items
- **Multi-symbol portfolio backtesting** — parked by the user (Q7). The engine keeps one account per run.
- **Desktop or web GUI** — parked (Q1). HTML reports are the UI.
- **Running `.el` files or auto-translating them** — rejected by the user (Q5: "Python for everything"). Strategies are ported by hand, with Claude's help, one at a time.
- **Tick-level backtesting and tick Bar Magnifier** — needs tick exports. It is parked until the 1-min parity tolerance proves insufficient.
- **Genetic or Bayesian optimizer** — grid search only in v1.
- **Live or paper trading, broker connectivity (Rithmic)** — not a backtester concern.
- **Porting the other ~29 MC strategies** — dropped by the user (2026-10-05): they are disregarded completely. New
  strategies are written fresh in Python and run directly; there is no MC export to compare them against.
- **Numba/Cython acceleration** — only if S2's 15-second target is missed.

## Approach
A walking skeleton first: import real data, resample it, then run a trivial strategy end to end. Then build the engine, fills, and parity check around the Vault Break port. The analysis layers (report, optimizer, walk-forward, prop firm) come after that, because they only consume the engine's trade list.

**Engine loop.**
- Iterate over the strategy timeframe's bars (e.g. 5-min).
- At each bar close, call `strategy.on_bar()`. Orders it places are "next bar" orders, as in PowerLanguage.
- Market orders fill at the next bar's open, plus slippage.
- Stop, limit, and dollar stop-loss/profit-target exits are evaluated inside the next bar:
  - **Magnifier mode (default):** walk the 1-minute sub-bars in order. Inside one 1-minute bar, apply MC's OHLC path rule: whichever of high or low is nearer the open is assumed to be hit first.
  - **OHLC-path mode:** apply the same rule to the whole strategy bar.
- `set_stop_loss` and `set_profit_target` stay active while a position is open, and are evaluated from the entry fill onward, including the entry bar's remaining sub-bars.

**Strategy API (executor may refine the names, not the behavior):**
```python
class VaultBreak(Strategy):
    inputs = dict(noise_mult=0.60, daily_atr_len=14, earliest_entry=945, latest_entry=1500,
                  max_trades_per_day=3, vwap_exit_bars=2, take_profit_usd=1000,
                  stop_loss_usd=2000, flatten_time=1550, num_contracts=1)
    def on_bar(self):
        if self.date != self.date_[1]: ...           # series lookback: self.close[1]
        self.buy("VB_Long", qty, at="market")          # next bar at market
        self.sell("VB_TimeX", qty, at="market")
        self.set_profit_target(self.p.take_profit_usd) # per-position dollars, like MC
        self.set_stop_loss(self.p.stop_loss_usd)
        self.market_position                           # -1 / 0 / 1
```
- Series such as `close`, `high`, and `date` support `[n]` lookback.
- Plain instance attributes play the role of `intrabarpersist` variables.
- Executor's choice: internal module layout, class names, and plotting details.

**Parity loop.** Port Vault Break literally, line by line, with no "improvements". Get the inputs used for the MC run from the matching `.xlsx` report. Run `guban compare`, study the mismatches, and fix engine semantics (not the strategy) until S3 passes.

## Requirements
- **R1.** WHEN `guban import <file> --symbol MNQ` is given a MultiCharts 1-min ASCII export, THE SYSTEM SHALL write `data/MNQ/1m.parquet`, de-duplicate timestamps, and print the first and last bar, the bar count, and missing-session days.
  - Check: test on a 3-day fixture, plus a real-file run.
- **R2.** WHEN a strategy requests timeframe N-min, THE SYSTEM SHALL build bars from 1-min data aligned to the session open (RTH 09:30 ET → first 5-min bar labeled 09:35 by its close time, matching MC's close-time bar labels).
  - Check: a unit test comparing against hand-built bars.
- **R3.** WHEN `on_bar` places a market order, THE SYSTEM SHALL fill it at the next bar's open ± slippage, never on the signal bar.
  - Check: a unit test on synthetic bars.
- **R4.** WHEN a position has a dollar stop and target and both lie inside one strategy bar, THE SYSTEM SHALL decide the order of hits from the 1-min sub-bars (magnifier mode), or from the OHLC path rule (OHLC mode).
  - Check: two synthetic-bar tests, one per mode.
- **R5.** THE SYSTEM SHALL compute dollar P&L from the contract spec: points × point value × contracts − commission − slippage. Both NQ ($20/pt, 0.25 tick) and MNQ ($2/pt) are in the spec table [A2].
  - Check: a unit test.
- **R6.** WHEN a backtest finishes, THE SYSTEM SHALL write `reports/<run>/report.html` (self-contained, opens offline), `trades.csv` (MC export column layout), and `metrics.json`.
  - Metrics: net profit, gross profit/loss, profit factor, total trades, % profitable, avg trade, avg win/loss, largest win/loss, max strategy drawdown (close-to-close and intraday peak-to-valley), max consecutive losers, return on account, Sharpe (daily).
  - Check: metrics tests against a hand-computed 5-trade fixture.
- **R7.** WHEN `guban compare --ours trades.csv --mc <MC csv>` runs, THE SYSTEM SHALL match trades by entry time ±1 strategy bar and direction, and print the S3 statistics, and write `diff.html` listing unmatched and price-mismatched trades.
  - Check: a fixture test plus S3.
- **R8.** WHEN `guban optimize` receives input ranges, THE SYSTEM SHALL run every combination with a process pool sized to CPU cores, and rank the results by a chosen metric (default net profit, with a minimum-trades filter).
  - Check: a test on a 2×2 grid; results must equal four separate runs.
- **R9.** WHEN `guban wfo` receives the in-sample length, out-of-sample length, and step (default 12 months / 3 months / 3 months, rolling), THE SYSTEM SHALL optimize each in-sample window, run the best inputs on the next out-of-sample window, and stitch the out-of-sample trades into one report with walk-forward efficiency = annualized OOS profit ÷ annualized IS profit.
  - Check: a test that no OOS window overlaps its IS window.
- **R10.** WHEN `guban propfirm --trades trades.csv --rules apex_50k_eod` runs, THE SYSTEM SHALL run the notebook's day-based Monte Carlo (EOD trailing drawdown, profit target, max trading days, cost model, size sweep, degradation scenarios), with rules held in a config file.
  - Check: S6 comparison against the notebook.
- **R11.** A backtest SHALL be deterministic: the same data, inputs, and seed produce byte-identical `trades.csv`.
  - Check: a test that runs twice and diffs the output.

## Key Decisions
- **Form factor:** Python library + CLI + HTML reports (user, Q1).
- **Strategy language:** Python only, with PowerLanguage-like behavior (user, Q5).
- **Data source:** MultiCharts 1-min exports (user, Q6). Strategy timeframes are resampled from 1-min.
- **Bar Magnifier:** default mode uses 1-min sub-bars, matching the user's MC setting (user, Q8). OHLC-path mode is kept as an option.
- **Language and tooling:** Python 3.14 managed with uv (verified: installed versions).
- **Libraries:** pandas, numpy, pyarrow, plotly (self-contained HTML), typer (CLI), pytest [A3].
- **Storage:** Parquet files per symbol and timeframe under `data/`. No database — this is the simplest store that fits ~2M rows.
- **Engine style:** a plain Python loop, not vectorized. PowerLanguage semantics (stateful, path-dependent exits) require it [A4].
- **Time zone:** store bars in America/New_York, which is chart time in the MC exports (verified: decoded the excel_time of row 1), with DST handled by pandas tz-aware indexes [A5].
- **Version control:** `git init` in Phase 1, and generate a `CLAUDE.md` for the repo (global instruction: new repos get a CLAUDE.md).

## Data & State Changes
- New files only:
  - `data/<SYMBOL>/1m.parquet` (plus a cached `<tf>.parquet` for each resampled timeframe).
  - `reports/<run-id>/…` outputs.
  - `config/contracts.toml`, `config/sessions.toml`, `config/propfirm/*.toml`.
- Nothing outside `D:\Guban Backtester` is written. MultiCharts' database, strategies, and exports are read only.
- Rollback: delete `data/` and re-import.
- Add `data/` and `reports/` to `.gitignore`.

## Interfaces, Integrations & Credentials
- **CLI:** `guban import | run | compare | optimize | wfo | propfirm` (typer).
- **Inputs:**
  - MC QuoteManager ASCII export. The format is not yet seen [A1]; the importer is written against a real file in Phase 1.
  - MC trade-list CSV (verified format, above).
- **Outputs:** HTML, CSV, and JSON only.
- **Credentials:** none. No network access is needed after `uv sync`.

## Edge Cases & Failure Handling
- **Export has duplicate or out-of-order timestamps** → sort and drop duplicates, and report the count.
- **Missing session days** (holidays, gaps) → listed in the import summary. The engine skips them; no forward-fill.
- **Daylight-saving transitions** → tz-aware index. A DST-week fixture test checks that the RTH 09:30 open stays 09:30.
- **Contract rolls in continuous data** → the plan assumes MC's export is already a continuous series [A6]. Roll gaps show up as large bar-to-bar jumps, and the importer flags jumps above 2% on roll weeks.
- **Stop and target both hit within one 1-min sub-bar** → OHLC path rule applied to that sub-bar. These trades are counted in the report as "ambiguous fills".
- **Strategy raises an exception** → stop and print the bar timestamp and strategy state. Partial results are not reported as complete.
- **Optimizer grid larger than 5,000 combinations** → warn and require `--yes`.
- **Wrong symbol/spec, or a timeframe not divisible into the session** → fail loudly before running.

## Risks, Landmines & Adaptations
- **The user's strategies are PowerLanguage, but the user wants Python only** → no interpreter. Strategies are ported by hand, and the MC trade exports become the correctness oracle (the S3 parity gate).
- **MC Bar Magnifier was on (Q8), at 1-min or tick** → default fill mode is a 1-min magnifier. If MC used tick resolution, a few same-bar stop/target trades will legitimately differ. That is why S3 is set at 95% matched with a 3% profit tolerance, not 100%, and why the diff report tags ambiguous fills.
- **Multi-year history is only inside MC's Firebird DB** → the user exports it (Q6). Phase 1 is gated on a real export file.
- **Session template drives `Date <> Date[1]` logic** → Vault Break's session-open and ATR calculations assume RTH bars. The parity run uses an RTH session; running it on ETH data would silently change the results [A7].
- **MC export profits are net of costs (~$2.10/trade observed)** → the parity run uses cost settings recovered from the MC `.xlsx` report, or inferred from gross-vs-net differences [A8].
- **Performance of a Python loop** → the 15-second target is checked in Phase 5. Fallback: precompute series with numpy and only drop to sub-bars when an order is pending (already the design). Numba only if still slow.

## Assumptions Ledger
| ID | Assumption | Basis | Blast radius if wrong | Check |
|----|-----------|-------|----------------------|-------|
| A1 | MC QuoteManager ASCII export is a delimited text file with date, time, O, H, L, C, volume columns | Usual MC ASCII export shape; not seen this session | Importer rewrite (small) | Phase 1: read the real export's first lines before coding the parser |
| A2 | NQ = $20/pt, MNQ = $2/pt, tick 0.25 | Consistent with Vault Break comments ("$1,000 = 50 NQ pts") (verified) and row 1 of the MNQ export math | Wrong P&L | Phase 2: unit test + parity |
| A3 | pandas/numpy/pyarrow/plotly/typer/pytest install cleanly on Python 3.14 | Mainstream packages; wheels not verified | Pin to Python 3.13 via uv | Phase 1: `uv sync` succeeds |
| A4 | A pure-Python loop over ~110k 5-min bars (+ sub-bars on pending orders) runs in ≤15 s | Rough count: 5.5 yrs × 252 d × 78 bars | Speed-up phase needed | Phase 5 timing |
| A5 | The MC export's timestamps are in the same Eastern chart time as the trade exports | Trade exports decode to ET (verified) | All times shift → parity fails | Phase 1: check that the first RTH bar lands at 09:31 (1-min close-time label) |
| A6 | The MC export is a continuous series (e.g. NQ continuous), not per-contract files | The user said they will export from MC (user); MC charts normally use continuous symbols | Add a roll/back-adjust step | Phase 1: inspect the export and check for roll jumps |
| A7 | The MC Vault Break parity run used the RTH session, 5-min bars | Strategy header says "5-minute bars, RTH (09:30–16:00 ET)" (verified) | Parity fails | Phase 5: check the `.xlsx` settings; try ETH if the match rate is low |
| A8 | MC run inputs and costs can be recovered from `Vault Break MNQ 500$ risk.xlsx` | Filename match; xlsx not opened yet | Guess inputs → parity harder | Phase 5: open the xlsx with pandas/openpyxl |
| A9 | Walk-forward defaults: 12-month IS / 3-month OUT, rolling | Common starting point; the user gave no numbers | Only defaults change | Exposed as CLI flags |
| A10 | Prop-firm rules to ship: Apex 50K EOD as in the notebook (including its $25k/$1,250/$1,000 values as written) | Verified from notebook cell 6 | Wrong pass rates | Rules in TOML; the user reviews the values in Phase 9 |

## Open Items (none blocking)
- ~~Which of the other ~29 strategies to port next~~ — closed 2026-10-05: none; the user runs new strategies instead.
- Whether MC's magnifier was 1-min or tick — proceed with 1-min. Revisit only if S3 fails because of ambiguous fills.
- The notebook's Apex values ($25k account labeled "50K") look inconsistent — proceed with them as written and flag them to the user in Phase 9.

## Verification
- `uv run pytest` — unit tests for importer, resampler, fills (both modes), costs, metrics, optimizer equivalence, WFO window separation, and determinism.
- `uv run guban import <real export> --symbol MNQ` → the printed summary shows 2021 → 2026 coverage.
- `uv run guban run strategies/vault_break.py --symbol MNQ --tf 5m` → it finishes in ≤15 s (time it) and `report.html` opens offline.
- `uv run guban compare --ours reports/<run>/trades.csv --mc "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv"` → prints the S3 statistics, which must pass.
- `uv run guban optimize … --grid noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4` → heatmap HTML.
- `uv run guban wfo …` → stitched OOS report.
- `uv run guban propfirm --trades <MC csv>` vs the notebook with the same seed → pass rate within ±1 pp.
- **How you confirm it's done:** open the Vault Break `report.html` next to MultiCharts' report for the same run, and compare net profit, trade count, and max drawdown.

## Build Phases
- [x] Phase 1: Walking skeleton on real data (done 2026-09-28 — see Phase 1 results below)
      Done when: `uv run guban import <real MC export> --symbol MNQ` writes `data/MNQ/1m.parquet` and prints a 2021→2026 summary; `uv run pytest` passes.
      Steps: ask the user for the MC 1-min export file path (they said they will export it) and read its first 20 lines to verify A1/A5/A6; `git init`, `uv init`, add dependencies, `uv sync` (A3); write `CLAUDE.md` and `.gitignore`; implement the importer with a fixture test; run it on the real file.
      Covers: R1; checks: A1, A3, A5, A6
- [x] Phase 2: Contract specs, sessions, resampler (done 2026-09-28 — see Phase 2 results below)
      Done when: tests prove 5-min RTH bars built from 1-min match hand-built bars, including a DST week.
      Steps: `config/contracts.toml` (NQ, MNQ, ES, MES); `config/sessions.toml` (RTH 09:30–16:00 ET, ETH); resample N-min and daily with close-time labels; cache results to Parquet.
      Covers: R2, R5; checks: A2
- [x] Phase 3: Engine core and Strategy API (done 2026-09-28 — see Phase 3 results below)
      Done when: a synthetic-bar test shows a market order placed on bar t fills at bar t+1's open ± slippage, and positions, trades, and P&L are correct.
      Steps: Series with `[n]` lookback; Strategy base (inputs, on_bar, buy/sell/sell_short/buy_to_cover, market_position); order book; position and trade ledger; commission and slippage; determinism test.
      Covers: R3, R5, R11
- [x] Phase 4: Exits and intrabar fill modes (done 2026-09-28 — see Phase 4 results below)
      Done when: synthetic tests for dollar stop/target, stop and limit orders, and same-bar stop+target pass in both magnifier and OHLC modes.
      Steps: `set_stop_loss` / `set_profit_target` (per position, active from entry); stop and limit next-bar orders; magnifier walk over 1-min sub-bars; OHLC path rule; ambiguous-fill tagging.
      Covers: R4
- [x] Phase 5: Port Vault Break and pass the parity gate (done 2026-09-28 — S3 met on the MNQ-data window; see Phase 5 results below)
      Done when: `guban compare` against `Vault Break MNQ.csv` meets S3, and the backtest time is ≤15 s.
      Steps: open `Vault Break MNQ 500$ risk.xlsx` to recover inputs, costs, and session (A7, A8); port `sc_Vaultc20Break.el` line by line to `strategies/vault_break.py`; implement `guban compare` and `diff.html`; iterate on engine semantics until the thresholds pass; time the run (A4).
      Covers: R7, S3; checks: A4, A7, A8
- [x] Phase 6: Metrics and HTML report (done 2026-09-28 — see Phase 6 results below)
      Done when: metrics tests pass on the hand-computed fixture, and the Vault Break `report.html` opens offline with equity, drawdown, trade list, and a price chart with trades.
      Steps: metrics module; MC-layout `trades.csv`; `metrics.json`; plotly self-contained HTML report.
      Covers: R6, S2
- [x] Phase 7: Parameter optimizer (done 2026-09-28 — see Phase 7 results below)
      Done when: the 2×2 grid equivalence test passes, and a Vault Break grid produces a ranked table plus a heatmap.
      Steps: grid spec parser; ProcessPoolExecutor, with data loaded once per worker; ranking and min-trades filter; heatmap; guard for grids over 5,000 combinations.
      Covers: R8, S4
- [x] Phase 8: Walk-forward analysis (done 2026-09-29 — see Phase 8 results below)
      Done when: the IS/OOS no-overlap test passes, and the Vault Break WFO report shows stitched OOS equity and walk-forward efficiency.
      Steps: window generator (rolling and anchored); per-window optimize → OOS run; stitch the trades; WFO report section.
      Covers: R9, S5; checks: A9
- [x] Phase 9: Prop-firm Monte Carlo (done 2026-09-29 — S6 met exactly, 0.00 pp; see Phase 9 results below)
      Done when: with the same trades, seed, and inputs, the pass rate is within ±1 pp of the notebook's.
      Steps: port notebook cells 4–9 to `guban/propfirm/`; move the rules into `config/propfirm/apex_50k_eod.toml`; add a size sweep and degradation scenarios; output HTML charts; flag the notebook's $25k-vs-50K discrepancy to the user.
      Covers: R10, S6; checks: A10
- [x] Phase 10: CLI polish and README (done 2026-09-29 — see Phase 10 results below)
      Done when: `uv run guban --help` lists all six commands; the README walks through import → run → compare → optimize → wfo → propfirm; the full `pytest` suite is green.
      Steps: consistent flags and run IDs; error messages for the failure cases above; README with the MC export instructions; update `CLAUDE.md`.
      Covers: all
- [x] Phase 11: Trade Monte Carlo in the `guban run` report (done 2026-09-29 — see "Phase 11 plan" and "Phase 11 results" below)
      Done when: every `guban run` shows a Monte Carlo section (resample and reorder side by side) in `report.html` and a
      `monte_carlo` block in `metrics.json`; Vault Break trades/net unchanged; MC ≤ 2 s; full `pytest` green.
      Steps: core module `guban/montecarlo.py` + tests; `run` flags and `metrics.json`; report section; real run, docs, commit.
      Covers: R12
- [x] Phase 12: IVB port, `at="close"` orders, trade-ledger integrity (done 2026-10-05 — see "IVB parity audit" below)
      Done when: `strategies/ivb.py` runs on NQ 1-min; an independent replay of its rules reproduces every trade; ledger
      tests prove each signal fills once and each fill is recorded once (both fill modes); full `pytest` green.
      MC parity (S3-style gate) waived by the user on 2026-10-05: "You don't need to compare it to MultiCharts."
      Covers: R3, R11 (ledger integrity)
- [x] Phase 13: POC-shift study on tick data, reusable research core (done 2026-10-05 — NOT REAL on both sides; see "Phase 13 results" below)
      Done when: `uv run python research/poc_shift.py` prints an up and a down verdict (REAL / NOT REAL) under the
      pre-registered rule and writes `reports/poc_shift/{days.csv,triples.csv,results.json,report.html}`; the
      tick reader, statistics and report live in reusable modules; full `pytest` green.
      Covers: R13
- [x] Phase 14: Shared market-data store at `D:\Market Data\` (done 2026-10-06 — see "Phase 14 results" below; build steps 1–3)
      Done when: `guban store build` has written every finished session of NQ, MNQ, ES, GC, BTC; `guban store verify`
      meets SR3 per symbol; a second build writes 0 files; full `pytest` green.
      Covers: SR1–SR5
- [x] Phase 15: Guban reads the store; BTC spec, costs, calendars (done 2026-10-07 — see "Phase 15 plan (revised 2026-10-07)" and "Phase 15 results" below)
      Done when: `guban import --symbol <SYM>` builds NQ, MNQ, ES, MES, BTC from the store; Vault Break passes S3 both on
      the full span (literal port, `--data-from 2021-01-03`) and with the Phase 5 recipe; `guban run` works on every
      symbol with default costs; `research/poc_shift.py` runs on store ticks (`tick_ok` only); full `pytest` green.
      Covers: SR6–SR9, SR11

---
**Defaulted decisions recap** — A1–A10 above are every choice made on your behalf; any of them can be vetoed cheaply.

## Phase 1 results (2026-09-28)
- **A1 replaced.** The user supplied a binary per-contract store, not an ASCII export. Layout: `Database/<SYM>-CME/<YYYYMM>/{Minute,Tick}/<YYYYMMDD>.data`, one Globex session per file. Minute record = 60 bytes: int64 .NET ticks (ET wall-clock, bar open time), f8 high/low/open/close, i4 bid_vol/ask_vol/volume/volume/unused (verified: decoded files, field order inferred statistically: field0 is always the max, field1 always the min, close[t]==open[t+1] continuity).
- **A5 verified.** Timestamps are Eastern time. The volume spikes land on the 09:31 and 16:00 bars (close-time labels) in both winter and summer.
- **A6 falsified → adapted.** Data is per-contract. The importer builds an unadjusted continuous series, picking the highest-volume contract each session and never rolling back. There were 16 rolls, each on the Sunday before expiry week. Back-adjustment is a later option. *(Superseded by the backend audit fixes — see "Backend audit fixes" at the end.)*
- **A3 verified.** All dependencies install on Python 3.14.
- NQ coverage: 2022-12-28 → 2026-09-24, 1,323,130 bars, 967 sessions, 9 holiday gaps. MNQ store covers 2025-03 → 2026-09 only.
- **Tick data exists:** NQ from 2023-11, ~20 GB. The tick Bar Magnifier moves from "parked" to "available if 1-min parity falls short"; tick record layout not yet decoded.
- MC trade CSVs wrap each whole line in quotes. The parity parser must strip them. Vault Break MNQ.csv has 744 of 1,081 rows inside the NQ data range.

## Phase 2 results (2026-09-28)
- `config/contracts.toml` (NQ $20, MNQ $2, ES $50, MES $5; tick 0.25) and `config/sessions.toml` (RTH 09:30–16:00, ETH 18:00–17:00 overnight), loaded by `guban/config.py`. `ContractSpec.pnl()` = points × point value × contracts − (commission + slippage) × contracts, with costs defined **per contract per round trip** (Phase 3 may refine this).
- `guban/data/resample.py`: N-minute bins counted from the session open, labeled by close time; bins are capped at the session close; gaps are not filled; daily bars are labeled at the session close; every bar carries `session_date` (trading date — an ETH session's evening bars belong to the next day). A timeframe that does not divide the session raises unless `allow_partial=True`.
- Cache: `data/<SYM>/<N>m_<session>.parquet` / `1d_<session>.parquet`, rebuilt when `1m.parquet` is newer. CLI: `guban bars --symbol NQ --tf 5m --session rth`.
- Tests (A2 checked): hand-built 5m RTH bars, spring and fall DST weeks, the ETH overnight bin that crosses midnight, gaps, daily, non-divisible timeframe, cache invalidation, and contract P&L.
- Real NQ: 73,906 5m RTH bars across 964 sessions, 78 per normal day; 37 short sessions (holidays and early closes, plus the truncated last day). Building from 1m takes ~1.4 s.
- Open point for Phase 5: on early-close days a daily bar is still labeled 16:00, not at the actual last bar. This only matters if MC labels it differently.

## Phase 3 results (2026-09-28)
- `guban/engine/`: `series.py` (Series with `[n]` lookback that raises on history before bar 1 or on negative offsets; `VarSeries` = a variable with lookback that carries its last value forward), `strategy.py` (Strategy base), `backtest.py` (loop, Broker, ledger, `run_backtest`).
- Strategy API: `inputs` dict → `self.p`, with unknown overrides rejected. Hooks: `on_start()` (create `self.series()` vars here) and `on_bar()`. Series: `open/high/low/close/volume`, `time` (HHMM int, MC `Time`), `date` (calendar date, MC `Date`), `session_date`, `timestamp`. Orders: `buy/sell_short(name, qty=1)`, `sell/buy_to_cover(name, qty=None → all)`, `at="market"` only (Phase 4 adds the others). State: `market_position`, `current_contracts`, `entry_price`, `bars_since_entry`, `current_bar`.
- Semantics: orders placed on bar i are processed at bar i+1's open and live for one bar only. Only one position at a time and no pyramiding (a same-direction entry is ignored). An opposite entry reverses at a single fill. Exits that don't match the position are ignored. Partial exits produce one trade row each. Orders on the same bar are processed in the order they were placed. A position still open after the last bar goes to `open_position`, not `trades`.
- Costs (`Costs`): `commission` and `slippage` in USD per contract per round trip, deducted through `ContractSpec.pnl()` (MC style: fill prices are left as-is), plus an optional `slippage_ticks` that moves each fill price against the trade (R3's "open ± slippage"). Phase 5 picks the settings that reproduce MC's ~$2.10/trade.
- Equity is marked at each bar close as closed net P&L plus open gross P&L. A strategy exception raises `StrategyError` naming the bar, its timestamp, and the position.
- Tests (`tests/test_engine.py`, 18): lookback, VarSeries, next-bar fills never on the signal bar (R3), slippage ticks, reversal, pyramiding and mismatched exits, partial exits, order sequencing, P&L and costs (R5), equity, inputs, error reporting, byte-identical `trades.csv` across two runs (R11). Full suite: 35 passed.
- Early speed read for A4: 74k synthetic 5m bars with 19.8k trades run in 0.51 s (no sub-bars yet).
- Not yet done: a `guban run` CLI command (comes with the report in Phase 6), and the MC-layout trades CSV (Phase 6; the current CSV is the engine's own column set).

## Phase 4 results (2026-09-28)
- API: `buy/sell/sell_short/buy_to_cover(..., at="market"|"stop"|"limit", price=...)`; `set_stop_loss(usd)` / `set_profit_target(usd)` (per position, MC names "Stop Loss" / "Profit Target"; they persist until changed, and `None` clears them). `run_backtest(..., sub_bars=<1m bars>, fill_mode="magnifier"|"ohlc")`: magnifier is the default when `sub_bars` is given; pass `load_bars(sym, "1m", session)`, which carries `session_date`.
- Intrabar model (`guban/engine/backtest.py` docstring):
  - Each sub-bar (a 1m bar, or the whole bar in OHLC mode) is walked O→H→L→C when the high is at least as near the open as the low, otherwise O→L→H→C. **A tie goes high first** (my own choice; check it in Phase 5).
  - Stops and limits fill at their own price, or at the sub-bar open when price has gapped through them.
  - After every fill, all orders are re-checked, so a stop loss is live from the entry fill onward (including the rest of the entry bar). An entry blocked by the open position can still fill after that position exits in the same bar.
  - Exit orders fill only while a matching position is open at the moment they would fill. So an exit placed while flat can close a position opened intrabar on the same bar. This is an assumption; Vault Break doesn't exercise it.
- Dollar exit prices = entry ∓ usd / (point value × contracts), **rounded to the nearest tick** (my own choice; check it against MC's exit prices in Phase 5).
- `Costs.slippage_ticks` moves market and stop fills (stop losses included), never limit fills or profit targets.
- Ambiguous fills: trades get an `ambiguous` column. It is True when the exit came from the path rule rather than the data, meaning either (a) a stop-type and a limit-type exit both lay inside the exit sub-bar's range, or (b) the position was entered intrabar in that same sub-bar. Fills at a sub-bar open are never ambiguous.
- Sub-bar mapping: each 1m bar belongs to the first strategy bar labeled at or after it, within the same `session_date`; 1m bars from other sessions are dropped. The run fails loudly if a 1m bar lies outside its strategy bar's high-low range. A bar with no 1m data falls back to its own OHLC and is counted in `BacktestResult.bars_without_sub_bars`.
- Tests: `tests/test_fills.py` has 22 tests (R4: OHLC path rule in three variants, magnifier vs OHLC on the same bar, a same-minute stop+target, an intrabar entry with its stop in the same or a later minute, gaps, stop/limit entries and exits, reversal by stop, tick rounding, slippage, validation, and magnifier determinism). Full suite: 57 passed.
- Speed read (A4), synthetic data: 74k 5m bars + 370k 1m bars, bracket every bar, $60 stop/target, ~58k trades → magnifier 3.0 s, OHLC 2.1 s. Ambiguous trades: 53% in OHLC mode vs 15% with the magnifier.

## Phase 5 results (2026-09-28)
- **A8 partly falsified.** `Vault Break MNQ 500$ risk.xlsx` holds only "List of Trades"; the Settings and analysis sheets say "Export cancelled by user". Inputs and costs were recovered from the trades instead:
  - Inputs: 5 contracts, profit target $500, stop loss $500 (both ±50 MNQ points). Everything else matches the `.el` defaults: entries run 09:50–15:05 (EarliestEntry 945, LatestEntry 1500), TimeX exits at 15:55 (FlattenTime 1550), at most 3 trades a day.
  - Costs: $0.40 commission + $0.04 slippage per contract per round trip. Profit-target trades cost $2.10 per 5-lot, market and stop exits $2.20, and run-up/drawdown carry $1.10 of entry-side cost. So MC charges half the slippage per market or stop fill and **none on limit fills**. The engine now does the same (`Costs.slippage`; this is the only engine semantics change in this phase).
- **A7 falsified → adapted.** The MC chart was in **exchange time (America/Chicago) on a 24-hour Globex session**, not RTH Eastern.
  - Evidence: every MC entry price equals our 1-minute open exactly one hour later in ET. Exits land at 15:20–15:55 CT (after the 15:15 CT equity close) and at 17:05/17:10 CT (evening reopen).
  - Consequences: Time, Date and the input times are Central; `Date <> Date[1]` fires at midnight CT; a "day" is a calendar date, Sunday evenings included.
  - Run with `--session eth --tz America/Chicago`. `load_bars(..., tz=)` relabels bars; sessions are still built in ET.
- **MC traded MNQ prices.** NQ opens are within 1 tick of MC's MNQ fills only ~86% of the time. So the price check needs MNQ data, and the MNQ store covers only 2025-06-08 → 2026-09-17.
- **MC's continuous contract rolls on the Wednesday of expiration week** (3rd Friday − 2 days). This held in 13 of 14 quarters 2023–2026; Mar 2023 had already rolled by Tuesday.
  - New importer mode: `--roll expiry [--roll-days-before 2]`. `volume` stays the default.
  - `data/NQ` and `data/MNQ` were re-imported with `--roll expiry`.
- **DailyATR has long memory.** It is `RngSum / RngCnt` over every day since the chart's first bar, never rolling off.
  - The MC chart starts around 2021-01-01: its first trade, 2021-01-20, is the first day allowed by `SessCount >= 15`. Local data starts 2022-12-28.
  - The port adds warm-up stand-in inputs `atr_seed_sum`, `atr_seed_count` and `sess_count_seed` (0 = literal port). `guban run --history-from NQ` prepends NQ bars ahead of MNQ's first bar (`splice_history`).
  - Seed used: 620 pre-data days (≈ 2 years × 310 Globex dates) × 276 points average range = 171,120. **The 276 was fitted against the MC export — one free parameter, not derived.**
  - The match rate is robust to the seed: 98–99% in the MNQ window for 272–280 points with 560–680 days. Net profit is not: one flipped ±$500 trade is ~7.6% of the MNQ-window net.
- **Parity (S3), against the CSV oracle as specified:**

  | Window | MC trades | Matched | Entry & exit within 1 tick | Net profit (MC / ours) | Result |
  |---|---|---|---|---|---|
  | MNQ data: 2025-06-08 → 2026-09-17 | 302 | 300 (99.3%) | 293 (97.7%) | 6,555.30 / 6,540.30 (−0.23%) | **PASS** |
  | NQ before Jun 2025 + MNQ after: 2022-12-29 → 2026-09-17 | 738 | 713 (96.6%) | 593 (83.2%) | 32,884.60 / 30,891.70 (−6.06%) | fails price and net (NQ ≠ MNQ prices) |

  - "Prices within 1 tick" is gated at ≥95% of matched trades, because the plan's diff report expects some mismatches.
  - The 7 MNQ-window price mismatches and the 2 unmatched pairs are all 0.5–2.75-point differences between MC's data and our minute store. None are tagged ambiguous. Only 1 trade in the whole run is ambiguous, so 1-minute magnifier resolution (Q8) is not a factor.
  - The Phase 4 choices (dollar exits rounded to the tick; tie goes high first) are consistent with every matched trade: stop and target exits sit exactly at entry ± 50 points.
- **A4 verified:** 263k 5m bars (24-hour session) + 1.3M one-minute sub-bars → load 0.5 s + backtest 1.7 s = 2.2 s. `trades.csv` is byte-identical across runs.
- **New code:**
  - `strategies/vault_break.py` — literal port.
  - `guban/parity.py`:
    - `load_mc_trades` reads the quoted CSV, or the report `.xlsx` with real exits.
    - Matching pairs trades of the same direction whose entries are within one bar. Our side gets one bar of slack at the window edges.
    - For the CSV, MC exit prices are implied from its net profit plus our costs (they agree exactly with the `.xlsx` exits).
    - It computes the S3 checks and writes `diff.html`.
  - CLI: `guban run` (for now it writes only `trades.csv`, plus timing) and `guban compare` (exit code 1 on FAIL).
  - Tests: `tests/test_parity.py` (9), plus slippage (1) and expiry-roll (3) tests. Full suite: 69 passed.
- **Reproduce:**
  ```
  uv run guban import Database/NQ-CME --symbol NQ --roll expiry
  uv run guban import Database/MNQ-CME --symbol MNQ --roll expiry
  uv run guban run strategies/vault_break.py --symbol MNQ --history-from NQ --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620 --set earliest_entry=945 --set latest_entry=1500 --set flatten_time=1550 --set flatten_early_close=false --out reports/vault_break_mnq
  uv run guban compare --ours reports/vault_break_mnq/trades.csv --mc "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --contract MNQ --start 2025-06-08
  ```
- **Open:**
  - S3 on the full 2021–2026 span needs MNQ 1-minute data for 2021-01 → 2025-06 exported from MC. That would also replace the fitted ATR seed with real history.
  - The other Vault Break exports (`Vault Break 1500.csv`, etc.) could cross-check the seed, if they came from the same chart.


## Phase 6 results (2026-09-28)
- **`guban run` now writes three files:** `trades.csv` (MC layout), `metrics.json`, and `report.html`.
  - New flags: `--capital` (default 1,000,000), `--no-report`, `--open` (opens the report in the browser).
  - Vault Break MNQ (263k bars) takes load 0.5 s + backtest 1.8 s + report 0.5 s = **2.8 s** (S2 ≤ 15 s). `report.html` is 5.5 MB, of which ~4.6 MB is the inlined plotly.js.
- **`trades.csv` in MC's trade-export layout** (`guban/tradelist.py`). Conventions were read off `Vault Break MNQ.csv`:
  - One row per trade. MC's 17 columns come first.
  - `order_id` = 2 × trade − 1. `type` is `EntryLong`/`EntryShort`; `direction` is `LONG`/`SHORT`.
  - `excel_date` = `excel_time` = the full Excel serial of the entry, in chart time.
  - `profit_pct`, `runup_pct`, `drawdown_pct` divide by the entry notional (price × point value × contracts). `cum_profit_pct` divides by the initial capital; the MC run used $1,000,000.
  - The engine's columns follow MC's: entry/exit ISO times, exit price and name, gross, commission, slippage, bars, and ambiguous.
  - Lines are not wrapped in quotes, unlike MC's file. `parity.load_our_trades` reads both this layout and the old one, and `load_mc_trades` reads ours too.
- **Run-up and drawdown per trade (new in the engine):**
  - They are tracked along the same intrabar path as fills (1-minute magnifier or OHLC), from the entry fill to the exit fill.
  - They are reported net of entry-side costs (half commission, plus half slippage on market or stop entries). Run-up is floored at 0.
  - All three rules come from MC's export, e.g. a profit-target trade shows run-up 498.9 = 500 − 1.10, and a trade that never went green shows 0.
  - **Check against MC:** on the 293 price-matched MNQ-window trades, run-up equals MC's to the cent on 99.7%, drawdown on 98.6%, both on 98.3%. The 5 misses are 1.5–19 points apart, which looks like the same MC-vs-our-data differences seen in Phase 5.
- **Engine additions:**
  - `BacktestResult.equity_low` is each bar's worst equity along the path (closed net + open gross at the adverse extreme, and closed net right after a fill). It feeds the intraday drawdown.
  - `BacktestResult` now also carries `spec` and `costs`.
- **Metrics** (`guban/metrics.py`, MC Strategy Performance Report core set; All / Long / Short):
  - Trade figures: net, gross profit/loss (sums of net winners/losers, as MC does), profit factor, trade counts (win/loss/even), percent profitable, averages, win:loss ratio, largest win and loss, max consecutive winners and losers (an even trade ends a streak), average bars in trade, commission, slippage, ambiguous fills.
  - Account figures:
    - Max close-to-close drawdown, on closed-trade equity.
    - Max intraday peak-to-valley drawdown: bar-close equity peak before the bar vs. the bar's `equity_low`. *(Superseded by the backend audit fixes — see "Backend audit fixes" at the end.)*
    - Return on account = net ÷ |intraday drawdown|.
    - Daily Sharpe: session-end equity changes, √252, no risk-free rate.
    - Trading days and open-position P/L.
  - `metrics.json` = run metadata + these figures (inf → null).
  - Vault Break MNQ: net 30,891.70, PF 1.20, 737 trades, 54.0% profitable, max intraday drawdown −7,182.50, close-to-close −7,081.10, ROA 430%, Sharpe 1.18.
  - MC's report `.xlsx` has no analysis sheets (Phase 5), so these summary figures have no MC oracle yet. Only the per-trade columns were checked against MC.
- **`report.html`:**
  - Self-contained: plotly.js is inlined and there are no external requests. It follows the light/dark system setting.
  - Sections: run details and inputs, KPI tiles, equity curve and intraday drawdown (one point per session), a performance summary table (All/Long/Short plus account figures), net profit by month, a price chart with trades, a paged and filterable trade list (click a row to show it on the chart), and notes (history splice, open position, ambiguous fills, bars without 1-minute data).
  - The price chart is a trade viewer. It shows strategy-bar candles ±24 bars around each trade, with gaps closed. Nearby trades share a window of up to 600 bars; windows over 1,500 bars merge k bars per candle, so the size stays bounded.
  - Checked by rendering in headless Edge (light and dark screenshots), with no console errors.
- Tests: `tests/test_metrics.py` (7) and `tests/test_report.py` (4).
  - Metrics are tested on a hand-computed 5-trade fixture: every figure, both drawdowns, ROA, Sharpe, and JSON safety.
  - Engine run-up/drawdown/`equity_low` are tested on synthetic bars, including the 0 floor and limit entries paying no slippage.
  - The report tests cover MC column values and the round-trip into compare, offline HTML with all sections, window encoding, and candle merging.
  - Full suite: 80 passed. `trades.csv` stays byte-identical across runs (R11), and Phase 5 parity is unchanged (S3 PASS, 300/302 matched, −0.23% net).

## Phase 7 results (2026-09-28)
- **`guban optimize <strategy.py> --grid ... [run's data/cost flags]`** writes three files to `reports/<strategy>_<symbol>_<tf>_<session>_opt/` (or `--out`):
  - `results.csv`: every run, ranked runs first.
  - `optimize.json`: run settings, the grid, and the best inputs with their metrics.
  - `optimize.html`: KPI tiles, a heatmap, and the ranked table.
  - New flags: `--metric` (default `net_profit`), `--min-trades` (default 30, my own choice), `--workers` (default: CPU cores), `--yes`, `--x/--y` (heatmap axes), `--top` (HTML table rows), `--open`. `--set` holds inputs fixed.
- **Grid spec** (`guban/optimize.py` `parse_grid`), one per input:
  - Forms: `name=start:stop[:step]` (inclusive; step 1 if omitted), `name=a,b,c`, or `name=v`.
  - `--grid` repeats, and one flag can hold several specs separated by spaces, so the plan's `--grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4"` works.
  - Ranges are stepped in decimal, so 0.4:1.0:0.1 gives exactly 0.4 … 1.0.
  - Values take the default's type: an int input rejects 0.5, and a float input turns 1 into 1.0.
- **Execution:**
  - `ProcessPoolExecutor` with one worker per core, capped at the number of combinations. `workers=1` runs in-process.
  - A pool initializer loads the strategy file and the bars **once per worker**:
    - From the CLI, each worker loads Parquet through `DataSpec.load()`. That function now also backs `guban run`: bars, 1-minute sub-bars, and the `--history-from` splice.
    - The parent loads the data first, so a missing bar cache is built once, not raced by the workers.
  - Results come back in grid order whatever the completion order, so the table is deterministic.
  - A failing run raises an error naming its inputs.
  - Grids over 5,000 combinations raise `TooManyCombinations`. The CLI exits with code 2 unless `--yes` is given.
- **Loading strategies in workers:** workers re-import the strategy from its file, so the class must be defined at a file's top level. `load_strategy_class` moved from the CLI to `guban/engine/strategy.py` and now registers the module in `sys.modules`, so a class loaded from a file can also be passed to `optimize()`.
- **Ranking** (`rank_results`):
  - One metric, best first. Runs need at least `min_trades` trades and a defined metric value (a profit factor with no losing trades is ∞, which ranks top). Runs left out stay in the table unranked.
  - The sort is stable, so ties keep grid order.
  - Metrics (`RESULT_METRICS`): net profit, profit factor, trades, % profitable, avg trade, both max drawdowns, return on account, daily Sharpe, max consecutive losers, largest loss.
  - Higher is better for all of them except max consecutive losers. Drawdowns and losses are ≤ 0, so the value nearest 0 wins.
  - `metrics.backtest_metrics(result, bars)` is now shared by `run` and `optimize`.
- **Heatmap** (`guban/optimize_report.py`):
  - Axes are the first two inputs with more than one value, or `--x/--y`.
  - Each cell holds the best value among runs that pass the min-trades filter, taken over every other grid input. Hover shows which values of the other inputs gave it.
  - Blank cells have no eligible run. ○ marks the best cell. The metric can be switched in the page.
  - With one varying input the chart is a line; with none, there is no chart.
  - Colors come from the dataviz reference palette:
    - Metrics that cross 0 use a diverging red–gray–blue scale centered on 0.
    - Other metrics use a single blue ramp, reversed where lower is better.
  - The page reuses the report's theme and inlined plotly.js.
  - Checked in headless Edge, light and dark, for the Vault Break grid, a 3-input grid, and a synthetic grid whose profits cross zero.
- **Vault Break grid** (S4): `noise_mult=0.4:1.0:0.1 × vwap_exit_bars=1:4` on the Phase 5 parity setup (MNQ + NQ history, ETH, Chicago, magnifier, $0.40/$0.04, 5 contracts, $500/$500, fitted ATR seed).
  - 28 runs took 15.3 s on 6 workers, versus about 64 s serially (2.3 s per run).
  - Every run had ≥ 30 trades.
  - Best: noise_mult 0.6 / vwap_exit_bars 4, net 32,769.50, 737 trades, max drawdown −7,332.50.
  - The whole noise_mult 0.6 column ranks 1–4. Profit falls off sharply above 0.7: 0.9 and 1.0 make about $8–9.6k.
  - The (0.6, 2) cell is 30,891.70, exactly the single `guban run` result. The MC defaults sit 4th of 28. This is an in-sample optimum, and Phase 8 (walk-forward) is where that gets tested.
- **Tests** (`tests/test_optimize.py`, 19):
  - Grid parsing: 9 bad-spec cases plus types and decimal steps.
  - **The R8 2×2 equivalence**: a 2-worker pool in magnifier mode with 1-minute sub-bars gives every metric exactly equal to four separate `run_backtest` runs, and `workers=1` gives the identical table.
  - Strategy loading: by file path and as a class loaded from a file.
  - Input validation, worker errors naming their inputs, and the 5,041-combination guard.
  - Ranking: the min-trades filter, NaN handling, stable ties, and lower-is-better.
  - Heatmap aggregation over a third input and axis defaults.
  - HTML: offline, heatmap, line and no-chart variants.
  - `results.csv` order.
  - Full suite: 99 passed. `guban run` for Vault Break still gives net 30,891.70 / 737 trades / max drawdown −7,182.50.

## Phase 8 results (2026-09-29)
- **`guban wfo <strategy.py> --grid ... [run's data/cost flags]`** writes five files to `reports/<strategy>_<symbol>_<tf>_<session>_wfo/` (or `--out`):
  - `trades.csv`: the stitched out-of-sample trades, in MC layout.
  - `windows.csv`: one row per window. It has the dates, the chosen combination and its inputs, IS and OOS net profit (total and per year), trades, WFE, and the chosen inputs' OOS rank.
  - `results.csv`: every window × combination, with `is_*` and `oos_*` metrics and both ranks.
  - `wfo.json`: settings, windows, efficiency, and the stitched OOS metrics.
  - `wfo.html`: the page.
  - Flags:
    - Window layout: `--is 12 --oos 3 --step` (months; step defaults to `--oos`), `--rolling/--anchored`, `--start YYYY-MM-DD` (default: the first whole month of data).
    - Otherwise as `optimize`: `--metric`, `--min-trades` (default 30, applied to trades entered in the IS window), `--workers`, `--yes`, `--capital`, `--open`.
- **Windows** (`guban/wfo.py` `make_windows`, A9 defaults):
  - Built on session dates, in whole months, with exclusive ends.
  - Each OOS window directly follows its IS window. The last one is cut off at the data end and marked partial.
  - `step < oos` is rejected, so no OOS day is in two windows. `step > oos` leaves gaps.
  - Vault Break data (2022-12-28 → 2026-09-17) gives 11 windows: OOS 2024-01 → 2026-09, the last one partial.
- **Warm-up (user decision, this session):** every run starts at the first bar of the data, so long-memory indicators (Vault Break's DailyATR) are warm in every window.
  - The engine is causal: later bars never change earlier trades or equity. A test proves this in both fill modes.
  - So a run through a window's end is the start of a run over all the data. Each combination is backtested **once** over all the data, and every IS and OOS window is scored from that run (`metrics.window_metrics`: trades by entry bar, equity rebased at the window start). A trade still open at a window's end counts whole in its entry window. *(Superseded by the backend audit fixes — see "Backend audit fixes" at the end.)*
  - Walk-forward costs one `optimize`, plus one re-run per distinct chosen input set. The re-runs rebuild the stitched equity curve.
- **Selection and stitching:**
  - Each IS window's winner is rank 1 by the metric among runs with ≥ `min_trades` IS trades (the same `rank_results` as `optimize`; ties keep grid order).
  - If no combination qualifies, that OOS window sits out (flat).
  - The stitched curve covers the OOS bars only. For each window it takes the chosen inputs' equity, rebased so the curve continues.
  - `boundary_trades` counts trades that span a window edge where the inputs change. There, the curve switches inputs while the trade list keeps each whole trade. Intraday Vault Break has 0. *(Superseded by the backend audit fixes — see "Backend audit fixes" at the end.)*
- **Walk-forward efficiency (WFE)** = annualized OOS net ÷ annualized IS net of the chosen inputs (net per 365.25 calendar days of the window).
  - Overall figure: pooled over the windows that traded.
  - Undefined when IS profit is ≤ 0.
- **Refactors:**
  - `guban/optimize.py`: the pool became a shared `_pool` context manager, with `_setup`, `pool_size` and `_run_full`. `_Job.windows` makes workers return per-window metrics. `optimize()` behavior is unchanged.
  - `guban/report.py` `write_report(..., sections=, bands=)` adds extra sections after the header and labeled, alternately shaded bands on the equity and drawdown charts. Sessions are aligned to the equity index, so a curve over part of the bars works.
- **Report** (`guban/wfo_report.py`):
  - A walk-forward section: layout and definitions; tiles for WFE, OOS and IS net per year, profitable OOS windows, and windows; the per-window table.
  - Then the full standard report of the stitched OOS result: KPIs, equity and drawdown with W1…Wn bands, summary, monthly table, trade viewer, trade list.
  - Checked in headless Edge, light and dark.
- **Vault Break WFO (S5)**, on the Phase 5 parity setup with the Phase 7 grid (`noise_mult=0.4:1.0:0.1 × vwap_exit_bars=1:4`), rolling 12/3/3:
  - 28 runs + 4 re-runs in 23.5 s on 6 workers.
  - Chosen inputs drift across windows: 0.4/1 → 0.6/4 → 0.6/1 → 0.5/1 → 0.4/1.
  - Stitched OOS 2024-01-01 → 2026-09-17: 674 trades, net 19,877.60 (equal to the sum of the per-window OOS nets), PF 1.14, max drawdown −7,136.70, Sharpe 0.92.
  - OOS 7,326/yr vs IS 10,247/yr → **WFE 71.5%**. 8 of 11 OOS windows profitable. The chosen inputs' OOS rank ranged from 3/28 to 25/28.
  - For comparison, the fixed MC defaults (0.6/2) earned 30,891.70 over the full 2022-12 → 2026-09 span.
- **Tests** (`tests/test_wfo.py`, 9):
  - Window layout, including the R9 check that no OOS window overlaps its IS window and OOS windows tile the span. Anchored windows, gaps, and error cases.
  - Causality, in magnifier and OHLC modes.
  - **Walk-forward equals runs truncated at each window end.** For every window: IS winner, IS/OOS net and trade counts, and the stitched trades frame, against independent `run_backtest` calls on bars through the IS end and the OOS end. On a seed where the choice changes between windows.
  - Every OOS trade lies in its OOS window and outside its IS span.
  - Stitched equity covers the OOS bars only and ends at the trade total. The WFE formula holds, pooled and per window.
  - `workers=1` equals `workers=2`. Sat-out windows. Boundary-trade counting. The HTML opens offline, and the CSVs are checked.
  - Full suite: 108 passed. `guban run` for Vault Break still gives net 30,891.70 / 737 trades / max drawdown −7,182.50.
- **Reproduce:**
  ```
  uv run guban wfo strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" --symbol MNQ --history-from NQ --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620
  ```

## Phase 9 results (2026-09-29)
- **`guban propfirm --trades <trades.csv | MC export .csv/.xlsx> --rules apex_50k_eod`** writes five files to `reports/<trades>_<rules>/` (or `--out`):
  - `propfirm.json`: rules, settings, trade statistics, the main run, degradation, sweep, best size, notes.
  - `sweep.csv`, `degradation.csv`: one row per size / scenario (pass rate with Wilson 95% CI, failure mix, days, drawdown).
  - `daily.csv`: the daily pool the simulation draws from.
  - `propfirm.html`: the page.
  - Flags override the rules file: `--size`, `--sims`, `--sweep-sims`, `--sweep 0.5,1,1.5` (empty: none), `--seed`, `--commission`, `--slippage`, `--risk`, `--open`.
- **Code:** `guban/propfirm/sim.py` (literal port of notebook cells 4–11), `guban/propfirm/report.py`, `config/propfirm/apex_50k_eod.toml`.
  - The rules file holds `[rules]` (target, drawdown, EOD trailing, daily loss limit, min/max days, account size), `[costs]`, `[simulation]` (sims, seed, size, sweep sizes, paths, bootstrap), `[[scenarios]]`, and `notes` shown in the report.
  - `parity.load_mc_trades(..., tz=None)` now returns naive chart time. `propfirm.load_trades` uses it, so MC CSV/xlsx exports and guban `trades.csv` all load.
  - Random draws happen in the notebook's exact order (`default_rng`; main = seed, degradation = seed+1000, sweep = seed+2000, paths = seed+5000). So results are bit-identical, not just statistically close.
  - The notebook's funded-account payout cells (12–16) are outside this phase's scope (cells 4–9) and were not ported.
- **S6 — verified by running the notebook's own cells 2–9** (Colab upload and matplotlib stubbed) side by side with `guban propfirm`:

  | Trade file | Notebook pass rate | guban | Other figures |
  |---|---|---|---|
  | `Vault Break Funded.csv` (the file behind the notebook's saved outputs) | 24.2% (4,836 / 8,414 / 0 / 6,750) | 24.2%, identical | Every main-run statistic, all 20,000 max drawdowns and days-to-pass, all 5 degradation rows and all 14 sweep rows are identical, and match the notebook's recorded output. |
  | `Vault Break MNQ.csv` | 35.8% (7,152 passed) | 35.8%, identical | All identical. |

  - Run time: 2.6–3.4 s for the whole notebook (20k main + 5×5k degradation + 14×5k sweep + paths).
- **Report** (`propfirm.html`, offline plotly, light/dark, checked in headless Edge):
  - Tiles: pass rate with CI, the failure mix, days to pass, expected attempts, verdict.
  - Charts: equity paths (pass / drawdown fail / time fail; target and limit lines), days-to-pass and max-drawdown distributions, degradation bars with CIs plus a table, size-sweep bars with CIs plus a table.
  - Also: trade and daily-pool statistics, and cumulative pass probability.
  - Two of the notebook's charts changed (dataviz rules): the outcome donut became tiles, and the degradation bars are one color instead of a position-based green→red ramp.
- **Findings about the notebook, flagged to the user (A10 check).** All are ported as written, and all appear as notes in every report:
  1. **"50K" vs $25k:** the rules are labeled Apex 50K but use a $25,000 account, $1,250 target and $1,000 drawdown. `account_size` is never used. The rule values need the user's review against the firm's current rules.
  2. **Costs are double-counted:** $2.25 + $5.00 per contract is deducted from `profit_usd`, which in MC exports (and guban `trades.csv`) is already net of the backtest's costs. The rate is also per *NQ* contract but applied to the file's contracts, so a 5-MNQ file pays $36.25 per trade.
     - On `Vault Break MNQ.csv`: 35.8% with the notebook's costs vs 43.2% with `--commission 0 --slippage 0`. Best size: 1.6× → 42.7% vs 1.9× → 51.5%.
  3. **Size labels:** the sweep's "MNQ" labels (`int(size×10)`) assume a 1-NQ trade file. guban labels size as a multiplier of the file's size, with the contract count.
  4. **Zero-trade weekdays are in the pool:** ~68% of Vault Break weekdays have no trades, so the 21-"trading-day" deadline is really 21 weekdays. Trades dated on a weekend are dropped (counted in a note; Vault Break has none).
  5. **The drawdown never locks:** it trails the end-of-day high for the whole evaluation.
  6. **Funded section inconsistencies (not ported):** the markdown says a 10-day window, a $2,500 drawdown and >$300 on 5 days; the code uses 20 days, the $1,000 challenge drawdown and >$100.
- **Tests** (`tests/test_propfirm.py`, 28):
  - The shipped rules equal the notebook's values.
  - Rules lookup and overrides; quoted MC CSV loading; costs and R; daily pool (weekday fill, weekend drop).
  - **`simulate` equals a verbatim copy of the notebook's `simulate_apex` / `apply_day_degradation`** across 16 combinations: EOD and non-EOD, daily limit, min days, sizes, degradation. Counts and per-simulation arrays match exactly.
  - Hand-computed outcomes (pass day, drawdown at exactly the limit, daily limit, min days, time); the trailing high with a scripted draw order; degradation.
  - Paths, Wilson CI (the notebook's 23.6–24.8%), verdicts, determinism, offline HTML, and the CLI.
  - Full suite: 136 passed.
- **Reproduce:**
  ```
  uv run guban propfirm --trades "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --rules apex_50k_eod
  ```

## Phase 10 results (2026-09-29)
- **`uv run guban --help`** lists the commands in workflow order: import, bars, run, compare, optimize, wfo, propfirm. The epilog gives the workflow and the exit codes. `guban` with no arguments shows help; `guban --version` prints the version.
- **Consistent flags** (`guban/cli.py`):
  - `run`, `optimize` and `wfo` now build their data, cost and strategy flags from the same module-level option objects: `--symbol --contract --tf --session --history-from --tz --fill-mode --data-dir --commission --slippage --set --class`. So names, defaults and help cannot drift. `tests/test_cli.py` checks this.
  - `--help` groups the flags into Data / Costs / Strategy / Optimization / Walk-forward / Output panels.
  - Validation now happens before any data is loaded:
    - `--fill-mode` accepts only magnifier or ohlc, and `import --roll` only volume or expiry.
    - `--tz` and `compare --mc-tz` must be valid IANA time zone names.
    - `import SOURCE` must be an existing folder.
    - `optimize` and `wfo` share one `_parse_search` for `--grid`, `--set`, `--metric` and the 5,000-combination guard.
  - `--out` is now a folder in every command. `compare --out` used to be a file path; a `.html` path is still accepted. `compare` gained `--open`.
- **Run IDs:** `run_dir()` gives `reports/<strategy>_<symbol>_<tf>_<session>[_opt|_wfo]`. `<symbol>` becomes `NQ-MNQ` when `--contract` differs from `--symbol`, so those runs no longer overwrite each other. The `propfirm` and `compare` defaults are unchanged.
- **Error messages** for the Edge Cases list, via `@_friendly` on every command:
  - What it catches: KeyError, ValueError, OSError and RuntimeError, which includes `StrategyError` and worker failures.
  - What it prints: one `Error: <library message>` line, then exit code **2**. `compare` FAIL stays exit 1. `GUBAN_DEBUG=1` re-raises with the full traceback.
  - Cases checked: unknown symbol, session or time zone; a timeframe that does not divide the session; missing Parquet ("run `guban import` first"); a bad `--history-from`; a file that is not a trade list (`compare` used to raise a bare `KeyError: 'entry_time'`); a grid over 5,000 combinations; a bad `--set` or `--grid`.
  - **Strategy exceptions:** the message names the bar, its timestamp and the position. It is followed by the frames from the strategy's own file (`File ".../boom.py", line 9, in on_bar`) and "no results were written".
    - This also works inside `optimize` and `wfo` worker processes. The strategy frames are parsed from the text of `concurrent.futures`' `_RemoteTraceback`, because the worker's `StrategyError` reaches the parent as a plain RuntimeError.
- **Bug fixed:** when stdout was piped or redirected on Windows (cp1252), the `optimize` and `wfo` summaries crashed on "−" and "→", after their files had been written. The CLI now switches non-console stdout and stderr to UTF-8.
- **README.md** (was empty):
  - Install, and where the two inputs come from:
    - Bar data comes from **DeepCharts (dxFeed)**, not MultiCharts (user, this session). This replaces the plan's assumption that MC exports it.
    - MC trade lists: Strategy Performance Report → Excel. guban reads the List of Trades sheet; the user's CSVs are a conversion of it.
  - The MC chart settings that parity needs.
  - A PowerShell walkthrough of import → run → compare → optimize → wfo → propfirm, with the Vault Break commands and their expected results.
  - A strategy-writing guide, with an example that was run verbatim.
  - Output folders, exit codes, configuration, and known limits.
- **CLAUDE.md:** a CLI conventions bullet, plus the data source.
- **Tests:** `tests/test_cli.py` has 22 tests. They cover the command list and order, version and no-args help, shared-flag identity, `run_dir`, default output folders, the `compare --out` folder, strategy-exception output and nothing being written, worker traceback parsing, 11 input-error cases, the missing-data message, UTF-8 redirect, and `GUBAN_DEBUG`. Full suite: 158 passed.
- **Regression check:** results are unchanged.
  - `run`: 737 trades, net 30,891.70, max drawdown −7,182.50, 3.3 s.
  - `compare`: S3 PASS, 300/302 matched, −0.23% net.
  - `optimize`: best 0.6/4 → 32,769.50, 15.6 s.
  - `wfo`: OOS 19,877.60, WFE 71%, 20.3 s.
  - `propfirm` runs on the guban `trades.csv`.
- **Open (not changed):**
  - ~~`StrategyError` bar numbers were 0-based~~ **Fixed (2026-09-29):** "failed at bar N" and "since bar N" are now 1-based, matching `current_bar` (MC CurrentBar). The two engine tests that pinned the old numbers were corrected.

## Backend audit fixes (2026-09-29)
The backend audit (FIX.md, findings #1–#15) is fixed in five commits ("Backend audit fixes, part 1–5"). Full suite:
220 passed. What changed for users:
- **Walk-forward (#1).** Every window, in sample and out of sample, is its own trading window: indicators warm up
  from the first bar, but nothing fills before the window's first bar, and a position still open after its last bar is
  closed at that bar's close (exit "Window End"). No later price reaches a window's score, and no position is carried
  into a window. Windows that start on the same bar share one run (`run_backtest(trade_from=, window_ends=)`,
  `BacktestResult.window`). The OOS stitch re-runs each traded window and checks final equity = Σ trade net.
  `boundary_trades` = "Window End" exits. Vault Break WFO: 675 OOS trades, net 19,777.90, WFE 71%, 1 boundary trade
  (the position open at the end of the data), 164 s on 6 workers (was 23.5 s).
- **Drawdown (#2).** Intraday peak-to-valley drawdown follows the running equity peak along the intrabar path
  (`equity_high`, `bar_drawdown`). Vault Break: max drawdown −7,315.00 (was −7,182.50); trades and net unchanged.
- **Validation (#3, #5, #9, #10, #12, #13, #15).** Bars and sub-bars (finite numeric OHLC, geometry, volume), costs,
  contract specs, order quantities, capital, grid / `--set` values (typed by the input's default), prop-firm rules
  files (strict TOML types and ranges), and the binary importer (whole records, prices, volume, times) all reject bad
  input with a message naming the fix. All 2,372 real minute files pass. Strategy errors in `__init__` / `on_start`
  are friendly `StrategyError`s. Bars whose sub-bars don't reproduce their OHLC use the OHLC path.
- **Parity (#4).** Counts and net use the exact window on both sides; the ±1-bar slack is for matching only.
  Matching is optimal (most pairs, then least total time difference), not greedy. Empty inputs and zero nets handled.
  Vault Break S3 unchanged: 300/302 matched, −0.23%, PASS.
- **Data (#6, #8).** The volume roll decides each session from the previous session's volume (existing `--roll volume`
  imports keep their old rolls until re-imported; MC-parity imports use `--roll expiry`, unchanged). `run` / `wfo`
  report trades held across a roll. Bar caches carry a fingerprint (source, timeframe, session, `CACHE_VERSION`) and
  every Parquet write is atomic.
- **Engine hygiene (#7, #14).** Inputs are deep-copied per strategy instance; reusing an instance raises.
- **Prop firm (#10, #11).** The bootstrap runs in bounded-memory batches with the notebook's exact draws; the report
  labels the method's modeling assumptions. S6 unchanged: Vault Break MNQ 35.8%, 7,152 passed.

- **Scheduled closes and data gaps (FIX.md VaultBreak follow-up A).** `config/calendar.toml` lists CME equity early
  closes (13:00 ET holidays, 13:15 ET half days, Good Friday mornings; 2023–2026 checked against the NQ data), named
  per contract by `calendar` in `contracts.toml`. `load_bars` adds `session_close` (not cached) and strategies read it
  as a series. Vault Break flattens `early_close_buffer` (10) minutes before an early close, and its last entry moves
  with it; `flatten_early_close=false` restores the literal .el. That setting is needed for MC parity, since MC held
  through those closes. `run` / `wfo` report trades held past their session's scheduled close or across a data gap
  (`metrics.session_exposure`). An unscheduled gap can't be known in advance, so the strategy acts on the first bar
  after it. Eastern defaults: 700 trades (was 702); 1 trade held over a close, #619, the 2026-05-14 feed gap (was 4).
  MC parity (Central inputs pinned, `flatten_early_close=false`): 737 trades, net 30,891.70, S3 PASS unchanged.

## Phase 11 plan: trade Monte Carlo (approved 2026-09-29)
Goal: every `guban run` also runs a Monte Carlo on its own trades and shows it in `report.html` (opened by `--open`).
It is separate from the prop-firm Monte Carlo (`guban propfirm`), which is unchanged.
- **Decisions (user):** two methods side by side: **resample** (n trades drawn with replacement: net profit and drawdown
  vary) and **reorder** (the same trades shuffled: net fixed, only the path and drawdown vary). Draw unit: single trades
  (a whole-day mode is parked). On by default on every `guban run`.
- **Defaults:** `--mc-sims 10000` (0 = off), `--mc-seed 42` (resample uses seed, reorder seed+1), optional
  `--mc-dd-limit USD`. Drawdown basis: close-to-close, exactly `compute_metrics`' definition (a trade list has no
  intraday path) — labeled on the page.
- **R12.** WHEN `guban run` finishes with ≥ 2 trades and `--mc-sims` > 0, THE SYSTEM SHALL simulate both methods and
  report per method: 5/25/50/75/95th percentiles of net profit and max drawdown, P(max DD deeper than the backtest's
  close-to-close max DD), P(net ≤ 0) (resample), P(max DD ≤ −limit) with `--mc-dd-limit`; equity bands with the actual
  path; histograms. Reorder net = backtest net in every run. Same seed → identical output. `--mc-sims 0` → no section,
  no `monte_carlo` key; < 2 trades → skipped with a reason; bad flag values → `Error:` + exit 2. `trades.csv` unchanged.
- **Build steps:** (1) `guban/montecarlo.py` (numpy, fixed 1,000-run batches, bands sampled at ≤ 400 trade indexes,
  60-bin histograms) + `tests/test_montecarlo.py`; (2) `run` flags in a "Monte Carlo" help panel, `metrics.json`
  `monte_carlo`, terminal line; (3) `write_report(..., monte_carlo=)` section after the Drawdown chart, two columns
  on wide screens, light/dark; (4) Vault Break run, docs (USE.md, README.md, CLAUDE.md, run-backtest skill), commit.

## Phase 11 results (2026-09-29)
- **`guban run`** now runs the trade Monte Carlo by default (`src/guban/montecarlo.py`): 10,000 resample + 10,000
  reorder runs of the run's `net_pnl`. Flags `--mc-sims` (0 = off), `--mc-seed`, `--mc-dd-limit`, in a "Monte Carlo"
  help panel; bad values are rejected before data loads (`Error: --mc-sims must be ...`, exit 2).
- **Outputs:** `metrics.json` `monte_carlo` = {sims, seed, unit "trade", dd_basis "close_to_close", dd_limit, trades,
  backtest {net_profit, max_drawdown}, resample/reorder {net_profit and max_drawdown percentiles p5–p95,
  p_dd_worse_than_backtest, resample p_net_le_0, p_dd_reaches_limit with a limit}}; no timing, so it is deterministic.
  `--mc-sims 0` → no key; < 2 trades → `{"skipped": reason}`. A terminal `monte carlo` line and its time.
- **Report:** a "Monte Carlo" section after the Drawdown chart: two columns (one below 800 px) with headline
  probabilities, percentile bands (5–95, 25–75, median) with the backtest path, max-drawdown histograms (and a
  resample net-profit histogram) with the backtest value dashed, and a percentile table. Checked in headless Edge in
  light, dark and 600 px widths. Adds ~43 KB to report.html.
- **Design:** batches sized by trade count (`BATCH_CELLS // n`) bound memory; numpy `integers`/`permuted` draws are
  batch-size invariant (verified, and pinned by a test), so results don't depend on it. Bands are percentiles at
  ≤ 400 trade counts; histograms are binned in Python (60 bins).
- **Vault Break MNQ parity run:** trades unchanged (737, net 30,891.70; `trades.csv` byte-identical with and without the
  Monte Carlo). Close-to-close max DD −7,081.10. Resample net P5 9,524.65 / P50 30,819.55 / P95 51,851.28,
  P(net ≤ 0) 0.9%, max DD P5 −13,491.37; reorder max DD P5 −11,697.53, P(DD worse than backtest) 58.0%. 0.7 s
  (20,000 trades × 10,000 runs: 13 s).
- **Tests:** `tests/test_montecarlo.py` (17: drawdown = compute_metrics' close-to-close, per-run hand checks, reorder
  net constant, determinism, batch-size invariance, zero spread, percentile order, band sampling, summary shape, flag
  validation) and 6 CLI tests (default on, determinism/seed, off, skipped, 3 bad-flag cases). Full suite: 255 passed.

## IVB parity audit (2026-10-05)
Audit of the engine against a second MC strategy, using the user's MC trade export `IVB 1 min.csv` (886 trades,
2021-01-05 → 2026-09-22, entry rows only, MNQ). The .el source name is unknown, so `strategies/ivb.py` was rebuilt
from the user's description (long only, 1-minute close above the opening range, stop at the IVB low, 1:1 target) plus
rules recovered from the export and the data:
- IVB = high/low of the 1-min bars labeled 08:31–09:00 CT; entry = the first bar closing above the IVB high, at that
  bar's close (540/584 MC entries), signal bars through 12:00 CT, 1 trade a day; size = floor($1,000 / ((entry − IVB
  low) × BigPointValue)) (562/584); time exit at the close of the 14:00 CT bar (395/584 exits within a tick vs ≤ 243 for
  any other minute); costs commission 1.22 + slippage 0.04 USD per contract.
- **Engine additions:** `at="close"` orders (MC `this bar on close`) and `Strategy.big_point_value`; 6 tests.
  Full suite: 261 passed.
- **Parity run:** `uv run guban run strategies/ivb.py --symbol MNQ --history-from NQ --tf 1m --session eth --tz America/Chicago --commission 1.22 --slippage 0.04 --set flatten_early_close=false --out reports/ivb_mnq`
  (584 trades, 15.6 s).
- **MNQ-data window** (`guban compare ... --contract MNQ --tf 1m --start 2025-06-08 --end 2026-09-17`): 199/199 matched
  (100%), prices within 1 tick 97.0%, net −8.25% (MC 3,541.10 / ours 3,249.00) → S3 FAILS on net only. 284.60 of the
  292.10 gap is two data-side trades: 2025-11-28 (12:15 CT early close, no 14:00 bar: we hold to the Sunday reopen;
  MC's implied exit 25,522.50 matches neither our 12:15 close nor the reopen) and 2026-05-14 (DeepCharts feed gap over
  14:00 CT). Without them the net differs by ≈ −0.2%.
- **Whole overlap** (2022-12-29 → 2026-09-17, NQ prices stand in before 2025-06-08): 570/583 matched (97.8%), net
  +0.51%, but prices within 1 tick 62.5%: NQ 1-min closes differ from MNQ's by 1–3 ticks (NQ period 43.9% vs MNQ period
  97.0%), and all 13 unmatched trades are NQ-period breakouts shifted by a bar.
- **Open:** (1) MC rolled MNQ Jun→Sep 2025 one session earlier than our store (MC on 202509 on Tue 2025-06-17; ours
  rolls at Tue 17:00 CT). Only one trade is affected and no other roll-week Tuesday has a trade, so it is unconfirmed
  whether this is systematic. (2) Flattening before early closes (`flatten_early_close=true`) does not match MC (MNQ
  window prices 95.0%, net −16.4%), so the parity run keeps it off; what MC does on those days is unresolved.
  Both are **parked**: the user waived MC parity for IVB (2026-10-05), so they are not worked on unless asked.
- **Ledger integrity check (2026-10-05, user asked: no trade counted twice, none missed when the signal appears).**
  Code read of the order/fill/ledger path (`Broker._execute`, `_close`, `_fire`, `fill_on_close`, `tradelist`,
  `metrics`): an order is removed from the live list when it fills, exits only fire against a matching position, a
  reversal closes and opens at one fill, and every closed lot is one ledger row. No bug found; no engine change.
  - Real run `guban run strategies/ivb.py --symbol NQ --tf 1m --session eth --tz America/Chicago --commission 1.22
    --slippage 0.04` (defaults, `flatten_early_close=true`): 589 trades, net 115,488.96 (TimeX 249, TP 180, SL 160).
    An independent replay of the docstring rules (no engine or strategy code) gives the same 589 trades: entry bar,
    price, contracts, exit bar, price, name, gross and net P&L all equal. 589 signal days, 589 trades: no signal day
    without a trade, no trade without a signal, every entry on the day's first breakout bar. Ledger: trade ids 1..N,
    no duplicate rows or entry times, no overlap, ≤ 1 entry a day, cum P&L = running sum, metrics.json totals =
    trades.csv. Trade #533 (2026-05-14) exits at 17:01 CT because the DeepCharts data stops at 13:22 CT that day (no
    14:00 bar); flagged by the run as held past close / across a gap. Correct given the data.
  - `tests/test_ledger.py` (9 tests, ohlc + magnifier on a synthetic 1-min random walk → 5-min bars): an IVB-style
    strategy's trade entries equal its signal bars exactly; random orders of every kind (market/close/stop/limit,
    reversals, partial exits, stop loss/target) with unique names → each entry and exit order fills at most once,
    each closed position's rows sum to its entry qty, every position seen at a close is recorded, reversal exits start
    the next position at the same fill, final equity = net + open P&L; stop + target + market exit in one bar → one
    exit. Mutation check: injecting a duplicated or a dropped ledger row fails both tests in both modes.
    Full suite: 270 passed.

## Phase 13 plan: POC-shift study on tick data (approved 2026-10-05)
Question (user): on NQ, when day D-1's RTH POC is above D-2's, is D's RTH POC above D-1's more often than on an
average day (and the mirror case for down)? First study run outside the engine; the reusable parts are split out
from the start so later studies only define their signal and outcome. A `guban study` command and `Study` base class
are **parked** until a second study exists (user, 2026-10-05).
- **Decisions (user):** fair value = the RTH POC (09:30–16:00 ET); POCs from **tick data**, using only clean days;
  the minute-bar POC is a secondary run over the full minute history (2022-12-28 →) and measures how often it agrees
  with tick. **Verdict rule**, per side: REAL only if (1) P(D up | D-1 shifted up) beats the base rate P(D up) by
  ≥ 3 pp, (2) a one-sided exact binomial test against the base rate gives p < 0.05, and (3) the gap is positive in
  every calendar year (2023's ~30 sessions pooled into 2024). Everything else is labeled secondary.
- **Tick store (verified 2026-10-05):** `Database/NQ-CME/<YYYYMM>/Tick/<YYYYMMDD>.data` for 202312–202612, sessions
  2023-11-16 → 2026-09-23 (737 front-month sessions; 309 of 1,309 files empty). Record = 57 bytes: i8 .NET ticks
  (ET wall-clock, trade time), f8 price, f8 bid, f8 ask, i4 size, i4 size (dup), i4 cumulative volume (its diff ==
  size), i4 ×3 zero, u1 flag (1). Files run 18:00 ET → ~16:59 ET next day. Quality vs the minute files: most sessions
  match the minute volume exactly; 20 end at 09:29:59 (no RTH), ~25 (Jan 2024) carry ~1.2× the volume, 3 carry ~3×,
  ~20 (mostly 2026) differ by 2–40%.
- **Defaults:** contract per session from the existing `choose_contracts` (volume roll); triples whose three days
  span different contracts are dropped (calendar spread ≠ a POC shift); D-1 = the previous session with data;
  early-close days count, RTH ending at the scheduled close; POC ties → the price nearest the RTH range midpoint;
  a zero shift is "flat" and kept out of the rates; minute POC spreads each 1-min bar's volume evenly over its
  0.25 ticks from low to high; no new dependencies (exact binomial from `math`); stop with an error if < 500 clean days.
- **Clean day:** tick file non-empty, last RTH trade within 2 min of the RTH close, RTH tick volume within ±2% of the
  same contract's RTH minute volume.
- **R13.** WHEN `research/poc_shift.py` runs, THE SYSTEM SHALL build (and cache) per-day RTH volume-at-price from tick
  data, compute tick and minute POCs, apply the clean-day and roll rules (reporting drop counts per reason), and
  report per side: base rate, conditional rate, gap, p-value, per-year gaps and the verdict; plus secondary tables
  (2×2 signal × outcome, shift-size buckets ≤5/5–20/20–50/>50 pts, "RTH close > open on D"), minute-vs-tick agreement,
  and the minute-history study.
- **Layout:** `src/guban/data/ticks.py` (reader + checks + RTH volume-at-price, cache `data/NQ/vap_rth_tick.parquet`);
  `src/guban/research/` (reusable: `profile.py` POC/value area, `stats.py` rates/binomial/verdict/per-year,
  `report.py` HTML); `research/poc_shift.py` (study-specific signal/outcome + runner, output `reports/poc_shift/`).
- **Build steps:** (1) confirm the half-day close rule and that tick times are ET (one clean day's RTH tick volume ==
  RTH minute volume); (2) tick reader + cache + clean-day rule, `tests/test_ticks.py`; (3) POCs and triples,
  `tests/test_research.py`; (4) statistics and verdict (binomial checked by hand: n=10, k=8, p=0.5 → 0.0547);
  (5) minute comparison, report, docs, commit.

## Phase 13 results (2026-10-05)
- **Answer: NOT REAL on both sides.** Tick POCs, 2023-11-21 → 2026-09-23, 594 observations (595 triples, 1 flat outcome):
  - Up: P(D POC up | D-1 shifted up) = 55.0% (182/331) vs base 56.6% (336/594), gap −1.6 pp, p = 0.74.
  - Down: P(D POC down | D-1 shifted down) = 41.2% (108/262) vs base 43.4%, gap −2.2 pp, p = 0.78.
  - Every year's gap is negative on both sides (up: 2023-24 −1.3, 2025 −2.1, 2026 −1.7 pp; down: −1.8, −3.5, −1.8 pp):
    a POC shift is followed by another shift the same way slightly *less* often than on an average day.
  - Secondary, all agree: minute POCs 2023-01-03 → 2026-09-23 (928 obs) up 57.4% vs 56.9% (p 0.42), down 43.8% vs 43.1%
    (p 0.41); RTH close vs open on D +1.0 / +1.1 pp (p 0.38). Value-area relationship: D's POC rose most often after an
    "overlapping higher" (60.9%, n 128) or a "lower" (59.8%, n 117) D-1 value area — not tested, descriptive only.
  - The shift-size buckets (A8: 5/20/50 pts) are too fine for NQ: 268 of 331 up-shifts and 198 of 262 down-shifts are
    > 50 pts. Secondary only; widen them if the table is used.
- **Data:** 964 RTH trading days in the store, 678 clean tick days (no tick file before 2023-11: 229; tick volume ≠
  minute volume: 38; no RTH ticks: 18; ticks end early: 1). Tick triples dropped: 346 touch an unclean day, 21 span a
  roll. Independent check: 6 random clean days' POCs recomputed from the raw files with pandas match exactly, and the
  rates recomputed from `triples.csv` match.
- **Minute vs tick POC:** within 2 pts on 37.8% of 678 days (median 3.25 pts, 90th pct 61 pts: the even-spread
  approximation sometimes picks the other peak of a two-humped profile), yet the shift direction agrees on 95.0% of
  triples (outcome 94.8%). For direction studies the minute history is a usable stand-in; for POC prices it is not.
- **Tick format change found:** from 2026-05-26 11:23 ET the feed fills the last two i4 fields, the flag is mostly 0,
  and size-0 records appear (5–25% of a session). Summed `size` still matches the minute volume within ~1%, so the
  reader accepts size 0 (rejects only negative sizes); the cumulative-volume field no longer tracks size and is unused.
- **Layout as planned:** `src/guban/data/ticks.py`, `src/guban/research/{days,profile,stats,report}.py`,
  `research/poc_shift.py`. First run ~3.5 min (964 tick files), then ~20 s from the cache. Report checked in headless
  Edge (light 1280 px, dark 600 px). Tests: `tests/test_ticks.py` (11), `tests/test_research.py` (16). Full suite:
  297 passed.

## Phase 14–15 plan: shared market-data store (approved 2026-10-06)
Goal (user): Guban and the Research Pipeline (`D:\Research Pipeline`) read the **same processed data** for NQ, MNQ, ES, GC
and BTC from one write-once store at `D:\Market Data\`, built from the DeepCharts folder `D:\Deepchart\Database`
(read-only, still being written by the app). Each 1-minute bar comes from **ticks where valid ticks exist**, else from
the DeepCharts minute records. The pipeline gets the same five symbols. Pipeline-side work is planned in its own
`PLAN.md` ("Shared data store" phases SD1–SD3) and follows its rules (one phase per commit, Ariel's go-ahead between).

- **Interview (2026-10-06, 9 questions):** source = `D:\Deepchart\Database`; symbols = all five; minute + tick,
  tick-built bars preferred; one shared processed store; pipeline gets all five symbols; a write-once store replaces the
  pipeline's frozen raw snapshot; commissions researched online; slippage 0.5 tick per side for every symbol; plan approved.
- **Source facts (verified 2026-10-06, `ls`):** `BTC-CME`, `ES-CME`, `GC-COMEX`, `MNQ-CME`, `NQ-CME`, 83 GB, each
  `<contract>/{Minute,Tick}/<YYYYMMDD>.data`. Minute from NQ 2018-12-30, ES 2019-12-29, BTC 2020-01-05, GC 2021-01-03,
  MNQ 2025-06-08; ticks from 2023-11-16 (MNQ 2025-11-30). Latest sessions 2026-10-04/05 (the newest is in progress).
- **Known tick quirks (Phase 13 + pipeline `docs/data_notes.md`):** ~25 Jan-2024 sessions carry ~1.2× the minute volume,
  3 carry ~3×, ~20 (mostly 2026) differ 2–40%, 20 end at 09:29:59; from 2026-05-26 11:23 ET size-0 records appear and the
  cumulative-volume field (`seq`) stops tracking size; tick bid/ask is a side marker, not a real quote, before 2025-11.
  → Store `seq` as-is but document it as unreliable from 2026-05-26; drop size-0 records when building bars.

### Store layout (contract between the two projects; also written to `D:\Market Data\README.md`)
```
D:\Market Data\
  README.md, manifest.csv       schema contract; one manifest row per stored session file (source path, bytes, sha256, built_at, bar source)
  <SYM>\minute\<contract>\<YYYYMMDD>.parquet   ts_utc (bar OPEN, UTC), open, high, low, close, volume, ask_vol, bid_vol, contract, source
  <SYM>\tick\<contract>\<YYYYMMDD>.parquet     ts_utc (ms), price, size, side (+1 at ask, -1 at bid, 0 between), seq, contract
  <SYM>\roll_calendar.csv       volume roll: front from the session AFTER the new contract first out-trades the old; never back
  <SYM>\quality.csv             per session: minute volume, tick volume, diff %, bar source, issues
```
Each project derives its own view: Guban = New York time, bar-CLOSE label, unadjusted, `--roll volume|expiry`;
pipeline = additive back-adjustment + holdout guard (trade_date ≥ 2026-01-01).

### Rules
- **Bar source per session:** tick file present, whole 57-byte records, and session tick volume within 2% of minute volume
  → bars from ticks (per minute, open label: O first, H max, L min, C last, volume Σsize, ask_vol Σsize side=+1,
  bid_vol Σsize side=−1); else minute records (minutes whose records hold no volume dropped; all records of a minute
  with volume merged — *amended 2026-10-06: the opening minute's zero-volume record is its true open, see Phase 14
  results*). A minute
  with a minute record but no ticks inside a tick session keeps the minute record. Every bar carries `source`.
- **Write-once:** build a session only after it has ended (now > file date + 1 day 17:00 ET) and its files are whole
  records; one Parquet file per session per kind, set read-only; a later source sha256 change for a stored session is
  reported (`changed_sources`), never overwritten, unless `--rebuild SYM:SESSION`. The DeepCharts folder is opened read-only.
- **Costs (USD per contract):**

  | Symbol | Commission RT | Per side (pipeline) | Slippage per side (0.5 tick) | Source |
  |---|---|---|---|---|
  | NQ | 2.50 | 1.25 | 2.50 | user's existing pipeline value |
  | MNQ | 2.00 | 1.00 | 0.25 | user's existing pipeline value |
  | ES | 3.78 | 1.89 | 6.25 | Topstep itemized all-in (help.topstep.com, 2026-10-06) |
  | GC | 4.32 | 2.16 | 5.00 | Topstep itemized all-in |
  | BTC | 16.04 | 8.02 | 12.50 | $1.00 commission + 2×$7.50 CME non-member exchange fee (CFTC filing) + 2×$0.02 NFA |

  Guban: per-symbol defaults in `config/contracts.toml`, used when no cost flags are given; explicit flags (MC-parity
  runs) still win. Slippage RT = 1 tick × tick value, charged MC style (half per market/stop fill).

### Requirements
- **SR1.** `guban store build` writes only finished, whole-record sessions and lists skipped ones.
- **SR2.** Valid tick sessions (±2%) are tick-built, others minute-built; `quality.csv` gives each session's source/reason.
- **SR3.** Tick-built bars reproduce minute volume on ≥ 99% of minutes and OHLC on ≥ 99.9% of minutes in valid sessions
  (`guban store verify` prints the rates per symbol). *(Restated 2026-10-06 from "every minute": see Step 1 findings.)*
- **SR4.** A second build with unchanged sources changes no file; a changed source for a stored session is reported.
- **SR5.** The DeepCharts folder is never opened for writing.
- **SR6.** `guban import --symbol <SYM>` builds `data/<SYM>/1m.parquet` from the store with Guban's conventions;
  minute-sourced sessions are identical to the old import.
- **SR7.** `config/contracts.toml` (and the pipeline's `instruments.yaml`) define all five symbols after the price-grid check.
- **SR8.** Default costs per the table appear in `metrics.json` when no cost flags are given.
- **SR9.** `config/calendar.toml` covers early closes for every year of every symbol (equity 2019–2022, `cme_metals`,
  `cme_crypto`), derived from each session's last bar and spot-checked against CME's holiday calendar.
- **SR10.** (Pipeline) loaders read all five symbols from the store; the holdout guard still holds.
- **SR11.** Vault Break still passes the S3 gate on the rebuilt NQ data; `tests/test_ledger.py` stays green.

### Assumptions (verify in step 1)
| ID | Assumption | If wrong |
|---|---|---|
| B1 | GC tick 0.10, $100/pt; BTC tick 5, $5/pt | grid check fails → correct config |
| B2 | GC/BTC files use NQ's layout and 18:00 → 17:00 ET session window | adapt decoder/session check per symbol |
| B3 | 2% volume tolerance suits every symbol | tune per symbol from `quality.csv` |
| B4 | GC RTH 08:20–13:30 ET (`rth_comex`); BTC RTH 09:30–16:00 ET | change in config |
| B5 | Early closes from the data's last bar are the scheduled closes | correct from CME's holiday calendar |
| B6 | BTC exchange fee $7.50/side (CFTC filing; date not confirmed) | update the BTC commission |
| B7 | Volume roll works for GC (bimonthly) and BTC (monthly) | add a per-symbol roll rule |
| B8 | Guban's `choose_contracts` and the pipeline's rule give the same NQ roll dates | store uses the pipeline's rule; Guban re-checks parity |
| B9 | The store fits on D: | measure one month per symbol first |
| B10 | `guban run` takes explicit cost flags, so defaults can sit behind them | adjust the CLI change |

### Build steps (Phase 14 = 1–3, Phase 15 = 4–7; pipeline steps are SD1–SD3 in its own PLAN.md)
1. **Verify B1, B2, B4, B6, B8, B9, B10.** Read `src/guban/data/ticks.py`, `resample.py`, `cli.py` (cost flags),
   `research/days.py`; pipeline `src/data/{deepcharts,minutes,rolls,ticks,sessions}.py`, `config/costs.yaml`. Decode one
   GC and one BTC minute + tick file (price grid, session window); diff both NQ roll calendars; measure one month of
   store output per symbol and free space on D:; check CME spec/fee pages. Findings appended here.
2. **Store builder core**, tests first (synthetic bytes): tick bars, minute fallback, 2% rule, skip in-progress/cut-off,
   write-once, changed-source report, read-only source. `src/guban/store/`, `config/store.toml`, `guban store build|verify`
   (shared CLI option objects), `D:\Market Data\README.md`.
3. **Build the store:** NQ first (compare with today's `data/NQ/1m.parquet` on minute-sourced sessions), then MNQ, ES,
   GC, BTC; review each `quality.csv` and GC/BTC `roll_calendar.csv`. Commit Phase 14.
4. **Guban import from the store**, GC/BTC specs + cost defaults for all five, `rth_comex` session, bump `CACHE_VERSION`.
5. **Calendars:** equity 2019–2022, `cme_metals`, `cme_crypto`; link GC/BTC in `contracts.toml`.
6. **Research ticks from the store:** `guban/data/ticks.py` + VAP cache; `research/poc_shift.py` matches Phase 13 or the
   differences are listed.
7. **Regression + docs:** Vault Break MC compare (Phase 5 command) passes S3; if not, add `guban import --bars minute`
   for parity runs and rerun; `guban run` on all five; update CLAUDE.md, USE.md, README; commit Phase 15.

### Step 1 findings (2026-10-06)
- **B1 holds.** Every GC price in the sampled files sits on the 0.10 grid (not 0.20), and every BTC price on the 5 grid
  (not 10), in both minute and tick files. CME specs: GC is 100 oz at $0.10 = $10 per tick → $100/pt; BTC is 5 BTC at
  $5 = $25 per tick → $5/pt ([CME gold fact card](https://www.cmegroup.com/trading/metals/files/fact-card-gold-futures-options.pdf),
  [StoneX BTC specs](https://futures.stonex.com/blog/understanding-cme-bitcoin-and-micro-bitcoin-futures-contract-specifications)).
- **B2 holds for GC (18:00 → 16:59 ET) and fails partly for BTC.** CME runs crypto futures 24/7 since 2026-05-29
  ([CME press release](https://www.cmegroup.com/media-room/press-releases/2026/6/01/cme_group_announceslaunchof247cryptocurrencyfuturesandoptionstra.html)).
  From then on, BTC files exist for weekend dates and carry bars up to 17:59 ET (116 files, 0.24% of volume). The
  decoder needs no change: one file is still one 18:00 → 18:00 day. → The store's "session ended" time is
  **18:00 ET the next day** (not 17:00), plus 10 min settle. An ambiguous fall-back hour (first on 2026-11-01) is
  resolved from record order.
- **B4:** GC settles at 13:30 ET (CME), so `rth_comex` 08:20–13:30 stands. GC early closes in the data end at 14:29 ET.
- **B6 holds:** the non-member exchange fee for BTC is $7.50 per contract (IBKR's CME fee schedule).
- **B8: the two roll rules agree on every NQ, MNQ and GC roll.** They differ on 1 ES roll and 27 BTC rolls. In every one of
  those, the old contract has **no file** in the session where Guban switches. Guban moves to the next contract in that
  same session; the pipeline's `roll_decisions` keeps the empty old contract as front for one more session, so that
  session is lost (66 times for BTC, once for ES). → The store uses **Guban's rule** (= the pipeline's rule, plus a
  same-session switch when the front contract has no file); `roll_calendar.csv` marks those rolls
  `reason = front has no file`. The pipeline's NQ calendar is unchanged.
- **B9 holds:** 646 GB free on D:. One month in Parquet (zstd) is 9–16% of the raw tick bytes → about 10 GB of ticks
  for the five symbols (raw: NQ 21.9, MNQ 22.8, ES 41.8, GC 6.3, BTC 0.3 GB).
- **B10 holds with a change:** `--commission/--slippage` default to 0.0. Phase 15 makes the default None, which means
  "use the contract's default costs".
- **B3 refined: the 2% total alone is not enough.** Tick-built bars vs minute records on sample sessions: before
  2025-11, volume matches on 100% of minutes and OHLC on all but the 18:00 opening minute (the minute file holds
  opening records that are not in the ticks). From 2025-12, NQ/ES/MNQ/BTC match volume on 99.7–99.9% of minutes
  (session totals within 0.01%). GC ticks from 2025-12 miss ~2% of trades spread over most minutes: only ~40% of
  minutes match, yet some sessions are within 2% overall. Tick timing is aligned: shifting it by ±100 ms makes the
  match worse. → A session is tick-built only when its total is within 2% **and** ≥ 99% of its minutes match exactly
  (`config/store.toml` `min_minute_match`). **SR3 restated:** in tick-built sessions, volume matches on ≥ 99% of
  minutes and OHLC on ≥ 99.9% (it said "every minute", which the 18:00 open and the 2025-12+ feed rule out).
- **Source facts:** the DeepCharts folder now also holds CL, SI and YM (out of scope). **MNQ files stop at session
  2026-09-16.** **ES 202609 files stop at session 2026-08-16**, so ES has only the thin back month until the roll.
  `quality.csv` flags such front sessions `thin`.
- **Side encoding:** both projects already read minute field 6 as the sell side (`bid_vol`).

Not in scope: deleting `Database/` (42 GB) or the pipeline's frozen snapshot (21 GB), which is the user's call later;
MES/MGC/MBT (not in the source); expiry roll for GC/BTC; new theses on ES/GC/BTC. Rollback: sources untouched; revert
the commits and delete `D:\Market Data\`.

## Phase 14 results (2026-10-06)
- **Built:** `guban store build` (`src/guban/store/{bars,build}.py`, `config/store.toml`, CLI `guban store build|verify`
  with shared `--symbol/--root/--source` options) wrote `D:\Market Data\`: 9.84 GB, 14,616 files, ~20 min for all
  five symbols on the process pool (the build keeps Windows awake; a sleeping PC had stretched one run to 3 h).

  | Symbol | Contract sessions | Tick-built | Minute-built | Rolls | Store | SR3 volume / OHLC |
  |---|---|---|---|---|---|---|
  | NQ | 2,689 | 871 | 1,818 | 31 | 2.5 GB | 99.92% / 99.98% PASS |
  | MNQ | 432 | 222 | 210 | 6 | 2.2 GB | 99.70% / 99.94% PASS |
  | ES | 2,320 | 891 | 1,429 | 27 | 3.6 GB | 99.91% / 99.98% PASS |
  | GC | 2,278 | 776 | 1,502 | 29 | 0.9 GB | 99.29% / 100.00% PASS |
  | BTC | 2,429 | 952 | 1,477 | 81 | 0.1 GB | 99.47% / 99.99% PASS |

- **SR1:** only finished, whole-record sessions are written. One is skipped and listed on every build: NQ 202612
  session 20261005, whose tick file is cut mid-record and has not grown since the session ended.
- **SR4:** a second build wrote 0 files; a size+mtime snapshot of all 14,616 files was identical before and after.
  Changed sources are reported (`CHANGED SOURCE ...`), never applied, unless `--rebuild SYM:YYYYMMDD`.
- **SR5:** the builder reads the source only with `np.fromfile` / `open("rb")`; `test_source_is_never_written` builds
  from read-only source files and checks their sha256 and mtime afterwards.
- **Two bar rules amended, from evidence (both reproduce DeepCharts' own minute bars):**
  1. *Opening minute.* DeepCharts writes a session's first minute as four records: the open (volume 0), the high,
     the low and the close. Dropping every zero-volume record therefore lost the true open, which equals the first tick.
     Rule now: drop a minute only when all its records hold no volume; otherwise merge all of them. With this,
     minute-built NQ sessions are **100.000% identical** to today's `data/NQ/1m.parquet` on all 454,222 compared bars.
     **The pipeline's `merge_minute_records` has the same defect** (pipeline step SD1 should adopt this rule).
  2. *Size-0 ticks* (2026-05-26+ feed). Their prices are in DeepCharts' minute OHLC, but their size is not in its
     volume. Rule now: O/H/L/C from every record, volume from `size`. MNQ's OHLC match went from 99.86% (FAIL) to 99.94%.
- **Roll volume:** a session whose minute file holds no volume counts its tick volume. GC and BTC have full tick
  files but empty minute files for 2026-08-18..24; without this they had no front contract.
- **Reviewed:** GC rolls through its active months (Feb/Apr/Jun/Aug/Dec) at the end of the month before delivery, and
  BTC rolls monthly near expiry (B7 holds). 66 BTC rolls have `reason = front has no file` (DeepCharts stops
  recording the old contract a few days early). `thin` flags holiday sessions, BTC's new weekend sessions, and **the ES hole
  2026-08-17 → ~09-01** (202609 files missing; only 1–3k contracts of 202612 per session). Phase 15 must decide how
  `guban import` treats thin front sessions.
- **For Phase 15 (SR6):** minute-built sessions already equal the old import. Tick-built ones differ from it on 0.3% of
  bars, by design. `--bars minute` (step 7) restores the old import's bars wherever a parity run needs them.
- Tests: `tests/test_store.py` (23). Full suite: 320 passed.

### Phase 14 addendum: 1-minute bars by default (2026-10-07)
- **User rule:** data comes from `D:\Deepchart\Database`; strategies use 1-minute bars; tick data only when a strategy
  needs it. → `config/store.toml` `bars = "minute"`: the store builds bars from DeepCharts' minute records whenever
  they hold volume, and from ticks only when they hold none. Ticks are still stored; `quality.csv` gained
  `tick_ok` (passes the 2% / 99%-of-minutes check), which tick consumers must filter on. The Phase 14 tick rule
  stays available as `bars = "tick"`.
- **DeepCharts reinstall (2026-10-07):** after it settled, the folder held NQ, ES, MNQ, **MES** (new) and BTC, all with
  minute data from 2020-01-01, plus NQ ticks from 2024-01-01. Gone: GC, ES ticks, MNQ ticks, NQ ticks before 2024,
  NQ minutes before 2020. Unchanged minute files were byte-identical to the store's sources (ES 3,180/3,182,
  NQ 3,066/3,200, BTC 3,095/3,254). The changed files mostly completed short sessions (NQ 2022-12-27, Memorial
  Day 2025, BTC expiry days). **The ES gap is filled:** 202609 is front at 0.8–1.6M contracts per session until the
  2026-09-14 roll.
- **Builder:** `--rebuild SYM:changed|all` (besides SYM:YYYYMMDD) rebuilds from the current source and keeps every stored
  file whose source is gone, together with its tick check. A symbol whose folder is missing is reported, not an error.
  A source with the same bytes but a new mtime is not a change: its new mtime is recorded once. `verify` now also
  requires every minute-built session to equal its source minute records, and skips sessions whose source is gone.
- **Rebuilt** NQ, ES, MNQ and BTC with `--rebuild SYM:all`, and added MES: every front session of these five symbols
  is built from minute records, and `verify` passes all six. A second build wrote 0 files. **GC** keeps its Phase 14 bars
  (511 front sessions tick-built), because its minute files no longer exist. Sessions whose sources are gone
  (NQ 2018-12..2019, the old NQ/MNQ/ES ticks) stay in the store as built.
- Known source hole: trade date 2020-11-30 holds ~1–2% of normal volume on NQ and ES.
- Tests: `tests/test_store.py` (27). Full suite: 324 passed.

## Phase 15 plan (revised 2026-10-07, approved)
Replaces build steps 4–7 above. Interview: Q1 read access (yes), Q2 plan approval (yes).
- **Symbols:** NQ, MNQ, ES, MES, BTC (user, commit 0ab8b59). GC is out: no GC spec, `cme_metals` or `rth_comex`.
- **Recon (2026-10-07):**
  - Store front sessions: NQ 2018-12-30 →, MNQ/MES/BTC 2020-01-01 →, ES 2019-12-29 → 2026-10-06; every front session
    minute-built. NQ has 691 `tick_ok` front sessions; MES has no ticks.
  - Store vs today's imports: NQ 99.56% of bars identical, MNQ 99.42%; every difference is in 2026-05..09, where
    DeepCharts rewrote sessions at its reinstall (MNQ: mostly volume, a few prices up to 52 points). The store is the truth.
  - **Vault Break on store data passes S3 twice** (scratch import + run): the Phase 5 recipe with data cut at the old
    starts (NQ 2022-12-28, MNQ 2025-06-08): 308 MC trades, 99.4% matched, 97.7% within 1 tick, net −0.17%. The
    **literal port** (no ATR seed, no `--history-from`, MNQ from 2021-01-03): **1,081 MC trades 2021-01-20 → 2026-09-22,
    99.2% matched, 97.8% within 1 tick, net +0.03%** — this closes Phase 5's open item. `--bars minute` is not needed.
  - MES cost: Topstep $1.22 RT ($0.50 commission + $0.70 exchange + $0.02 NFA; help.topstep.com, 2026-10-07).
- **Default costs** (USD per contract per round trip; slippage = 1 tick, charged half per market/stop fill):
  NQ 2.50 / 5.00, MNQ 2.00 / 0.50, ES 3.78 / 12.50, MES 1.22 / 1.25, BTC 16.04 / 25.00.
- **Decisions:** volume roll = quality.csv `front`; expiry roll = third-Friday rule on store minute volumes (equity only,
  BTC errors); thin front sessions imported as-is (summary counts them); old binary import kept behind `--source`;
  `--data-from` (shared run/optimize/wfo option) drops bars before a date so warm-up starts there; BTC gets `h24`
  (18:00 → 18:00); `CACHE_VERSION` unchanged (the fingerprint already sees a new 1m.parquet); `data/NQ`, `data/MNQ`
  backed up to `data/_pre15/` before re-import.
- **Steps:**
  1. Import from the store (tests first; all five imported; NQ/MNQ comparison shows only 2026-05+ differences).
  2. Per-contract default costs (`--commission/--slippage` default None) and `--data-from`.
  3. BTC spec (5 $/pt, tick 5, `cme_crypto`) and `h24` session.
  4. Calendars: `cme_equity` 2019–2022, `cme_crypto` 2020–2026, from each front session's last bar, spot-checked vs CME.
  5. Research ticks from the store (`tick_ok` only); `poc_shift.py` vs Phase 13, differences listed.
  6. Both Vault Break gates, `guban run` on all five, docs (CLAUDE.md, USE.md, README, run-backtest skill), commit.
- **Assumptions:** A1 MC's chart starts with the 2021-01-03 session; A2 thin sessions are real data; A3 `resample`
  handles a 1,440-minute session; A4 early closes in the data are the scheduled ones; A5 store ticks reproduce Phase 13.

## Phase 15 results (2026-10-07)
- **Import from the store (SR6):** `guban import --symbol SYM [--roll volume|expiry] [--root] [--source]`
  (`src/guban/store/read.py`: `import_from_store`, `read_minute`, `front_sessions`). Bars = the store's minute files,
  ts_utc + 1 min in New York (close label); volume roll = quality.csv `front`; expiry roll = third-Friday rule on the
  store's minute volumes (equity only; BTC errors). The DeepCharts-folder import stays behind `--source`. ~15 s per symbol.

  | Symbol | Bars | First bar | Roll | Thin front sessions |
  |---|---|---|---|---|
  | NQ | 2,740,357 | 2018-12-30 18:01 | expiry | 48 |
  | MNQ | 2,389,143 | 2020-01-01 18:01 | expiry | 29 |
  | ES | 2,392,190 | 2019-12-29 18:01 | expiry | 49 |
  | MES | 2,388,074 | 2020-01-01 18:01 | expiry | 45 |
  | BTC | 1,588,012 | 2020-01-01 18:05 | volume | 102 |

  - **vs the old imports** (backed up in `data/_pre15/`): the same contract on 100% of shared bars. NQ 99.46% and MNQ
    99.40% of bars identical (all seven fields); every difference is in 2026-05..09, where DeepCharts rewrote sessions at
    its 2026-10-07 reinstall (MNQ: mostly volume, a few prices up to 52 points). NQ gains 360 bars in 2025-05 (the
    Memorial Day 2025 session the reinstall completed). The store is the truth.
  - **Bug found and fixed: BTC expiration sessions.** The store's volume roll still named the expiring contract front on
    38 of 82 BTC expiry days (it stops trading at 11:00 ET; the roll is decided on the previous session's volume), so
    those days ended at 11:00. The import now never trades a contract in its expiration session (`last_trade_date`:
    3rd Friday for equity, last Friday of the month for BTC, the business day before when closed); the next contract
    takes it (+24,738 BTC bars). Equity volume rolls never reach expiry: no change. The store's roll calendar is untouched.
  - Thin front sessions are imported as they are (holiday sessions, BTC weekends); the summary lists them (A2 holds).
- **Default costs (SR8):** `ContractSpec.commission/slippage` (`config/contracts.toml`): NQ 2.50/5.00, MNQ 2.00/0.50,
  ES 3.78/12.50, MES 1.22/1.25 (Topstep), BTC 16.04/25.00. `--commission/--slippage` default to None; `cli._costs`
  fills each missing one from the `--contract` spec; `metrics.json` `run.costs` names the source.
- **`--data-from YYYY-MM-DD`:** shared `run/optimize/wfo` option (`DataSpec.data_from`), drops bars and sub-bars before
  the date in the chart time zone (after any `--history-from` splice).
- **BTC spec + `h24` session (SR7):** BTC $5/pt, tick 5, calendar `cme_crypto`. `h24` = 18:00 → 18:00 (A3 holds:
  `resample` handles the 1,440-minute day); it keeps the 17:00–18:00 hour BTC trades since 2026-05-29 (+414 5m bars).
- **Calendars (SR9):** `cme_equity` 2019–2022 added (33 dates) and `cme_crypto` 2020–2026 (24 dates), each from the
  last bar of every short front session, checked against QuantConnect Lean's open-source market-hours database
  (its NQ entries equal every derived equity date 2019–2026) and CME's 2021 Good Friday advisory. From 2022 BTC trades
  through most US holidays; its own short days: 13:45 ET day after Thanksgiving / Christmas Eve, 11:15 Good Friday,
  14:45 day after Thanksgiving from 2024 (2024-11-29 is missing in Lean; the data trades to 14:45). Lean lists
  2026-06-19 and 2026-07-03 13:00 for BTC, but the data trades through both (24/7 since 2026-05-29): left out.
  Every short front session of all five symbols is now explained, except 2026-05-14 (all symbols end 14:22 ET: a source
  outage, not a halt) and one thin BTC weekend session (2026-07-26). A4 holds.
- **Research ticks from the store (step 6):** `guban/data/ticks.py` reads a store symbol folder (recognized by its
  quality.csv; Parquet ticks in UTC → the binary reader's fields) or a DeepCharts folder; tick cache `CACHE_VERSION` 2
  (paths in the fingerprint). `research.days.trading_days` takes the contract from the store's roll and adds `tick_ok`;
  `poc_shift.py` defaults to `D:\Market Data\NQ` and drops days whose store `tick_ok` is not True.
  - **Same verdict, NOT REAL on both sides** (A5 holds). Up: gap −0.98 pp, p = 0.67 (Phase 13: −1.58 pp, p = 0.74);
    down: −1.26 pp, p = 0.69 (Phase 13: −2.21 pp, p = 0.78). 688 clean tick days (Phase 13: 678), through 2026-10-07.
  - Differences on the 964 Phase 13 days: 18 days are clean only now (14 whose old tick files stopped at 09:29:59 are
    complete in the store; 3 volume mismatches; 1 cut short), 17 only then (store `tick_ok` False: 2024-02-01,
    2024-02-13, 2024-06-05 and 14 sessions in 2026-05..09). Tick POCs equal on 98.9% of days clean in both; the 7
    that differ are all 2026-05+ (rewritten sessions).
- **Vault Break MC parity (SR11), both gates PASS:**
  - **Full span, literal port** (new reference; `--data-from 2021-01-03`, no seed, no `--history-from`): 1,081 MC
    trades 2021-01-20 → 2026-09-22, 1,072 matched (99.2%), 97.8% within 1 tick, net 49,017.50 vs 49,003.20 (+0.03%).
    This closes Phase 5's open item: the fitted ATR seed is retired.
  - **Phase 5 recipe** (fitted seed, NQ cut at 2022-12-28 and MNQ at 2025-06-08, window from 2025-06-08): 308 MC trades,
    306 matched (99.4%), 97.7% within 1 tick, net −0.17%. `--bars minute` was not needed.
- **`guban run` on all five** (Vault Break defaults, 5m RTH, no cost flags; a smoke test, not a tuned result): every run
  writes trades.csv / metrics.json / report.html with the contract's default costs, 5–7 s each; BTC also on `h24`.
- USE.md examples rerun on the new data with the parity flags: optimize best noise_mult 0.5 / vwap_exit_bars 4, net
  56,341.80 (28 runs, 41 s); wfo 19 windows, 1,067 OOS trades, net 43,531.10, WFE 77%.
- Docs: CLAUDE.md, USE.md, README.md, `.claude/skills/run-backtest/SKILL.md` (seed flags removed). Tests: new
  `tests/test_store_read.py` (9) + cost/`--data-from`/`h24`/calendar/store-tick/store-days tests. Full suite: 342 passed.
