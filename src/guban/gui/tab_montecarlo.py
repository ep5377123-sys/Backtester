"""Monte Carlo tab: two separate simulations of one trade list, the source.

- Trade Monte Carlo: `guban run`'s resample / reorder / whole-day Monte Carlo with EV and horizons (guban.montecarlo).
  With the Backtest source it is exactly the `monte_carlo` block the Backtest tab's Save writes (and `guban run`).
- Prop-firm evaluation: `guban propfirm` (guban.propfirm) with a rules file's settings, editable on the tab.

Sources: the Backtest tab's last run, the Walk Forward tab's stitched out-of-sample trades (written by the "opt"
worker to WALKFORWARD_DIR, see tab_walkforward), or a trades file: a guban trades.csv or an MC export (.csv/.xlsx).
For a file the trading day is the exit date in the chart time zone (the entry date when an MC CSV has no exits) and
the R figures need a risk value (an MC export has no stops).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from guban.gui.handlers import State, method

SOURCES = {"backtest": "Backtest run", "walkforward": "Walk-forward out of sample", "file": "Trades file"}
TMP = Path("reports") / "_gui_tmp"
WALKFORWARD_DIR = TMP / "walkforward"
TRADE_DEFAULTS = {"mc_sims": 10_000, "mc_seed": 42}
DEFAULT_TZ = "America/New_York"


@dataclass
class Source:
    """A trade list to simulate: engine trade columns (at least net_pnl, qty, direction, exit_time, risk_usd)."""

    kind: str
    label: str
    trades: pd.DataFrame
    info: dict
    tz: str
    trades_csv: Path | None = None


def _num(params: dict, key: str, kind=float, default=None, low=None):
    from guban.gui.tab_backtest import _num as num

    return num(params, key, kind, default, low)


def trade_settings(params: dict) -> dict:
    """The trade Monte Carlo's settings from a form (mc_sims, mc_seed, mc_dd_limit, mc_risk, mc_oos_from), checked."""
    from guban.montecarlo import check_settings

    s = {"sims": _num(params, "mc_sims", int, TRADE_DEFAULTS["mc_sims"], low=0),
         "seed": _num(params, "mc_seed", int, TRADE_DEFAULTS["mc_seed"]),
         "dd_limit": _num(params, "mc_dd_limit", float, None),
         "risk": _num(params, "mc_risk", float, None),
         "oos_from": str(params.get("mc_oos_from") or "").strip() or None}
    check_settings(s["sims"], s["seed"], s["dd_limit"], s["risk"], s["oos_from"])
    return s


def trade_monte_carlo(trades: pd.DataFrame, info: dict, s: dict, emit=None):
    """MonteCarloResult, {"skipped": reason} or None (sims = 0): what `guban run` computes for these trades."""
    from guban.montecarlo import MIN_TRADES, run_monte_carlo

    if not s["sims"]:
        return None
    if len(trades) < MIN_TRADES:
        return {"skipped": f"needs at least {MIN_TRADES} trades; this run has {len(trades)}"}
    if emit:
        emit("progress", message=f"Monte Carlo: {s['sims']:,} runs per method on {len(trades):,} trades…")
    return run_monte_carlo(trades["net_pnl"].to_numpy(float), s["sims"], s["seed"], s["dd_limit"], risk=s["risk"], oos_from=s["oos_from"], **info)


def _file_info(trades: pd.DataFrame, days) -> dict:
    return {"days": np.asarray(days), "contracts": trades["qty"].to_numpy(float), "sides": trades["direction"].to_numpy(str),
            "times": trades["exit_time"], "risks": trades["risk_usd"].to_numpy(float)}


def read_trades_file(path: Path, tz: str) -> pd.DataFrame:
    """A guban trades.csv (engine columns) or an MC export, as engine-style columns with exit_time in `tz`."""
    from guban.parity import load_mc_trades, load_our_trades

    head = pd.read_csv(path, nrows=0).columns if path.suffix.lower() == ".csv" else []
    if "exit_time" in head:
        tr = load_our_trades(path)
        tr["exit_time"] = tr["exit_time"].dt.tz_convert(tz)
    else:
        tr = load_mc_trades(path, tz=None)
        exits = tr["exit_time"].where(tr["exit_time"].notna(), tr["entry_time"])
        tr["exit_time"] = exits.dt.tz_localize(tz, ambiguous="NaT", nonexistent="shift_forward")
        tr["risk_usd"] = np.nan
    if tr.empty:
        raise ValueError(f"{path.name}: no trades")
    return tr


def source(state: State, params: dict) -> Source:
    """The trade list a Monte Carlo request names ({"source": kind, "file": path})."""
    from guban.cli import _mc_trade_info

    kind = str(params.get("source") or "backtest")
    if kind not in SOURCES:
        raise ValueError(f"unknown trade source {kind!r}; one of: {', '.join(SOURCES)}")
    tz = state.data.tz if state.data is not None else DEFAULT_TZ
    if kind == "backtest":
        b = state.backtest
        if b is None:
            raise ValueError("No backtest to simulate: run one on the Backtest tab, or pick another trade source")
        tr = b.a.result.trades
        return Source(kind, f"Backtest: {b.a.cls.__name__}, {len(tr):,} trades", tr, _mc_trade_info(tr, b.a.bars), tz)
    if kind == "walkforward":
        path = WALKFORWARD_DIR / "trades.csv"
        if state.walkforward is None or not path.is_file():
            raise ValueError("No walk-forward to simulate: run one on the Walk Forward tab, or pick another trade source")
        tr = read_trades_file(path, state.walkforward.get("tz") or tz)
        days = pd.read_csv(WALKFORWARD_DIR / "days.csv")["day"].to_numpy()
        info = _file_info(tr, pd.to_datetime(days).date)
        return Source(kind, f"Walk-forward: {state.walkforward.get('strategy', '')}, {len(tr):,} out-of-sample trades", tr, info,
                      tz, path)
    path = Path(str(params.get("file") or "").strip().strip('"'))
    if not str(path) or str(path) == "." or not path.is_file():
        raise FileNotFoundError(f"trades file {path} not found: pick a guban trades.csv or an MC export (.csv / .xlsx)")
    tr = read_trades_file(path, tz)
    if tr["exit_time"].isna().any():
        raise ValueError(f"{path.name}: some trade times fall in a daylight-saving change in {tz}; set the Data tab's time zone to the chart's")
    days = tr["exit_time"].dt.date.to_numpy()
    return Source(kind, f"File: {path.name}, {len(tr):,} trades", tr, _file_info(tr, days), tz, path)


def _span(tr: pd.DataFrame) -> str:
    if tr.empty:
        return "no trades"
    t = tr["exit_time"]
    return f"{len(tr):,} trades, exits {t.min():%Y-%m-%d} → {t.max():%Y-%m-%d}"


@method("mc.sources")
def mc_sources(state: State, params: dict, emit) -> dict:
    """Which sources hold trades now, for the selector."""
    have = {"backtest": state.backtest is not None,
            "walkforward": state.walkforward is not None and (WALKFORWARD_DIR / "trades.csv").is_file(), "file": True}
    return {"sources": [{"key": k, "label": label, "available": have[k]} for k, label in SOURCES.items()], "defaults": TRADE_DEFAULTS}


@method("mc.trade_run")
def mc_trade_run(state: State, params: dict, emit) -> dict:
    from guban.report import _monte_carlo_html

    s = trade_settings(params)
    if not s["sims"]:
        raise ValueError("Monte Carlo runs must be at least 1 here")
    src = source(state, params)
    t0 = time.perf_counter()
    mc = trade_monte_carlo(src.trades, src.info, s, emit)
    state.monte_carlo = {"trade": mc, "source": src.kind, "settings": s}
    return {"source": src.label, "span": _span(src.trades), "seconds": round(time.perf_counter() - t0, 2), "html": _monte_carlo_html(mc),
            "summary": mc if isinstance(mc, dict) else mc.summary()}


PROPFIRM_FIELDS = [
    ("rules", "name", "Name", "text"),
    ("rules", "account_size", "Account size ($, not simulated)", "number"),
    ("rules", "profit_target", "Profit target ($)", "number"),
    ("rules", "max_drawdown", "Max drawdown ($)", "number"),
    ("rules", "eod_trailing", "End-of-day trailing", "bool"),
    ("rules", "daily_loss_limit", "Daily loss limit ($, 0 = none)", "number"),
    ("rules", "min_trading_days", "Min trading days", "whole"),
    ("rules", "max_trading_days", "Max trading days", "whole"),
    ("costs", "commission", "Added commission ($ / contract / trade)", "number"),
    ("costs", "slippage", "Added slippage ($ / contract / trade)", "number"),
    ("costs", "risk_per_contract", "1R per contract ($)", "number"),
    ("simulation", "size", "Size (× the file's P&L)", "number"),
    ("simulation", "sims", "Simulations", "whole"),
    ("simulation", "sweep_sims", "Simulations per scenario / size", "whole"),
    ("simulation", "seed", "Seed", "whole"),
    ("simulation", "sweep", "Sizes to sweep", "list"),
    ("simulation", "paths", "Equity paths drawn", "whole"),
    ("simulation", "bootstrap_sims", "EV bootstrap runs", "whole"),
]


def _setup_value(setup, section: str, key: str):
    return getattr(setup.rules if section == "rules" else setup, key)


def _form_value(setup, section: str, key: str, kind: str) -> str:
    v = _setup_value(setup, section, key)
    if kind == "list":
        return ", ".join(f"{x:g}" for x in v)
    if kind == "bool":
        return "true" if v else "false"
    return f"{v:g}" if isinstance(v, float) else str(v)


def _parse(label: str, kind: str, raw):
    text = str(raw).strip() if raw is not None else ""
    try:
        if kind == "text":
            return text
        if kind == "bool":
            if text.lower() not in ("true", "false"):
                raise ValueError
            return text.lower() == "true"
        if kind == "list":
            return tuple(float(x) for x in text.replace(",", " ").split())
        v = float(text)
        if kind == "whole":
            if not v.is_integer():
                raise ValueError
            return int(v)
        return v
    except ValueError:
        want = {"bool": "true or false", "list": "numbers separated by commas", "whole": "a whole number"}.get(kind, "a number")
        raise ValueError(f"{label} must be {want}, got {raw!r}") from None


def propfirm_setup(params: dict):
    """(Setup, edited keys): the rules file named by params["rules"] with the form's values (params["fields"])."""
    from dataclasses import replace

    from guban.propfirm import load_setup

    base = load_setup(str(params.get("rules") or "apex_50k_eod"))
    fields = params.get("fields") or {}
    rules, other, edited = {}, {}, []
    for section, key, label, kind in PROPFIRM_FIELDS:
        if key not in fields:
            continue
        v = _parse(label, kind, fields[key])
        if v != _setup_value(base, section, key):
            edited.append(key)
        (rules if section == "rules" else other)[key] = v
    return replace(base, rules=replace(base.rules, **rules), **other), edited


@method("mc.propfirm_rules")
def mc_propfirm_rules(state: State, params: dict, emit) -> dict:
    """The rules files in config/propfirm/ and one file's values for the form."""
    from guban.config import CONFIG_DIR
    from guban.propfirm import load_setup

    names = sorted(p.stem for p in (CONFIG_DIR / "propfirm").glob("*.toml"))
    if not names:
        raise FileNotFoundError(f"no prop-firm rules files in {CONFIG_DIR / 'propfirm'}")
    name = str(params.get("rules") or "")
    name = name if name in names else ("apex_50k_eod" if "apex_50k_eod" in names else names[0])
    setup = load_setup(name)
    return {
        "rules": names,
        "selected": name,
        "fields": [{"section": sec, "key": key, "label": label, "kind": kind, "value": _form_value(setup, sec, key, kind)}
                   for sec, key, label, kind in PROPFIRM_FIELDS],
        "scenarios": [f"{s.label}: win rate {s.win_rate_adj:+.0%}, P&L ×{1 - s.pnl_shrink:g}" for s in setup.scenarios],
        "notes": list(setup.notes),
    }


def _trades_csv(state: State, src: Source) -> tuple[Path, str]:
    """A trades file of the source (what `guban propfirm --trades` reads) and a folder stem for Save."""
    from guban.cli import _slug

    if src.kind == "backtest":
        from guban.gui.tab_backtest import run_folder

        b = state.backtest
        path = b.a.result.write_trades_csv(TMP / "backtest" / "trades.csv", b.capital)
        return path, run_folder(b, state.data).name
    if src.kind == "walkforward":
        return src.trades_csv, _slug(f"{state.walkforward.get('strategy', 'walk_forward')}_wfo")
    p = src.trades_csv
    return p, _slug(p.parent.name if p.stem == "trades" else p.stem)


@method("mc.propfirm_run")
def mc_propfirm_run(state: State, params: dict, emit) -> dict:
    from guban.propfirm import load_trades, run_propfirm
    from guban.propfirm.report import propfirm_body

    setup, edited = propfirm_setup(params)
    src = source(state, params)
    path, stem = _trades_csv(state, src)
    t0 = time.perf_counter()
    res = run_propfirm(load_trades(path), setup, progress=lambda msg: emit("progress", message=msg))
    if state.monte_carlo is None:
        state.monte_carlo = {}
    state.monte_carlo["propfirm"] = {"res": res, "trades": path, "stem": stem, "edited": edited}
    m, s = res.main.summary(), res.summary()
    return {"source": src.label, "span": _span(src.trades), "seconds": round(time.perf_counter() - t0, 2), "html": propfirm_body(res),
            "pass_rate": m["pass_rate"], "verdict": s["verdict"], "edited": edited}


@method("mc.propfirm_save")
def mc_propfirm_save(state: State, params: dict, emit) -> dict:
    """The folder `guban propfirm --trades <the source's trades.csv>` writes: daily, degradation, sweep, json, html."""
    from guban.charts import theme
    from guban.cli import REPORTS, write_propfirm_folder

    pf = (state.monte_carlo or {}).get("propfirm")
    if pf is None:
        raise ValueError("Nothing to save: run the prop-firm evaluation first")
    res = pf["res"]
    out = Path(params["out"]) if params.get("out") else REPORTS / f"{pf['stem']}_{res.setup.key}"
    edited = f"edited in Guban Studio: {', '.join(pf['edited'])}" if pf["edited"] else None
    with theme("light"):
        html_path = write_propfirm_folder(res, Path(pf["trades"]), out, edited)
    files = [out / n for n in ("propfirm.json", "sweep.csv", "degradation.csv", "daily.csv")] + [html_path]
    return {"folder": str(out), "files": [str(f) for f in files], "report": str(html_path.resolve())}
