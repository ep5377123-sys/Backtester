# Guban Backtester

A Python backtester for CME futures that reproduces MultiCharts (MC) results. You write a strategy in Python, backtest
it on multi-year 1-minute data, and get an MC-style performance report in the browser. You can then check it trade by
trade against MC's own trade list, optimize and walk-forward its inputs, and estimate prop-firm pass rates.

The Vault Break port matches MC on 99.3% of trades with −0.23% net-profit difference (PLAN.md, Phase 5).

## Contents
- [Install](#install)
- [The data you need](#the-data-you-need)
- [Walkthrough: import → run → compare → optimize → wfo → propfirm](#walkthrough)
- [Guban Studio: the desktop GUI](#guban-studio-the-desktop-gui)
- [Writing a strategy](#writing-a-strategy)
- [Outputs, run folders and exit codes](#outputs-run-folders-and-exit-codes)
- [Configuration](#configuration)
- [Known limits](#known-limits)

## Install

Needs Python 3.14 and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync                 # create .venv and install dependencies
uv run guban --help     # list the commands
uv run pytest           # run the test suite
```

Every command has `--help`. `run`, `optimize` and `wfo` share the same data, cost and strategy flags.

## The data you need

Two kinds of input, from two places. Neither goes into git: `Database/`, `data/` and `reports/` are git-ignored.

### 1. Bar data: the shared market-data store (from DeepCharts / dxFeed)

The bar history comes from **DeepCharts**, which gets its data from dxFeed. It does not come from MultiCharts.
`guban store build` turns DeepCharts' live folder (`D:\Deepchart\Database`, opened read-only) into a write-once store
of Parquet files at `D:\Market Data`, shared with the Research Pipeline:

```
D:\Market Data\<SYM>\minute\<YYYYMM contract>\<YYYYMMDD>.parquet   one contract session of 1-minute bars
D:\Market Data\<SYM>\tick\<YYYYMM contract>\<YYYYMMDD>.parquet     its trades (where DeepCharts has ticks)
D:\Market Data\<SYM>\quality.csv, roll_calendar.csv                 per-session checks; the volume roll
```

- Symbols: NQ (from 2018-12-30), ES (2019-12-29), MNQ, MES, BTC (2020-01-01). The store's `README.md` documents
  every column and rule; settings are in `config/store.toml`.
- `guban store build` adds sessions as they finish; `guban store verify` checks the stored files against their source.
- `guban import --symbol <SYM>` then builds Guban's continuous series from the store (step 1 below).
- Bars come from DeepCharts' 1-minute records. Ticks are used only by studies that need them, and only for sessions
  whose `quality.csv` `tick_ok` is True.

### 2. MC trade lists: the parity oracle

To check a port against MultiCharts, you need MC's own trade list for the same strategy and inputs:

1. In MultiCharts, open the strategy's **Strategy Performance Report** and export it to Excel (`.xlsx`).
2. Guban reads the **List of Trades** sheet straight from that `.xlsx`. It also takes the converted CSV layout used in
   `D:\Multi Charts\Performance Reports\CSV Data\` (`trade_id,order_id,type,signal,excel_date,excel_time,price,
   contracts,profit_usd,...,direction`, lines optionally wrapped in quotes).
3. **Write down the inputs and costs** the chart used. The Vault Break export had only the trade list; its settings
   sheets were empty, and the inputs had to be recovered from the trades.

For parity, run guban with the same settings as the MC chart. The Vault Break MNQ chart used:

| MC chart setting | Vault Break MNQ chart | guban flag |
|---|---|---|
| Time zone | Exchange (Central) | `--tz America/Chicago` |
| Session | 24-hour Globex | `--session eth` |
| Chart start | ~2021-01-01 (first trade 2021-01-20) | `--data-from 2021-01-03` |
| Continuous contract | Rolls on the Wednesday of expiration week | `guban import --roll expiry` |
| Bar Magnifier | On, 1 minute | `--fill-mode magnifier` (default) |
| Commission / slippage | $0.40 / $0.04 per contract per round trip | `--commission 0.40 --slippage 0.04` |
| Initial capital | $1,000,000 | `--capital 1000000` (default) |

## Walkthrough

These commands reproduce the Vault Break MNQ results from PLAN.md. They are written for PowerShell. `$vb` holds the flags
that `run`, `optimize` and `wfo` share: data, costs, and the inputs of the MC run.

```powershell
$vb = @('--symbol','MNQ','--data-from','2021-01-03','--tf','5m','--session','eth','--tz','America/Chicago',
        '--commission','0.40','--slippage','0.04',
        '--set','take_profit_usd=500','--set','stop_loss_usd=500','--set','num_contracts=5',
        '--set','earliest_entry=945','--set','latest_entry=1500','--set','flatten_time=1550','--set','flatten_early_close=false')
```

The last line pins the MC chart's Central-time entry/flatten inputs (the strategy's defaults are now Eastern) and turns
off the scheduled early-close flatten, which the MC strategy does not have.

### 1. import: build the continuous 1-minute series from the store

```powershell
uv run guban import --symbol NQ  --roll expiry
uv run guban import --symbol MNQ --roll expiry
```

- Writes `data/<SYM>/1m.parquet`: one unadjusted continuous series, in Eastern time, bars labeled by their close.
  About 15 s per symbol. `--root` reads another store folder.
- Prints the first and last bar, the bar count, the rolls, the missing session days and the store's thin sessions.
- `--roll expiry` switches contracts on the Wednesday of expiration week, like MC (equity index contracts only).
  `--roll volume` (the default) uses the store's roll calendar: the contract that had the most volume in the previous
  session, except that a contract is never traded in its expiration session (BTC stops at 11:00 ET that day).
- `--source Database/NQ-CME` reads a DeepCharts per-contract folder directly instead (the import before the store). The series is unadjusted either way, so `run` and
  `wfo` list any trade held across a roll (its P&L includes the gap between the contracts). They also list trades
  held past their session's scheduled close, and trades held across missing bars (a data gap inside a session, or
  a session whose data stops before its scheduled close). These are notes in the report and `session_exposure` in the JSON.
- The store checks every source file when it is built (whole records, sane prices and volume, times inside the
  session); the import stops with the file's name if a session the roll needs is missing.
- Optional: `guban bars --symbol NQ --tf 5m --session rth` builds and caches strategy bars and prints a per-session summary. `run` builds these caches on its own, and rebuilds them when `1m.parquet` or the session in `config/sessions.toml` changes.

### 2. run: backtest one set of inputs

```powershell
uv run guban run strategies/vault_break.py @vb --out reports/vault_break_mnq --open
```

- Writes `trades.csv` (MC trade-export layout plus guban's own columns), `metrics.json`, `report.html`, and one
  price-chart image per trade in `trades/` next to it. The report has KPIs, equity and drawdown, a trade Monte Carlo,
  the performance summary, monthly net, a trade viewer and the trade list. Every chart is a matplotlib image and the
  page is light-only; it opens offline (the trade viewer needs the `trades/` folder beside it).
- **Trade Monte Carlo.** Every run simulates its own trades 10,000 times three ways, shown side by side: *resample* draws
  the same number of trades with replacement (how lucky was the net profit?), *reorder* shuffles the same trades (how
  bad could the drawdown have been?), *whole days* draws trading days with replacement and keeps each day's trades
  together (do losers cluster?). The section has equity bands with 100 sample paths and the actual path, drawdown and
  net-profit histograms, percentile and drawdown-depth tables, the expected value per trade (USD and R, 95% bootstrap
  CI, P(EV ≤ 0); all, long, short) and results after the first 10/20/40/100 trades or days. `metrics.json` gets the
  numbers under `monte_carlo`. Drawdowns are close-to-close (a trade list has no intraday path). `--mc-sims 0` turns it
  off, `--mc-seed` sets the seed (default 42), `--mc-dd-limit 2500` adds the chance the max drawdown reaches $2,500,
  `--mc-risk 400` overrides the risk per contract for R (default: each trade's own stop, below), and
  `--mc-oos-from 2025-01-01` also splits the EV into trades before and from that date.
- **Reward:risk.** Each trade records the stop and target in force from its entry fill — `set_stop_loss` /
  `set_profit_target`, or the strategy's own stop and limit exit orders (the tightest stop and nearest target win;
  later moves don't count). `trades.csv` ends with `risk_usd`, `reward_usd`, `planned_rr` (target distance ÷ stop
  distance: a 20 pt stop with a 40 pt target is 2.00) and `r_multiple` (net profit ÷ risk, costs included). They are
  empty for a trade without a stop. The report shows R and RR per trade and their averages in the summary.
- **Trade images.** `--no-trade-charts` skips them (the MNQ run: 9 s instead of 23 s). `guban chart <run folder>
  [--trade N ...]` draws them later, or again, from the run's `trades.csv`: it reloads the bars with the settings in
  `metrics.json` / `wfo.json` and refuses when the data changed since the run (other than new sessions at the end).
- Expected result: 1,094 trades, net 45,579.80, max drawdown −7,315.00, about 23 s with trade images (9 s without).
  The max drawdown is peak to valley along the intrabar equity path, so a rise and a fall inside one bar both count.
- `--data-from 2021-01-03` starts the data where MC's chart starts, so Vault Break's long-memory DailyATR sees the same
  history MC did. (The `atr_seed_*` inputs, fitted in Phase 5 when local MNQ data began in 2025, are no longer needed.)
- Costs: without `--commission` / `--slippage`, a run uses the contract's default costs from `config/contracts.toml`.
- `--history-from NQ` fills in NQ bars before a symbol's first bar, so long-memory indicators warm up.
- `--fill-mode ohlc` resolves exits with the OHLC path rule on the whole bar instead of 1-minute sub-bars.

### 3. compare: check parity against MC

```powershell
uv run guban compare --ours reports/vault_break_mnq/trades.csv `
    --mc "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --contract MNQ --open
```

- Pairs trades that have the same direction and entries within one bar, one to one, pairing as many as possible
  (then the closest). Trade counts and net profit use exactly the same window on both sides. Checks prices within
  1 tick and net profit.
  Writes `diff.html`, listing every unmatched and price-mismatched trade, into the `--ours` folder (or into `--out`).
- **Pass** (the plan's S3 gate): ≥ 95% of trades matched, ≥ 95% of matched trades within 1 tick, net profit within 3%.
  Exit code 1 means FAIL.
- `--start` / `--end` limit the window, in MC chart time (`--mc-tz`, default America/Chicago). Expected on the full span
  2021-01-20 → 2026-09-22: 1,072 of 1,081 MC trades matched (99.2%), 97.8% within 1 tick, net +0.03%, PASS.

### 4. optimize: grid-search inputs

```powershell
uv run guban optimize strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" @vb --open
```

- Grid specs are `name=start:stop[:step]` (inclusive), `name=a,b,c` or `name=v`. `--grid` can repeat, and one flag can
  hold several specs separated by spaces. Inputs set with `--set` stay fixed.
- Runs on every CPU core (`--workers`). Ranks by `--metric` (default `net_profit`), counting only runs with at least
  `--min-trades` trades (default 30).
- Writes `results.csv`, `optimize.json`, and `optimize.html` with a heatmap (`--x/--y` pick its axes). Grids over 5,000
  combinations need `--yes`.
- Expected: 28 runs in about 40 s. The best is noise_mult 0.5 / vwap_exit_bars 4, net 56,341.80.

### 5. wfo: walk-forward the inputs

```powershell
uv run guban wfo strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" @vb --open
```

- Optimizes each in-sample window and trades the winner in the next out-of-sample window. `--is 12 --oos 3 --step 3`
  (months) are the defaults; `--anchored` makes every in-sample window start at `--start` and grow.
- Every window is traded on its own: indicators warm up on the earlier data, the window starts flat, and a position
  still open at its end is closed at its last bar's close (exit "Window End"). So no price after a window reaches its
  score, and the stitched equity always equals the sum of the stitched trades.
- Writes the stitched out-of-sample `trades.csv`, `windows.csv`, `results.csv`, `wfo.json` and `wfo.html` (plus one
  image per OOS trade in `trades/`; `--no-trade-charts` skips them), with walk-forward efficiency = annualized OOS net ÷
  annualized IS net.
- Expected: 19 windows, 1,067 OOS trades, OOS net 43,531.10, WFE 77%. It took 998 s on 6 workers (measured with the test suite running alongside, so expect less).

### 6. propfirm: prop-firm evaluation Monte Carlo

```powershell
uv run guban propfirm --trades reports/vault_break_mnq/trades.csv --rules apex_50k_eod --open
```

- Takes a guban `trades.csv` or an MC export (`.csv` / `.xlsx`). Rules and simulation settings come from
  `config/propfirm/<rules>.toml`, and flags such as `--size`, `--sims`, `--sweep 0.5,1,1.5`, `--seed`, `--commission`
  and `--slippage` override them. Rules files are read strictly (true/false for switches, numbers in range), and
  the report labels the simulation's own assumptions apart from the firm's rules.
- Writes `propfirm.json`, `sweep.csv`, `degradation.csv`, `daily.csv` and `propfirm.html`: pass rate with a 95% CI,
  failure mix, degradation scenarios, and a size sweep.
- This is a bit-exact port of `25k Prop Firm Accurate Rules.ipynb`, quirks included. Its `--commission` / `--slippage`
  are added **on top of** the trade file's already-net profit, as in the notebook. Pass `--commission 0 --slippage 0`
  to avoid charging costs twice. The report lists the notebook's quirks under Notes.

## Guban Studio: the desktop GUI

`uv run guban gui` opens Guban Studio, an Electron window over the same engine (needs [Node.js](https://nodejs.org); the
first launch installs Electron into `gui/node_modules`). A Python worker does all the work; every chart is a matplotlib
image in a dark version of the reports' style.

- **Data:** symbol, timeframe, session, time zone and dates → Load (imports the symbol from the store first if needed;
  *Update from store* re-imports it). In-sample and out-of-sample windows are their own date ranges; *OOS dataset size*
  fills them as the last fraction of the range, *Swap* puts the out-of-sample window first. *Current dataset* (In sample
  / Out of sample / Full) is what the Strategy and Backtest tabs run on. Shows a paged bar table, the close price with
  the windows shaded, and a data-quality report (gaps, rolls, OHLC checks, the store's tick / thin-session flags).
- **Strategy:** pick a file from `strategies/`, Load / Reload (re-imports it after you edit it), edit the inputs, Apply.
  Shows entry / exit counts, every trade, and a candlestick chart (Start index, Bars to plot; click a trade to jump to
  it) with entries ▲▼, exits ✕ and the strategy's `plots` lines. Apply uses the contract's default costs and 1-minute
  magnifier fills.
- **Backtest:** reruns the applied strategy with this tab's capital, costs, fill mode and Monte Carlo settings and shows
  what `guban run` reports: metrics, evaluation, equity, drawdown, the resample / reorder / whole-day Monte Carlo with
  EV and horizons, net by month, plus daily P&L and daily drawdown histograms. *Save results* writes the same run folder
  as `guban run` (`trades.csv`, `metrics.json`, `report.html`; `_is` / `_oos` suffix for a window).
- **Optimization:** tick inputs and give their values (`0.4:1.0:0.1`, `1,2,3`); the grid runs on the in-sample window
  only (each combination scored as its own run on that window), then the top N are rerun out of sample: equity curves
  (in sample blue, out of sample purple), Sharpe and drawdown histograms, in- vs out-of-sample net, and a table. *Stop*
  ends the run at once; *Use selected inputs* copies a row's inputs to the Strategy tab. *Save* writes
  `reports/<strategy>_<sym>_<tf>_<session>_opt_is_oos/` (`results.csv`, `oos.csv`, `optimize.json`).

Windows match the engine's trading windows: warm-up from the first loaded bar, flat at the window's start, a position
still open at its end closed there ("Window End"). The Full dataset is exactly `guban run`. `uv run guban gui --smoke`
drives every tab in a hidden window, saves a screenshot per step to `reports/_gui_smoke/` and exits 0 when all pass.

## Writing a strategy

A strategy is a Python class in its own file. It mirrors PowerLanguage semantics.

```python
from guban.engine import Strategy


class OpeningRange(Strategy):
    """Buy a break of the first N bars' high, once per session; dollar stop and target; flat at flatten_time."""

    inputs = dict(range_bars=6, stop_usd=400, target_usd=800, flatten_time=1550, contracts=1)

    def on_start(self):
        self.day_bar = 0  # plain attributes keep their value from bar to bar (MC intrabarpersist)
        self.range_high = 0.0
        self.traded = False

    def on_bar(self):
        if self.current_bar == 1 or self.session_date[0] != self.session_date[1]:  # new session (MC: Date <> Date[1])
            self.day_bar, self.range_high, self.traded = 0, 0.0, False
        self.day_bar += 1
        if self.market_position != 0:
            self.traded = True
        if self.day_bar <= self.p.range_bars:
            self.range_high = max(self.range_high, self.high[0])
            return
        self.set_stop_loss(self.p.stop_usd)  # per position, in dollars; active until changed
        self.set_profit_target(self.p.target_usd)
        if self.time[0] >= self.p.flatten_time:
            if self.market_position == 1:
                self.sell("TimeX")  # next bar at market
        elif self.market_position == 0 and not self.traded:
            self.buy("ORB", self.p.contracts, at="stop", price=self.range_high)  # next bar, buy stop
```

Save it as `strategies/opening_range.py` and run it:

```powershell
uv run guban run strategies/opening_range.py --symbol NQ --tf 5m --session rth --commission 4.50 --set range_bars=12
```

- **Inputs:** `inputs` holds the defaults. They are read as `self.p.<name>` and overridden with `--set name=value`.
  `optimize` and `wfo` sweep them with `--grid`.
- **Series:** `open high low close volume time date session_date timestamp contract session_close`. `[0]` is the current
  bar and `[n]` is n bars ago. Looking back before bar 1 raises. `time` is HHMM in the chart time zone (`--tz`), like
  MC's `Time`. `session_close` is when the bar's session is scheduled to close (a timestamp in chart time): the session
  template's end, or the exchange's early halt on the days listed in `config/calendar.toml` (published in advance, so
  using it is not look-ahead). Use it to be flat before a holiday close; `strategies/vault_break.py` shows how.
- **Orders:** `buy` / `sell_short` open a position. `sell` / `buy_to_cover` close it (all contracts by default).
  `at="market"` (default), `"stop"` or `"limit"` with `price=`. Orders placed in `on_bar` are for the **next bar**,
  and they live for one bar. `at="close"` is MC's `this bar on close`: it fills at the close of the bar that placed it,
  as a market fill, and `market_position` shows it from the next bar on. An opposite entry reverses the position.
  There is no pyramiding.
- **Exits:** `set_stop_loss(usd)` / `set_profit_target(usd)` are per-position dollars, as in MC. They are live from the
  entry fill onward and persist until set to `None`.
- **State:** `market_position` (−1/0/1), `current_contracts`, `entry_price`, `bars_since_entry`, `current_bar`
  (1-based), `big_point_value` (USD per point per contract, MC's `BigPointValue`).
- **Costs:** `--commission` / `--slippage` are USD per contract per round trip. Slippage is charged half per market or
  stop fill and none on limit fills (MC behavior).
- **Rules for `optimize` / `wfo`:** define the class at the top level of its file, because worker processes re-import
  it. Never look at future bars. `--set` and `--grid` values take the type of the input's default (`true`/`false`
  for a bool, a finite number for a number, text for a string).
- **Plots (Guban Studio):** `plots = ("ema_fast", ...)` names `self.series()` variables (by attribute, or by the
  `name=` given to `self.series`) that the Strategy tab draws on its price chart. It changes nothing in the backtest.
- **Errors:** if the strategy's `__init__`, `on_start` or `on_bar` raises, the run stops, nothing is written, and
  guban prints where it failed (for `on_bar`: the bar, its timestamp and the position) and the failing line of your file.

`strategies/vault_break.py` is the reference port: a literal, line-by-line translation of `sc_Vaultc20Break.el`.
`strategies/ivb.py` (IVB 1-min, long-only opening-range breakout) shows `at="close"` entries and stop/limit exits; run it
on 1-minute bars in exchange time: `--tf 1m --session eth --tz America/Chicago --commission 1.22 --slippage 0.04`.

## Outputs, run folders and exit codes

`--out` is always a folder. Without it, each command writes to `reports/<run id>/`:

| Command | Default folder | Files |
|---|---|---|
| `run` | `reports/<strategy>_<symbol>_<tf>_<session>` | `trades.csv`, `metrics.json`, `report.html`, `trades/trade_<no>.png` |
| `compare` | the folder of `--ours` | `diff.html` |
| `optimize` | `reports/<strategy>_<symbol>_<tf>_<session>_opt` | `results.csv`, `optimize.json`, `optimize.html` |
| `wfo` | `reports/<strategy>_<symbol>_<tf>_<session>_wfo` | `trades.csv`, `windows.csv`, `results.csv`, `wfo.json`, `wfo.html`, `trades/trade_<no>.png` |
| `chart` | the run folder it is given | `trades/trade_<no>.png` |
| `propfirm` | `reports/<trades file or its run folder>_<rules>` | `propfirm.json`, `sweep.csv`, `degradation.csv`, `daily.csv`, `propfirm.html` |

`<strategy>` is the file name without `.py`. When `--contract` differs from `--symbol` (for example NQ bars priced as
MNQ), `<symbol>` becomes `NQ-MNQ`. `--open` opens the HTML page when the command finishes. A backtest is
deterministic: the same data, inputs and flags give a byte-identical `trades.csv`.

| Exit code | Meaning |
|---|---|
| 0 | Done |
| 1 | `compare` ran but the parity check FAILED |
| 2 | Error: a bad flag or input, missing data, an unknown symbol, session or time zone, a timeframe that does not divide the session, a strategy exception, or a grid over 5,000 combinations without `--yes` |

Errors print one `Error: ...` line that names the problem and the fix. Set `GUBAN_DEBUG=1` to get the full Python
traceback instead.

## Configuration

- `config/contracts.toml`: contract specs (point value, tick size, calendar) and default costs (commission and 1 tick
  slippage per round trip, used when a run gives no cost flags). NQ, MNQ, ES, MES and BTC are included. Add a symbol
  here before you import or run it.
- `config/sessions.toml`: session templates. `rth` is 09:30–16:00 ET; `eth` is the 23-hour Globex session, 18:00–17:00
  ET; `h24` is 18:00–18:00 ET for BTC, which trades 24/7 since 2026-05-29. The timeframe must divide the session length.
- `config/calendar.toml`: scheduled early closes per exchange calendar (`cme_equity`: 13:00 ET on CME holidays, 13:15 ET
  half days, Good Friday mornings; `cme_crypto` for BTC), named by `calendar` in `contracts.toml`. They set each bar's
  `session_close`. 2019–2026 were derived from the store's short sessions and checked against QuantConnect Lean's
  market-hours database; add each new year's dates from CME's holiday calendar.
- `config/store.toml`: the market-data store's folders, symbols and build rules.
- `config/propfirm/<rules>.toml`: prop-firm rules, costs, simulation settings and degradation scenarios.
  `apex_50k_eod` is copied from the notebook; review its values (see below).

## Known limits

- **Bar data differs slightly from MC's.** Guban's bars come from DeepCharts/dxFeed, and MC charts its own feed. The few
  trades that don't match in the parity window are small price differences between the two data sources.
- **Prop-firm rules need review.** `apex_50k_eod` is labeled 50K but uses the notebook's $25,000 / $1,250 target /
  $1,000 drawdown values. The report's notes list this and the notebook's other quirks.
- **The trade Monte Carlo treats trades as independent.** Trades that tend to win or lose together (several on one
  day) are drawn separately, which can make its drawdowns look milder. A whole-day mode is not built yet.
- **Out of scope for v1:** multi-symbol portfolios, tick-level fills, running `.el` files directly, and live trading.
  Other MC strategies get ported by hand, one at a time, and each one is done only when `guban compare` passes.

The full plan, the decisions behind it, and each phase's results are in [PLAN.md](PLAN.md).
