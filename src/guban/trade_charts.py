"""One price-chart image per trade for report.html's trade viewer (Phase 16).

Each trade is drawn on the strategy bars around it, CONTEXT_BARS either side; windows longer than MAX_CANDLES bars
merge k bars per candle. Other trades inside the window are drawn faded. Gaps between sessions are closed up
(candles sit at consecutive x positions).

The parent cuts each trade's window out of the bars (trade_payloads: small numpy arrays) and renders the images on a
process pool (write_trade_charts); each worker writes its own files as <folder>/trade_<no>.png. Few trades render in
this process, since a pool's start-up costs more than it saves.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from guban.charts import COLORS

CONTEXT_BARS = 24
MAX_CANDLES = 240
WIDTH, HEIGHT, DPI = 10.0, 4.2, 120
PARALLEL_MIN = 64  # fewer trades render in this process
CHUNKS_PER_WORKER = 4
FOLDER = "trades"


def image_name(trade_no: int) -> str:
    return f"trade_{int(trade_no)}.png"


def trade_payloads(tr: pd.DataFrame, bars: pd.DataFrame, tick: float, nos=None) -> list[dict]:
    """Per trade (only trade numbers in `nos`, when given): its window's candles (o, h, l, c, close time as wall-clock
    minutes), the step (bars per candle), the trade itself and the other trades that start and end inside the window
    (candle positions, prices)."""
    n = len(bars)
    if not len(tr):
        return []
    wall = bars.index.tz_localize(None) if bars.index.tz is not None else bars.index
    minutes = wall.as_unit("s").asi8 // 60
    o, h, lo, c = (bars[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    eb, xb = tr["entry_bar"].to_numpy(int), tr["exit_bar"].to_numpy(int)
    side = np.where(tr["direction"].to_numpy(str) == "long", 1, -1)
    ep, xp, net = tr["entry_price"].to_numpy(float), tr["exit_price"].to_numpy(float), tr["net_pnl"].to_numpy(float)
    fmt = lambda t: t.strftime("%Y-%m-%d %H:%M")  # noqa: E731
    out = []
    keep = None if nos is None else {int(no) for no in nos}
    for k, r in enumerate(tr.itertuples(index=False)):
        if keep is not None and int(r.trade_no) not in keep:
            continue
        s, e =max(0, eb[k] - CONTEXT_BARS), min(n - 1, xb[k] + CONTEXT_BARS)
        step = max(1, math.ceil((e - s + 1) / MAX_CANDLES))
        cuts = np.arange(0, e - s + 1, step)
        last = np.r_[cuts[1:], e - s + 1] - 1
        sl = slice(s, e + 1)
        j0, j1 = np.searchsorted(eb, s), np.searchsorted(xb, e, side="right")  # trades inside [s, e]
        others = [(int(side[j]), int((eb[j] - s) // step), float(ep[j]), int((xb[j] - s) // step), float(xp[j]), float(net[j]))
                  for j in range(j0, j1) if j != k and eb[j] >= s and xb[j] <= e]  # fmt: skip
        out.append({
            "no": int(r.trade_no), "tick": tick, "step": step,
            "o": o[sl][cuts], "h": np.maximum.reduceat(h[sl], cuts), "l": np.minimum.reduceat(lo[sl], cuts),
            "c": c[sl][last], "t": minutes[sl][last],
            "trade": (int(side[k]), int((eb[k] - s) // step), float(ep[k]), int((xb[k] - s) // step), float(xp[k]), float(net[k])),
            "others": others,
            "title": f"Trade #{int(r.trade_no)} · {'Long' if side[k] > 0 else 'Short'} {int(r.qty)} · {fmt(r.entry_time)} {r.entry_name} @ "
                     f"{ep[k]:,.{_decimals(tick)}f} → {fmt(r.exit_time)} {r.exit_name} @ {xp[k]:,.{_decimals(tick)}f} · net {money_2(net[k])}"
                     + (f" · {step} bars per candle" if step > 1 else ""),
        })  # fmt: skip
    return out


def money_2(v: float) -> str:
    return f"{v:,.2f}".replace("-", "−")


def _decimals(tick: float) -> int:
    """Decimals that show every multiple of the tick exactly (0.25 -> 2, 5 -> 0)."""
    from decimal import Decimal

    return max(0, -Decimal(repr(float(tick))).normalize().as_tuple().exponent) if tick > 0 else 2


_CANVAS = None  # per process: (rc, fig, ax, wicks, bodies, span, font); figure set-up costs more than a trade's drawing


def _canvas():
    """One figure per process, reused for every trade. Its text (tick labels, title) is drawn by Pillow afterwards in
    the same font: matplotlib's text layout costs ~5 ms per label here, ~3/4 of an image's time (Phase 16)."""
    global _CANVAS
    if _CANVAS is None:
        import matplotlib
        from matplotlib import font_manager
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.collections import LineCollection, PolyCollection
        from matplotlib.figure import Figure
        from PIL import ImageFont

        from guban.charts import _RC

        with matplotlib.rc_context(_RC):
            fig = Figure(figsize=(WIDTH, HEIGHT), dpi=DPI)
            FigureCanvasAgg(fig)
            fig.subplots_adjust(left=0.085, right=0.99, top=0.91, bottom=0.12)
            ax = fig.add_subplot()
            ax.grid(axis="x", visible=False)
            ax.tick_params(labelleft=False, labelbottom=False)
            wicks = ax.add_collection(LineCollection([], linewidths=0.8, zorder=1), autolim=False)
            bodies = ax.add_collection(PolyCollection([], linewidths=0.6, zorder=2), autolim=False)
            span = ax.axvspan(0, 1, color=COLORS["s1"], alpha=0.07, lw=0, zorder=0)
            font_file = font_manager.findfont(font_manager.FontProperties(family=_RC["font.family"]))
        font = ImageFont.truetype(font_file, round(_RC["font.size"] * DPI / 72))
        _CANVAS = (_RC, fig, ax, wicks, bodies, span, font)
    return _CANVAS


def render_trade(p: dict, path: str | Path) -> Path:
    """Draw one trade payload (trade_payloads) and save it as a PNG."""
    import matplotlib
    from matplotlib.ticker import MaxNLocator
    from PIL import Image, ImageDraw

    rc, fig, ax, wicks, bodies, span, font = _canvas()
    with matplotlib.rc_context(rc):
        for a in ax.lines[:] + [c for c in ax.collections if c is not wicks and c is not bodies]:
            a.remove()  # the previous trade's markers
        o, h, lo, c = p["o"], p["h"], p["l"], p["c"]
        m = len(o)
        x = np.arange(m)
        col = np.where(c >= o, COLORS["up"], COLORS["down"])
        side, ei, epx, xi, xpx, net = p["trade"]
        span.set_x(ei - 0.5)
        span.set_width(xi - ei + 1)
        wicks.set_segments(np.stack([np.c_[x, lo], np.c_[x, h]], axis=1))
        wicks.set_color(col)
        b0, b1 = np.minimum(o, c), np.maximum(o, c)
        w = 0.32
        bodies.set_verts(np.stack([np.c_[x - w, b0], np.c_[x + w, b0], np.c_[x + w, b1], np.c_[x - w, b1]], axis=1))
        bodies.set_facecolor(col)
        bodies.set_edgecolor(col)
        for t, sel in [(t, False) for t in p["others"]] + [(p["trade"], True)]:
            sd, a, pa, b, pb, nt = t
            alpha, win = (1.0 if sel else 0.45), nt >= 0
            ax.plot([a, b], [pa, pb], color=COLORS["ink2"], lw=1.6 if sel else 0.9, ls="--", alpha=alpha, zorder=3)
            ax.scatter([a], [pa], marker="^" if sd > 0 else "v", s=90 if sel else 40, color=COLORS["ink"], edgecolors=COLORS["surface"],
                       linewidths=1.2, alpha=alpha, zorder=4)  # fmt: skip
            ax.scatter([b], [pb], marker="o", s=70 if sel else 30, facecolors=COLORS["good"] if win else "none",
                       edgecolors=COLORS["surface"] if win else COLORS["bad"], linewidths=1.5, alpha=alpha, zorder=4)  # fmt: skip
        ymin, ymax = min(lo.min(), epx, xpx), max(h.max(), epx, xpx)
        pad = (ymax - ymin) * 0.05 or p["tick"] * 4
        y0, y1 = ymin - pad, ymax + pad
        ax.set_ylim(y0, y1)
        ax.set_xlim(-1, m)
        yt = MaxNLocator(nbins=8, steps=[1, 2, 2.5, 5, 10]).tick_values(y0, y1)
        yt = yt[(yt >= y0) & (yt <= y1)]
        ax.set_yticks(yt)
        xt = np.unique(np.linspace(0, m - 1, min(m, 9)).round().astype(int))
        ax.set_xticks(xt)
        fig.canvas.draw()
        img = Image.fromarray(np.asarray(fig.canvas.buffer_rgba())[..., :3].copy())
    # text: matplotlib display coordinates are from the bottom left, Pillow's from the top left
    height, box = img.height, ax.bbox
    draw = ImageDraw.Draw(img)
    ink, ink2 = COLORS["ink"], COLORS["ink2"]
    dec = _decimals(p["tick"])
    for v, (_, py) in zip(yt, ax.transData.transform(np.c_[np.zeros(len(yt)), yt])):
        draw.text((box.x0 - 7, height - py), f"{v:,.{dec}f}".replace("-", "−"), fill=ink2, font=font, anchor="rm")
    t = pd.to_datetime(p["t"][xt], unit="m")
    days = t.normalize()
    line = font.size * 1.25
    for i, ((px, _), tt) in enumerate(zip(ax.transData.transform(np.c_[xt, np.full(len(xt), y0)]), t)):
        top = height - box.y0 + 6
        draw.text((px, top), f"{tt:%H:%M}", fill=ink2, font=font, anchor="ma")
        if i == 0 or days[i] != days[i - 1]:
            draw.text((px, top + line), f"{tt:%Y-%m-%d}", fill=ink2, font=font, anchor="ma")
    draw.text((box.x0, (height - box.y1) / 2), p["title"], fill=ink, font=font, anchor="lm")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")
    return path


def _render_chunk(folder: str, payloads: list[dict]) -> int:
    for p in payloads:
        render_trade(p, Path(folder) / image_name(p["no"]))
    return len(payloads)


def write_trade_charts(tr: pd.DataFrame, bars: pd.DataFrame, folder: str | Path, tick: float, workers: int | None = None, nos=None) -> int:
    """Render every trade's image into `folder` (old trade_*.png there are removed first), or with `nos` only those
    trades' (other images are kept); returns the image count."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    if nos is None:
        for old in folder.glob("trade_*.png"):
            old.unlink()
    payloads = trade_payloads(tr, bars, tick, nos)
    workers = workers or max(1, (os.cpu_count() or 2) - 1)
    if len(payloads) < PARALLEL_MIN or workers == 1:
        return _render_chunk(str(folder), payloads)
    k = max(1, math.ceil(len(payloads) / (workers * CHUNKS_PER_WORKER)))
    chunks = [payloads[i:i + k] for i in range(0, len(payloads), k)]
    with ProcessPoolExecutor(max_workers=min(workers, len(chunks))) as ex:
        return sum(ex.map(_render_chunk, [str(folder)] * len(chunks), chunks))
