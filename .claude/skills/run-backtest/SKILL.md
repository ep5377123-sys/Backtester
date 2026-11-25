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

   - Defaults: 5m bars, the 24-hour Globex session, Eastern chart time (strategy input times are Eastern), 1-minute magnifier fills, and $2.25 commission + $5 slippage per NQ contract per round trip.
   - `--open` opens `report.html` in the default browser. Always pass it.
   - `<strategy --set flags>`:
     - `vault_break.py`: `--set take_profit_usd=1000 --set stop_loss_usd=1000 --set num_contracts=1`. That is 1 NQ with a 50-point target and stop.
     - `ivb.py`: its defaults are Central, so shift them to Eastern (always run in ET, user 2026-10-08): `--tf 1m --set ivb_start=930 --set ivb_end=1000 --set latest_entry=1300 --set time_exit=1500`. If the user gives a time (e.g. an 11:00 entry cutoff), use it as Eastern.
     - `orb_bubbles.py`: `--tf 1m --data-from 2024-02-01` (it reads tick bubbles; NQ ticks start 2024), no `--set` flags.
     - Any other strategy: no `--set` flags (its own input defaults), unless the user gives inputs.
   - If the user names a different symbol, timeframe, costs or inputs, use theirs. Imported symbols: NQ, MNQ, ES, MES, BTC
     (BTC: `--session h24`, it trades 24/7 since 2026-05-29). Leaving out `--commission`/`--slippage` uses the contract's
     default costs (config/contracts.toml); keep the explicit NQ costs above unless the user asks for the defaults.

4. **Report the results** from the command's stdout, briefly:
   - trades, net profit, profit factor, max drawdown, ambiguous fills, run time
   - the `monte carlo` line: resample net P5 / P50 / P95 and P(net <= 0); reorder max DD P5 vs the backtest's
     close-to-close drawdown, and P(DD worse than backtest). The report's "Monte Carlo" section has the charts.
   - bar range (first → last bar)
   - that the report is open in the browser, with a link to `reports/<file>_nq/report.html` in case it didn't open
   - one line listing the settings used, and an offer to rerun with different ones

5. **Write the model evaluation every time** (user, 2026-10-08). It's a written verdict on whether the model is good,
   built from the `evaluation` block of `reports/<file>_nq/metrics.json`. The report's "Evaluation" section shows the
   same tables. Read the block with a short `uv run python -c` and include:
   - **Verdict**: a one-line yes/no.
   - **Criteria table**: the user's criteria (Sharpe > 1, PF > 1.2, max drawdown ≤ 25R, Sortino > 1), as run and at equal risk.
     Say what 1 R is (`r_usd`, `r_source`).
   - **What drives the result**: the stop-width thirds (is the edge concentrated or missing in one?), the exit-signal
     split (which exits make the money?) and the per-year table (losing years, recent decay).
   - **Monte Carlo read**: P(net ≤ 0); the backtest drawdown vs the drawdown percentiles (lucky or typical ordering);
     reorder vs whole days (do losses cluster within a day?); the EV CI in USD and R; the 10/20/40/100-trade horizons;
     the equal-risk R Monte Carlo (drawdown median/P95/P99, P(> 25R)).
   - **Prop-firm notes**:
     - the size of one stop vs the trailing drawdown in `config/propfirm/*.toml` (say those values need checking against the firm's current rules)
     - what R in USD keeps the P95 R drawdown inside it, and whether MNQ can size that small
     - trades per evaluation window and the 10-trade P(net < 0)
     - the worst losing streak and the max loss per day
     - holding past the close
   - **Next steps**: concrete ones to test with `guban wfo`, not tuned on the same data. Offer them; don't run them.

6. **Stop there.** Don't run `propfirm`, `compare`, `optimize` or `wfo` unless the user asks for them.

## If it fails
- Exit code 2 prints `Error: ...` with the fix, for example "run `guban import` first" when data is missing.
- If the error is a bug, fix it and rerun.
- If data is missing, tell the user and offer the import command from USE.md.
