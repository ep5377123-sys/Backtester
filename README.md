# Guban Backtester

A Python backtester for CME futures that reproduces MultiCharts (MC) results. You write a strategy in Python, backtest
it on multi-year 1-minute data, and get an MC-style performance report in the browser. You can then check it trade by
trade against MC's own trade list, optimize and walk-forward its inputs, and estimate prop-firm pass rates.

The Vault Break port matches MC on 99.3% of trades with −0.23% net-profit difference (PLAN.md, Phase 5).

## Contents
- [Install](#install)
- [The data you need](#the-data-you-need)
- [Walkthrough: import → run → compare → optimize → wfo → propfirm](#walkthrough)
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

### 1. Bar data: per-contract minute files from DeepCharts (dxFeed)

The bar history comes from **DeepCharts**, which gets its data from dxFeed. It does not come from MultiCharts. Copy
DeepCharts' per-contract store into this folder with this layout:

```
Database/<SYM>-CME/<YYYYMM contract>/Minute/<YYYYMMDD>.data     e.g. Database/NQ-CME/202609/Minute/20260524.data
```

- There is one binary file per contract per Globex session. The record format is documented in
  `src/guban/data/importer.py`.
- The `Tick/` folders next to `Minute/` are not used yet.
- To extend the history (for example, MNQ before June 2025), copy the extra contract folders in and re-run
  `guban import`. The importer rebuilds the whole continuous series.

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
| Continuous contract | Rolls on the Wednesday of expiration week | `guban import --roll expiry` |
| Bar Magnifier | On, 1 minute | `--fill-mode magnifier` (default) |
| Commission / slippage | $0.40 / $0.04 per contract per round trip | `--commission 0.40 --slippage 0.04` |
| Initial capital | $1,000,000 | `--capital 1000000` (default) |

## Walkthrough

These commands reproduce the Vault Break MNQ results from PLAN.md. They are written for PowerShell. `$vb` holds the flags
that `run`, `optimize` and `wfo` share: data, costs, and the inputs of the MC run.

```powershell
$vb = @('--symbol','MNQ','--history-from','NQ','--tf','5m','--session','eth','--tz','America/Chicago',
        '--commission','0.40','--slippage','0.04',
        '--set','take_profit_usd=500','--set','stop_loss_usd=500','--set','num_contracts=5',
        '--set','atr_seed_sum=171120','--set','atr_seed_count=620','--set','sess_count_seed=620')
```

### 1. import: build the 1-minute store

```powershell
uv run guban import Database/NQ-CME  --symbol NQ  --roll expiry
uv run guban import Database/MNQ-CME --symbol MNQ --roll expiry
```

- Writes `data/<SYM>/1m.parquet`: one unadjusted continuous series, in Eastern time.
- Prints the first and last bar, the bar count and the missing session days.
- `--roll expiry` switches contracts on the Wednesday of expiration week, like MC. `--roll volume` (the default) trades
  the contract that had the most volume in the previous session. The series is unadjusted either way, so `run` and
  `wfo` list any trade held across a roll (its P&L includes the gap between the contracts).
- Every `.data` file is checked while it is read (whole records, sane prices and volume, times inside the session);
  a bad file stops the import with its name and the record's byte offset.
- Optional: `guban bars --symbol NQ --tf 5m --session rth` builds and caches strategy bars and prints a per-session summary. `run` builds these caches on its own, and rebuilds them when `1m.parquet` or the session in `config/sessions.toml` changes.

### 2. run: backtest one set of inputs

```powershell
uv run guban run strategies/vault_break.py @vb --out reports/vault_break_mnq --open
```

- Writes `trades.csv` (MC trade-export layout plus guban's own columns), `metrics.json`, and a self-contained
  `report.html`. The report has KPIs, equity and drawdown, the performance summary, monthly net, a trade viewer and the
  trade list, and it opens offline.
- Expected result: 737 trades, net 30,891.70, max drawdown −7,315.00, about 3 s. The max drawdown is peak to valley
  along the intrabar equity path, so a rise and a fall inside one bar both count.
- `--history-from NQ` fills in NQ bars before MNQ's first bar, so long-memory indicators warm up. The `atr_seed_*`
  inputs stand in for the MC chart's pre-2023 history (PLAN.md, Phase 5).
- `--fill-mode ohlc` resolves exits with the OHLC path rule on the whole bar instead of 1-minute sub-bars.

### 3. compare: check parity against MC

```powershell
uv run guban compare --ours reports/vault_break_mnq/trades.csv `
    --mc "D:\Multi Charts\Performance Reports\CSV Data\Vault Break MNQ.csv" --contract MNQ --start 2025-06-08 --open
```

- Pairs trades that have the same direction and entries within one bar, one to one, pairing as many as possible
  (then the closest). Trade counts and net profit use exactly the same window on both sides. Checks prices within
  1 tick and net profit.
  Writes `diff.html`, listing every unmatched and price-mismatched trade, into the `--ours` folder (or into `--out`).
- **Pass** (the plan's S3 gate): ≥ 95% of trades matched, ≥ 95% of matched trades within 1 tick, net profit within 3%.
  Exit code 1 means FAIL.
- `--start` / `--end` are in MC chart time (`--mc-tz`, default America/Chicago). Here `--start` limits the check to the
  window with MNQ data. Expected: 300 of 302 trades matched, net −0.23%, PASS.

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
- Expected: 28 runs in about 15 s. The best is noise_mult 0.6 / vwap_exit_bars 4, net 32,769.50.

### 5. wfo: walk-forward the inputs

```powershell
uv run guban wfo strategies/vault_break.py --grid "noise_mult=0.4:1.0:0.1 vwap_exit_bars=1:4" @vb --open
```

- Optimizes each in-sample window and trades the winner in the next out-of-sample window. `--is 12 --oos 3 --step 3`
  (months) are the defaults; `--anchored` makes every in-sample window start at `--start` and grow.
- Every window is traded on its own: indicators warm up on the earlier data, the window starts flat, and a position
  still open at its end is closed at its last bar's close (exit "Window End"). So no price after a window reaches its
  score, and the stitched equity always equals the sum of the stitched trades.
- Writes the stitched out-of-sample `trades.csv`, `windows.csv`, `results.csv`, `wfo.json` and `wfo.html`, with
  walk-forward efficiency = annualized OOS net ÷ annualized IS net.
- Expected: 11 windows, 675 OOS trades, OOS net 19,777.90, WFE 71%, about 3 minutes on 6 cores.

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
- **Series:** `open high low close volume time date session_date timestamp contract`. `[0]` is the current bar and `[n]` is n
  bars ago. Looking back before bar 1 raises. `time` is HHMM in the chart time zone (`--tz`), like MC's `Time`.
- **Orders:** `buy` / `sell_short` open a position. `sell` / `buy_to_cover` close it (all contracts by default).
  `at="market"` (default), `"stop"` or `"limit"` with `price=`. Orders placed in `on_bar` are for the **next bar**,
  and they live for one bar. An opposite entry reverses the position. There is no pyramiding.
- **Exits:** `set_stop_loss(usd)` / `set_profit_target(usd)` are per-position dollars, as in MC. They are live from the
  entry fill onward and persist until set to `None`.
- **State:** `market_position` (−1/0/1), `current_contracts`, `entry_price`, `bars_since_entry`, `current_bar`
  (1-based).
- **Costs:** `--commission` / `--slippage` are USD per contract per round trip. Slippage is charged half per market or
  stop fill and none on limit fills (MC behavior).
- **Rules for `optimize` / `wfo`:** define the class at the top level of its file, because worker processes re-import
  it. Never look at future bars. `--set` and `--grid` values take the type of the input's default (`true`/`false`
  for a bool, a finite number for a number, text for a string).
- **Errors:** if the strategy's `__init__`, `on_start` or `on_bar` raises, the run stops, nothing is written, and
  guban prints where it failed (for `on_bar`: the bar, its timestamp and the position) and the failing line of your file.

`strategies/vault_break.py` is the reference port: a literal, line-by-line translation of `sc_Vaultc20Break.el`.

## Outputs, run folders and exit codes

`--out` is always a folder. Without it, each command writes to `reports/<run id>/`:

| Command | Default folder | Files |
|---|---|---|
| `run` | `reports/<strategy>_<symbol>_<tf>_<session>` | `trades.csv`, `metrics.json`, `report.html` |
| `compare` | the folder of `--ours` | `diff.html` |
| `optimize` | `reports/<strategy>_<symbol>_<tf>_<session>_opt` | `results.csv`, `optimize.json`, `optimize.html` |
| `wfo` | `reports/<strategy>_<symbol>_<tf>_<session>_wfo` | `trades.csv`, `windows.csv`, `results.csv`, `wfo.json`, `wfo.html` |
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

- `config/contracts.toml`: contract specs (point value, tick size). NQ, MNQ, ES and MES are included. Add a symbol
  here before you import or run it.
- `config/sessions.toml`: session templates. `rth` is 09:30–16:00 ET; `eth` is the 24-hour Globex session, 18:00–17:00
  ET. The timeframe must divide the session length.
- `config/propfirm/<rules>.toml`: prop-firm rules, costs, simulation settings and degradation scenarios.
  `apex_50k_eod` is copied from the notebook; review its values (see below).

## Known limits

- **MNQ history starts in June 2025**, so the parity window is 2025-06-08 → 2026-09-17. Before that, NQ bars stand in,
  and NQ prices are not MNQ prices. Full 2021–2026 parity needs older MNQ minute data from DeepCharts. It would also
  replace the fitted Vault Break ATR seed (`atr_seed_sum=171120`, fitted against MC's export).
- **Bar data differs slightly from MC's.** Guban's bars come from DeepCharts/dxFeed, and MC charts its own feed. The few
  trades that don't match in the parity window are 0.5–2.75-point price differences between the two data sources.
- **Prop-firm rules need review.** `apex_50k_eod` is labeled 50K but uses the notebook's $25,000 / $1,250 target /
  $1,000 drawdown values. The report's notes list this and the notebook's other quirks.
- **Out of scope for v1:** multi-symbol portfolios, tick-level fills, running `.el` files directly, and live trading.
  Other MC strategies get ported by hand, one at a time, and each one is done only when `guban compare` passes.

The full plan, the decisions behind it, and each phase's results are in [PLAN.md](PLAN.md).
