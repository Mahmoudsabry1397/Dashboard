"""
well_test_units.py: read the gross and net rate columns of a well-test sheet, whatever they are called and whatever unit they are in.

The dashboard keeps rates in m3/d (shown as CM/D; the Optimizer tab converts to bbl/d for display). A test sheet may arrive in barrels instead
(BOPD / BFPD / BLPD / bbl/d), so each rate column is
  1. recognised by name:  gross = BFPD, BLPD, Gross, Total fluid, Total liquid, Fluid rate, M3L/day, ACT M3/D, Rate CM/D ...
                          net   = BOPD, Net oil, Net rate, Oil rate, ACT NET, Net CM/D ...
  2. given a unit:        from its header (bbl/d, BOPD, m3/d, CM/D ...), else from the number's own text ("350 BOPD"), else from the other rate
                          column of the same sheet, else by comparing with the wells' earlier tests (a ratio near 6.29 means barrels),
                          else metric
  3. converted to m3/d    (barrels / 6.28981).
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

BBL_PER_M3 = 6.28981

GROSS_KEYS = ("BFPD", "BLPD", "GROSS", "TOTALFLUID", "TOTALLIQUID", "TOTALPROD", "TOTALFLOW", "TOTALRATE", "FLUIDRATE", "LIQUIDRATE",
              "LIQRATE", "FLUIDPROD", "LIQUIDPROD", "ACTM3D", "ACTUALM3D", "M3LDAY", "M3LD", "M3FD", "M3LIQ", "ACTUALFLUID", "ACTUALLIQUID",
              "ACTUALGROSS", "RATECMD", "GROSSCMD", "LIQUIDCMD", "TOTALCMD")
NET_KEYS = ("BOPD", "NETOIL", "NETRATE", "NETCMD", "NETM3D", "OILRATE", "OILPROD", "OILM3D", "OILCMD", "ACTNET", "ACTUALNET", "ACTOIL",
            "ACTUALOIL", "NETPROD")
EXCLUDE = ("WATER", "BWPD", "WC", "PCT", "PERCENT", "CUT", "DATE", "TIME", "LEVEL", "GRAVITY", "API", "PRESS", "PSI", "WHP", "CHOKE", "SPM",
           "DEPTH", "TEMP", "GAS", "GOR", "SALIN", "VISC", "HOURS", "DAYS", "STATUS", "SAMPLE")

_BBL_TOKENS = {"bopd", "bfpd", "blpd", "bwpd", "bpd", "bbl", "bbls", "barrel", "barrels", "stb", "stbd", "bfd", "bld", "bod"}
_METRIC_TOKENS = {"cmd", "cm", "sm3", "stm3"}


def compact(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def kind_of(compact_header: str):
    """'gross', 'net' or None for an already compacted (letters and digits, upper case) header."""
    c = compact_header
    if not c or any(x in c for x in EXCLUDE):
        return None
    if c in ("NET", "OIL", "BOD", "NETACT", "NETACTUAL") or any(k in c for k in NET_KEYS):
        return "net"
    if c in ("LIQUID", "FLUID", "LIQ", "TOTAL", "BFD", "BLD", "TOTALACT", "ACTUALTOTAL") or any(k in c for k in GROSS_KEYS):
        return "gross"
    if any(u in c for u in ("BBL", "BARREL", "STB", "BPD", "M3", "CMD")):     # a unit in the name: 'Oil (bbl/d)', 'Liquid bbl', 'Total m3'
        if "OIL" in c:
            return "net"
        if any(x in c for x in ("LIQ", "FLUID", "TOTAL", "GROSS")):
            return "gross"
    return None


def rate_kind(header):
    return kind_of(compact(header))


def unit_of_text(text) -> str | None:
    """'bbl' or 'm3' when the text names a unit (a header such as 'Gross (bbl/d)' or a cell such as '350 BOPD'), else None."""
    toks = re.findall(r"[a-z0-9\u00b3]+", str(text).lower())
    if any(t in _BBL_TOKENS or t.startswith("bbl") for t in toks):
        return "bbl"
    if any(t.startswith("m3") or t in _METRIC_TOKENS or "\u00b3" in t for t in toks):
        return "m3"
    return None


def looks_like_srp(keys) -> bool:
    """A production-test / water-cut sample sheet: a plain 'Well' column plus a gross and a net rate column. (DATABASE, Monthly and the
    Operation Mode sheet use 'Well Name' / 'Well No.', so they are never taken for it.)"""
    keys = {str(k) for k in keys}
    if "WELL" not in keys or any(k.startswith(("WELLNAME", "WELLNO")) for k in keys):
        return False
    kinds = {kind_of(k) for k in keys}
    return "gross" in kinds and "net" in kinds


def _cells(series: pd.Series, base: str):
    """Numeric values in m3/d. Cells that carry their own unit ('350 BOPD') use it, the others use the column unit `base`."""
    f_base = 1 / BBL_PER_M3 if base == "bbl" else 1.0
    num = pd.to_numeric(series, errors="coerce")
    out = num * f_base
    own = 0
    if series.dtype == object or pd.api.types.is_string_dtype(series):
        for i in series.index[num.isna() & series.notna()]:
            m = re.match(r"\s*([-+]?\d[\d,]*\.?\d*)\s*(.*)$", str(series.at[i]))
            if not m:
                continue
            try:
                v = float(m.group(1).replace(",", ""))
            except ValueError:
                continue
            u = unit_of_text(m.group(2))
            if u:
                own += 1
            out.at[i] = v * (1 / BBL_PER_M3 if (u or base) == "bbl" else 1.0)
    return out, own


def _infer_by_history(values: pd.Series, wells: pd.Series, ref: pd.Series | None) -> str | None:
    """'bbl' if this column is about 6.29 times the wells' earlier tests, 'm3' if it is about the same, None if it cannot tell."""
    if ref is None or ref.empty:
        return None
    r = wells.astype(str).str.strip().map(ref)
    ok = (pd.to_numeric(values, errors="coerce") > 0) & (pd.to_numeric(r, errors="coerce") > 0)
    if ok.sum() < 3:
        return None
    med = float(np.median(pd.to_numeric(values, errors="coerce")[ok] / pd.to_numeric(r, errors="coerce")[ok]))
    if 5.0 <= med <= 7.8:
        return "bbl"
    if 0.5 <= med <= 2.0:
        return "m3"
    return None


def _unit_row_hint(series: pd.Series) -> str | None:
    """Some sheets put the unit in a row under the header ('bbl/d' in the first data row): text cells without digits that name a unit."""
    txt = series.dropna().astype(str)
    txt = txt[~txt.str.contains(r"\d")]
    hints = txt.head(5).map(unit_of_text).dropna()
    return None if hints.empty else hints.mode().iat[0]


def extract_rates(frame: pd.DataFrame, key: str, ref: dict | None = None, exact: dict | None = None, report: list | None = None):
    """Gross and net rate of a test sheet in m3/d.

    exact : {'gross': {compact names}, 'net': {...}} columns that are always taken first (the names the pipeline already knew)
    ref   : {'gross': Series, 'net': Series} last known rate per well in m3/d, used to tell barrels from m3 when no header says so
    Returns ({'gross': Series, 'net': Series} aligned to frame.index, [notes about conversions]).
    """
    exact, ref = exact or {}, ref or {}
    cols = {"gross": [], "net": []}
    for c in frame.columns:
        if c == key:
            continue
        k = kind_of(compact(c))
        if k and pd.to_numeric(frame[c], errors="coerce").notna().any() or (k and frame[c].dropna().astype(str).str.contains(r"\d").any()):
            cols[k].append(c)
    for k in cols:      # known exact names first, then the rest in sheet order
        first = [c for c in cols[k] if compact(c) in exact.get(k, ())]
        cols[k] = first + [c for c in cols[k] if c not in first]
    hint = {c: unit_of_text(c) or _unit_row_hint(frame[c]) for k in cols for c in cols[k]}
    notes, out = [], {}
    if report is not None:      # every column of the sheet and what was made of it (written to Merge_Report.xlsx, sheet Test_Sheet_Units)
        used = {c for k in cols for c in cols[k]}
        for c in frame.columns:
            if c != key and c not in used:
                smp = " | ".join(frame[c].dropna().astype(str).head(3))
                report.append({"Column": str(c), "Read as": "not a gross / net rate", "Unit": "", "Why": "", "Samples": smp})
    for k in ("gross", "net"):
        other = "net" if k == "gross" else "gross"
        pieces = []
        for c in cols[k]:
            unit, why = hint[c], "from its header"
            if unit is None:
                unit = next((hint[o] for o in cols[other] if hint[o]), None)
                why = "same unit as the other rate column"
            if unit is None:
                cell_units = frame[c].dropna().astype(str).map(unit_of_text).dropna()
                unit = None if cell_units.empty else cell_units.mode().iat[0]
                why = "from the values' own units"
            if unit is None:
                unit = _infer_by_history(pd.to_numeric(frame[c], errors="coerce"), frame[key], ref.get(k))
                why = "inferred: about 6.29 times the earlier tests" if unit == "bbl" else "inferred: same size as the earlier tests"
            if unit is None:
                unit, why = "m3", "no unit found, taken as m3/d"
            vals, own = _cells(frame[c], unit)
            if report is not None:
                report.append({"Column": str(c), "Read as": k, "Unit": "barrels -> m3/d" if (unit == "bbl" or own) else "m3/d", "Why": why,
                               "Samples": " | ".join(frame[c].dropna().astype(str).head(3))})
            if unit == "bbl" or own:
                notes.append(f"{k} column '{c}' is in barrels ({why}); converted to m3/d" + (f" ({own} cell(s) with their own unit)" if own else ""))
            pieces.append(vals)
        if pieces:
            comb = pd.concat(pieces, axis=1).bfill(axis=1).iloc[:, 0]
            notes.append(f"{k} <- " + ", ".join(f"'{c}'" for c in cols[k]))
        else:
            comb = pd.Series(np.nan, index=frame.index)
        out[k] = comb
    return out, notes
