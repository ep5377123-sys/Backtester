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

## Repair progress (paused 2026-09-29 at the user's request)

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

### Remaining (in this order)
8. #10/#11 prop-firm: strict typed TOML loading + `__post_init__` checks on Rules/Scenario/Setup; batch
   `bootstrap_ci` as `x[rng.integers(0, n, (b, n))]` (verified bit-identical to `rng.choice`); add
   methodology-assumption notes (entry-date attribution, fractional size, iid days, paths ignore DLL/min days).
9. #12 importer: size % 60 check; finite/geometry/volume checks; timestamps inside [session date, +2 days)
   (two real NQ files have one swapped minute each — keep sorting, don't reject disorder). Real data scanned
   clean for all other checks.
10. #15 rest: CLI wraps strategy construction in `StrategyError`.
11. Tests for every item, then update CLAUDE.md / PLAN.md / README.
