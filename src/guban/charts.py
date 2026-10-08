"""Shared matplotlib chart helpers for every guban report (Phase 16).

One look for all reports: the colours are the reports' light CSS tokens (guban.report._CSS), so a chart image sits on
its page without a seam. Figures are plain matplotlib Figures on the Agg canvas (no pyplot state, no display), which
makes them safe to draw in worker processes.

Page charts are embedded as PNG data URIs (to_data_uri), so report.html still opens offline from one file; the trade
images of the price viewer are files next to the report (save_png, guban.report).
"""

from __future__ import annotations

import base64
import html
import io
from pathlib import Path

import matplotlib
import numpy as np

matplotlib.use("Agg")  # no display: reports are written from the CLI and worker processes

from matplotlib.figure import Figure  # noqa: E402
from matplotlib.ticker import Formatter, FuncFormatter  # noqa: E402

DPI = 150
COLORS = {
    "page": "#f9f9f7", "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7", "s1": "#2a78d6", "up": "#2a78d6", "down": "#e34948",
    "good": "#0ca30c", "bad": "#d03b3b", "s2": "#e8a33d", "s3": "#7a5ccf", "s4": "#2fa59a",
}  # fmt: skip
SERIES = [COLORS["s1"], COLORS["s2"], COLORS["s3"], COLORS["s4"], COLORS["bad"], COLORS["muted"]]
_RC = {
    "font.family": ["Segoe UI", "DejaVu Sans", "sans-serif"],
    "font.size": 9,
    "text.color": COLORS["ink2"],
    "axes.facecolor": COLORS["surface"],
    "axes.edgecolor": COLORS["axis"],
    "axes.labelcolor": COLORS["ink2"],
    "axes.titlesize": 10,
    "axes.titlecolor": COLORS["ink"],
    "axes.grid": True,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.axisbelow": True,
    "grid.color": COLORS["grid"],
    "grid.linewidth": 0.6,
    "xtick.color": COLORS["ink2"],
    "ytick.color": COLORS["ink2"],
    "figure.facecolor": COLORS["surface"],
    "legend.frameon": False,
    "legend.fontsize": 8,
    "axes.unicode_minus": True,
}


def figure(width: float = 9.0, height: float = 3.2, rows: int = 1, cols: int = 1, **kw):
    """(Figure, axes) in the report style; axes is one Axes or an array of them (rows x cols > 1)."""
    with matplotlib.rc_context(_RC):
        fig = Figure(figsize=(width, height), layout="constrained")
        axes = fig.subplots(rows, cols, squeeze=True, **kw)
    return fig, axes


def styled(fn):
    """Run `fn` (which draws on axes made by figure()) under the report style, so text added later matches."""

    def wrapper(*a, **kw):
        with matplotlib.rc_context(_RC):
            return fn(*a, **kw)

    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


def money(v: float, _pos=None) -> str:
    """Axis label: whole dollars with thousands separators and a real minus sign."""
    s = f"{v:,.0f}"
    return s.replace("-", "−") if s not in ("-0", "−0") else "0"


MONEY = FuncFormatter(money)


class _ValueFormatter(Formatter):
    """Axis labels for a report value kind; usd and pct get as many decimals as the tick spacing needs."""

    def __init__(self, kind: str):
        self.kind = kind
        self.step = 1.0  # tick spacing, set by format_ticks (Formatter.locs is deprecated in matplotlib 3.11)

    def format_ticks(self, values) -> list[str]:
        v = np.asarray(values, float)
        self.step = float(np.min(np.diff(v))) if len(v) > 1 else 1.0
        return [self(x, i) for i, x in enumerate(values)]

    def __call__(self, v, pos=None) -> str:
        step = self.step
        places = lambda s: next((d for d in range(4) if abs(round(s, d) - s) < 1e-9 * max(1.0, abs(s))), 4)  # noqa: E731
        if self.kind == "usd":
            d = places(step)
            s = f"{v:,.{d}f}"
        elif self.kind == "pct":
            d = places(step * 100)
            s = f"{v:.{d}%}"
        elif self.kind == "ratio":
            s = f"{v:.2f}"
        elif self.kind == "int":
            s = f"{v:,.0f}"
        else:
            s = f"{v:,.4g}"
        return "0" + s[2:] if s.startswith("-0") and not any(c in "123456789" for c in s) else s.replace("-", "−")


def value_formatter(kind: str) -> Formatter:
    """Axis labels for a guban.report.fmt_value kind: usd, pct (a fraction), ratio, int, num. One per axis."""
    return _ValueFormatter(kind)


def _png(fig: Figure, dpi: int) -> bytes:
    buf = io.BytesIO()
    with matplotlib.rc_context(_RC):
        fig.savefig(buf, format="png", dpi=dpi, facecolor=fig.get_facecolor())
    return buf.getvalue()


def to_data_uri(fig: Figure, dpi: int = DPI) -> str:
    """The figure as an inline PNG (data:image/png;base64,...)."""
    return "data:image/png;base64," + base64.b64encode(_png(fig, dpi)).decode("ascii")


def save_png(fig: Figure, path: str | Path, dpi: int = DPI) -> Path:
    """Write the figure as a PNG file (parents created); returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_png(fig, dpi))
    return path


def img_tag(src: str, alt: str, cls: str = "chart", **attrs) -> str:
    """An <img> for a chart: `src` a data URI or a relative path."""
    extra = "".join(f" {k.replace('_', '-')}='{html.escape(str(v), quote=True)}'" for k, v in attrs.items())
    return f"<img class='{cls}' src='{html.escape(src, quote=True)}' alt='{html.escape(alt, quote=True)}'{extra}>"
