"""Parity harness: compare our trade list with a MultiCharts trade export (S3 / R7).

MC exports come in two layouts, both read by load_mc_trades():
- CSV from the TradesToCSV workbook: one row per trade (the entry order), every line wrapped in quotes,
  Excel serial date/time, net profit_usd. No exit time or price.
- The Strategy Performance Report .xlsx, sheet "List of Trades": an entry row followed by an exit row per
  trade, with the exit signal, time and price.
MC times are chart wall-clock time with no zone; `tz` says which zone the chart used (exchange time for CME
is America/Chicago).

Scoring window: both sides are counted over the same window [start, end] of entry times (default: the span both
lists cover; when one list is empty, the other's span). Trade counts, net profit, and the unmatched lists use
exactly that window on both sides.

Matching: MC trades are paired one-to-one with our trades of the same direction whose entry is within one
strategy bar. Candidates may lie up to one bar outside the scoring window (so a trade one bar off at the edge
still finds its partner), but only pairs whose MC trade is inside the window count as matched. The pairing is
optimal, not greedy: the most pairs possible, and among those the smallest total entry-time difference. Such a
pairing never needs two pairs to cross in time, so it is found by an order-preserving dynamic program over each
cluster of trades that lie within a bar of each other. For a CSV export the MC exit price is implied from its net
profit and the matched trade's costs, so it is only as good as the cost model.
"""

from __future__ import annotations

import html
import io
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

import numpy as np
import pandas as pd

MC_COLUMNS = ["trade_no", "direction", "qty", "entry_time", "entry_price", "entry_name", "exit_time", "exit_price", "exit_name", "net_pnl"]
EXCEL_EPOCH = pd.Timestamp("1899-12-30")

MIN_MATCH_RATE = 0.95
MIN_PRICE_OK_RATE = 0.95
MAX_NET_DIFF = 0.03


def load_mc_trades(path: str | Path, tz: str | None = "America/Chicago") -> pd.DataFrame:
    """Read an MC trade export (.csv or .xlsx) into MC_COLUMNS, times tz-aware in `tz` (tz=None: naive chart time)."""
    path = Path(path)
    if path.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        out = _read_mc_xlsx(path)
    else:
        out = _read_mc_csv(path)
    for col in ("entry_time", "exit_time"):
        out[col] = pd.to_datetime(out[col]).dt.round("1s")
        if tz is not None:
            out[col] = out[col].dt.tz_localize(tz, ambiguous="raise", nonexistent="raise")
    return out[MC_COLUMNS].sort_values("entry_time", kind="stable").reset_index(drop=True)


def _read_mc_csv(path: Path) -> pd.DataFrame:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    text = "\n".join(ln.strip()[1:-1] if ln.strip().startswith('"') and ln.strip().endswith('"') else ln for ln in lines)
    df = pd.read_csv(io.StringIO(text))
    need = {"trade_id", "type", "signal", "excel_time", "price", "contracts", "profit_usd"}
    if need - set(df.columns):
        raise ValueError(f"{path.name}: not an MC trade CSV, missing columns {sorted(need - set(df.columns))}")
    df = df[df["type"].str.startswith("Entry")]
    return pd.DataFrame({
        "trade_no": df["trade_id"].astype(int),
        "direction": np.where(df["type"].str.endswith("Long"), "long", "short"),
        "qty": df["contracts"].astype(int),
        "entry_time": EXCEL_EPOCH + pd.to_timedelta(df["excel_time"].astype(float), unit="D"),
        "entry_price": df["price"].astype(float),
        "entry_name": df["signal"],
        "exit_time": pd.NaT,
        "exit_price": np.nan,
        "exit_name": None,
        "net_pnl": df["profit_usd"].astype(float),
    })


def _read_mc_xlsx(path: Path) -> pd.DataFrame:
    raw = pd.read_excel(path, sheet_name="List of Trades", header=None)
    hdr = raw.index[raw.iloc[:, 0].astype(str).str.strip() == "Trade #"]
    if not len(hdr):
        raise ValueError(f"{path.name}: no 'List of Trades' table header ('Trade #') found")
    df = raw.iloc[hdr[0] + 1 :].copy()
    df.columns = [str(c).strip() for c in raw.iloc[hdr[0]]]
    df = df[df["Type"].notna()].reset_index(drop=True)
    rows, entry = [], None
    for r in df.to_dict("records"):
        r["stamp"] = _combine(r["Date"], r["Time"])
        kind = str(r["Type"])
        if kind.startswith("Entry"):
            entry = r
        elif kind.startswith("Exit") and entry is not None:
            rows.append((
                int(entry["Trade #"]), "long" if kind.endswith("Long") else "short", int(entry["Contracts"]),
                entry["stamp"], float(entry["Price"]), entry["Signal"],
                r["stamp"], float(r["Price"]), r["Signal"], float(entry["Profit ($)"]),
            ))
            entry = None
    return pd.DataFrame(rows, columns=MC_COLUMNS)


def _combine(d, t) -> datetime:
    d = pd.Timestamp(d)
    if isinstance(t, time):
        return datetime.combine(d.date(), t)
    return datetime.combine(d.date(), pd.Timestamp(t).time()) if pd.notna(t) else d.to_pydatetime()


def load_our_trades(path: str | Path) -> pd.DataFrame:
    """Read a trades.csv written by BacktestResult.write_trades_csv() (MC layout, or the older engine layout)."""
    from guban.tradelist import from_mc_layout

    df = pd.read_csv(path)
    if "trade_id" in df.columns:
        df = from_mc_layout(df)
    for col in ("entry_time", "exit_time"):
        df[col] = pd.to_datetime(df[col], utc=True)
    return df


@dataclass
class Comparison:
    matched: pd.DataFrame
    mc_only: pd.DataFrame
    ours_only: pd.DataFrame
    start: pd.Timestamp
    end: pd.Timestamp
    n_mc: int
    n_ours: int
    net_mc: float
    net_ours: float
    implied_exits: bool
    tick: float
    notes: list[str] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        return len(self.matched) / self.n_mc if self.n_mc else 0.0

    @property
    def price_ok_rate(self) -> float:
        return float(self.matched["price_ok"].mean()) if len(self.matched) else 0.0

    @property
    def net_diff(self) -> float:
        """(ours - MC) / |MC| net profit; 0 when both are 0, inf when only MC's is 0."""
        if self.net_mc:
            return (self.net_ours - self.net_mc) / abs(self.net_mc)
        return 0.0 if abs(self.net_ours) < 0.005 else float("inf")

    @property
    def checks(self) -> dict[str, bool]:
        return {
            f"trades matched >= {MIN_MATCH_RATE:.0%}": self.match_rate >= MIN_MATCH_RATE,
            f"matched prices within 1 tick >= {MIN_PRICE_OK_RATE:.0%}": self.price_ok_rate >= MIN_PRICE_OK_RATE,
            f"net profit within {MAX_NET_DIFF:.0%}": abs(self.net_diff) <= MAX_NET_DIFF,
        }

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    def summary(self) -> str:
        m = self.matched
        lines = [
            f"Window: {self.start} -> {self.end}",
            f"MC trades: {self.n_mc}   ours: {self.n_ours}   matched: {len(m)} ({self.match_rate:.1%})"
            f"   MC-only: {len(self.mc_only)}   ours-only: {len(self.ours_only)}",
            f"Matched with entry and exit within 1 tick: {int(m['price_ok'].sum()) if len(m) else 0} ({self.price_ok_rate:.1%})"
            + ("   [MC exit prices implied from net profit]" if self.implied_exits else ""),
        ]
        if len(m):
            line = f"  entry within 1 tick: {m['entry_ok'].mean():.1%}   exit within 1 tick: {m['exit_ok'].mean():.1%}"
            if not self.implied_exits:
                line += f"   same exit time: {m['exit_time_ok'].mean():.1%}"
            lines.append(line)
            if "ambiguous" in m.columns:
                bad = m[~m["price_ok"]]
                lines.append(f"  price mismatches tagged ambiguous (ours): {int(bad['ambiguous'].sum())} of {len(bad)}")
        lines.append(f"Net profit: MC {self.net_mc:,.2f}   ours {self.net_ours:,.2f}   diff {self.net_diff:+.2%}")
        for name, ok in self.checks.items():
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        lines.append("S3 PASSED" if self.passed else "S3 FAILED")
        return "\n".join(lines + self.notes)


def compare_trades(
    ours: pd.DataFrame,
    mc: pd.DataFrame,
    bar: pd.Timedelta,
    tick: float,
    point_value: float,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> Comparison:
    """Match MC trades to ours (same direction, entry within one `bar`) and score both over one window.

    The window defaults to the span both lists cover: from the later first entry to the earlier last entry (the
    non-empty list's span when the other is empty). entry_time / exit_time must be tz-aware datetime columns.
    """
    ours, mc = _check_trades(ours, "ours"), _check_trades(mc, "MC")
    if bar <= pd.Timedelta(0):
        raise ValueError(f"bar must be a positive duration, got {bar}")
    spans = [(t["entry_time"].min(), t["entry_time"].max()) for t in (ours, mc) if len(t)]
    if (start is None or end is None) and not spans:
        raise ValueError("both trade lists are empty: nothing to compare (pass --start and --end to score an empty window)")
    if start is None:
        start = max(a for a, _ in spans)
    if end is None:
        end = min(b for _, b in spans)
    start, end = _utc(start), _utc(end)
    if end < start:
        raise ValueError(f"the comparison window is empty ({start} > {end}): the two trade lists do not overlap in time; "
                         "pass --start / --end")

    def near(t: pd.DataFrame) -> pd.DataFrame:
        t = t[(t["entry_time"] >= start - bar) & (t["entry_time"] <= end + bar)]
        return t.sort_values("entry_time", kind="stable").reset_index(drop=True)

    ours, mc = near(ours), near(mc)
    ours_in = ((ours["entry_time"] >= start) & (ours["entry_time"] <= end)).to_numpy()
    mc_in = ((mc["entry_time"] >= start) & (mc["entry_time"] <= end)).to_numpy()
    every = _match(ours, mc, bar)
    pairs = [(i, j) for i, j in every if mc_in[i]]
    partnered = {j for _, j in every}
    mc_scored = mc[mc_in]
    ours_scored = ours[ours_in]
    implied = bool(mc_scored["exit_price"].isna().all()) if len(mc_scored) else False
    rows = []
    for i_mc, i_ours in pairs:
        a, b = mc.loc[i_mc], ours.loc[i_ours]
        d = 1 if b["direction"] == "long" else -1
        costs = float(b.get("commission", 0.0)) + float(b.get("slippage", 0.0))
        mc_exit = a["exit_price"]
        if pd.isna(mc_exit):
            mc_exit = round(a["entry_price"] + d * (a["net_pnl"] + costs) / (point_value * a["qty"]), 6)
        rows.append({
            "mc_trade": int(a["trade_no"]), "our_trade": int(b["trade_no"]), "direction": b["direction"],
            "mc_entry_time": a["entry_time"], "our_entry_time": b["entry_time"],
            "mc_entry": a["entry_price"], "our_entry": b["entry_price"],
            "mc_exit_time": a["exit_time"], "our_exit_time": b["exit_time"],
            "mc_exit": mc_exit, "our_exit": b["exit_price"],
            "mc_exit_name": a["exit_name"], "our_exit_name": b["exit_name"],
            "mc_net": a["net_pnl"], "our_net": b["net_pnl"],
            "mc_qty": int(a["qty"]), "our_qty": int(b["qty"]),
            "ambiguous": bool(b.get("ambiguous", False)),
        })
    cols = ["mc_trade", "our_trade", "direction", "mc_entry_time", "our_entry_time", "mc_entry", "our_entry",
            "mc_exit_time", "our_exit_time", "mc_exit", "our_exit", "mc_exit_name", "our_exit_name",
            "mc_net", "our_net", "mc_qty", "our_qty", "ambiguous"]
    m = pd.DataFrame(rows, columns=cols)
    eps = 1e-9
    m["entry_ok"] = (m["our_entry"] - m["mc_entry"]).abs() <= tick + eps
    m["exit_ok"] = (m["our_exit"] - m["mc_exit"]).abs() <= tick + eps
    m["exit_time_ok"] = m["mc_exit_time"].isna() | (m["mc_exit_time"] == m["our_exit_time"])
    m["price_ok"] = m["entry_ok"] & m["exit_ok"] & (m["mc_qty"] == m["our_qty"])

    matched_mc = {i for i, _ in pairs}
    notes = []
    if len(mc_scored) and (mc_scored["qty"] != mc_scored["qty"].iloc[0]).any():
        notes.append("note: MC trade sizes vary")
    if not len(mc_scored):
        notes.append("note: MC has no trades in the window")
    if not len(ours_scored):
        notes.append("note: we have no trades in the window")
    return Comparison(
        matched=m,
        mc_only=mc_scored[~mc_scored.index.isin(matched_mc)],
        ours_only=ours_scored[~ours_scored.index.isin(partnered)],
        start=start, end=end, n_mc=len(mc_scored), n_ours=len(ours_scored),
        net_mc=float(mc_scored["net_pnl"].sum()), net_ours=float(ours_scored["net_pnl"].sum()),
        implied_exits=implied, tick=tick, notes=notes,
    )


def _check_trades(df: pd.DataFrame, who: str) -> pd.DataFrame:
    """A copy with entry/exit times in UTC; raises unless they are tz-aware datetime columns."""
    need = {"trade_no", "direction", "qty", "entry_time", "entry_price", "exit_time", "exit_price", "net_pnl"}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"{who} trades are missing columns {sorted(missing)}")
    df = df.copy()
    for col in ("entry_time", "exit_time"):
        dt = df[col].dtype
        if not isinstance(dt, pd.DatetimeTZDtype):
            raise ValueError(f"{who} trades: {col} must be a tz-aware datetime column, got {dt} (localize it to the chart's zone)")
        df[col] = df[col].dt.tz_convert("UTC")
    if df["entry_time"].isna().any():
        raise ValueError(f"{who} trades: {int(df['entry_time'].isna().sum())} trade(s) have no entry time")
    bad = ~df["direction"].isin(["long", "short"])
    if bad.any():
        raise ValueError(f"{who} trades: direction must be 'long' or 'short', got {sorted(set(df.loc[bad, 'direction'].astype(str)))}")
    return df


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _match(ours: pd.DataFrame, mc: pd.DataFrame, bar: pd.Timedelta) -> list[tuple[int, int]]:
    """Optimal one-to-one pairing (MC row, our row), same direction, entries within `bar`: the most pairs, then the
    smallest total entry-time difference. Both frames must be sorted by entry_time.

    Some optimal pairing never crosses (if a1 < a2 and b1 < b2 and both crossed pairs are within `bar`, so are
    (a1, b1) and (a2, b2), with no larger total difference), so an order-preserving DP finds it. Trades farther than
    `bar` from every other trade split the timeline into independent clusters, which keeps the DP small."""
    tol = int(bar.total_seconds() * 1e9)
    pairs = []
    for d in ("long", "short"):
        ia = np.flatnonzero(mc["direction"].to_numpy() == d)
        ib = np.flatnonzero(ours["direction"].to_numpy() == d)
        ta = mc["entry_time"].to_numpy("datetime64[ns]").astype(np.int64)[ia]
        tb = ours["entry_time"].to_numpy("datetime64[ns]").astype(np.int64)[ib]
        events = sorted([(t, 0, k) for k, t in enumerate(ta)] + [(t, 1, k) for k, t in enumerate(tb)])
        cluster: list[tuple] = []
        for ev in events + [None]:
            if cluster and (ev is None or ev[0] - cluster[-1][0] > tol):
                a = [k for _, side, k in cluster if side == 0]
                b = [k for _, side, k in cluster if side == 1]
                pairs += [(int(ia[x]), int(ib[y])) for x, y in _match_cluster(ta[a], tb[b], tol, a, b)]
                cluster = []
            if ev is not None:
                cluster.append(ev)
    return sorted(pairs)


def _match_cluster(ta: np.ndarray, tb: np.ndarray, tol: int, a: list[int], b: list[int]) -> list[tuple[int, int]]:
    """Order-preserving DP: best[i][j] = (pairs, -total |dt|) over the first i of `ta` and the first j of `tb`."""
    n, m = len(ta), len(tb)
    if not n or not m:
        return []
    best = [[(0, 0)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        row, prev = best[i], best[i - 1]
        for j in range(1, m + 1):
            v = max(prev[j], row[j - 1])
            dt = abs(int(ta[i - 1]) - int(tb[j - 1]))
            if dt <= tol:
                c, cost = prev[j - 1]
                v = max(v, (c + 1, cost - dt))
            row[j] = v
    out, i, j = [], n, m
    while i and j:
        if best[i][j] == best[i - 1][j]:
            i -= 1
        elif best[i][j] == best[i][j - 1]:
            j -= 1
        else:
            out.append((a[i - 1], b[j - 1]))
            i, j = i - 1, j - 1
    return out[::-1]


_CSS = """
:root { --bg:#fff; --fg:#1b1f24; --muted:#5b6570; --line:#d8dde3; --pass:#1a7f37; --fail:#cf222e; --head:#f3f5f7; }
@media (prefers-color-scheme: dark) { :root { --bg:#0f1216; --fg:#e6e9ed; --muted:#9aa4ae; --line:#2b323a;
  --pass:#3fb950; --fail:#f85149; --head:#171b21; } }
body { background:var(--bg); color:var(--fg); font:14px/1.45 system-ui, sans-serif; margin:24px; }
h1 { font-size:20px; } h2 { font-size:16px; margin-top:28px; }
pre { background:var(--head); padding:12px; border-radius:6px; overflow-x:auto; }
table { border-collapse:collapse; font-variant-numeric:tabular-nums; font-size:13px; }
th, td { border-bottom:1px solid var(--line); padding:3px 8px; text-align:right; white-space:nowrap; }
th { background:var(--head); position:sticky; top:0; }
td.l, th.l { text-align:left; } .bad { color:var(--fail); font-weight:600; } .muted { color:var(--muted); }
.wrap { overflow-x:auto; max-height:70vh; }
"""


def write_diff_html(cmp: Comparison, path: str | Path, title: str = "Parity diff", tz: str = "America/New_York") -> Path:
    """Self-contained HTML listing unmatched and price-mismatched trades."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    m = cmp.matched
    bad = m[~m["price_ok"]]
    parts = [
        f"<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        f"<title>{html.escape(title)}</title><style>{_CSS}</style></head><body>",
        f"<h1>{html.escape(title)}</h1>",
        f"<pre>{html.escape(cmp.summary())}</pre>",
        f"<p class='muted'>Times shown in {html.escape(tz)}. Prices differ when |ours - MC| &gt; {cmp.tick} (1 tick)."
        + (" MC exit prices are implied from MC net profit and our costs (CSV export has no exits)." if cmp.implied_exits else "")
        + "</p>",
        _table(f"Price mismatches ({len(bad)})", bad, [
            ("mc_trade", "MC #"), ("our_trade", "our #"), ("mc_entry_time", "entry (MC)"), ("our_entry_time", "entry (ours)"),
            ("mc_entry", "MC entry"), ("our_entry", "our entry"), ("mc_exit_time", "MC exit time"), ("our_exit_time", "our exit time"),
            ("mc_exit", "MC exit"), ("our_exit", "our exit"), ("mc_exit_name", "MC exit signal"), ("our_exit_name", "our exit signal"),
            ("mc_net", "MC net"), ("our_net", "our net"), ("ambiguous", "ambiguous"),
        ], tz, flag=True),
        _table(f"MC trades with no match ({len(cmp.mc_only)})", cmp.mc_only, [
            ("trade_no", "MC #"), ("direction", "dir"), ("entry_time", "entry"), ("entry_price", "entry price"),
            ("exit_time", "exit"), ("exit_price", "exit price"), ("exit_name", "exit signal"), ("net_pnl", "net"),
        ], tz),
        _table(f"Our trades with no match ({len(cmp.ours_only)})", cmp.ours_only, [
            ("trade_no", "our #"), ("direction", "dir"), ("entry_time", "entry"), ("entry_price", "entry price"),
            ("exit_time", "exit"), ("exit_price", "exit price"), ("exit_name", "exit signal"), ("net_pnl", "net"),
            ("ambiguous", "ambiguous"),
        ], tz),
        "</body></html>",
    ]
    path.write_text("\n".join(parts), encoding="utf-8")
    return path


def _table(heading: str, df: pd.DataFrame, cols: list[tuple[str, str]], tz: str, flag: bool = False) -> str:
    out = [f"<h2>{html.escape(heading)}</h2>"]
    if df.empty:
        return out[0] + "<p class='muted'>None.</p>"
    cols = [(c, label) for c, label in cols if c in df.columns]
    out.append("<div class='wrap'><table><tr>" + "".join(f"<th>{html.escape(label)}</th>" for _, label in cols) + "</tr>")
    for _, r in df.iterrows():
        cells = []
        for c, _ in cols:
            v = r[c]
            cls = ""
            if flag and c in ("our_entry", "mc_entry") and not r["entry_ok"]:
                cls = "bad"
            if flag and c in ("our_exit", "mc_exit") and not r["exit_ok"]:
                cls = "bad"
            cells.append(f"<td class='{cls}'>{html.escape(_fmt(v, tz))}</td>")
        out.append("<tr>" + "".join(cells) + "</tr>")
    out.append("</table></div>")
    return "\n".join(out)


def _fmt(v, tz: str) -> str:
    if isinstance(v, pd.Timestamp):
        return "" if pd.isna(v) else v.tz_convert(tz).strftime("%Y-%m-%d %H:%M")
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    if isinstance(v, (bool, np.bool_)):
        return "yes" if v else ""
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)
