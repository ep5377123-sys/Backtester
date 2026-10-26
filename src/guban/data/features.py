"""Extra bar columns a strategy can declare in `Strategy.extra_columns` (Phase 18), built at load time, not cached
with the bars. Each builder takes (bars, symbol, tf, data_dir) and returns a DataFrame on the bars' index holding
that column (and any sibling columns built in the same pass).

    big_buy_body, big_sell_body  guban.data.bigtrades: largest buy / sell bubble inside the 1-minute bar's body
                                 (NaN without usable ticks)
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from guban.data.resample import parse_timeframe


def _bubbles(bars: pd.DataFrame, symbol: str, tf: str, data_dir: Path) -> pd.DataFrame:
    from guban.data.bigtrades import bubble_columns

    if parse_timeframe(tf) != 1:
        raise ValueError(f"big_buy_body / big_sell_body are built per 1-minute bar; run the strategy with --tf 1m (got --tf {tf})")
    return bubble_columns(bars, symbol, data_dir=data_dir)


BUILDERS = {"big_buy_body": _bubbles, "big_sell_body": _bubbles}


def add_columns(bars: pd.DataFrame, names: tuple[str, ...], symbol: str, tf: str, data_dir: Path = Path("data")) -> pd.DataFrame:
    """`bars` with each column in `names` added (a column the bars already hold is kept as it is)."""
    unknown = [n for n in names if n not in BUILDERS and n not in bars.columns]
    if unknown:
        raise ValueError(f"unknown extra column(s) {unknown}; known: {sorted(BUILDERS)}")
    out = bars
    for name in names:
        if name not in out.columns:
            built = BUILDERS[name](out, symbol, tf, Path(data_dir))
            out = out.assign(**{c: built[c] for c in built.columns if c in names and c not in out.columns})
    return out
