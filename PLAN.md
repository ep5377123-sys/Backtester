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
- **Porting the other ~29 strategies** — done later, one at a time, using the same parity workflow.
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
- Which of the other ~29 strategies to port next — proceed with none until Vault Break passes parity.
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
- [ ] Phase 7: Parameter optimizer
      Done when: the 2×2 grid equivalence test passes, and a Vault Break grid produces a ranked table plus a heatmap.
      Steps: grid spec parser; ProcessPoolExecutor, with data loaded once per worker; ranking and min-trades filter; heatmap; guard for grids over 5,000 combinations.
      Covers: R8, S4
- [ ] Phase 8: Walk-forward analysis
      Done when: the IS/OOS no-overlap test passes, and the Vault Break WFO report shows stitched OOS equity and walk-forward efficiency.
      Steps: window generator (rolling and anchored); per-window optimize → OOS run; stitch the trades; WFO report section.
      Covers: R9, S5; checks: A9
- [ ] Phase 9: Prop-firm Monte Carlo
      Done when: with the same trades, seed, and inputs, the pass rate is within ±1 pp of the notebook's.
      Steps: port notebook cells 4–9 to `guban/propfirm/`; move the rules into `config/propfirm/apex_50k_eod.toml`; add a size sweep and degradation scenarios; output HTML charts; flag the notebook's $25k-vs-50K discrepancy to the user.
      Covers: R10, S6; checks: A10
- [ ] Phase 10: CLI polish and README
      Done when: `uv run guban --help` lists all six commands; the README walks through import → run → compare → optimize → wfo → propfirm; the full `pytest` suite is green.
      Steps: consistent flags and run IDs; error messages for the failure cases above; README with the MC export instructions; update `CLAUDE.md`.
      Covers: all

---
**Defaulted decisions recap** — A1–A10 above are every choice made on your behalf; any of them can be vetoed cheaply.

## Phase 1 results (2026-09-28)
- **A1 replaced.** The user supplied a binary per-contract store, not an ASCII export. Layout: `Database/<SYM>-CME/<YYYYMM>/{Minute,Tick}/<YYYYMMDD>.data`, one Globex session per file. Minute record = 60 bytes: int64 .NET ticks (ET wall-clock, bar open time), f8 high/low/open/close, i4 bid_vol/ask_vol/volume/volume/unused (verified: decoded files, field order inferred statistically: field0 is always the max, field1 always the min, close[t]==open[t+1] continuity).
- **A5 verified.** Timestamps are Eastern time. The volume spikes land on the 09:31 and 16:00 bars (close-time labels) in both winter and summer.
- **A6 falsified → adapted.** Data is per-contract. The importer builds an unadjusted continuous series, picking the highest-volume contract each session and never rolling back. There were 16 rolls, each on the Sunday before expiry week. Back-adjustment is a later option.
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
  uv run guban run strategies/vault_break.py --symbol MNQ --history-from NQ --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620 --out reports/vault_break_mnq
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
    - Max intraday peak-to-valley drawdown: bar-close equity peak before the bar vs. the bar's `equity_low`.
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
