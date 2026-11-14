"""
dash_core.py: the data helpers of the Petrobel dashboard that the Digitalization section needs (column recognition, running-hours matrix,
well-test and DFL history, run status). These are the same functions as in the full dashboard's app.py; here they are plain Python
(no Streamlit) so that both the app and export_data.py can use them.
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

WELL_COL = "Standard_Well"
NOT_A_WELL = {"UNKNOWN", "NAN", "NONE", ""}   # labels in the well column that are not real wells (hidden)
DAYFIRST = False                              # set True if dates in the CSV look like 31/12/2025
DFL_DATE_COLUMN = None                        # exact header of the DFL date column, if auto-detection picks the wrong one
NLAP_COLUMN = None                            # exact header of the NLAP column, if it is not found automatically

CANDIDATES = {
    "vendor": ("VENDOR",),
    "location": ("LOCATION",),
    "zone": ("ZONE",),
    "lift": ("ART.LIFT TYPE", "ART. LIFT TYPE", "ART LIFT"),
    "pump_params": ("PUMPING PARAMETERS",),
    "test_date": ("DATE OF TEST", "TESTING"),
    "rate": ("RATE CM/D (ACTUAL)", "RATE CM/D", "GROSS RATE", "GROSS PRODUCTION", "TOTAL FLUID", "FLUID RATE", "ACTUAL M3/D", "ACT , M3/D", "ACT M3/D"),
    "whp": ("WHP (PSI)", "WHP"),
    "wc": ("W.C (ACTUAL)", "W.C", "ACT.W.C %", "ACT WC", "WATER CUT"),
    "net_oil": ("NET OIL CM/D (ACTUAL)", "NET OIL CM/D", "NET OIL", "NET RATE", "OIL RATE", "ACTUAL NET", "ACT NET"),
    "pump_depth": ("PUMP DEPTH, MT (TVD)", "PUMP DEPTH"),
    "liq_level": ("LIQUID LEVEL, MT (MD)", "LIQUID LEVEL"),
    "casing": ("CASING PRESS. (PSIG)", "CASING PRESS"),
    "annulus": ("LIQUID % IN ANNULUS",),
    "bhp_pump": ("BHP@ PUMP DEPTH, (PSIG)", "BHP@ PUMP"),
    "bhp_datum": ("BHP@ DATUM, PSIG", "BHP@ DATUM"),
    "dyno": ("DYNAMOMETER TEST RESULTS",),
    "action": ("RECOMMENDED ACTION",),
    "nlap": ("NLAP", "NET LIQUID ABOVE PUMP", "NET LIQUID"),
    "dfl_date": ("TESTING DATE", "DATE OF TESTING", "DFL DATE", "DATE OF DFL", "DFL TEST DATE", "DATE OF SURVEY", "SURVEY DATE", "TESTING"),
}


def norm(s) -> str:
    """Normalise a column name: collapse whitespace/newlines, upper-case."""
    return re.sub(r"\s+", " ", str(s)).strip().upper()


def resolve_columns(columns) -> dict:
    lookup = {norm(c): c for c in columns}
    out = {}
    for key, cands in CANDIDATES.items():
        found = next((lookup[norm(c)] for c in cands if norm(c) in lookup), None)
        if found is None:
            for c in cands:
                n = norm(c)
                found = next((orig for k, orig in lookup.items() if n in k), None)
                if found:
                    break
        out[key] = found
    return out


def to_num(s: pd.Series) -> pd.Series:
    """Robust numeric conversion (handles '%', thousands separators, stray text)."""
    cleaned = s.astype(str).str.replace(",", "", regex=False)
    return pd.to_numeric(cleaned.str.extract(r"(-?\d+(?:\.\d+)?)", expand=False), errors="coerce")


def parse_dates(s: pd.Series) -> pd.Series:
    """Dates from text, real dates or Excel serial numbers."""
    import warnings

    if pd.api.types.is_numeric_dtype(s):
        v = pd.to_numeric(s, errors="coerce")
        if v.dropna().between(20000, 80000).mean() > 0.8:
            return pd.to_datetime(v, unit="D", origin="1899-12-30", errors="coerce")
        return pd.Series(pd.NaT, index=s.index)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.to_datetime(s, errors="coerce", dayfirst=DAYFIRST)


def auto_date_col(frame: pd.DataFrame, exclude=()):
    """Best guess for a date column: header contains DATE and most values parse as dates."""
    best, best_key = None, (0.5, False, 0)
    for c in frame.columns:
        if c in exclude or "DATE" not in norm(c) or re.match(r"^\d{4}-\d{2}-\d{2}", str(c)):
            continue
        vals = frame[c].dropna()
        if len(vals) < 3:
            continue
        parsed = parse_dates(vals)
        key = (round(float(parsed.notna().mean()), 2), "DFL" in norm(c), int(parsed.nunique()))
        if key > best_key:
            best, best_key = c, key
    return best


def detect_date_columns(columns: tuple) -> list:
    """Columns whose *header* is a date (daily running-hours columns), sorted by date."""
    found = []
    for c in columns:
        s = str(c).strip()
        if len(s) < 6 or not re.search(r"\d", s) or not re.search(r"[-/.\s]", s):
            continue
        ts = pd.to_datetime(s, errors="coerce", dayfirst=DAYFIRST)
        if pd.notna(ts):
            found.append((c, ts))
    return sorted(found, key=lambda x: x[1])


def build_hours_matrix(_df: pd.DataFrame, key: str, date_cols: tuple) -> pd.DataFrame:
    """Wells x dates matrix of average running hours."""
    cols = [c for c, _ in date_cols]
    if not cols:
        return pd.DataFrame()
    num = _df[cols].apply(pd.to_numeric, errors="coerce")
    num.columns = pd.to_datetime([t for _, t in date_cols])
    num.index = _df[WELL_COL].values
    mat = num.T.groupby(level=0).mean().T if num.columns.duplicated().any() else num
    mat = mat.groupby(level=0).mean()
    return mat.sort_index(axis=1)


def build_tests(_df: pd.DataFrame, key: str, mapping: tuple) -> pd.DataFrame:
    """Clean, de-duplicated well-test history for all wells."""
    m = dict(mapping)
    td = m.get("test_date")
    if not td:
        return pd.DataFrame(columns=["Well", "Date", "Rate", "WHP", "WC", "Net Oil"])
    t = pd.DataFrame(
        {"Well": _df[WELL_COL].values, "Date": pd.to_datetime(_df[td], errors="coerce", dayfirst=DAYFIRST).values}
    )
    for k, label in [("rate", "Rate"), ("whp", "WHP"), ("wc", "WC"), ("net_oil", "Net Oil")]:
        t[label] = to_num(_df[m[k]]).values if m.get(k) else np.nan
    t = t.dropna(subset=["Date"]).sort_values(["Well", "Date"])
    t = t.drop_duplicates(["Well", "Date"], keep="last").reset_index(drop=True)
    wc_max = t["WC"].dropna().max()
    if pd.notna(wc_max) and wc_max <= 1.0:  # water cut stored as a fraction -> convert to %
        t["WC"] = t["WC"] * 100
    return t


def build_dfl(_df: pd.DataFrame, key: str, mapping: tuple) -> pd.DataFrame:
    """DFL history: one row per well and DFL date."""
    m = dict(mapping)
    dd = m.get("dfl_date")
    if not dd:
        return pd.DataFrame(columns=DFL_COLS)
    t = pd.DataFrame({"Well": _df[WELL_COL].values, "Date": parse_dates(_df[dd]).values})
    for k, label in [("nlap", "NLAP"), ("liq_level", "Liquid level"), ("casing", "Casing pressure"),
                     ("bhp_pump", "BHP @ pump"), ("pump_depth", "Pump depth")]:
        t[label] = to_num(_df[m[k]]).values if m.get(k) else np.nan
    if not m.get("nlap"):  # no NLAP column: approximate it as pump depth minus liquid level
        t["NLAP"] = (t["Pump depth"] - t["Liquid level"]).clip(lower=0)
    return t.dropna(subset=["Date"]).sort_values(["Well", "Date"]).drop_duplicates(["Well", "Date"], keep="last").reset_index(drop=True)


def status_limit() -> pd.Timestamp:
    """Latest date a running-hours reading may have to count as 'last available' (a day of slack for time zones; later columns are ignored)."""
    return pd.Timestamp.now().normalize() + pd.Timedelta(days=1)


def up_to_today(mat: pd.DataFrame) -> pd.DataFrame:
    """The hours matrix without columns dated in the future (e.g. a month sheet pre-filled with 0 or blanks for days still to come)."""
    if mat is None or mat.empty:
        return mat
    keep = mat.columns[mat.columns <= status_limit()]
    return mat.loc[:, keep] if len(keep) else mat


def run_status_map(mat: pd.DataFrame) -> dict:
    """Well -> RUNNING / STOPPED / NO DATA from the most recent available running-hours reading: not 0 = running, 0 = stopped."""
    if mat.empty:
        return {}
    last = up_to_today(mat).ffill(axis=1).iloc[:, -1]
    return {w: ("NO DATA" if pd.isna(v) else "RUNNING" if v != 0 else "STOPPED") for w, v in last.items()}


DFL_COLS = ["Well", "Date", "NLAP", "Liquid level", "Casing pressure", "BHP @ pump", "Pump depth"]
