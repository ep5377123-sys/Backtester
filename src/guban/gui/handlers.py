"""Guban Studio actions: one function per worker method, registered in METHODS by name.

A handler takes (state, params, emit): State is the worker's memory between requests, params the request's JSON
object, emit(event, **data) sends a progress line. It returns a JSON-able result or raises one of the CLI's expected
error types (ValueError, KeyError, OSError, RuntimeError) with a message that names the fix.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Any

METHODS: dict[str, Callable] = {}


def method(name: str):
    def register(fn):
        METHODS[name] = fn
        return fn

    return register


@dataclass
class State:
    """What the worker remembers between requests: each tab's inputs and results, later tabs built on earlier ones."""

    data: Any = None  # tab_data.Loaded: bars, windows, dataset
    strategy: Any = None  # the loaded Strategy subclass
    applied: Any = None  # tab_strategy.Applied: the last Apply's run
    backtest: Any = None  # tab_backtest.Backtested: the Backtest tab's last run
    optimization: Any = None  # tab_optimize: the last optimize.run (in the "opt" worker process)
    walkforward: Any = None  # tab_walkforward: the last wf.run (the result itself in the "opt" worker; here its summary)
    monte_carlo: Any = None  # tab_montecarlo: the last Monte Carlo runs of the Monte Carlo tab

    def on_data_changed(self) -> None:
        """New bars, windows or dataset: results built on the old ones are gone."""
        self.applied = None
        self.on_applied()

    def on_strategy_changed(self) -> None:
        self.applied = None
        self.on_applied()

    def on_applied(self) -> None:
        """A new (or no) Apply: later tabs' results are gone."""
        self.backtest = None


@method("ping")
def ping(state: State, params: dict, emit) -> dict:
    try:
        v = version("guban")
    except PackageNotFoundError:
        v = "unknown"
    return {"guban": v, "python": sys.version.split()[0], "pid": os.getpid(), "cwd": os.getcwd()}


@method("debug.exit")
def debug_exit(state: State, params: dict, emit) -> None:
    """End the worker process at once, as a crash would (the smoke test's check of the banner and Restart)."""
    print("debug.exit: worker ending on request", file=sys.stderr, flush=True)
    os._exit(int(params.get("code", 3)))


@method("debug.chart")
def debug_chart(state: State, params: dict, emit) -> dict:
    """A random-walk chart: the walking skeleton's proof that a matplotlib PNG reaches the window."""
    import numpy as np

    from guban.charts import COLORS, figure, to_data_uri

    t0 = time.perf_counter()
    n = int(params.get("points", 2000))
    y = np.cumsum(np.random.default_rng(int(params.get("seed", 1))).normal(size=n))
    fig, ax = figure(float(params.get("width", 9.0)), float(params.get("height", 3.2)))
    if params.get("noise"):  # incompressible pixels: a worst-case message size for timing the bridge
        ax.imshow(np.random.default_rng(1).random((600, 1800, 3)), aspect="auto")
    ax.plot(y, color=COLORS["s1"], linewidth=1)
    ax.set_title("Python worker → Electron (random walk)")
    uri = to_data_uri(fig, dpi=int(params.get("dpi", 150)))
    return {"image": uri, "bytes": len(uri), "draw_ms": round((time.perf_counter() - t0) * 1000, 1)}


from guban.gui import tab_backtest, tab_data, tab_montecarlo, tab_optimize, tab_strategy, tab_walkforward  # noqa: E402, F401  (register the tabs' methods)
