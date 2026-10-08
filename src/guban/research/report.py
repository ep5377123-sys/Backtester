"""Self-contained HTML page for a study: header, verdict cards, bar charts and tables. Same look as the backtest
report (guban.report CSS tokens, light only); charts are inline matplotlib PNGs (Phase 16, guban.charts), so the
file opens offline."""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd
from matplotlib.ticker import FuncFormatter

from guban.charts import COLORS, figure, img_tag, styled, to_data_uri
from guban.report import _CSS


def write_study_report(
    path: str | Path,
    meta: dict,
    verdicts: list[dict] | None = None,
    charts: list[dict] | None = None,
    tables: list[dict] | None = None,
    notes: list[str] | None = None,
) -> Path:
    """Write the page.

    meta: {"title", "subtitle", "details": {label: value}}.
    verdicts: [{"label", "real": bool, "headline": str, "checks": [{"rule", "passed", "detail"}]}].
    charts: [{"id", "title", "caption", "x": [...], "series": [{"name", "y": [...]}], "yformat": format spec, "ref": y}]
    drawn as grouped bars (a dashed line at `ref` when given; y None = no bar). yformat is a Python format spec for
    the y axis (",.2f" by default; "+.1f", ".0%", ...).
    tables: [{"title", "caption", "frame": DataFrame}], cells shown as given (format before passing); index shown.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    charts = charts or []
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(meta.get('title', 'Study'))}</title>",
        f"<style>{_CSS}{_EXTRA_CSS}</style>",
        "</head><body><main>",
        _header(meta),
        _verdicts(verdicts or []),
        *(_chart(c) for c in charts),
        *(_table(t) for t in tables or []),
        _notes(notes or []),
        "</main>",
        "</body></html>",
    ]
    path.write_text("\n".join(p for p in parts if p), encoding="utf-8")
    return path


def _header(meta: dict) -> str:
    details = meta.get("details") or {}
    dl = "".join(f"<div><dt>{html.escape(str(k))}</dt><dd>{html.escape(str(v))}</dd></div>" for k, v in details.items())
    return (
        f"<header><h1>{html.escape(meta.get('title', 'Study'))}</h1>"
        f"<div class='sub'>{html.escape(meta.get('subtitle', ''))}</div>" + (f"<dl class='meta'>{dl}</dl>" if dl else "") + "</header>"
    )


def _verdicts(verdicts: list[dict]) -> str:
    if not verdicts:
        return ""
    cards = []
    for v in verdicts:
        checks = "".join(
            f"<li class='{'ok' if c['passed'] else 'no'}'>{'✓' if c['passed'] else '✗'} {html.escape(c['rule'])}: "
            f"<span class='muted'>{html.escape(c['detail'])}</span></li>"
            for c in v.get("checks", [])
        )
        word = "REAL" if v["real"] else "NOT REAL"
        cards.append(
            f"<div class='verdict {'real' if v['real'] else 'not'}'><div class='k'>{html.escape(v['label'])}</div>"
            f"<div class='v'>{word}</div><div class='h'>{html.escape(v.get('headline', ''))}</div><ul>{checks}</ul></div>"
        )
    return "<section><h2>Verdict</h2><div class='verdicts'>" + "".join(cards) + "</div></section>"


def _chart(c: dict) -> str:
    return (
        f"<section><h2>{html.escape(c['title'])}</h2><p class='cap'>{html.escape(c.get('caption', ''))}</p>"
        f"{img_tag(_bars_png(c), c['title'], id=c['id'])}</section>"
    )


@styled
def _bars_png(c: dict) -> str:
    series = c["series"]
    colors = [COLORS["s1"], COLORS["muted"], COLORS["bad"], COLORS["good"]]
    pos = np.arange(len(c["x"]))
    width = 0.8 / max(1, len(series))
    fig, ax = figure(9.6, 3.0)
    for i, s in enumerate(series):
        y = np.array([np.nan if v is None else v for v in s["y"]], float)
        ax.bar(pos + (i - (len(series) - 1) / 2) * width, y, width=width * 0.92, color=colors[i % len(colors)], label=s["name"])
    if c.get("ref") is not None:
        ax.axhline(c["ref"], color=COLORS["ink2"], ls="--", lw=1)
    ax.axhline(0, color=COLORS["axis"], lw=0.8)
    ax.set_xticks(pos, labels=[str(x) for x in c["x"]])
    ax.grid(axis="x", visible=False)
    spec = c.get("yformat") or ",.2f"
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos=None: format(v, spec).replace("-", "−")))
    if len(series) > 1:
        ax.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncols=len(series))
    return to_data_uri(fig)


def _table(t: dict) -> str:
    df: pd.DataFrame = t["frame"]
    idx = df.index.name or ""
    head = f"<th class='l'>{html.escape(str(idx))}</th>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns)
    body = "".join(
        f"<tr><td class='l'>{html.escape(str(k))}</td>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
        for k, row in zip(df.index, df.itertuples(index=False))
    )
    return (
        f"<section><h2>{html.escape(t['title'])}</h2><p class='cap'>{html.escape(t.get('caption', ''))}</p>"
        f"<div class='wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></section>"
    )


def _notes(notes: list[str]) -> str:
    if not notes:
        return ""
    return "<section><h2>Notes</h2><ul class='notes'>" + "".join(f"<li>{html.escape(n)}</li>" for n in notes) + "</ul></section>"


_EXTRA_CSS = """
.verdicts { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr)); gap:12px; margin-top:8px; }
.verdict { border:1px solid var(--ring); border-radius:8px; padding:12px; }
.verdict .k { font-size:12px; color:var(--ink2); } .verdict .v { font-size:22px; font-weight:700; }
.verdict.real .v { color:var(--good); } .verdict.not .v { color:var(--bad); }
.verdict .h { margin:2px 0 6px; } .verdict ul { margin:0; padding-left:0; list-style:none; font-size:13px; }
.verdict li.ok { color:var(--good); } .verdict li.no { color:var(--bad); } .verdict li .muted { color:var(--ink2); }
"""
