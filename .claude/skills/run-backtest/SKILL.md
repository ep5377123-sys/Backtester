---
name: run-backtest
description: Run a guban backtest on NQ data and open report.html in the user's browser. Use whenever the user says "run a backtest", "backtest <strategy>", or asks to see backtest results. Ask which strategy first, then run it and open the report. Do not run propfirm, compare, optimize or wfo unless asked.
---

# run-backtest

Runs one strategy through `guban run` on NQ data and opens the HTML report in the user's default browser.

## Steps

1. **List the strategies without reading them.** Use Glob `strategies/*.py` to get file names only. Don't open the files; the CLAUDE.md file-access rule applies.

2. **Ask which strategy to run.** Use AskUserQuestion with one option per strategy file, even when there is only one. Don't assume.

3. **Run it** from the repo root with PowerShell, using a timeout of 300000 ms:

   ```
   uv run guban run strategies/<file>.py --symbol NQ --tf 5m --session eth --tz America/New_York --commission 2.25 --slippage 5 <strategy --set flags> --out reports/<file>_nq --open
   ```

   - Defaults: 5m bars, the 24-hour Globex session, Eastern chart time (user, 2026-09-29; strategy input times are Eastern), 1-minute magnifier fills, and $2.25 commission + $5 slippage per NQ contract per round trip. The MC exports were made on Chicago chart time, so a run meant for `guban compare` against an old MC export needs `--tz America/Chicago` and the old input times.
   - `--open` opens `report.html` in the default browser. Always pass it.
   - `<strategy --set flags>`:
     - `vault_break.py`: `--set take_profit_usd=1000 --set stop_loss_usd=1000 --set num_contracts=1 --set atr_seed_sum=171120 --set atr_seed_count=620 --set sess_count_seed=620`. That is 1 NQ with a 50-point target and stop, the same distance as the MC run, plus the fitted ATR warm-up seed (PLAN.md Phase 5).
     - Any other strategy: no `--set` flags (its own input defaults), unless the user gives inputs.
   - If the user names a different symbol, timeframe, costs or inputs, use theirs.

4. **Report the results** from the command's stdout, briefly:
   - trades, net profit, profit factor, max drawdown, ambiguous fills, run time
   - bar range (first → last bar)
   - that the report is open in the browser, with a link to `reports/<file>_nq/report.html` in case it didn't open
   - one line listing the settings used, and an offer to rerun with different ones

5. **Stop there.** Don't run `propfirm`, `compare`, `optimize` or `wfo` unless the user asks for them.

## If it fails
- Exit code 2 prints `Error: ...` with the fix, for example "run `guban import` first" when data is missing.
- If the error is a bug, fix it (the bug-fix exception in CLAUDE.md) and rerun.
- If data is missing, tell the user and offer the import command from USE.md.
