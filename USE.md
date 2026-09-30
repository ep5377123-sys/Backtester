# USE.md — how to run each guban command

Operating guide for Claude. Run everything from the repo root in PowerShell with `uv run guban ...`.
Background and the reasons behind each setting: PLAN.md (Phase 5 = parity setup, Phase 7–9 = analysis commands).
`uv run guban <command> --help` is the source of truth for flags; this file says which flags to use and why.

## Before you start
- Data already imported (check with Glob `data/*/*.parquet`, not by reading files):
  - `data/NQ/1m.parquet` — NQ 2022-12-28 → 2026-09-24.
  - `data/MNQ/1m.parquet` — MNQ 2025-06-08 → 2026-09-17 only.
- Only one strategy exists: `strategies/vault_break.py` (class `VaultBreak`).
- Exit codes: 0 done, 1 `compare` FAIL, 2 error (bad input, missing data, strategy exception). `GUBAN_DEBUG=1` shows full tracebacks.
- All outputs go to `reports/` (git-ignored). `--out` is always a folder.

## The MC-parity flag set (use by default)
The MultiCharts charts run in exchange time on the 24-hour Globex session, so any run meant to line up with MC needs:

```
--tf 5m --session eth --tz America/Chicago
```

Vault Break also needs a warm-up seed for its long-memory DailyATR, because local data starts in Dec 2022 and MC's chart starts in Jan 2021.
The 276-point average range was fitted to the MC export (PLAN.md Phase 5):

```
--set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620
```

Default fill mode is `magnifier` (1-minute sub-bars). Use `--fill-mode ohlc` only to compare.

## 1. import — build the 1-minute store (only when the source data changes)
```
uv run guban import Database/NQ-CME  --symbol NQ  --roll expiry
uv run guban import Database/MNQ-CME --symbol MNQ --roll expiry
```
- `--roll expiry` matches MC's continuous contract, which rolls on the Wednesday of expiration week. Always use it for MC parity.
- The source is the DeepCharts (dxFeed) binary store, so prices can differ slightly from MC's feed.

## 2. bars — build or inspect resampled bars (optional; `run` builds them itself)
```
uv run guban bars --symbol NQ --tf 5m --session eth
```
`--rebuild` ignores the cache.

## 3. run — a single backtest
**Vault Break on NQ** (run 2026-09-29; 1 NQ with a 50-pt target and stop, same point distances as the MC run):
```
uv run guban run strategies/vault_break.py --symbol NQ --tf 5m --session eth --tz America/Chicago --commission 2.25 --slippage 5 --set take_profit_usd=1000 --set stop_loss_usd=1000 --set num_contracts=1 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620 --set earliest_entry=945 --set latest_entry=1500 --set flatten_time=1550 --set flatten_early_close=false --out reports/vault_break_nq
```
Result: 264,679 bars, 744 trades, net 63,441.00, PF 1.20, max drawdown −14,726.25, 1 ambiguous fill, ~5 s.
Costs are the prop-firm notebook's NQ rates ($2.25 commission + $5 slippage per contract per round trip).

**Vault Break MNQ, the MC parity run** (5 MNQ, $500 target and stop = 50 pts, MC's recovered costs):
```
uv run guban run strategies/vault_break.py --symbol MNQ --history-from NQ --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620 --set earliest_entry=945 --set latest_entry=1500 --set flatten_time=1550 --set flatten_early_close=false --out reports/vault_break_mnq
```
Expected: 737 trades, net 30,891.70, max drawdown −7,315.00.

Notes:
- Outputs: `trades.csv` (MC trade-export layout + engine columns), `metrics.json`, `report.html`. Add `--open` to open the report, or `--no-report` to skip it.
- **Trade Monte Carlo (on by default):** every run also simulates its own trades two ways, side by side in the report's "Monte Carlo" section and under `monte_carlo` in `metrics.json`:
  - resample: n trades drawn with replacement (net profit and drawdown vary; gives P(net ≤ 0));
  - reorder: the same trades shuffled (net fixed; only the drawdown varies).
  - Drawdowns are close-to-close, so compare them with the backtest's close-to-close max drawdown (printed on the `monte carlo` line), not the intraday one.
  - Flags: `--mc-sims` (default 10000; `0` turns it off), `--mc-seed` (default 42), `--mc-dd-limit USD` (adds "chance the max drawdown reaches $X", e.g. a prop-firm limit).
  - MNQ parity run: resample net P5 9,524.65 / P50 30,819.55 / P95 51,851.28, P(net ≤ 0) 0.9%; reorder max DD P5 −11,697.53 vs backtest −7,081.10; ~0.7 s.
  - This is not `propfirm` (which simulates a prop-firm evaluation's rules).
- `--set name=value` overrides a strategy input (repeatable). Unknown names are rejected.
- `--history-from NQ` prepends NQ bars before MNQ's first bar. Use it for MNQ runs only.
- `--contract` sets the P&L spec when it differs from the data symbol (for example NQ data priced as MNQ). The default folder name becomes `NQ-MNQ`.
- Costs are USD per contract per round trip. Slippage is charged half per market or stop fill and not at all on limit fills (MC behavior).

## 4. compare — parity check against an MC export
```
uv run guban compare --ours reports/vault_break_mnq/trades.csv --mc "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --contract MNQ --start 2025-06-08
```
- Expected: S3 PASS, 300/302 matched, net −0.23%. Writes `diff.html` next to `--ours` (or `--out`); `--open` opens it.
- `--start 2025-06-08` limits the check to the MNQ-data window. Price checks need MNQ data, because NQ prices are within 1 tick of MC's MNQ fills only ~86% of the time.
- Pass gate (CLAUDE.md): ≥95% trades matched, prices within 1 tick, net within 3%.
- The MC exports in `D:\Multi Charts\...` are read-only. Never write there.

## 5. optimize — grid search
```
uv run guban optimize strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" --symbol MNQ --history-from NQ --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620
```
- Grid forms: `name=start:stop[:step]` (inclusive), `name=a,b,c`, `name=v`. Several specs per `--grid`, space-separated.
- `--metric` (default `net_profit`), `--min-trades` (default 30), `--workers`, `--x/--y` heatmap axes, `--top`.
- More than 5,000 combinations needs `--yes`. Ask the user before running a grid that large.
- Expected (MNQ grid above): 28 runs in ~15 s, best 0.6 / 4 → net 32,769.50.
- Writes `reports/<strategy>_<symbol>_<tf>_<session>_opt/results.csv`, `optimize.json`, `optimize.html`.
- For NQ, use the NQ `run` flags above in place of the MNQ ones.

## 6. wfo — walk-forward
```
uv run guban wfo strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" --symbol MNQ --history-from NQ --tf 5m --session eth --tz America/Chicago --commission 0.40 --slippage 0.04 --set take_profit_usd=500 --set stop_loss_usd=500 --set num_contracts=5 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620
```
- Window flags: `--is 12 --oos 3 --step 3` (months), `--rolling` (default) or `--anchored`, `--start YYYY-MM-DD`.
- Expected: 675 OOS trades, net 19,777.90, WFE 71%, ~164 s on 6 workers. Run it with `run_in_background` or a long timeout.
- Writes `reports/<...>_wfo/trades.csv` (stitched OOS), `windows.csv`, `results.csv`, `wfo.json`, `wfo.html`.

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
