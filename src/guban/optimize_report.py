"""Self-contained HTML page for an optimizer run (S4): the ranked results table and a heatmap of a metric over
two inputs (a line over one input when only one varies). Same look as guban.report; charts are matplotlib images
(Phase 16, guban.charts), one per metric, and the metric menu switches between them.

Heatmap cells hold the best value of the metric among the runs with that (x, y) pair that pass the min-trades
filter, over every other input in the grid; blank cells have no such run. Small grids print the value in each cell.
Color: the better end of the scale is the darker one. A metric with values on both sides of zero uses a diverging
red-gray-blue scale symmetric around 0.
"""

from __future__ import annotations

import html
import math
from pathlib import Path

import numpy as np

from guban.charts import COLORS, figure, img_tag, styled, to_data_uri, value_formatter
from guban.optimize import METRICS, RESULT_METRICS, OptimizeResult, heatmap, varying
from guban.report import _CSS, _header, _notes, fmt_value

TOP_ROWS = 50
MAX_TICKS = 24
ANNOTATE_CELLS = 80
SEQ = ("#cde2fb", "#104281")
DIV = ("#e34948", "#f0efec", "#1c5cab")


def write_optimize_report(opt: OptimizeResult, path: str | Path, meta: dict, x: str | None = None, y: str | None = None, top: int = TOP_ROWS) -> Path:
    """Write optimize.html. meta: {"title", "subtitle", "details": {label: value}, "inputs": fixed inputs, "notes"}.

    x / y: heatmap axes (default: the first two inputs with more than one value).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    x, y = heatmap_axes(opt.grid, x, y)
    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>{html.escape(meta.get('title', 'Optimization'))}</title>",
        f"<style>{_CSS}{_EXTRA_CSS}</style>",
        "</head><body><main>",
        _header(meta),
        _tiles(opt),
        _chart_html(opt, x, y) if x is not None else "",
        _ranked_table(opt, top),
        _notes(meta),
        "</main>",
        f"<script>{_JS}</script>" if x is not None else "",
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
        f"Each cell: the best value over {', '.join(map(html.escape, other))} (results.csv has every run). " if other else ""
    ) + f"Blank: no run with at least {opt.min_trades:,} trades. The better end of the scale is the darker one; ○ marks the best cell."
    opts = "".join(f"<option value='{k}'{' selected' if k == opt.metric else ''}>{html.escape(lbl)}</option>" for k, lbl, _, _ in RESULT_METRICS)
    xs = [_label(v) for v in opt.grid[x]]
    ys = None if y is None else [_label(v) for v in opt.grid[y]]
    imgs = []
    for key, label, kind, hib in RESULT_METRICS:
        z = heatmap(opt, x, y, key)["z"]
        uri = _heatmap_png(z, xs, ys, x, y, label, kind, hib) if y else _line_png(z[0], xs, x, label, kind, hib)
        attrs = {"data_metric": key} if key == opt.metric else {"data_metric": key, "hidden": "hidden"}
        imgs.append(img_tag(uri, f"{label} by {what}", **attrs))
    return (
        f"<section><h2>{'Heatmap' if y else 'Metric by input'}: {what}</h2>"
        f"<div class='controls'><label>Metric <select id='metric'>{opts}</select></label></div>"
        f"<div id='heat'>{''.join(imgs)}</div><p class='cap'>{cap}</p></section>"
    )


def _best(z: np.ndarray, hib: bool) -> tuple[int, int] | None:
    """(row, col) of the best finite cell, the first one in row-major order on ties (as guban.optimize.heatmap)."""
    if not np.isfinite(z).any():
        return None
    filled = np.where(np.isfinite(z), z, -np.inf if hib else np.inf)
    return np.unravel_index(int(np.argmax(filled) if hib else np.argmin(filled)), z.shape)


def _ticks(ax_set, labels: list[str]) -> None:
    step = max(1, math.ceil(len(labels) / MAX_TICKS))
    idx = np.arange(0, len(labels), step)
    ax_set(idx + 0.5, labels=[labels[i] for i in idx])


def _cell(v: float, kind: str) -> str:
    s = {"usd": f"{v:,.0f}", "pct": f"{v:.1%}", "ratio": f"{v:.2f}", "num": f"{v:,.1f}"}.get(kind, f"{v:,.0f}")
    return s.replace("-", "−")


@styled
def _heatmap_png(z: np.ndarray, xs: list[str], ys: list[str], x: str, y: str, label: str, kind: str, hib: bool) -> str:
    from matplotlib.colors import LinearSegmentedColormap, Normalize

    ny, nx = z.shape
    fig, ax = figure(9.6, min(6.5, max(2.8, 0.32 * ny + 1.3)))
    vals = z[np.isfinite(z)]
    lo, hi = (float(vals.min()), float(vals.max())) if len(vals) else (0.0, 1.0)
    if lo < 0 < hi:
        m = max(-lo, hi)
        cmap, norm = LinearSegmentedColormap.from_list("div", DIV), Normalize(-m, m)
    else:
        cmap, norm = LinearSegmentedColormap.from_list("seq", SEQ if hib else SEQ[::-1]), Normalize(lo, hi)
    cmap = cmap.with_extremes(bad=COLORS["surface"])
    mesh = ax.pcolormesh(np.ma.masked_invalid(z), cmap=cmap, norm=norm, edgecolors=COLORS["surface"], linewidth=1.5)
    ax.grid(False)
    for s in ax.spines.values():
        s.set_visible(False)
    ax.tick_params(length=0)
    _ticks(ax.set_xticks, xs)
    _ticks(ax.set_yticks, ys)
    ax.set_xlabel(x)
    ax.set_ylabel(y)
    if nx * ny <= ANNOTATE_CELLS:
        for (iy, ix), v in np.ndenumerate(z):
            if math.isfinite(v):
                r, g, b, _ = cmap(norm(v))
                dark = 0.299 * r + 0.587 * g + 0.114 * b < 0.55
                ax.text(ix + 0.5, iy + 0.5, _cell(v, kind), ha="center", va="center", fontsize=7, color="white" if dark else COLORS["ink"])
    best = _best(z, hib)
    if best is not None:
        ax.scatter([best[1] + 0.5], [best[0] + 0.5], s=320, facecolors="none", edgecolors=COLORS["ink"], linewidths=2)
    cb = fig.colorbar(mesh, ax=ax, pad=0.01, fraction=0.035, format=value_formatter(kind))
    cb.outline.set_visible(False)
    if lo == hi:
        cb.set_ticks([lo])
    cb.set_label(label)
    return to_data_uri(fig)


@styled
def _line_png(z: np.ndarray, xs: list[str], x: str, label: str, kind: str, hib: bool) -> str:
    fig, ax = figure(9.6, 3.2)
    xi = np.arange(len(xs))
    ax.plot(xi, z, color=COLORS["s1"], lw=2, marker="o", ms=6, mec=COLORS["surface"], mew=1.5)
    best = _best(z[None, :], hib)
    if best is not None:
        ax.scatter([best[1]], [z[best[1]]], s=220, facecolors="none", edgecolors=COLORS["ink"], linewidths=2, zorder=3)
    _ticks(lambda pos, labels: ax.set_xticks(pos - 0.5, labels=labels), xs)
    ax.set_xlabel(x)
    ax.set_ylabel(label)
    ax.yaxis.set_major_formatter(value_formatter(kind))
    return to_data_uri(fig)


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
table.ranked th.in, table.ranked td.in { color:var(--ink2); }
table.ranked th.sortkey { color:var(--ink); text-decoration:underline; text-underline-offset:3px; }
table.ranked tr.best td { background:var(--hl); font-weight:600; }
"""

_JS = r"""
(function () {
  const sel = document.getElementById('metric');
  const show = () => document.querySelectorAll('#heat img').forEach(i => { i.hidden = i.dataset.metric !== sel.value; });
  sel.onchange = show;
  show();
})();
"""
