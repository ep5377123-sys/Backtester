# USE.md — how to run each guban command

Operating guide for Claude. Run everything from the repo root in PowerShell with `uv run guban ...`.
Background and the reasons behind each setting: PLAN.md (Phase 5 = parity setup, Phase 7–9 = analysis commands).
`uv run guban <command> --help` is the source of truth for flags; this file says which flags to use and why.

## Before you start
- Data already imported from the shared market-data store `D:\Market Data` (check with Glob `data/*/*.parquet`, not by
  reading files), all through 2026-10-07:
  - `data/NQ/1m.parquet` — NQ from 2018-12-30; `data/ES/1m.parquet` — ES from 2019-12-29.
  - `data/MNQ/1m.parquet`, `data/MES/1m.parquet`, `data/BTC/1m.parquet` — from 2020-01-01.
- Strategies: Glob `strategies/*.py` (e.g. `vault_break.py`, class `VaultBreak`; `ivb.py`).
- Exit codes: 0 done, 1 `compare` FAIL, 2 error (bad input, missing data, strategy exception). `GUBAN_DEBUG=1` shows full tracebacks.
- All outputs go to `reports/` (git-ignored). `--out` is always a folder.

## The MC-parity flag set (use by default)
The MultiCharts charts run in exchange time on the 24-hour Globex session, so any run meant to line up with MC needs:

```
--tf 5m --session eth --tz America/Chicago
```

Vault Break's DailyATR has long memory (every day since the chart's first bar), so a parity run must start where MC's
chart starts: `--data-from 2021-01-03`. The fitted warm-up seed (`atr_seed_*`, `sess_count_seed`) is retired: it stood
in for history the old import lacked, and on the store's longer history it would count those days twice (PLAN.md Phase 15).

Default fill mode is `magnifier` (1-minute sub-bars). Use `--fill-mode ohlc` only to compare.

## 1. import — build `data/<SYM>/1m.parquet` from the market-data store (only when the store changes)
```
uv run guban import --symbol NQ  --roll expiry
uv run guban import --symbol MNQ --roll expiry
uv run guban import --symbol ES  --roll expiry
uv run guban import --symbol MES --roll expiry
uv run guban import --symbol BTC
```
- Reads `D:\Market Data` (config/store.toml `root`; `--root` overrides). Each import takes ~15 s.
- `--roll expiry` matches MC's continuous contract, which rolls on the Wednesday of expiration week. Always use it for the
  equity index symbols (MC parity). BTC has no expiry rule: it uses the store's volume roll, and a contract is never
  traded in its expiration session (the next contract takes it).
- The summary lists rolls, weekday sessions with no data and the store's thin front sessions (holidays, BTC weekends).
- `--source Database/NQ-CME` still reads a DeepCharts folder directly (the old import), for comparisons only.
- The data comes from DeepCharts (dxFeed), so prices can differ slightly from MC's feed. Updating the store itself:
  `guban store build` (CLAUDE.md "Shared market-data store").

## 2. bars — build or inspect resampled bars (optional; `run` builds them itself)
```
uv run guban bars --symbol NQ --tf 5m --session eth
```
`--rebuild` ignores the cache.

## 3. run — a single backtest
**Vault Break on NQ** (run 2026-10-07; 1 NQ with a 50-pt target and stop, same point distances as the MC run):
```
uv run guban run strategies/vault_break.py --symbol NQ --tf 5m --session eth --tz America/Chicago --commission 2.25 --slippage 5 --set take_profit_usd=1000 --set stop_loss_usd=1000 --set num_contracts=1 --set earliest_entry=945 --set latest_entry=1500 --set flatten_time=1550 --set flatten_early_close=false --out reports/vault_break_nq
```
Result: 548,414 bars (2018-12-30 →), 1,625 trades, net 114,983.75, PF 1.18, max drawdown −18,421.25, 1 ambiguous fill, ~10 s.
Costs are the prop-firm notebook's NQ rates ($2.25 commission + $5 slippage per contract per round trip).

**Vault Break MNQ, the MC parity run** (5 MNQ, $500 target and stop = 50 pts, MC's recovered costs, MC's chart start):
```
uv run guban run strategies/vault_break.py --symbol MNQ --data-from 2021-01-03 --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set earliest_entry=945 --set latest_entry=1500 --set flatten_time=1550 --set flatten_early_close=false --out reports/vault_break_mnq
```
Expected: 408,201 bars, 1,094 trades, net 45,579.80, PF 1.20, max drawdown −7,315.00, 2 ambiguous fills; ~23 s with trade images (report ~16 s), ~9 s with `--no-trade-charts`.

**Bubble ORB on NQ** (15-min ORB; a 1-min breakout candle with a big bubble of its own side in its body; defaults since
2026-10-23: longs and shorts, bubble > 60 lots — `--set allow_shorts=false --set min_bubble=40` is the first, long-only run):
```
uv run guban run strategies/orb_bubbles.py --symbol NQ --tf 1m --session eth --tz America/New_York --data-from 2024-02-01 --commission 2.25 --slippage 5 --out reports/orb_bubbles_nq --open
```
Result (long + short, > 60 lots): 949,467 bars, 661 of 694 days with usable ticks, 581 trades, net 1,515.25, PF 1.01,
max drawdown −26,522.50; ~30 s (the first run builds `data/NQ/big_trades.parquet` from the tick store, ~13 min).
Long-only, > 40 lots: 367 trades, net 3,576.75, PF 1.04, max drawdown −17,908.00. Needs `--tf 1m`; NQ ticks start 2024
(2024-01 is defective), hence `--data-from 2024-02-01`.

Notes:
- **Reward:risk per trade:** every trade records the stop and target in force from its entry fill (`set_stop_loss` / `set_profit_target`, or the strategy's own stop / limit exit orders; the tightest stop and nearest target win; later moves don't count). `trades.csv` ends with `risk_usd`, `reward_usd`, `planned_rr` (target ÷ stop, e.g. 20 pt stop + 40 pt target = 2.00) and `r_multiple` (net ÷ risk, so costs make a full stop-out slightly worse than −1R); empty for a trade without a stop. The report's trade list shows R and RR, and the summary has the average R, average planned RR and the count of trades with a stop. MNQ parity run: every trade risks $500 for $500 (RR 1.00), average R +0.083; IVB (`strategies/ivb.py --symbol MNQ --tf 1m --session eth --tz America/Chicago`, default inputs): RR 1.00 on all 1,065 trades, average R +0.067.
- Outputs: `trades.csv` (MC trade-export layout + engine columns), `metrics.json`, `report.html`, and one price-chart image per trade in `trades/trade_<no>.png` (the report's trade viewer loads them; ~29 MB for the MNQ run). Add `--open` to open the report, `--no-report` to skip it, `--no-trade-charts` to skip the images (draw them later with `guban chart`).
- **Trade Monte Carlo (on by default):** every run also simulates its own trades three ways, side by side in the report's "Monte Carlo" section and under `monte_carlo` in `metrics.json`:
  - resample: n trades drawn with replacement (net profit and drawdown vary; gives P(net ≤ 0));
  - reorder: the same trades shuffled (net fixed; only the drawdown varies);
  - whole days: the trading days (exit session) drawn with replacement, each day's trades kept together, so losing streaks within a day stay in the simulation.
  - Also: EV per trade in USD and R with a 95% bootstrap CI and P(EV ≤ 0) (all, long, short), and net / drawdown-depth percentiles after the first 10/20/40/100 trades or days.
  - Drawdowns are close-to-close, so compare them with the backtest's close-to-close max drawdown (printed on the `monte carlo` line), not the intraday one.
  - Flags: `--mc-sims` (default 10000; `0` turns it off), `--mc-seed` (default 42), `--mc-dd-limit USD` (adds "chance the max drawdown reaches $X", e.g. a prop-firm limit), `--mc-risk USD` (override: risk per contract for R; default each trade's own stop, and the average losing trade per contract only when no trade has one), `--mc-oos-from YYYY-MM-DD` (also splits the EV into trades exiting before / from that date).
  - MNQ parity run: resample net P5 20,195.38 / P50 45,937.95 / P95 71,327.57, P(net ≤ 0) 0.1%; reorder max DD P5 −12,813.70 vs backtest −7,081.10; whole days (476) max DD P5 −14,880.40; EV 41.66 per trade (CI 14.02 to 69.49), +0.083 R (per-trade stop, $100 per contract); with `--mc-oos-from 2025-01-01` EV 54.12 in sample, 23.01 out of sample (P(EV ≤ 0) 15.8%); ~2 s.
  - This is not `propfirm` (which simulates a prop-firm evaluation's rules).
- **Redraw trade images:** `uv run guban chart reports/vault_break_mnq --trade 560` draws `trades/trade_560.png` again (repeat `--trade`; without it every trade, ~14 s for the MNQ run). It reloads the run's bars from its `metrics.json` (or `wfo.json`) settings; if the data changed other than by new sessions at the end, it refuses: run the backtest again. Use it after a `--no-trade-charts` run: the report shows the command when an image is missing.
- `--set name=value` overrides a strategy input (repeatable). Unknown names are rejected.
- `--data-from YYYY-MM-DD` drops bars before that date (chart time zone), so the strategy's warm-up starts there.
- `--history-from NQ` prepends NQ bars before MNQ's first bar. MNQ now has its own history from 2020, so it is rarely needed.
- **Default costs:** without `--commission` / `--slippage`, a run uses the contract's defaults from `config/contracts.toml`
  (commission + 1 tick slippage per round trip: NQ 2.50 + 5.00, MNQ 2.00 + 0.50, ES 3.78 + 12.50, MES 1.22 + 1.25,
  BTC 16.04 + 25.00). A flag you give wins; `metrics.json` `run.costs` says where the costs came from.
- **BTC** trades 24/7 since 2026-05-29: `--session h24` (18:00 → 18:00) keeps the 17:00–18:00 hour that `eth` drops.
- `--contract` sets the P&L spec when it differs from the data symbol (for example NQ data priced as MNQ). The default folder name becomes `NQ-MNQ`.
- Costs are USD per contract per round trip. Slippage is charged half per market or stop fill and not at all on limit fills (MC behavior).

## 4. compare — parity check against an MC export
```
uv run guban compare --ours reports/vault_break_mnq/trades.csv --mc "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --contract MNQ
```
- Expected (2026-10-07): S3 PASS on the full span 2021-01-20 → 2026-09-22: 1,072 of 1,081 MC trades matched (99.2%),
  97.8% within 1 tick, net +0.03%. Writes `diff.html` next to `--ours` (or `--out`); `--open` opens it.
- `--start` / `--end` limit the check to a window. Price checks need MNQ data: NQ prices are within 1 tick of MC's MNQ
  fills only ~86% of the time.
- Pass gate (CLAUDE.md): ≥95% trades matched, prices within 1 tick, net within 3%.
- The MC exports in `D:\Multi Charts\...` are read-only. Never write there.

## 5. optimize — grid search
```
uv run guban optimize strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" --symbol MNQ --data-from 2021-01-03 --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5
```
- Grid forms: `name=start:stop[:step]` (inclusive), `name=a,b,c`, `name=v`. Several specs per `--grid`, space-separated.
- `--metric` (default `net_profit`), `--min-trades` (default 30), `--workers`, `--x/--y` heatmap axes, `--top`.
- More than 5,000 combinations needs `--yes`. Ask the user before running a grid that large.
- Expected (MNQ grid above): 28 runs in ~40 s, best 0.5 / 4 → net 56,341.80.
- Writes `reports/<strategy>_<symbol>_<tf>_<session>_opt/results.csv`, `optimize.json`, `optimize.html`.
- For NQ, use the NQ `run` flags above in place of the MNQ ones.

## 6. wfo — walk-forward
```
uv run guban wfo strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" --symbol MNQ --data-from 2021-01-03 --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5
```
- Window flags: `--is 12 --oos 3 --step 3` (months), `--rolling` (default) or `--anchored`, `--start YYYY-MM-DD`.
- 70/30 rolling over 5 years (Phase 17), with the MC-parity inputs: `uv run guban wfo strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" --symbol MNQ --data-from 2021-01-03 --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set earliest_entry=945 --set latest_entry=1500 --set flatten_early_close=false --is 14 --oos 6 --step 6 --start 2021-10-01 --out reports/vault_break_mnq_wfo_70_30` → 8 windows (last partial), 896 OOS trades, net 24,603.00, PF 1.13, average R +0.055, WFE 50%; ~25 min on 6 workers.
- Expected: 19 windows, 1,067 OOS trades, net 43,531.10, WFE 77%; 998 s on 6 workers (measured alongside the test suite). Run it with `run_in_background` or a long timeout.
- Writes `reports/<...>_wfo/trades.csv` (stitched OOS), `windows.csv`, `results.csv`, `wfo.json`, `wfo.html`, and `trades/trade_<no>.png` per OOS trade (`--no-trade-charts` skips them).

## 7. propfirm — prop-firm evaluation Monte Carlo
```
uv run guban propfirm --trades reports/vault_break_nq/trades.csv --rules apex_50k_eod --commission 0 --slippage 0
```
- `--trades` takes a guban `trades.csv` or an MC export (.csv / .xlsx).
- **Costs pitfall:** propfirm deducts its own costs on top of the file's profit, and that profit is already net. The notebook's defaults are $2.25 + $5 per contract. For a guban `trades.csv` that already has costs in it, pass `--commission 0 --slippage 0` to avoid counting costs twice. Keep the defaults only when reproducing the notebook.
- Other flags: `--size`, `--sims`, `--sweep-sims`, `--sweep 0.5,1,1.5` (empty string: no sweep), `--seed`, `--risk`.
- Rules live in `config/propfirm/apex_50k_eod.toml`. It is labeled 50K but uses a $25k account, a $1,250 target and a $1,000 drawdown, as the notebook does (PLAN.md Phase 9).
- Reproduce the notebook: `uv run guban propfirm --trades "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --rules apex_50k_eod` → 35.8% pass rate.
- Writes `reports/<trades>_<rules>/propfirm.json`, `sweep.csv`, `degradation.csv`, `daily.csv`, `propfirm.html`.

## Reporting results to the user
Read the command's stdout summary (trades, net profit, PF, max drawdown). For more figures, read the run's `metrics.json`; it is a file this session created, so re-reading it is allowed.
Link the HTML report (`reports/<run>/report.html`) and offer `--open`.
