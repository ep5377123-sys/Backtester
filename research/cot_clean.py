"""Clean COT workbook: S&P 500 and Nasdaq-100 commercial / non-commercial and dealer positioning, 2019-2026 (2026-11-03)

Sources, both futures only (the two CFTC reports classify traders differently, so each gets its own sheet and neither
is mapped to the other):
  Comm_NonComm  Legacy report: data/COT Report/dea_fut_xls_<year>/annual.xls.
  Dealer_TFF    Traders in Financial Futures: data/COT Report/fut_fin_xls_<year>/FinFutYY.xls.
                `COT Report 2026.xlsx` is an older copy of the 2026 file and is not read.

Rules:
  values   as reported by CFTC, in contracts, never rescaled. Net = long - short; spreading positions are not in net.
  markets  only the S&P 500 and Nasdaq-100 contracts in MARKETS; sector, S&P 400, dividend and total-return contracts
           are left out. One name per CFTC code (its latest TFF name), so a rename stays one market in a filter.
  checks   one row per code + week; no blank positions; each side's positions add up to open interest within
           IDENTITY_TOLERANCE contracts; open interest and non-reportable positions are identical in both reports.
           A failure raises instead of writing the workbook.

    uv run --with xlrd python research/cot_clean.py [--out "data/COT Report"]
"""

from __future__ import annotations

import argparse
import glob
from pathlib import Path

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

TFF_GLOB = "data/COT Report/fut_fin_xls_*/FinFutYY.xls"
LEGACY_GLOB = "data/COT Report/dea_fut_xls_*/annual.xls"
WORKBOOK = "COT_SP_Nasdaq_2019_2026.xlsx"
START = "2019-01-01"
IDENTITY_TOLERANCE = 5
UNIT_BREAK = "2023-05-02"
MARKETS = {
    "13874+": "S&P 500",
    "13874A": "S&P 500",
    "13874U": "S&P 500",
    "138741": "S&P 500",
    "20974+": "Nasdaq-100",
    "209742": "Nasdaq-100",
    "209747": "Nasdaq-100",
}

KEY_COLUMNS = {
    "Market_and_Exchange_Names": "Market",
    "CFTC_Contract_Market_Code": "Code",
    "Contract_Units": "Contract Units",
}
LEGACY_COLUMNS = {
    "Open_Interest_All": "Open Interest",
    "NonComm_Positions_Long_All": "NonComm Long",
    "NonComm_Positions_Short_All": "NonComm Short",
    "NonComm_Postions_Spread_All": "NonComm Spread",
    "Comm_Positions_Long_All": "Comm Long",
    "Comm_Positions_Short_All": "Comm Short",
    "NonRept_Positions_Long_All": "NonRept Long",
    "NonRept_Positions_Short_All": "NonRept Short",
}
TFF_GROUPS = {"Dealer": "Dealer", "Asset_Mgr": "Asset Mgr", "Lev_Money": "Lev Funds", "Other_Rept": "Other Rept"}
TFF_COLUMNS = {"Open_Interest_All": "Open Interest"}
for _raw, _name in TFF_GROUPS.items():
    for _side in ("Long", "Short", "Spread"):
        TFF_COLUMNS[f"{_raw}_Positions_{_side}_All"] = f"{_name} {_side}"
TFF_COLUMNS |= {"NonRept_Positions_Long_All": "NonRept Long", "NonRept_Positions_Short_All": "NonRept Short"}


def _clean_keys(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize the text columns: whitespace, 6-character codes, one pair of parentheses around the units."""
    frame["Market"] = frame["Market"].str.replace(r"\s+", " ", regex=True).str.strip()
    frame["Code"] = frame["Code"].str.strip().str.zfill(6)
    units = frame["Contract Units"].fillna("").astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    wrapped = units.str.fullmatch(r"\([^()]*\)")
    frame["Contract Units"] = units.where(~wrapped, units.str[1:-1].str.strip())
    return frame


def _read_xls(path: str | Path, columns: dict[str, str]) -> pd.DataFrame:
    wanted = {**KEY_COLUMNS, **columns, "Report_Date_as_MM_DD_YYYY": "Date"}
    frame = pd.read_excel(path, engine="xlrd", usecols=list(wanted), dtype={"CFTC_Contract_Market_Code": str})
    frame = frame.rename(columns=wanted)
    frame["Date"] = pd.to_datetime(frame["Date"])
    return _clean_keys(frame)


def load_report(pattern: str, columns: dict[str, str]) -> pd.DataFrame:
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise OSError(f"no files match {pattern}: run from the repo root")
    frame = pd.concat([_read_xls(path, columns) for path in paths], ignore_index=True)
    return frame[frame["Date"] >= START].copy()


def _check_shared(tff: pd.DataFrame, legacy: pd.DataFrame) -> int:
    """Both reports split the same futures positions, so these columns must agree; returns the rows compared."""
    columns = ["Open Interest", "NonRept Long", "NonRept Short"]
    shared = tff[["Code", "Date", *columns]].merge(legacy[["Code", "Date", *columns]], on=["Code", "Date"], suffixes=("_tff", "_legacy"))
    mismatched = sum((shared[f"{column}_tff"] != shared[f"{column}_legacy"]).sum() for column in columns)
    if mismatched:
        raise ValueError(f"TFF and Legacy disagree on {mismatched} values of {columns}")
    return len(shared)


def _missing_weeks(frame: pd.DataFrame) -> dict[str, int]:
    """Weeks with no row inside each market's span, for the markets that have any; holiday-shifted weeks aren't gaps."""
    gaps = frame.sort_values("Date").groupby("Code")["Date"].diff().dt.days
    missing = (gaps[gaps > 8] / 7 - 1).round().groupby(frame["Code"]).sum().astype("int64")
    return missing[missing > 0].to_dict()


def _check(frame: pd.DataFrame, label: str, long_columns: list[str], short_columns: list[str]) -> int:
    """Raises on a duplicate row, a blank position or a side that doesn't add up; returns rows off by rounding only."""
    duplicates = frame.duplicated(["Code", "Date"]).sum()
    blanks = frame[["Open Interest", *long_columns, *short_columns]].isna().sum().sum()
    long_gap = (frame[long_columns].sum(axis=1) - frame["Open Interest"]).abs()
    short_gap = (frame[short_columns].sum(axis=1) - frame["Open Interest"]).abs()
    worst = max(long_gap.max(), short_gap.max())
    if duplicates or blanks or worst > IDENTITY_TOLERANCE:
        raise ValueError(f"{label}: {duplicates} duplicate rows, {blanks} blanks, positions off by up to {worst}")
    return int(((long_gap > 0) | (short_gap > 0)).sum())


def build(tff: pd.DataFrame, legacy: pd.DataFrame) -> dict[str, pd.DataFrame | list[str]]:
    tff = tff[tff["Code"].isin(MARKETS)].copy()
    legacy = legacy[legacy["Code"].isin(MARKETS)].copy()
    absent = set(MARKETS) - (set(tff["Code"]) & set(legacy["Code"]))
    if absent:
        raise ValueError(f"markets missing from a report: {sorted(absent)}")
    names = tff.sort_values("Date").groupby("Code")["Market"].last()

    spreads = [f"{name} Spread" for name in TFF_GROUPS.values()]
    tff_rounded = _check(
        tff,
        "Dealer_TFF",
        [f"{name} Long" for name in TFF_GROUPS.values()] + spreads + ["NonRept Long"],
        [f"{name} Short" for name in TFF_GROUPS.values()] + spreads + ["NonRept Short"],
    )
    legacy_rounded = _check(
        legacy,
        "Comm_NonComm",
        ["NonComm Long", "NonComm Spread", "Comm Long", "NonRept Long"],
        ["NonComm Short", "NonComm Spread", "Comm Short", "NonRept Short"],
    )
    shared = _check_shared(tff, legacy)

    legacy_view = _finish(legacy, names, ["NonComm", "Comm", "NonRept"], ["NonComm Spread"])
    tff_view = _finish(tff, names, [*TFF_GROUPS.values(), "NonRept"], spreads)
    markets = _markets(tff, legacy, names)

    tff_keys = set(zip(tff["Code"], tff["Date"]))
    legacy_keys = set(zip(legacy["Code"], legacy["Date"]))
    missing = tff_view[[key not in legacy_keys for key in zip(tff_view["Code"], tff_view["Date"])]]
    legacy_only = len(legacy_keys - tff_keys)
    notes = _notes(legacy_view, tff_view, markets, missing, legacy_only, tff_rounded, legacy_rounded, shared)
    return {"Notes": notes, "Comm_NonComm": legacy_view, "Dealer_TFF": tff_view, "Markets": markets}


def _finish(frame: pd.DataFrame, names: pd.Series, groups: list[str], spreads: list[str]) -> pd.DataFrame:
    """Final layout: keys, open interest, then long / short / spread / net per trader group, sorted by market and week."""
    frame = frame.copy()
    frame["Market"] = frame["Code"].map(names)
    frame["Year"] = frame["Date"].dt.year
    frame["Index"] = frame["Code"].map(MARKETS)
    frame["Consolidated"] = frame["Code"].str.endswith("+").map({True: "Yes", False: "No"})
    columns = ["Date", "Year", "Index", "Market", "Code", "Consolidated", "Contract Units", "Open Interest"]
    for group in groups:
        frame[f"{group} Net"] = frame[f"{group} Long"] - frame[f"{group} Short"]
        columns += [f"{group} Long", f"{group} Short"]
        columns += [f"{group} Spread"] if f"{group} Spread" in spreads else []
        columns += [f"{group} Net"]
    numeric = columns[7:]
    frame[numeric] = frame[numeric].astype("int64")
    return frame[columns].sort_values(["Index", "Market", "Date"], ignore_index=True)


def _markets(tff: pd.DataFrame, legacy: pd.DataFrame, names: pd.Series) -> pd.DataFrame:
    """One row per CFTC code: its coverage in each report, latest contract units and any former names."""
    ordered = tff.sort_values("Date")
    former = ordered.groupby("Code")["Market"].agg(lambda s: "; ".join(n for n in s.drop_duplicates() if n != s.iloc[-1]))
    table = pd.DataFrame({"Market": names})
    table["Index"] = table.index.map(MARKETS)
    table["Consolidated"] = table.index.str.endswith("+")
    table["Consolidated"] = table["Consolidated"].map({True: "Yes", False: "No"})
    table["Latest Contract Units"] = ordered.groupby("Code")["Contract Units"].last()
    table["First Week"] = ordered.groupby("Code")["Date"].min()
    table["Last Week"] = ordered.groupby("Code")["Date"].max()
    table["TFF Weeks"] = ordered.groupby("Code").size()
    table["Legacy Weeks"] = legacy.groupby("Code").size().reindex(table.index, fill_value=0).astype("int64")
    table["Former Names"] = former
    return table.reset_index().sort_values(["Index", "Market"], ignore_index=True)[
        ["Index", "Market", "Code", "Consolidated", "Latest Contract Units", "First Week", "Last Week", "TFF Weeks", "Legacy Weeks", "Former Names"]
    ]


def _notes(
    legacy: pd.DataFrame,
    tff: pd.DataFrame,
    markets: pd.DataFrame,
    missing: pd.DataFrame,
    legacy_only: int,
    tff_rounded: int,
    legacy_rounded: int,
    shared: int,
) -> list[str]:
    def span(frame: pd.DataFrame) -> str:
        return f"{frame['Date'].min():%Y-%m-%d} to {frame['Date'].max():%Y-%m-%d}"

    def missing_weeks(frame: pd.DataFrame) -> str:
        return ", ".join(f"{code} {weeks}" for code, weeks in _missing_weeks(frame).items())

    lines = [
        "S&P 500 and Nasdaq-100 COT positioning, 2019-2026",
        "",
        "#Sheets",
        f"Comm_NonComm: Commercial and Non-Commercial positions from the Legacy report, futures only. {len(legacy):,} rows, {span(legacy)}.",
        f"Dealer_TFF: Dealer positions and the other trader groups from the Traders in Financial Futures report, futures only. {len(tff):,} rows, {span(tff)}.",
        f"Markets: the {len(markets)} contracts included, their coverage and any former names.",
        "",
        "#Read this before comparing the two sheets",
        "Both sheets split the same futures positions, so Open Interest and NonRept are identical on them. Only the grouping of the large traders differs.",
        "TFF has no Commercial or Non-Commercial category, and Legacy has no Dealer category. Neither is mapped to the other here. The groups overlap, so don't add or subtract a Legacy group and a TFF group.",
        "",
        f"#Consolidated rows change units on {UNIT_BREAK}",
        "Rows with Consolidated = Yes (codes ending in +) are CFTC's roll-ups of the related contracts. They include the contracts listed separately, so never add them to those rows.",
        f"Before {UNIT_BREAK} the roll-ups are in big-contract units: Nasdaq ($100) = E-mini / 5, S&P ($250) = big contract + E-mini / 5 in the weeks where the big contract has a row. From that date both are in E-mini units ($20, $50) and equal E-mini + Micro / 10.",
        "So the two Consolidated series jump about 5x that week with no real change in positioning. Values are as reported, not rescaled.",
        "For one unbroken history use the E-mini: E-MINI S&P 500 13874A or NASDAQ MINI 209742.",
        "S&P 500 STOCK INDEX 138741 is the old big contract. Its last TFF row is 2021-09-14.",
        "",
        "#Missing weeks",
        "The E-mini and Consolidated contracts have every week. The Micros and the big S&P contract have weeks with no row in CFTC's files, up to March 2022 for the S&P Micro. A missing week means no data, not a zero position.",
        f"Weeks missing by code on Dealer_TFF: {missing_weeks(tff)}. On Comm_NonComm: {missing_weeks(legacy)}.",
        "",
        "#Definitions",
        "Date is CFTC's report date: positions as of that day's close, normally a Tuesday. All positions are in contracts.",
        "Net = Long - Short. Spreading positions are shown separately and are not in Net.",
        "Market uses each code's latest name, so renames and exchange moves stay one market. Earlier names are on the Markets sheet.",
        "",
        "#Sources",
        "Dealer_TFF: CFTC fut_fin_xls_2019 to 2026 files (FinFutYY.xls).",
        "Comm_NonComm: CFTC dea_fut_xls_2019 to 2026 files (annual.xls).",
        "Left out on purpose: S&P sector E-minis, S&P 400, S&P 500 dividend and total-return contracts, and the one-week EVENT - EMINI NASDAQ 100 contract.",
        "",
        "#Checks passed",
        "One row per market per week on both sheets, with no blank positions.",
        f"Each side's positions add up to open interest within {IDENTITY_TOLERANCE} contracts. {tff_rounded:,} Dealer_TFF rows and {legacy_rounded:,} Comm_NonComm rows are off by a few contracts and are left as reported.",
        f"Open Interest, NonRept Long and NonRept Short are identical on all {shared:,} rows the two sheets share.",
        f"{legacy_only:,} market-weeks have a Legacy row but no TFF row, and {len(missing):,} have a TFF row but no Legacy row.",
    ]
    lines += [f"TFF row with no Legacy row: {row.Market} ({row.Code}), {row.Date:%Y-%m-%d}." for row in missing.head(20).itertuples()]
    if len(missing) > 20:
        lines.append(f"...and {len(missing) - 20} more.")
    return lines


def _format_table(sheet, frame: pd.DataFrame) -> None:
    fill = PatternFill("solid", fgColor="1F3864")
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    sheet.row_dimensions[1].height = 32
    sheet.freeze_panes = "F2" if "Year" in frame.columns else "C2"
    sheet.auto_filter.ref = sheet.dimensions
    for index, column in enumerate(frame.columns, start=1):
        letter = get_column_letter(index)
        if pd.api.types.is_datetime64_any_dtype(frame[column]):
            width = 12
            for cell in sheet[letter][1:]:
                cell.number_format = "yyyy-mm-dd"
        elif pd.api.types.is_numeric_dtype(frame[column]):
            width = 7 if column == "Year" else 12
            if column != "Year":
                for cell in sheet[letter][1:]:
                    cell.number_format = "#,##0"
        else:
            width = min(int(frame[column].astype(str).str.len().max()) + 3, 60)
        sheet.column_dimensions[letter].width = width


def _format_notes(sheet, lines: list[str]) -> None:
    sheet.column_dimensions["A"].width = 120
    for row, line in enumerate(lines, start=1):
        heading = line.startswith("#")
        cell = sheet.cell(row=row, column=1, value=line.removeprefix("#"))
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        cell.font = Font(bold=True, size=14) if row == 1 else Font(bold=heading)


def write(sheets: dict[str, pd.DataFrame | list[str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(path, engine="openpyxl", datetime_format="yyyy-mm-dd") as writer:
        for name, content in sheets.items():
            if isinstance(content, list):
                writer.book.create_sheet(name)
                _format_notes(writer.book[name], content)
            else:
                content.to_excel(writer, sheet_name=name, index=False)
                _format_table(writer.sheets[name], content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("data/COT Report"))
    args = parser.parse_args()

    sheets = build(load_report(TFF_GLOB, TFF_COLUMNS), load_report(LEGACY_GLOB, LEGACY_COLUMNS))
    path = args.out / WORKBOOK
    write(sheets, path)
    print(f"wrote {path}")
    for line in sheets["Notes"]:
        if line.startswith(("Comm_NonComm:", "Dealer_TFF:", "Markets:", "Each side", "TFF row")) or "market-weeks" in line:
            print(" ", line)


if __name__ == "__main__":
    main()
