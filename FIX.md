# Backend Audit Findings

Audit date: 2026-09-29

Scope: Guban's backend, including the backtest engine, data importer and resampler, metrics, parity comparison, optimizer, walk-forward analysis, prop-firm simulation, and CLI validation.

Baseline at the time of the audit:

- All 158 existing tests passed.
- Source compilation passed.
- No source files were modified during the audit.

Passing tests do not eliminate the findings below. Most are invariant gaps, boundary-condition bugs, or documented design choices whose consequences are more serious than the current tests expose.

## Critical findings

### 1. Walk-forward analysis leaks future trade outcomes

Location: `src/guban/metrics.py`, `window_metrics()`, around lines 161-172.

`window_metrics()` selects trades according to their entry bar:

```python
tr = tr[(tr["entry_bar"] >= lo) & (tr["entry_bar"] < hi)]
```

It then includes each selected trade's complete `net_pnl`, even when the trade exits after `hi`, the end of the window. Consequently, prices from after an in-sample boundary can influence the metric used to choose parameters.

Minimal reproduction observed during the audit:

```text
Trade entry bar:          1
Trade exit bar:           3
In-sample window:         [0, 3)
Equity at window end:     $0
Net profit used to rank:  $100
```

This is look-ahead bias. A backtest stopped at the window boundary would not yet know the trade's eventual exit or realized profit.

The out-of-sample stitch has a related problem in `src/guban/wfo.py`, `_stitch()`, around lines 246-275. Each chosen parameter combination is run over the full history. The stitch rebases a slice of that run's equity at the OOS boundary. This allows an OOS slice to inherit P&L from a position that the chosen combination hypothetically opened before its OOS period—even though that position was never taken by the actual stitched sequence.

Minimal reproduction:

```text
Stitched trades:           0
Stitched ending equity:    $100
Reported trade net profit: $0
Boundary trades:           1
```

The reverse inconsistency is also possible: a trade entered inside a window is copied into the trade list with its full eventual profit even when the stitched equity curve stops or switches parameter sets before the trade exits.

Impact:

- In-sample winners can be chosen using future information.
- OOS results can include positions that were never actually entered.
- Stitched equity and stitched trade P&L can disagree.
- Per-window net profit, WFE, optimizer ranks, trade totals, and reports can be unreliable.
- The issue is especially severe for swing or overnight strategies. An intraday strategy that is always flat at every boundary may avoid it by accident.

Recommended fix:

- Never score an IS window using a trade's eventual result after the boundary.
- Define an explicit boundary policy: mark open positions to market, force-liquidate them, or carry the actual preceding window's position until it exits.
- Warm indicators before a window without allowing hypothetical pre-window orders to create inherited positions.
- Execute the selected OOS parameter sequence as one causal simulation rather than stitching slices from unrelated full-history runs.
- Assert that stitched ending equity, cumulative trade P&L, and any open-position P&L reconcile exactly.
- Add tests with trades that cross both IS and OOS boundaries.

### 2. "Intraday peak-to-valley" drawdown is understated

Location: `src/guban/metrics.py`, `intraday_drawdown()`, around lines 99-105.

The metric compares a bar's worst equity against the maximum equity at prior bar closes. It does not track equity peaks reached earlier inside the current bar.

Minimal reproduction:

```text
Intrabar equity path:          $0 -> +$10 -> -$10
True peak-to-valley drawdown:  -$20
Reported drawdown:             -$10
```

`BacktestResult.equity_low` records the adverse extreme, but there is no corresponding sequential intrabar equity peak. A high and a later low inside the same bar are therefore measured relative to the previous bar close instead of the actual preceding peak.

Impact:

- Maximum drawdown is understated.
- Return on account is overstated.
- Optimizing by drawdown, return on account, or related metrics can choose the wrong parameters.
- The report calls the figure "intraday peak-to-valley," but the implementation does not satisfy that definition.

Recommended fix:

- Track a running equity peak in the same chronological path used for fills.
- Update drawdown after each sequential path segment and fill, not only from a bar-wide low/high range.
- Preserve ordering: `open -> high -> low -> close` and `open -> low -> high -> close` produce different peak-to-valley paths.
- Add a regression test where a position makes a new intrabar equity high and then falls to a new low in the same bar.

### 3. Invalid price bars are silently accepted

Location: `src/guban/engine/backtest.py`, `_check_bars()`, around lines 330-339.

The current validation checks column presence, non-emptiness, a timezone-aware index, monotonicity, and uniqueness. It does not validate:

- NaN or infinite OHLC values.
- Numeric OHLC dtypes.
- `low <= high`.
- `low <= open <= high`.
- `low <= close <= high`.
- Nonnegative or finite volume.

A bar with a NaN open and close was accepted during the audit and produced this equity series without an exception:

```text
[0.0, nan]
```

Impact:

- Corrupt data can silently produce NaN fills, positions, equity, metrics, optimizer results, and reports.
- Malformed OHLC bars can cause orders to trigger or fail incorrectly.
- A bad parameter run can contaminate an entire optimization table instead of failing with a useful error.

Recommended fix:

- Validate OHLC columns as finite numeric values before starting a run.
- Validate OHLC geometry for every row.
- Validate volume when present.
- Report the first offending timestamp and values.
- Apply equivalent checks to strategy bars and magnifier sub-bars.

## Significant findings

### 4. Parity comparison can calculate incorrect totals and matches

Locations:

- `src/guban/parity.py`, `compare_trades()`, around lines 197-250.
- `src/guban/parity.py`, `_match()`, around lines 259-278.

#### Matching slack contaminates totals

Guban trades receive one strategy bar of slack on both sides of the comparison window so edge trades can still be matched. Those slack-region trades are also included in `n_ours` and `net_ours`, while MC totals use only the exact window.

Minimal reproduction:

```text
Matched trades:       1
MC net in window:     $10
Guban net reported:   $110
```

The extra $100 Guban trade existed only in the edge-slack region.

Matching candidates may use slack, but count and net-profit comparisons must use the same exact economic window on both sides.

#### Greedy matching is not globally optimal

`_match()` processes MC trades in order and assigns the nearest currently unmatched Guban trade. A locally nearest choice can consume the only valid match for a later trade.

The audit reproduced a case where the greedy algorithm matched only one of two trades even though a valid one-to-one pairing existed for both.

Use a maximum-cardinality, minimum-cost bipartite assignment within each direction instead.

#### Empty-list handling hides unmatched trades

When Guban's list is empty and MC's list is not, the default-window calculation can produce `NaT` bounds and filter all MC trades away. The result reports zero MC trades and produces no useful unmatched-trade diff, instead of listing every MC trade as unmatched.

Other edge cases:

- When MC net profit is exactly zero, `net_diff` is always infinity—even when Guban net is also zero.
- Public comparison functions assume timezone-aware datetime columns without validating that contract.

Recommended fix:

- Separate the exact scoring window from the candidate matching window.
- Use optimal one-to-one assignment.
- Handle either or both empty inputs explicitly.
- Define sensible zero-denominator net-profit behavior.
- Add adversarial matching and empty-list tests.

### 5. Order quantities, costs, and contract specifications are insufficiently validated

Locations:

- `src/guban/engine/strategy.py`, `Strategy._order()`, around lines 84-96.
- `src/guban/engine/backtest.py`, `Costs`, around lines 63-77.
- `src/guban/config.py`, `ContractSpec`, around lines 21-51.

Entry orders permit `qty=None`, because `_order()` allows `None` for both entries and exits. An entry with `qty=None` fails later with an unrelated arithmetic exception:

```text
TypeError: unsupported operand type(s) for *: 'float' and 'NoneType'
```

Boolean quantities are also accepted because `bool` is a subclass of `int` in Python.

`Costs` accepts negative or non-finite commission, slippage, and slippage ticks. A confirmed reproduction with `commission=-10` produced:

```text
Gross profit:  $100
Commission:    -$10
Net profit:    $110
```

`ContractSpec` likewise permits zero, negative, or non-finite `point_value` and `tick_size`, which can cause invalid P&L or division by zero.

Recommended fix:

- Require a positive, non-boolean integer quantity for entries.
- Permit `None` only for exit quantities where it means "all contracts."
- Add `__post_init__()` validation to `Costs` and `ContractSpec`.
- Require finite, nonnegative monetary costs and integer, nonnegative slippage ticks.
- Require finite, positive point and tick values.
- Validate positive report capital before calculating percentages.

### 6. Continuous-contract construction uses hindsight and can create artificial P&L

Locations:

- `src/guban/data/importer.py`, `choose_contracts()`, around lines 117-134.
- `src/guban/data/importer.py`, `import_store()`, around lines 172-221.
- `src/guban/engine/backtest.py`, strategy column binding around lines 399-409.

The default volume roll chooses the contract for a session using that same session's complete volume, and applies the choice from the session open. The total session volume was not knowable at the session open, so the rule uses future information.

The continuous series is also unadjusted. The engine allows a position to remain open across a contract switch, so a rollover price gap can become artificial strategy profit or loss. Although resampled data carries a `contract` column, `run_backtest()` does not bind it to the strategy API, so a strategy cannot reliably detect the switch and flatten.

Recommended fix:

- Use the prior completed session's volume to select the next session's contract.
- Define an explicit roll policy: back-adjust, ratio-adjust, force-close/reopen, or reject open positions across a roll.
- Expose contract identity to strategies.
- Record rolls in run metadata and report any trade that spans one.
- Make the implications of an unadjusted series prominent in reports.

### 7. Mutable strategy input defaults leak state between runs

Location: `src/guban/engine/strategy.py`, `Strategy.__init__()`, around lines 37-44.

The `inputs` dictionary is shallow-copied, but mutable values inside it are shared between the class and every strategy instance.

Confirmed reproduction:

```text
a.p.levels.append(1)
b.p.levels == [1]
Mutable.inputs["levels"] == [1]
```

Impact:

- One run can change the starting inputs of later runs.
- Optimizer results can depend on grid order, process grouping, or worker count.
- A supposedly deterministic run can become stateful across repeated executions.

Recommended fix:

- Deep-copy default input values for each strategy instance, or restrict inputs to immutable scalar values and validate that restriction.
- Deep-copy mutable fixed/grid values passed into optimizer jobs.
- Add a test that mutating one instance's input cannot affect another instance or the class defaults.

### 8. Bar cache invalidation is incomplete

Location: `src/guban/data/resample.py`, `load_bars()`, around lines 114-136.

A cached timeframe is rebuilt only when `1m.parquet` has a newer modification time. Existing caches remain valid according to this check after changes to:

- `config/sessions.toml`.
- Session start or end times.
- Resampling behavior or bug fixes.
- Cache schema or aggregation rules.

This can silently mix new code or configuration with old derived bars.

Cache and import files are also written directly to their final paths. An interrupted write can leave a partial or corrupt Parquet file that later appears current.

Recommended fix:

- Store a cache fingerprint containing source identity, source size/mtime, timeframe, normalized session definition, schema version, and relevant implementation version.
- Rebuild when any fingerprint component changes.
- Write to a temporary sibling file, flush/close it, then atomically replace the final file.
- Validate cached columns, index, timezone, and fingerprint before returning it.

## Additional weaknesses

### 9. Optimizer input type coercion contradicts its stated contract

Location: `src/guban/optimize.py`, `_coerce()`, around lines 104-119.

Numeric defaults receive some type checking, but boolean and nonnumeric defaults accept values of unrelated types.

Confirmed reproduction:

```python
inputs = {"enabled": True, "label": "x"}
parse_grid(["enabled=not_bool", "label=123"], inputs)
```

Result:

```python
{"enabled": ["not_bool"], "label": [123]}
```

This contradicts the module's claim that grid values take the type of the input default.

Other missing optimizer validation includes negative `min_trades`, zero/negative worker counts, non-finite grid values, and direct API grids that bypass `parse_grid()`.

### 10. Prop-firm simulation configuration is permissive and can produce nonsensical results

Locations:

- `src/guban/propfirm/sim.py`, `load_setup()`, around lines 95-117.
- `src/guban/propfirm/sim.py`, `add_costs()` and `bootstrap_ci()`, around lines 139-153.
- `src/guban/propfirm/sim.py`, `run_propfirm()`, around lines 446-473.

The rules loader casts values instead of strictly validating them. Examples of values that are not adequately rejected include:

- Negative or zero profit targets and drawdown limits.
- Negative position sizes.
- Negative costs.
- Zero risk per contract, producing infinite R multiples.
- Invalid degradation percentages.
- Zero or negative bootstrap/path counts.
- Minimum trading days greater than maximum trading days.
- Strings used where TOML booleans are expected; `bool("false")` evaluates to `True` in Python.

`bootstrap_ci()` allocates an array with shape `(sims, number_of_trades)`. Large trade lists or simulation counts can cause excessive memory use or process termination; it should operate in batches or use an online/bootstrap strategy with a documented memory bound.

### 11. Prop-firm methodology has important realism limitations

These behaviors are mostly documented as notebook parity choices, but they remain backend limitations:

- Trade P&L is assigned to entry date, not exit date. Multi-day trades can therefore be charged to the wrong evaluation day.
- Size is a continuous multiplier and can imply fractional futures contracts.
- Weekdays without trades are sampled as simulated trading days and count toward the deadline.
- Daily outcomes are sampled independently, destroying autocorrelation, volatility clustering, and market-regime structure.
- Added costs can be charged on top of a Guban trade file whose profits are already net of backtest costs.
- Equity-path charts ignore the daily loss limit and minimum-trading-day rule even when the main simulation uses them.

These assumptions should be separated from literal firm-rule simulation and labeled prominently in output.

### 12. Binary importer does not validate complete records

Location: `src/guban/data/importer.py`, `read_session_file()`, around lines 48-78.

The importer does not verify that a `.data` file's byte length is divisible by the 60-byte record size. NumPy can ignore trailing incomplete bytes, allowing a truncated file to be processed without a direct corruption error.

Recommended fix:

- Check file size modulo `RECORD.itemsize` before reading.
- Validate timestamp ordering, finite OHLC values, volume, and OHLC geometry after decoding.
- Include the source filename and record offset in corruption errors.

### 13. Magnifier coverage can be incomplete without being rejected

Location: `src/guban/engine/backtest.py`, `_map_sub_bars()`, around lines 342-369.

The mapper checks that a sub-bar stays inside its owning strategy bar's high-low range. It does not require the supplied sub-bars to reproduce the strategy bar's complete OHLC or expected minute coverage. A partially supplied magnifier bar can therefore omit the minute that should trigger a stop or target without falling back to OHLC mode.

Recommended fix:

- Compare aggregated sub-bar open/high/low/close with the owning strategy bar.
- Detect missing expected minutes where the session and timeframe make completeness knowable.
- Reject inconsistent partial coverage or explicitly mark and report it.

### 14. Reusing a Strategy instance is unsafe

Location: `src/guban/engine/strategy.py`, `_bind()` and `series()`, around lines 53-65.

`_bind()` replaces the clock and broker but does not clear `_vars`. Running the same strategy instance more than once can retain variable-series objects from the prior run and append new ones in `on_start()`.

Recommended fix:

- Either reject reuse with a clear error or fully reset all run-specific strategy state before binding.
- Document the lifecycle explicitly and test it.

### 15. Strategy startup exceptions do not receive the same diagnostics as `on_bar()` exceptions

Location: `src/guban/engine/backtest.py`, around lines 425-447, and `src/guban/cli.py`, `_friendly()`.

`on_bar()` exceptions are wrapped in `StrategyError` with bar, timestamp, and position context. `on_start()` is called outside that wrapper. Exceptions such as `ZeroDivisionError` during startup are also outside the CLI's expected exception tuple, so they may produce a raw traceback even when debug mode is disabled.

Recommended fix:

- Wrap `on_start()` in a strategy-specific error with clear lifecycle context.
- Ensure the CLI gives consistent friendly diagnostics for strategy import, construction, startup, and bar execution failures.

## Test-suite gaps exposed by the audit

The existing suite is strong on normal intraday Vault Break behavior, but it does not adequately cover:

- Trades crossing IS or OOS WFO boundaries.
- Reconciliation of WFO equity, realized trades, and open-position P&L when boundary trades exist.
- Intrabar peaks followed by adverse moves in the same bar.
- NaN, infinite, inverted, or internally inconsistent OHLC bars.
- `qty=None` or boolean quantities on entry orders.
- Negative/non-finite costs and invalid contract specifications.
- Adversarial parity matching where greedy assignment loses feasible matches.
- Empty parity inputs and zero-net-profit comparisons.
- Mutable strategy defaults across instances and optimizer runs.
- Session-configuration changes invalidating cached bars.
- Corrupt binary files with incomplete records.
- Strict prop-firm rules/config validation.

In particular, the main WFO correctness test explicitly asserts that its test strategy has no open position at the window end. The separate boundary test only counts boundary trades; it does not assert that stitched equity and trade P&L reconcile. This lets the central boundary defect pass the suite.

## Recommended repair order

1. Redesign WFO execution, scoring, and boundary-position handling.
2. Track true sequential intrabar peak-to-valley drawdown.
3. Add strict OHLC, cost, contract, and order-quantity validation.
4. Correct parity-window accounting, empty handling, and trade assignment.
5. Make continuous-contract selection causal and rollover handling explicit.
6. Isolate mutable strategy inputs and define strategy-instance lifecycle.
7. Add robust cache fingerprints and atomic writes.
8. Add strict optimizer and prop-firm configuration validation.
9. Harden binary import and magnifier completeness checks.
10. Add regression tests for every minimal reproduction in this document.

## Overall assessment

The core intraday execution path is compact, deterministic, and has good conventional test coverage. However, the backend should not currently be trusted for:

- Swing-strategy walk-forward analysis.
- Strategies that hold positions across WFO boundaries or contract rolls.
- Optimization using the reported intraday drawdown or return on account.
- Parity decisions near comparison-window edges or with dense clusters of trades.
- Unvalidated external bar data or permissive user configuration.

The WFO and drawdown defects should be treated as the highest-priority correctness issues because they can make plausible-looking reports systematically optimistic without producing an exception.

## Repair progress (completed 2026-09-29)

Verified after the partial work: `uv run pytest` = 158 passed; the Vault Break parity run is unchanged
(737 trades, net 30,891.70, max drawdown −7,182.50).

### Done (committed)
- `engine/strategy.py`: #7 inputs deep-copied per instance; #14 reusing an instance raises; #5 entry `qty`
  must be a positive non-bool int (`None` only for exits; numpy ints accepted); #15 `StrategyError` moved
  here (re-exported by `engine.backtest`) and strategy-file import errors raise it; `contract` series added.
- `engine/backtest.py`: #3 `_check_bars` validates numeric dtypes, finite OHLC, OHLC geometry, volume
  (also applied to sub-bars); #5 `Costs.__post_init__` validation; #13 bars whose sub-bars don't reproduce
  their OHLC fall back to the OHLC path (`BacktestResult.bars_with_partial_sub_bars`); #15 `on_start()`
  wrapped in `StrategyError`; #2 engine groundwork: equity is marked point by point along the path, with new
  `equity_high` / `bar_drawdown` per bar; #1 engine groundwork: `run_backtest(..., trade_from=, flatten_at_end=,
  window_ends=)` and `BacktestResult.window(lo, hi)` (forced close named "Window End").

### Done (part 2)
- #2: `metrics.intraday_drawdown(equity, low, high, bar_drawdown)` tracks the running peak along the intrabar path;
  threaded through `compute_metrics`, `backtest_metrics`, `report.py` and the WFO stitch. Regression tests
  ($0 → +$10 → −$10 in one bar = −$20). Vault Break `guban run`: 737 trades, net 30,891.70 unchanged; max drawdown
  −7,182.50 → −7,315.00 (the true peak-to-valley). S3 still PASSES.
- #1: every WFO window (IS and OOS) is its own trading window: flat at its start (`trade_from`), a position still
  open at its end closed at its last bar's close ("Window End"). `window_metrics` = `backtest_metrics(result.window())`;
  `optimize._run_one` shares one run per distinct window start; `_run_full` replaced by `_run_window` (one run per
  traded OOS window); `wfo._stitch` chains flat-to-flat windows and raises if final equity ≠ Σ trade net;
  `boundary_trades` = "Window End" exits. Tests: oracle = separate `run_backtest(bars[:hi], trade_from=lo,
  flatten_at_end=True)` runs; Momentum boundary-crossing test; both FIX repros. Vault Break WFO: 164 s (was 23.5 s),
  675 OOS trades, net 19,777.90, WFE 71%, 1 boundary trade (the position open at the end of the data).

### Done (part 3)
- #4 parity: exact scoring window for counts/net/unmatched lists on both sides, ±1-bar candidate window for
  matching only; optimal order-preserving DP matching per direction and per cluster (checked against exhaustive
  search on 300 random cases); empty inputs handled (one side empty → the other's span; both empty → error unless
  a window is given; non-overlapping lists → error); `net_diff` = 0 when both nets are 0; tz-aware time columns
  validated. Vault Break S3 unchanged (300/302 matched, −0.23%).
- #6: `choose_contracts` (volume roll) decides each session from the previous session's volumes (first session =
  front month). `metrics.contract_rolls` / `held_across_roll` / `roll_summary`: `run` and `wfo` note trades held
  across a roll and write `contract_rolls` to metrics.json / wfo.json. Unadjusted series kept for MC parity.
  Existing `data/<SYM>/1m.parquet` files built with `--roll volume` keep the old (hindsight) rolls until re-imported.
- #5 rest: `ContractSpec.__post_init__` (finite positive point_value / tick_size); `to_mc_layout` rejects
  capital <= 0 before anything is written. `run` also notes bars with partial sub-bars (#13).

### Done (part 4)
- #8: bar caches carry a fingerprint in their Parquet schema metadata (source size + mtime_ns, timeframe, session
  key/start/end, `resample.CACHE_VERSION`) and are rebuilt when it differs or the file is unreadable/malformed.
  Caches and `1m.parquet` are written atomically (`guban/data/parquet.py`: temp sibling + fsync + `os.replace`).
  Existing caches rebuild once on first use (Vault Break: identical results).
- #9: `parse_grid` coerces by default type (bool: true/false only; str: token as written, no ranges; numbers
  finite); direct-API grids/fixed values are checked against the defaults; `min_trades < 0` / `workers < 1`
  rejected. `--set` now uses the same coercion (`--set flag=false` used to be the truthy string "false").

### Done (part 5)
- #10: rules files read strictly (`_typed`: TOML types, unknown keys/sections rejected, no casting — `"false"` is
  not a switch); `Rules` / `Scenario` / `Setup.__post_init__` range checks (so CLI overrides are checked too);
  `bootstrap_ci` draws in batches of rows (≤ 4M indices; verified bit-identical to the notebook's `rng.choice`).
- #11: `sim.assumptions()` adds labeled "Assumption:" notes (entry-date attribution, iid days, paths ignore the
  daily loss limit / minimum days, fractional contracts when a size implies them). S6 unchanged: Vault Break MNQ
  35.8%, 7,152 passed.

### Done (part 6)
- #12: `importer.read_records` rejects truncated files (size % 60) and records with non-finite prices, broken OHLC
  geometry, negative volume, or times outside [session date, +2 days), naming the file, record and byte offset
  (`CorruptFileError`); out-of-order records are still sorted. All 2,372 real minute files pass.
- #15 rest: `guban run` wraps the strategy's own `__init__` in `StrategyError` (friendly message + strategy frames).
- Docs: README, CLAUDE.md and a "Backend audit fixes" section in PLAN.md (stale Phase 2/4/8 statements marked
  superseded).

All findings #1–#15 are fixed. Full suite: 220 passed.

## VaultBreak execution audit follow-up (2026-09-29)

Scope: the only Python strategy currently in `strategies/`, `strategies/vault_break.py`, plus the engine paths
that submit and fill its orders. This follow-up audited the latest Eastern-time version after its defaults were
changed to `EarliestEntry=1045`, `LatestEntry=1530`, and `FlattenTime=1550` with
`--tz America/New_York`.

Fresh default run used for the audit:

```text
uv run guban run strategies/vault_break.py --symbol NQ --tf 5m --session eth \
  --tz America/New_York --fill-mode magnifier --no-report \
  --out reports/audit_vault_break_default_et
```

Independent rule replay over that ledger found:

```text
Eligible entry signals:       702
Actual entries:               702
Missing / extra entries:      0 / 0
Maximum entries per day:      3
Direction / quantity errors:  0 / 0
Exit-rule errors:             0

Profit Target exits:          369
Stop Loss exits:              119
VWAP exits:                    52
Time exits:                   162
```

The audit independently rebuilt the calendar-day ATR, noise boundary, calendar-day VWAP, consecutive bars below
VWAP, entry window, position state, and daily trade count. It also checked that market entries and VWAP/time exits
used the next bar's open and that stop/target exits reached the correct per-position dollar level. The ordinary
entry, sizing, daily-cap, stop, target, VWAP, and next-bar execution rules all behaved as implemented.

### New finding A: `FlattenTime` does not guarantee flat/no-overnight

Locations:

- `strategies/vault_break.py`, time-exit condition around lines 137-138.
- `src/guban/engine/backtest.py`, bar loop and pending-order processing around lines 584-607.

The strategy submits `VB_TimeX` only while processing an observed bar whose chart time is at least 15:50. If a
shortened session or a whole-bar data gap removes the 15:50 bar, no time-exit order exists. The position remains
open until trading/data resumes. At the reopening bar the strategy can finally submit `VB_TimeX`, which fills on
the following bar, unless a persistent stop or target exits first.

Four violations occurred in the fresh Eastern-time default run:

```text
#238  2024-07-03 13:10 ET -> 2024-07-03 18:10 ET  VB_TimeX       -$650
#330  2024-12-24 13:00 ET -> 2024-12-25 18:05 ET  Profit Target  +$1,000
#446  2025-07-03 11:50 ET -> 2025-07-03 18:10 ET  VB_TimeX     -$1,285
#621  2026-05-14 13:55 ET -> 2026-05-14 18:10 ET  VB_TimeX       -$400
```

The July 3 and December 24 cases are shortened holiday sessions. The May 14, 2026 case contains a multi-hour data
gap before the 18:00 ET reopen. The longest hold in this default run crossed into the next calendar day. The
current customized `reports/vault_break_nq` ledger also has four late/overnight exits, including a position held
across a holiday weekend.

Impact:

- The stated "flat by 15:50 / no overnight" risk rule is not actually guaranteed.
- A position can be exposed through a market closure, holiday, weekend, or missing-data interval.
- The eventual stop/target fill can include a reopening gap that the advertised intraday strategy should not
  have carried.
- The backtest currently gives no prominent warning that the flatten bar was absent.

Recommended fix:

- Define the intended policy explicitly for scheduled shortened sessions and unexpected missing bars.
- Provide each bar with a causal, exchange-calendar-derived scheduled session close/last tradable bar. Flatten on
  the final available bar before that known close; do not infer it by looking at future prices.
- Add a data-continuity warning for missing whole strategy bars, especially when a position or pending market
  order spans the gap.
- Consider broker-side/session-close protection for live use because a backtest-only gap rule cannot protect a
  live position when data stops unexpectedly.
- Add regression tests for July 3/December 24 early closes and for a multi-hour missing-data interval.
- Add a run-level invariant/report warning listing every position that crosses the intended close or reopen.

### New finding B: the Eastern-time default change broke the strategy test

Location: `tests/test_parity.py`, `test_vault_break_enters_next_bar_after_breakout_and_exits_at_target()`, around
lines 226-244.

Current full-suite result:

```text
219 passed, 1 failed
```

The failing test creates Central-time bars with a breakout at 09:45 CT, but it relies on the strategy's defaults.
The strategy now defaults to `EarliestEntry=1045` in an Eastern-time chart, so the test produces no trade and
fails while unpacking the empty trade list.

Recommended fix:

- If the test is for the new strategy, convert the synthetic chart to `America/New_York` and place the breakout
  at 10:45 ET.
- If it is intentionally an MC-parity test for the old Central-time variant, pass
  `earliest_entry=945`, `latest_entry=1500`, and the old chart-zone assumptions explicitly rather than relying on
  mutable strategy defaults.
- Keep separate tests for the current Eastern-time strategy and the historical MC-compatible configuration.

### Configuration and parity caveat

The current strategy header correctly states that the Eastern-time defaults no longer reproduce the MC export
trade for trade. Therefore the old parity result is not proof for the new default schedule.

The historical MC-compatible configuration was re-compared over the period backed by actual MNQ data
(`2025-06-08 17:05 CT` onward) and still passed S3:

```text
MC trades / ours:                 302 / 302
Matched:                          300 (99.3%)
Matched prices within one tick:   293 (97.7%)
Net profit:                       MC $6,555.30 / ours $6,540.30 (-0.23%)
S3:                               PASSED
```

That result validates the engine and old explicit inputs against the MC oracle; it must remain labeled separately
from reports made with the new Eastern-time defaults.

One timestamp detail should also be documented clearly: a bar labeled 15:50 is a close-time label. An order it
submits fills at the next bar's open (wall-clock 15:50) but the trade ledger associates the fill with the next
close-labeled bar (normally 15:55). Reports will therefore show a 15:55 `exit_time` for the ordinary 15:50 time
exit.

### Acceptance criteria for this follow-up

- `uv run pytest -q` returns all green after the timezone test is corrected.
- An Eastern-time regression test proves a valid 10:45 signal fills at the next bar's open.
- Shortened-session tests prove an open position is closed before the scheduled holiday close.
- Missing-whole-bar gaps while a position is open are surfaced prominently and follow a documented policy.
- A full NQ default run has zero positions crossing the intended daily close/reopen, or explicitly reports every
  unavoidable exception.
- The MC-compatible parity test remains separate and continues to pass its three S3 thresholds.

### Follow-up repair (completed 2026-09-29)

- **B** was already fixed before this repair: the Central-time test pins `earliest_entry=945`. There is now also an Eastern-time
  regression test: a 10:45 ET signal fills at the 10:50 bar's open, and the 15:50 exit shows as 15:55.
- **A, policy.** *Scheduled* closes are known in advance. `config/calendar.toml` lists CME equity early closes
  (checked against every short session in the NQ store: 27 × 13:00 ET, 8 × 13:15 ET, 2 Good Fridays, 2025-01-09).
  `load_bars` adds `session_close`, and strategies see it as a series. VaultBreak flattens at
  min(FlattenTime, close − `early_close_buffer`=10 min), and its last entry moves with it (keeping the 20-min gap).
  `flatten_early_close=false` gives the literal .el. *Unscheduled* gaps cannot be known without look-ahead, so the
  strategy acts on the first bar after data resumes. `run` / `wfo` then list every trade held past its session's
  scheduled close or across missing bars (notes, CLI summary, `session_exposure` in metrics.json / wfo.json).
- **A, results.** Eastern default run: 700 trades (was 702). #238 (07-03) and #330 (12-24) are no longer entered,
  because their signals came after the capped last entry. #446 (2025-07-03) exits at 13:05 ET for −$960 (was −$1,285
  at 18:10). The only remaining trade held over a close is #619, the 2026-05-14 feed gap, and it is reported.
  With the switch off, the detector flags exactly the audit's four trades.
- **Parity.** The MC-compatible configuration pins `earliest_entry=945 latest_entry=1500 flatten_time=1550
  flatten_early_close=false` (README `$vb`, USE.md, PLAN.md). MC held through early closes, so the flatten would
  cost parity (+18.6% net). Result: 737 trades, net 30,891.70, 300/302 matched, −0.23%, S3 PASS.
- Tests: 232 passed (early close 13:00/13:15, moved last entry, the gap exit + report, calendar, `session_close`
  in chart time, `data_gaps` / `session_exposure`).
