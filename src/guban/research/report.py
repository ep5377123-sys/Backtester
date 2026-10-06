"""Self-contained HTML page for a study: header, verdict cards, bar charts and tables. Same look as the backtest
report (guban.report: CSS tokens, light/dark, inlined plotly.js, so the file opens offline)."""

from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from guban.report import _CSS, _json, _plotly_js


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
    charts: [{"id", "title", "caption", "x": [...], "series": [{"name", "y": [...]}], "yformat": d3 format, "ref": y}]
    drawn as grouped bars (a dashed line at `ref` when given).
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
        f"<script>{_plotly_js()}</script>" if charts else "",
        "</head><body><main>",
        _header(meta),
        _verdicts(verdicts or []),
        *(_chart(c) for c in charts),
        *(_table(t) for t in tables or []),
        _notes(notes or []),
        "</main>",
        f"<script>const C = {_json(charts)};</script><script>{_JS}</script>" if charts else "",
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
        f"<div id='{html.escape(c['id'])}' class='chart'></div></section>"
    )


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

_JS = r"""
(function () {
  const css = () => { const s = getComputedStyle(document.documentElement); const v = n => s.getPropertyValue(n).trim();
    return { ink: v('--ink'), ink2: v('--ink2'), muted: v('--muted'), grid: v('--grid'), axis: v('--axis'), surface: v('--surface'),
             s1: v('--s1'), bad: v('--bad'), good: v('--good') }; };
  const cfg = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d', 'toggleSpikelines'] };
  function draw() {
    const c = css(), colors = [c.s1, c.muted, c.bad, c.good];
    for (const ch of C) {
      const traces = ch.series.map((s, i) => ({ type: 'bar', name: s.name, x: ch.x, y: s.y, marker: { color: colors[i % colors.length] },
        hovertemplate: `${s.name}<br>%{x}: %{y:${ch.yformat || ',.2f'}}<extra></extra>` }));
      const shapes = ch.ref == null ? [] : [{ type: 'line', xref: 'paper', x0: 0, x1: 1, y0: ch.ref, y1: ch.ref, line: { color: c.ink2, width: 1, dash: 'dash' } }];
      Plotly.react(ch.id, traces, {
        paper_bgcolor: c.surface, plot_bgcolor: c.surface, font: { family: 'system-ui, -apple-system, Segoe UI, sans-serif', size: 12, color: c.ink2 },
        margin: { l: 56, r: 16, t: 8, b: 36 }, barmode: 'group', showlegend: ch.series.length > 1, legend: { orientation: 'h', y: 1.12 },
        hoverlabel: { bgcolor: c.surface, bordercolor: c.axis, font: { color: c.ink } }, shapes,
        xaxis: { gridcolor: c.grid, linecolor: c.axis, type: 'category' },
        yaxis: { gridcolor: c.grid, linecolor: c.axis, zerolinecolor: c.axis, tickformat: ch.yformat || ',.2f' } }, cfg);
    }
  }
  draw();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
  new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
"""
