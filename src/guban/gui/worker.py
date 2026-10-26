"""Guban Studio's Python worker: one JSON message per line on stdin, one reply line per request on stdout.

Request  {"id": 7, "method": "data.load", "params": {...}}
Reply    {"id": 7, "ok": true, "result": ...}  or  {"id": 7, "ok": false, "error": {"message": ..., "frames": [...]}}
Event    {"event": "progress", "id": 7, ...}  (sent by a handler while request 7 runs)

Requests run one at a time, in arrival order, against one State that keeps the loaded bars and results in memory.
stdout carries protocol lines only: anything else printed while a handler runs goes to stderr, whose tail Electron
shows when the worker dies. Expected failures (the CLI's _EXPECTED types) reply with the message `guban` would print
plus the strategy's own frames; anything else also carries the traceback.

Run: uv run python -m guban.gui.worker  (Electron's main.js starts it in the repo root).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from typing import IO

from guban.gui.handlers import METHODS, State
from guban.metrics import jsonable


def _write(out: IO[str], msg: dict) -> None:
    out.write(json.dumps(jsonable(msg), ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
    out.flush()


def call(state: State, method: str | None, params: dict, emit) -> dict:
    """Run one request; the reply body without its id."""
    from guban.cli import _EXPECTED, _message, _strategy_frames

    fn = METHODS.get(method or "")
    if fn is None:
        return {"ok": False, "error": {"message": f"unknown method {method!r}; known: {', '.join(sorted(METHODS))}"}}
    try:
        return {"ok": True, "result": fn(state, params, emit)}
    except _EXPECTED as e:
        error = {"message": _message(e), "frames": _strategy_frames(e)}
        if os.environ.get("GUBAN_DEBUG"):
            error["traceback"] = traceback.format_exc()
        return {"ok": False, "error": error}
    except Exception as e:  # a bug, not bad input: say so and keep serving
        tb = traceback.format_exc()
        print(tb, file=sys.stderr, flush=True)
        return {"ok": False, "error": {"message": f"Internal error: {type(e).__name__}: {e}", "frames": [], "traceback": tb}}


def serve(inp: IO[str], out: IO[str], state: State | None = None) -> None:
    """Answer requests from `inp` on `out` until `inp` closes."""
    state = state or State()
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            if not isinstance(req, dict):
                raise ValueError("a request is a JSON object")
        except ValueError as e:
            _write(out, {"id": None, "ok": False, "error": {"message": f"Bad request line: {e}"}})
            continue
        rid = req.get("id")

        def emit(event: str, _rid=rid, **data) -> None:
            _write(out, {"event": event, "id": _rid, **data})

        _write(out, {"id": rid, **call(state, req.get("method"), req.get("params") or {}, emit)})


def main() -> None:
    out = open(sys.stdout.fileno(), "w", encoding="utf-8", newline="\n", closefd=False)
    sys.stdout = sys.stderr  # stray prints must not corrupt the protocol stream
    sys.stdin.reconfigure(encoding="utf-8")
    from guban.charts import set_theme

    set_theme("dark")  # the window is dark; report files written from here switch back with charts.theme("light")
    serve(sys.stdin, out)


if __name__ == "__main__":
    main()
