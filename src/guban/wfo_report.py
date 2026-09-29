"""Walk-forward report (S5): the standard backtest report of the stitched out-of-sample result (guban.report),
with a walk-forward section first (efficiency, per-window chosen inputs and profits) and the out-of-sample windows
marked on the equity and drawdown charts."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from guban.optimize import METRICS, varying
from guban.report import fmt_value, write_report
from guban.wfo import WalkForwardResult


def write_wfo_report(wf: WalkForwardResult, path: str | Path, meta: dict) -> Path:
    """Write wfo.html. meta: as guban.report.write_report."""
    bands = []
    for w in wf.windows:
        mid = w.oos_start + (w.oos_end - w.oos_start) / 2
        bands.append({"x0": f"{w.oos_start:%Y-%m-%d}", "x1": f"{w.oos_end:%Y-%m-%d}", "xm": f"{mid:%Y-%m-%d}", "label": f"W{w.no}"})
    return write_report(wf.oos_result(), wf.bars, path, wf.metrics, meta, sections=[wfo_section(wf)], bands=bands)


def _d(t) -> str:
    return f"{pd.Timestamp(t):%Y-%m-%d}"


def _v(v, kind: str) -> str:
    return "n/a" if v is None or pd.isna(v) else fmt_value(v.item() if hasattr(v, "item") else v, kind)


def wfo_section(wf: WalkForwardResult) -> str:
    s, p = wf.summary, wf.pooled
    traded = s[s["combo"].notna()]
    label, kind, _ = METRICS[wf.metric]
    step = f"{wf.step_months} month{'s' if wf.step_months != 1 else ''}"
    if wf.anchored:
        layout = (f"Anchored: every in-sample window starts on {_d(wf.windows[0].is_start)}, the first spans {wf.is_months} months "
                  f"and each next one {step} more; {wf.oos_months}-month out-of-sample windows")  # fmt: skip
    else:
        layout = f"Rolling: {wf.is_months}-month in-sample, {wf.oos_months}-month out-of-sample windows, stepped {step}"
    cap = (
        f"{html.escape(layout)}. Each in-sample window picks the inputs with the best {html.escape(label.lower())} among the "
        f"{wf.combinations:,} combinations with at least {wf.min_trades:,} trades in the window; those inputs trade the next "
        "out-of-sample window. Every window is traded on its own: it starts flat (indicators warmed on the earlier data) "
        "and a position still open at its end is closed at its last bar's close (exit “Window End”), so no later price "
        "reaches its result. Walk-forward efficiency (WFE) = annualized out-of-sample net profit ÷ annualized in-sample "
        "net profit of the chosen inputs. OOS rank: where the chosen inputs ranked out of sample among all combinations. "
        "Everything below this section is the stitched out-of-sample result."
    )
    skipped = len(s) - len(traded)
    tiles = [
        ("Walk-forward efficiency", _v(p["wfe"], "pct")),
        ("OOS net profit / year", _v(p["oos_net_per_year"], "usd")),
        ("IS net profit / year", _v(p["is_net_per_year"], "usd")),
        ("Profitable OOS windows", f"{int((traded['oos_net_profit'] > 0).sum())} of {len(traded)}"),
        ("Windows", f"{len(s)}" + (f" ({skipped} sat out)" if skipped else "")),
    ]
    tile_html = "<div class='tiles'>" + "".join(f"<div class='tile'><div class='k'>{k}</div><div class='v'>{v}</div></div>" for k, v in tiles) + "</div>"

    names = varying(wf.grid) or list(wf.grid)
    show_metric = wf.metric != "net_profit"
    head = ["Window", "In-sample", "Out-of-sample", *names]
    head += [f"IS {label.lower()}"] if show_metric else []
    head += ["IS net / year", "OOS net", "OOS net / year", "OOS trades", "WFE", "OOS rank"]
    left = {0, 1, 2}
    ths = "".join(f"<th{' class=l' if i in left else ''}>{html.escape(h)}</th>" for i, h in enumerate(head))
    rows = []
    for _, r in s.iterrows():
        oos = f"{_d(r['oos_from'])} → {_d(r['oos_to'])}" + (" (partial)" if r["partial"] else "")
        cells = [f"W{r['window']}", f"{_d(r['is_from'])} → {_d(r['is_to'])}", oos]
        if pd.isna(r["combo"]):
            n_cols = len(head) - 3
            rows.append("<tr>" + "".join(f"<td class=l>{html.escape(c)}</td>" for c in cells)
                        + f"<td class='l muted' colspan='{n_cols}'>No combination with ≥ {wf.min_trades:,} in-sample trades; sat out.</td></tr>")  # fmt: skip
            continue
        vals = [html.escape(_label(r[n])) for n in names]
        vals += [_v(r["is_metric"], kind)] if show_metric else []
        vals += [
            _v(r["is_net_per_year"], "usd"), _v(r["oos_net_profit"], "usd"), _v(r["oos_net_per_year"], "usd"),
            _v(r["oos_trades"], "int"), _v(r["wfe"], "pct"),
            f"{int(r['oos_rank'])} / {wf.combinations:,}" if not pd.isna(r["oos_rank"]) else "n/a",
        ]  # fmt: skip
        rows.append("<tr>" + "".join(f"<td class=l>{html.escape(c)}</td>" for c in cells) + "".join(f"<td>{v}</td>" for v in vals) + "</tr>")
    table = f"<div class='wrap'><table class='windows'><tr>{ths}</tr>{''.join(rows)}</table></div>"
    return f"<section><h2>Walk-forward</h2><p class='cap'>{cap}</p>{tile_html}{table}</section>"


def _label(v) -> str:
    v = v.item() if hasattr(v, "item") else v
    return repr(v) if isinstance(v, float) else str(v)
