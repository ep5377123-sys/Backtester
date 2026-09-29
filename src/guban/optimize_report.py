"""Self-contained HTML page for an optimizer run (S4): the ranked results table and a heatmap of a metric over
two inputs (a line over one input when only one varies). Same look and offline plotly.js as guban.report.

Heatmap cells hold the best value of the metric among the runs with that (x, y) pair that pass the min-trades
filter, over every other input in the grid; blank cells have no such run. The metric can be switched in the page.
Color: the better end of the scale stands out most from the background (darker in light mode, lighter in dark
mode). A metric with values on both sides of zero uses a diverging red-gray-blue scale
centred on 0.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

import numpy as np

from guban.optimize import METRICS, RESULT_METRICS, OptimizeResult, heatmap, varying
from guban.report import _CSS, _header, _json, _notes, _plotly_js, fmt_value

TOP_ROWS = 50


def write_optimize_report(opt: OptimizeResult, path: str | Path, meta: dict, x: str | None = None, y: str | None = None, top: int = TOP_ROWS) -> Path:
    """Write optimize.html. meta: {"title", "subtitle", "details": {label: value}, "inputs": fixed inputs, "notes"}.

    x / y: heatmap axes (default: the first two inputs with more than one value).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    x, y = heatmap_axes(opt.grid, x, y)
    data = None
    if x is not None:
        other = [k for k in varying(opt.grid) if k not in (x, y)]
        maps, combos = {}, set()
        for key, _, _, _ in RESULT_METRICS:
            h = heatmap(opt, x, y, key)
            maps[key] = {"z": _nulls(h["z"]), "trades": _nulls(h["trades"]), "combo": h["combo"].tolist()}
            combos.update(int(c) for c in h["combo"].ravel() if c)
        r = opt.results.set_index("combo")
        data = {
            "metrics": [{"key": k, "label": lbl, "kind": kind, "hib": hib} for k, lbl, kind, hib in RESULT_METRICS],
            "metric": opt.metric,
            "x": {"name": x, "values": [_label(v) for v in opt.grid[x]]},
            "y": None if y is None else {"name": y, "values": [_label(v) for v in opt.grid[y]]},
            "maps": maps,
            "others": {str(c): ", ".join(f"{k} = {_label(r.at[c, k])}" for k in other) for c in sorted(combos)} if other else {},
            "minTrades": opt.min_trades,
        }
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(meta.get('title', 'Optimization'))}</title>",
        f"<style>{_CSS}{_EXTRA_CSS}</style>",
        f"<script>{_plotly_js()}</script>" if data else "",
        "</head><body><main>",
        _header(meta),
        _tiles(opt),
        _chart_html(opt, x, y) if data else "",
        _ranked_table(opt, top),
        _notes(meta),
        "</main>",
        f"<script>const D = {_json(data)};</script><script>{_JS}</script>" if data else "",
        "</body></html>",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def heatmap_axes(grid: dict[str, list], x: str | None, y: str | None) -> tuple[str | None, str | None]:
    """Resolve the heatmap axes: given names are checked; missing ones default to the varying inputs in order."""
    for name in (x, y):
        if name is not None and name not in grid:
            raise ValueError(f"heatmap axis {name!r} is not a grid input; grid: {list(grid)}")
    if x is not None and x == y:
        raise ValueError("heatmap x and y must be different inputs")
    free = [k for k in varying(grid) if k not in (x, y)]
    if x is None and free:
        x = free.pop(0)
    if y is None and x is not None and free:
        y = free.pop(0)
    return x, y


def _nulls(a: np.ndarray) -> list:
    return [[None if not math.isfinite(v) else float(v) for v in row] for row in np.asarray(a, float)]


def _label(v) -> str:
    v = v.item() if isinstance(v, np.generic) else v
    return repr(v) if isinstance(v, float) else str(v)


def _tiles(opt: OptimizeResult) -> str:
    label, kind, _ = METRICS[opt.metric]
    r = opt.results
    best = r[r["rank"] == 1]
    tiles = [
        ("Combinations", f"{len(r):,}"),
        (f"≥ {opt.min_trades:,} trades", f"{int(r['rank'].notna().sum()):,}"),
        (f"Best {label.lower()}", fmt_value(_py(best[opt.metric].iat[0]), kind) if len(best) else "n/a"),
        ("Time", f"{opt.seconds:,.1f} s on {opt.workers} worker{'s' if opt.workers > 1 else ''}"),
    ]
    return "<section class='tiles'>" + "".join(f"<div class='tile'><div class='k'>{html.escape(k)}</div><div class='v'>{v}</div></div>" for k, v in tiles) + "</section>"


def _py(v):
    v = v.item() if isinstance(v, np.generic) else v
    return None if isinstance(v, float) and math.isnan(v) else v


def _chart_html(opt: OptimizeResult, x: str, y: str | None) -> str:
    other = [k for k in varying(opt.grid) if k not in (x, y)]
    what = f"{html.escape(y)} × {html.escape(x)}" if y else html.escape(x)
    cap = (
        f"Each cell: the best value over {', '.join(map(html.escape, other))}. " if other else ""
    ) + f"Blank: no run with at least {opt.min_trades:,} trades. The better end of the scale stands out most from the background; ○ marks the best cell."
    opts = "".join(f"<option value='{k}'{' selected' if k == opt.metric else ''}>{html.escape(lbl)}</option>" for k, lbl, _, _ in RESULT_METRICS)
    return (
        f"<section><h2>{'Heatmap' if y else 'Metric by input'}: {what}</h2>"
        f"<div class='controls'><label>Metric <select id='metric'>{opts}</select></label></div>"
        f"<div id='heat' class='chart{' tall' if y else ''}'></div><p class='cap'>{cap}</p></section>"
    )


def _ranked_table(opt: OptimizeResult, top: int) -> str:
    ranked = opt.ranked()
    shown = ranked[ranked["rank"].notna()].head(top)
    names = list(opt.grid)
    head = "<tr><th>Rank</th>" + "".join(f"<th class='in'>{html.escape(n)}</th>" for n in names)
    head += "".join(f"<th{' class=sortkey' if k == opt.metric else ''}>{html.escape(lbl)}</th>" for k, lbl, _, _ in RESULT_METRICS) + "</tr>"
    rows = []
    for _, row in shown.iterrows():
        cells = f"<td>{int(row['rank'])}</td>" + "".join(f"<td class='in'>{html.escape(_label(row[n]))}</td>" for n in names)
        cells += "".join(f"<td>{fmt_value(_py(row[k]), kind)}</td>" for k, _, kind, _ in RESULT_METRICS)
        rows.append(f"<tr{' class=best' if row['rank'] == 1 else ''}>{cells}</tr>")
    n_ranked, n_out = int(ranked["rank"].notna().sum()), int(ranked["rank"].isna().sum())
    label = METRICS[opt.metric][0]
    cap = f"Ranked by {label.lower()}, best first; top {len(shown):,} of {n_ranked:,}."
    if n_out:
        cap += f" {n_out:,} run(s) with fewer than {opt.min_trades:,} trades (or no {label.lower()}) are not ranked."
    cap += " Every run is in results.csv."
    body = f"<div class='wrap'><table class='ranked'>{head}{''.join(rows)}</table></div>" if rows else "<p class='muted'>No run passed the filter.</p>"
    return f"<section><h2>Ranked results</h2><p class='cap'>{cap}</p>{body}</section>"


_EXTRA_CSS = """
:root { --seq-lo:#cde2fb; --seq-hi:#104281; --div-neg:#e34948; --div-mid:#f0efec; --div-pos:#1c5cab; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --seq-lo:#0d366b; --seq-hi:#9ec5f4; --div-neg:#e66767; --div-mid:#383835; --div-pos:#5598e7; } }
:root[data-theme="dark"] { --seq-lo:#0d366b; --seq-hi:#9ec5f4; --div-neg:#e66767; --div-mid:#383835; --div-pos:#5598e7; }
.chart.tall { height:480px; }
table.ranked th.in, table.ranked td.in { color:var(--ink2); }
table.ranked th.sortkey { color:var(--ink); text-decoration:underline; text-underline-offset:3px; }
table.ranked tr.best td { background:var(--hl); font-weight:600; }
"""

_JS = r"""
(function () {
  const css = () => { const s = getComputedStyle(document.documentElement); const v = n => s.getPropertyValue(n).trim();
    return { ink: v('--ink'), ink2: v('--ink2'), muted: v('--muted'), grid: v('--grid'), axis: v('--axis'), surface: v('--surface'),
             s1: v('--s1'), lo: v('--seq-lo'), hi: v('--seq-hi'), neg: v('--div-neg'), mid: v('--div-mid'), pos: v('--div-pos') }; };
  const fmt = (v, kind) => {
    if (v === null || v === undefined) return 'n/a';
    const neg = v < 0 ? '−' : '', a = Math.abs(v);
    if (kind === 'usd') return neg + a.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (kind === 'pct') return neg + (a * 100).toFixed(1) + '%';
    if (kind === 'ratio') return neg + a.toFixed(2);
    return neg + a.toLocaleString('en-US');
  };
  const cfg = { displaylogo: false, responsive: true, modeBarButtonsToRemove: ['select2d', 'lasso2d', 'autoScale2d', 'toggleSpikelines'] };
  const sel = document.getElementById('metric');

  function draw() {
    const c = css(), m = D.metrics.find(m => m.key === sel.value), map = D.maps[m.key];
    const xs = D.x.values, ys = D.y ? D.y.values : null;
    const vals = map.z.flat().filter(v => v !== null);
    let best = null;
    map.z.forEach((row, iy) => row.forEach((v, ix) => { if (v !== null && (best === null || (m.hib ? v > best.v : v < best.v))) best = { v, ix, iy }; }));
    const tip = (iy, ix) => {
      const v = map.z[iy][ix]; if (v === null) return `No run with ≥ ${D.minTrades} trades`;
      const o = D.others[String(map.combo[iy][ix])];
      return `${m.label} ${fmt(v, m.kind)}<br>${fmt(map.trades[iy][ix], 'int')} trades` + (o ? `<br>best at ${o}` : '');
    };
    const L = {
      paper_bgcolor: c.surface, plot_bgcolor: c.surface, font: { family: 'system-ui, -apple-system, Segoe UI, sans-serif', size: 12, color: c.ink2 },
      margin: { l: 72, r: 16, t: 8, b: 56 }, showlegend: false,
      hoverlabel: { bgcolor: c.surface, bordercolor: c.axis, font: { color: c.ink } },
      xaxis: { type: 'category', title: { text: D.x.name }, linecolor: c.axis, gridcolor: c.grid, zeroline: false },
      yaxis: { linecolor: c.axis, gridcolor: c.grid, zerolinecolor: c.axis },
    };
    let traces;
    if (ys) {
      const lo = Math.min(...vals), hi = Math.max(...vals), div = lo < 0 && hi > 0;
      let scale = div ? [[0, c.neg], [0.5, c.mid], [1, c.pos]] : [[0, c.lo], [1, c.hi]];
      if (!div && !m.hib) scale = [[0, c.hi], [1, c.lo]];
      const text = map.z.map((row, iy) => row.map((_, ix) => tip(iy, ix)));
      traces = [{ type: 'heatmap', x: xs, y: ys, z: map.z, text, hovertemplate: `${D.x.name} = %{x}<br>${D.y.name} = %{y}<br>%{text}<extra></extra>`,
        colorscale: scale, zmid: div ? 0 : undefined, xgap: 2, ygap: 2, hoverongaps: true,
        colorbar: { outlinewidth: 0, thickness: 12, tickfont: { color: c.ink2 }, title: { text: m.label, side: 'right', font: { color: c.ink2 } } } }];
      if (best) traces.push({ type: 'scatter', mode: 'markers', x: [xs[best.ix]], y: [ys[best.iy]], hoverinfo: 'skip',
        marker: { symbol: 'circle-open', size: 16, color: c.ink, line: { width: 2, color: c.ink } } });
      L.yaxis = Object.assign(L.yaxis, { type: 'category', title: { text: D.y.name } });
      L.hovermode = 'closest';
    } else {
      const y = map.z[0], text = y.map((_, ix) => tip(0, ix));
      traces = [{ type: 'scatter', mode: 'lines+markers', x: xs, y, text, connectgaps: false, line: { color: c.s1, width: 2 },
        marker: { size: 8, color: c.s1, line: { color: c.surface, width: 2 } }, hovertemplate: `${D.x.name} = %{x}<br>%{text}<extra></extra>` }];
      if (best) traces.push({ type: 'scatter', mode: 'markers', x: [xs[best.ix]], y: [best.v], hoverinfo: 'skip',
        marker: { symbol: 'circle-open', size: 16, color: c.ink, line: { width: 2, color: c.ink } } });
      L.yaxis = Object.assign(L.yaxis, { title: { text: m.label }, tickformat: m.kind === 'pct' ? '.0%' : ',.2~f' });
      L.hovermode = 'x';
    }
    Plotly.react('heat', traces, L, cfg);
  }
  sel.onchange = draw;
  draw();
  window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', draw);
  new MutationObserver(draw).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
})();
"""
