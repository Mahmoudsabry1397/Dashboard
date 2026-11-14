"""
STEP 2 — Merge the cleaned sheets into one master table
=======================================================
Reads Cleaned_<file>.xlsx (written by step 1), merges them on 'Standard_Well' and writes
Master_Dashboard_Database.csv for the dashboard.

What it protects you from
  * row explosion  - tables with several rows per well are collapsed to ONE row per well
                     (the latest), except the single "history" table (DATABASE = well tests).
  * silent column clashes - columns with the same name in two files are combined into one column
                     (first non-empty value) instead of Vendor / Vendor_db / Vendor_dfl.
  * bad days       - if the new master looks broken (far fewer wells / rows than yesterday), or a
                     required file is missing, yesterday's master is KEPT and the script exits with 1.
  * unmatched wells - REVIEW_NEEDED rows are kept out of the master and listed in the report.

Extra outputs:  Merge_Report.xlsx (what happened), data_status.json (when/what was updated) and the full
histories DFL_History.csv / Test_History.csv (used by the dashboard's NLAP and test-history charts).
Set the environment variable FORCE_OVERWRITE=1 to bypass the safety checks once.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from datetime import datetime

import numpy as np
import pandas as pd

try:                                  # gross / net rate recognition and barrel -> m3 conversion for well-test sheets
    import well_test_units as wtu
except Exception:  # noqa: BLE001
    wtu = None

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
INPUT_DIR = os.path.join(PROJECT_DIR, "03_DATA", "PROCESSED")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "03_DATA", "PROCESSED")
CLEANED_PREFIX = "Cleaned_"
MASTER_CSV = "Master_Dashboard_Database.csv"
REPORT_NAME = "Merge_Report.xlsx"
STATUS_JSON = "data_status.json"
KEY = "Standard_Well"
NOT_A_WELL = {"UNKNOWN", "NAN", "NONE", ""}   # labels that are not real wells -> treated like REVIEW_NEEDED

STALE_HOURS = 36            # warn if a cleaned file is older than this (step 1 may not have run)
MIN_ROW_RATIO = 0.5         # refuse to overwrite if new rows < 50 % of the previous master
MIN_WELL_RATIO = 0.8        # ... or if distinct wells < 80 % of the previous master
EXCLUDE_REVIEW_ROWS = True  # keep REVIEW_NEEDED rows out of the master (they are listed in the report)
ALLOW_PARTIAL = False       # True = still build the master when a required file is missing

# Order matters: the first available source with "base": True becomes the starting table.
#   "mode": "latest"  -> keep ONE row per well (the last one; use "sort_by" to define "latest")
#           "history" -> keep every row (use for the table with many rows per well, e.g. well tests)
#   "drop": raw well-name columns that are no longer needed once Standard_Well exists
#   "sort_by": date column used to order rows so the LAST row = latest record: a list of candidate
#              column names, or "auto" to detect the date column by itself (the choice is logged)
SOURCES = [
    {
        "name": "Petrobel Cumulative DFL.xlsx",
        "drop": ["Well No."],
        "mode": "history",   # Keeps all historical rows (Base Table)
        "sort_by": "auto",
        "base": True,
        "export_history": "DFL_History.csv",      # full DFL history for the NLAP trend
    },
    {
        "name": "Petrobel Wells Operation Mode.xlsx",
        "drop": ["Well Name"],
        "mode": "latest",    # Keeps 1 row per well
    },
    {
        "name": "DATABASE.xlsx",
        "drop": ["Well\n Name"],
        "mode": "latest",    # Keeps 1 row per well (latest test)
        "sort_by": ["DATE OF TEST", "TESTING"],
    },
    {"name": "SRP Test Results.xlsx", "drop": ["Well", "No.", "Type", "Status"], "mode": "latest", "required": False},
    {
        "name": "Monthly.xlsx",
        "drop": ["Well Name"],
        "mode": "latest",    # Keeps 1 row per well
    },
]

log = logging.getLogger("merge")


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def norm_header(s) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().upper()


def clean_col(c) -> str:
    """Column labels become plain text; date headers become 'YYYY-MM-DD'."""
    if isinstance(c, (pd.Timestamp, datetime)):
        return pd.Timestamp(c).strftime("%Y-%m-%d")
    return str(c)


def find_col(df: pd.DataFrame, candidates) -> str | None:
    lookup = {norm_header(c): c for c in df.columns}
    for cand in candidates:
        if norm_header(cand) in lookup:
            return lookup[norm_header(cand)]
    for cand in candidates:
        n = norm_header(cand)
        hit = next((orig for k, orig in lookup.items() if n in k), None)
        if hit:
            return hit
    return None


def auto_date_column(df: pd.DataFrame) -> str | None:
    """Pick the column (name contains 'DATE') whose values parse best as dates."""
    import warnings

    best, best_key = None, (0.5, 0)
    for c in df.columns:
        if "DATE" not in norm_header(c) or re.match(r"^\d{4}-\d{2}-\d{2}", str(c)) or pd.api.types.is_numeric_dtype(df[c]):
            continue
        vals = df[c].dropna()
        if len(vals) < 3:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            parsed = pd.to_datetime(vals, errors="coerce")
        key = (float(parsed.notna().mean()), int(parsed.nunique()))
        if key > best_key:
            best, best_key = c, key
    return best


def load_source(src: dict):
    """Returns (df, info) – df is None if the cleaned file is missing."""
    path = os.path.join(INPUT_DIR, CLEANED_PREFIX + src["name"])
    if not os.path.exists(path):
        return None, {"path": path}

    df = pd.read_excel(path)
    df.columns = [clean_col(c) for c in df.columns]
    df = df.dropna(how="all")
    df = df.drop(columns=[c for c in df.columns if c.startswith("Unnamed:") and df[c].isna().all()])

    if KEY not in df.columns:
        raise ValueError(f"'{KEY}' column missing – did step 1 run for this file?")
    df[KEY] = df[KEY].astype(str).str.strip()

    info = {"path": path, "modified": datetime.fromtimestamp(os.path.getmtime(path))}
    info["rows_in"] = len(df)
    info["wells_in"] = df[KEY].nunique()
    info["max_rows_per_well"] = int(df.groupby(KEY).size().max()) if len(df) else 0
    info["per_well_rows"] = df.groupby(KEY).size()

    # raw well-name columns are no longer needed
    drop_norm = {norm_header(c) for c in src.get("drop", [])}
    df = df.drop(columns=[c for c in df.columns if norm_header(c) in drop_norm])

    # keep unmatched wells out of the master
    review = df[KEY].str.upper().str.startswith("REVIEW_NEEDED") | df[KEY].str.upper().isin(NOT_A_WELL)
    info["review_rows"] = int(review.sum())
    info["review_labels"] = df.loc[review, KEY].value_counts()
    if EXCLUDE_REVIEW_ROWS:
        df = df[~review]

    # order rows so that the LAST row of a well is its latest record
    spec = src.get("sort_by")
    if spec:
        sort_col = auto_date_column(df) if spec == "auto" else (find_col(df, spec) or auto_date_column(df))
        if sort_col:
            order = pd.to_datetime(df[sort_col], errors="coerce")
            df = df.assign(_o=order).sort_values([KEY, "_o"], kind="stable", na_position="first").drop(columns="_o")
            log.info("   %s: rows ordered by column '%s' (latest record = newest date)", src["name"], str(sort_col).replace("\n", " "))
        else:
            log.warning("   %s: no date column found - 'latest' means the last row in the file. "
                        "Set \"sort_by\" in SOURCES if the file is not in date order.", src["name"])

    hist_file = src.get("export_history")
    if hist_file and write_csv_atomic(df, os.path.join(OUTPUT_DIR, hist_file)):
        log.info("   %s: full history saved to %s (%d rows)", src["name"], hist_file, len(df))

    if src["mode"] == "latest":
        df = df.drop_duplicates(subset=KEY, keep="last")
    info["rows_used"] = len(df)
    info["cols_used"] = len(df.columns) - 1
    return df.reset_index(drop=True), info


def merge_two(left: pd.DataFrame, right: pd.DataFrame, tag: str):
    """Outer-merge on KEY; same-named columns are combined (first non-empty value)."""
    overlap = [c for c in right.columns if c != KEY and c in left.columns]
    merged = left.merge(right, on=KEY, how="outer", suffixes=("", f"__{tag}"))
    if len(merged) > len(left) + len(right):  # only possible with a many-to-many join
        raise ValueError(f"merging '{tag}' multiplied the rows ({len(left)} + {len(right)} -> {len(merged)})")
    notes = []
    for c in overlap:
        rc = f"{c}__{tag}"
        both = merged[c].notna() & merged[rc].notna()
        differ = both & (merged[c].astype(str).str.strip() != merged[rc].astype(str).str.strip())
        merged[c] = merged[c].where(merged[c].notna(), merged[rc])
        merged = merged.drop(columns=rc)
        notes.append({"Column": c, "Combined with": tag, "Rows where both files had a value": int(both.sum()),
                      "…of which values differ": int(differ.sum())})
    return merged, notes


def previous_master_stats(path: str):
    if not os.path.exists(path):
        return None
    try:
        prev = pd.read_csv(path, usecols=[KEY])
        return {"rows": len(prev), "wells": prev[KEY].nunique()}
    except Exception:  # noqa: BLE001
        return None


def write_csv_atomic(df: pd.DataFrame, path: str, retries: int = 3) -> bool:
    tmp = path + ".tmp"
    df.to_csv(tmp, index=False, encoding="utf-8-sig")
    for _ in range(retries):
        try:
            os.replace(tmp, path)
            return True
        except PermissionError:  # e.g. file open in Excel / locked by OneDrive
            time.sleep(2)
    log.error("Could not replace %s – close it if it is open in Excel, then run again.", os.path.basename(path))
    if os.path.exists(tmp):
        os.remove(tmp)
    return False


UNIT_NOTES: list = []                     # which rate columns were used and any conversion, in the latest run (status + merge report)
UNIT_REPORT: list = []                    # every column of the test sheets and how it was read (sheet Test_Sheet_Units of Merge_Report.xlsx)
SRP_STATE = "SRP_Test_State.json"          # last SRP reading seen for every well (stops an old file being added again later)
SRP_COMPARISON = "SRP_Test_Comparison.csv"  # what was decided for each well at the latest SRP upload
SRP_FIELDS = ["RATE CM/D (ACTUAL)", "NET OIL CM/D (ACTUAL)", "W.C (ACTUAL)"]


def _close(a: float, b: float, atol: float, pct: bool = False) -> bool:
    """Equal within rounding. Water cut may be stored as a fraction in one file and as % in the other."""
    def near(x, y):
        return abs(x - y) <= max(atol, 0.002 * max(abs(x), abs(y)))      # rounding of the source (e.g. barrels) is not a new test
    return near(a, b) or (pct and (near(a * 100, b) or near(a, b * 100)))


def _same_reading(a, b) -> bool:
    """True when two (rate, net oil, water cut) readings are the same test: every value present in both agrees."""
    pairs = [(x, y, k) for k, (x, y) in enumerate(zip(a, b)) if pd.notna(x) and pd.notna(y)]
    if not pairs:
        return False
    return all(_close(float(x), float(y), 0.05 if k == 2 else 0.02, pct=(k == 2)) for x, y, k in pairs)


def _load_srp_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def build_test_history():
    """Append newly observed tests to the durable history.

    DATABASE tests are added as before. A new SRP file (Production Test Results / Water Cut Samples) is handled per well:
      1. its reading (rate, net oil, water cut) is compared with the LAST available test of that well;
      2. the same test  -> ignored;  a different test -> added as a NEW test dated the day the file was uploaded, old tests are kept.
    A reading that is identical to the one already seen from the previous SRP file is not added again later (state file).
    Returns a small dict describing what happened (or None).
    """
    UNIT_NOTES.clear()
    UNIT_REPORT.clear()
    if wtu is None:
        UNIT_NOTES.append("WARNING: well_test_units.py was not found next to merge_data.py - units are NOT converted. Copy it into the dashboard app folder.")
        log.warning("well_test_units.py not found - barrel sheets are not converted")
    history_path = os.path.join(OUTPUT_DIR, "Test_History.csv")
    history_cols = [KEY, "DATE OF TEST", "RATE CM/D (ACTUAL)", "NET OIL CM/D (ACTUAL)", "W.C (ACTUAL)", "WHP (PSI)"]
    existing = pd.DataFrame(columns=history_cols)
    if os.path.exists(history_path):
        try:
            existing = pd.read_csv(history_path, low_memory=False)
            existing.columns = [clean_col(c) for c in existing.columns]
            for col in history_cols:
                if col not in existing:
                    existing[col] = pd.NA
        except Exception as exc:
            log.warning("Could not read prior Test_History.csv; rebuilding from current sources (%s)", exc)
    def read_clean(name):
        p = os.path.join(INPUT_DIR, CLEANED_PREFIX + name)
        if not os.path.exists(p):
            return pd.DataFrame()
        frame = pd.read_excel(p)
        frame.columns = [clean_col(c) for c in frame.columns]
        frame = frame.dropna(how="all")
        frame[KEY] = frame.get(KEY, "").astype(str).str.strip()
        review = frame[KEY].str.upper().str.startswith("REVIEW_NEEDED") | frame[KEY].str.upper().isin(NOT_A_WELL)
        return frame.loc[~review].copy()

    db = read_clean("DATABASE.xlsx")
    srp = read_clean("SRP Test Results.xlsx")
    def first(frame, aliases):
        cols = [c for c in frame.columns if re.sub(r"[^A-Z0-9]", "", norm_header(c)) in aliases]
        if not cols:
            return pd.Series(pd.NA, index=frame.index)
        return frame[cols].bfill(axis=1).iloc[:, 0]
    def canonical(frame, source_tag, ref=None):
        if frame.empty:
            return pd.DataFrame(columns=[KEY, "DATE OF TEST", "RATE CM/D (ACTUAL)", "NET OIL CM/D (ACTUAL)", "W.C (ACTUAL)"])
        gross_aliases = {"RATECMDACTUAL", "GROSSRATE", "GROSSPRODUCTION", "TOTALFLUID", "FLUIDRATE", "ACTUALM3D", "ACTM3D"}
        net_aliases = {"NETOILCMDACTUAL", "NETRATE", "NETOIL", "OILRATE", "ACTUALNET", "ACTNET"}
        if wtu is not None:
            # any spelling of gross / net (BFPD, BLPD, M3L/day, Total fluid ... BOPD, Net oil ...); barrels are converted to m3/d
            rep_rows = []
            rates, unit_notes = wtu.extract_rates(frame, KEY, ref, {"gross": gross_aliases, "net": net_aliases}, report=rep_rows)
            UNIT_REPORT.extend({"Sheet": source_tag, **r} for r in rep_rows)
            for n in unit_notes:
                UNIT_NOTES.append(f"{source_tag}: {n}")
                log.info("   %s: %s", source_tag, n)
            gross, net = rates["gross"], rates["net"]
        else:
            gross, net = first(frame, gross_aliases), first(frame, net_aliases)
        out = pd.DataFrame({
            KEY: frame[KEY],
            "DATE OF TEST": first(frame, {"DATEOFTEST", "TESTING"}),
            "RATE CM/D (ACTUAL)": gross,
            "NET OIL CM/D (ACTUAL)": net,
            "W.C (ACTUAL)": first(frame, {"WCACTUAL", "WC", "ACTWCPCT", "ACTWC"}),
            "WHP (PSI)": first(frame, {"WHPPSI", "WHP"}),
        })
        # Retain source-specific columns as well as the common dashboard fields,
        # so reports can include the full test record from either workbook.
        for col in frame.columns:
            if col != KEY:
                out[f"{source_tag} | {col}"] = frame[col].to_numpy()
        return out
    def parse_test_dates(values):
        parsed = pd.to_datetime(values, errors="coerce")
        numeric = pd.to_numeric(values, errors="coerce")
        # Excel serial dates are occasionally exported as plain numbers.
        serial = numeric.between(20000, 80000)
        if serial.any():
            parsed.loc[serial] = pd.to_datetime(numeric.loc[serial], unit="D", origin="1899-12-30", errors="coerce")
        # Repair serials previously misread as nanoseconds since the Unix epoch.
        ancient = parsed.notna() & (parsed < pd.Timestamp("2000-01-01"))
        if ancient.any():
            old_serial = parsed.loc[ancient].astype("int64")
            recover = old_serial.between(20000, 80000)
            idx = old_serial.index[recover]
            parsed.loc[idx] = pd.to_datetime(old_serial.loc[idx], unit="D", origin="1899-12-30", errors="coerce")
        return parsed
    def last_rates(frame):
        """Last known gross / net rate (m3/d) per well, used to tell barrels from m3 when a sheet names no unit."""
        if frame is None or frame.empty:
            return {}
        f = frame[[KEY, "DATE OF TEST", "RATE CM/D (ACTUAL)", "NET OIL CM/D (ACTUAL)"]].copy()
        f[KEY] = f[KEY].astype(str).str.strip()
        f["_d"] = parse_test_dates(f["DATE OF TEST"])
        f = f.dropna(subset=["_d"]).sort_values([KEY, "_d"], kind="stable").drop_duplicates(KEY, keep="last").set_index(KEY)
        return {"gross": pd.to_numeric(f["RATE CM/D (ACTUAL)"], errors="coerce"), "net": pd.to_numeric(f["NET OIL CM/D (ACTUAL)"], errors="coerce")}
    db_c = canonical(db, "DATABASE", last_rates(existing))
    srp_c = canonical(srp, "SRP", last_rates(pd.concat([existing, db_c], ignore_index=True)))
    if len(db_c):
        db_c["DATE OF TEST"] = parse_test_dates(db_c["DATE OF TEST"])
        db_c = db_c.sort_values("DATE OF TEST", kind="stable", na_position="first")
    latest_db = db_c.drop_duplicates(KEY, keep="last").set_index(KEY) if len(db_c) else pd.DataFrame()
    srp_stats, srp_state_new, srp_log = None, None, []
    if len(srp_c):
        if "DATE OF TEST" in srp.columns:                      # written by step 1: the day the file was uploaded
            srp_c["DATE OF TEST"] = srp["DATE OF TEST"].to_numpy()
        srp_c["DATE OF TEST"] = parse_test_dates(srp_c["DATE OF TEST"]).fillna(pd.Timestamp.now().normalize())
        for col in SRP_FIELDS:
            srp_c[col] = pd.to_numeric(srp_c[col], errors="coerce")
        srp_c = srp_c.sort_values("DATE OF TEST", kind="stable").drop_duplicates(KEY, keep="last").reset_index(drop=True)

        # last available test of every well BEFORE this SRP file is applied
        prior = pd.concat([existing[[KEY, "DATE OF TEST"] + SRP_FIELDS], db_c[[KEY, "DATE OF TEST"] + SRP_FIELDS] if len(db_c) else None], ignore_index=True)
        prior[KEY] = prior[KEY].astype(str).str.strip()
        prior["DATE OF TEST"] = parse_test_dates(prior["DATE OF TEST"])
        for col in SRP_FIELDS:
            prior[col] = pd.to_numeric(prior[col], errors="coerce")
        prior = prior.dropna(subset=["DATE OF TEST"]).sort_values([KEY, "DATE OF TEST"], kind="stable").drop_duplicates(KEY, keep="last").set_index(KEY)

        state_path = os.path.join(OUTPUT_DIR, SRP_STATE)
        srp_state = _load_srp_state(state_path)
        srp_state_new = dict(srp_state)
        keep = []
        for _, r in srp_c.iterrows():
            w, vals = r[KEY], tuple(r[c] for c in SRP_FIELDS)
            last = prior.loc[w] if w in prior.index else None
            seen = srp_state.get(w)
            if pd.notna(vals[0]) and pd.notna(vals[1]) and vals[0] == 0 and vals[1] == 0:
                decision = "Zero gross and net (no fluid measured) - ignored"
            elif last is not None and _same_reading(vals, tuple(last[c] for c in SRP_FIELDS)):
                decision = "Same as the last test - ignored"
            elif seen is not None and _same_reading(vals, tuple(np.nan if v is None else v for v in seen["values"])):
                decision = "Already added from an earlier SRP file - ignored"
            else:
                decision = "New test - added"
                keep.append(True)
            if decision != "New test - added":
                keep.append(False)
            srp_log.append({"Well": w, "Test date used": r["DATE OF TEST"].date(), "SRP rate": vals[0], "SRP net oil": vals[1], "SRP water cut": vals[2],
                            "Last test date": last["DATE OF TEST"].date() if last is not None else None,
                            "Last test rate": last[SRP_FIELDS[0]] if last is not None else None,
                            "Last test net oil": last[SRP_FIELDS[1]] if last is not None else None,
                            "Last test water cut": last[SRP_FIELDS[2]] if last is not None else None, "Decision": decision})
            srp_state_new[w] = {"values": [None if pd.isna(v) else float(v) for v in vals], "date": str(r["DATE OF TEST"].date())}
        n_add = sum(keep)
        srp_stats = {"added": int(n_add), "same_as_last_test": sum(1 for x in srp_log if x["Decision"].startswith("Same")),
                     "already_added": sum(1 for x in srp_log if x["Decision"].startswith("Already")),
                     "zero_rate_ignored": sum(1 for x in srp_log if x["Decision"].startswith("Zero")), "test_date": str(srp_c["DATE OF TEST"].max().date())}
        log.info("SRP tests: %d new test(s) added (dated %s), %d same as the last test (ignored), %d already added earlier, %d with zero gross and net (ignored)",
                 srp_stats["added"], srp_stats["test_date"], srp_stats["same_as_last_test"], srp_stats["already_added"], srp_stats["zero_rate_ignored"])
        srp_c = srp_c[pd.Series(keep, index=srp_c.index)]
        srp_c["WHP (PSI)"] = pd.NA          # not measured in the SRP file: leave blank rather than copy an older value
    # Keep prior dated tests even if the newest DATABASE/SRP workbooks no longer
    # contain them. New DATABASE records update matching well/date keys; a new SRP test is a new row
    # dated at the upload (appended last, so it wins if a test already exists for that same well and day).
    combined = pd.concat([existing, db_c, srp_c], ignore_index=True)
    if not combined.empty:
        for col in history_cols:
            if col not in combined:
                combined[col] = pd.NA
        combined[KEY] = combined[KEY].astype(str).str.strip()
        combined["DATE OF TEST"] = parse_test_dates(combined["DATE OF TEST"])
        invalid = combined[KEY].str.upper().str.startswith("REVIEW_NEEDED") | combined[KEY].str.upper().isin(NOT_A_WELL)
        combined = combined.loc[~invalid & combined["DATE OF TEST"].notna()].copy()
        def last_nonblank(values):
            values = values.dropna()
            return values.iloc[-1] if len(values) else pd.NA
        combined = combined.groupby([KEY, "DATE OF TEST"], sort=False, as_index=False).agg(last_nonblank)
        combined = combined.sort_values([KEY, "DATE OF TEST"], kind="stable").reset_index(drop=True)
    if write_csv_atomic(combined, history_path) and srp_state_new is not None:
        try:
            with open(os.path.join(OUTPUT_DIR, SRP_STATE), "w", encoding="utf-8") as fh:
                json.dump(srp_state_new, fh)
            write_csv_atomic(pd.DataFrame(srp_log), os.path.join(OUTPUT_DIR, SRP_COMPARISON))
        except OSError as exc:
            log.warning("Could not save the SRP comparison (%s)", exc)
    return srp_stats


def write_report(sheets: dict, path: str) -> None:
    tmp = path[:-5] + ".tmp.xlsx"
    with pd.ExcelWriter(tmp, engine="openpyxl") as xw:
        for name, frame in sheets.items():
            frame.to_excel(xw, sheet_name=name, index=False)
            ws = xw.sheets[name]
            ws.freeze_panes = "A2"
            for col in ws.columns:
                width = max(len(str(c.value)) if c.value is not None else 0 for c in col[:200])
                ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 60)
    os.replace(tmp, path)


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
def run() -> int:
    problems, abort_reasons = 0, []
    loaded, summary, coverage, review_rows, notes = [], [], {}, [], []
    now = datetime.now()

    for src in SOURCES:
        name = src["name"]
        try:
            df, info = load_source(src)
        except Exception as exc:  # noqa: BLE001
            log.error("%s: %s", name, exc)
            problems += 1
            abort_reasons.append(f"{name}: {exc}")
            continue
        if df is None:
            required = src.get("required", True)
            (log.error if required else log.warning)("%s: cleaned file not found (%s)", name, os.path.basename(info["path"]))
            if required:
                problems += 1
                abort_reasons.append(f"{name}: cleaned file missing")
            continue

        age_h = (now - info["modified"]).total_seconds() / 3600
        if age_h > STALE_HOURS:
            log.warning("%s: cleaned file is %.0f h old – did step 1 run today?", name, age_h)
            notes.append({"Level": "WARNING", "Message": f"{name}: cleaned file is {age_h:.0f} h old"})
        log.info("%s: %d rows (%d wells, max %d rows/well) -> %d used [%s]%s", name, info["rows_in"], info["wells_in"],
                 info["max_rows_per_well"], info["rows_used"], src["mode"],
                 f", {info['review_rows']} REVIEW_NEEDED row(s) excluded" if info["review_rows"] else "")
        loaded.append((src, df, info))
        coverage[name] = info["per_well_rows"]
        review_rows += [{"Source": name, "Label": k, "Rows": int(v)} for k, v in info["review_labels"].items()]
        summary.append({
            "Source": name, "Mode": src["mode"], "Cleaned file updated": info["modified"].strftime("%Y-%m-%d %H:%M"),
            "Rows in": info["rows_in"], "Distinct wells": info["wells_in"], "Max rows per well": info["max_rows_per_well"],
            "REVIEW_NEEDED rows excluded": info["review_rows"] if EXCLUDE_REVIEW_ROWS else 0,
            "Rows used": info["rows_used"], "Columns used": info["cols_used"],
        })

    if abort_reasons and not ALLOW_PARTIAL:
        log.error("Missing/unreadable required input – master NOT updated.")
        _save_report(summary, coverage, [], review_rows, notes + [{"Level": "ERROR", "Message": m} for m in abort_reasons])
        return 1
    if not loaded:
        log.error("No cleaned files found – run step 1 first.")
        return 1

    srp_stats = build_test_history()
    for n in UNIT_NOTES:
        notes.append({"Level": "INFO", "Message": "Units: " + n})
    if srp_stats:
        notes.append({"Level": "INFO", "Message": f"SRP tests: {srp_stats['added']} new test(s) added dated {srp_stats['test_date']}, "
                      f"{srp_stats['same_as_last_test']} same as the last test (ignored), {srp_stats['already_added']} already added earlier, "
                      f"{srp_stats['zero_rate_ignored']} with zero gross and net (ignored) (see {SRP_COMPARISON})"})

    # ── start with the first base source, then merge the rest ────────────────
    base_idx = next((i for i, (s, _, _) in enumerate(loaded) if s.get("base")), 0)
    master = loaded[base_idx][1].copy()
    log.info("Base table: %s", loaded[base_idx][0]["name"])
    column_notes = []
    for i, (src, df, _) in enumerate(loaded):
        if i == base_idx:
            continue
        try:
            master, n = merge_two(master, df, tag=re.sub(r"\W+", "", src["name"])[:12])
        except ValueError as exc:
            log.error("%s", exc)
            _save_report(summary, coverage, column_notes, review_rows, notes + [{"Level": "ERROR", "Message": str(exc)}])
            return 1
        column_notes += n
        log.info("-> merged %s (%d columns combined with existing ones)", src["name"], len(n))

    master = master.sort_values(KEY, kind="stable").reset_index(drop=True)
    master = master[[KEY] + [c for c in master.columns if c != KEY]]
    n_rows, n_wells, n_cols = len(master), master[KEY].nunique(), len(master.columns)
    log.info("Master table: %d rows | %d wells | %d columns", n_rows, n_wells, n_cols)

    # ── safety checks against yesterday's master ─────────────────────────────
    out_path = os.path.join(OUTPUT_DIR, MASTER_CSV)
    prev = previous_master_stats(out_path)
    force = os.environ.get("FORCE_OVERWRITE") == "1"
    if n_rows == 0:
        abort_reasons.append("merged table is empty")
    if prev and not force:
        if n_rows < prev["rows"] * MIN_ROW_RATIO:
            abort_reasons.append(f"rows dropped from {prev['rows']} to {n_rows}")
        if n_wells < prev["wells"] * MIN_WELL_RATIO:
            abort_reasons.append(f"wells dropped from {prev['wells']} to {n_wells}")

    status = "updated"
    if abort_reasons and not (ALLOW_PARTIAL and not any("dropped" in r or "empty" in r for r in abort_reasons)):
        for r in abort_reasons:
            log.error("SAFETY CHECK: %s – master NOT overwritten (set FORCE_OVERWRITE=1 to override).", r)
        status = "kept previous master"
        problems += 1
    elif not write_csv_atomic(master, out_path):
        status, problems = "write failed", problems + 1
    else:
        log.info("Saved %s", MASTER_CSV)

    if status == "updated":
        with open(os.path.join(OUTPUT_DIR, STATUS_JSON), "w", encoding="utf-8") as fh:
            json.dump({
                "updated_at": now.isoformat(timespec="seconds"), "rows": n_rows, "wells": int(n_wells), "columns": n_cols,
                "sources": {s["name"]: {"rows_used": i["rows_used"], "file_updated": i["modified"].isoformat(timespec="seconds")}
                            for s, _, i in loaded},
                "review_rows_excluded": sum(r["Rows"] for r in review_rows),
                **({"srp_tests": srp_stats} if srp_stats else {}),
                **({"unit_conversions": list(UNIT_NOTES)} if UNIT_NOTES else {}),
            }, fh, indent=2)

    notes += [{"Level": "ERROR", "Message": r} for r in abort_reasons]
    notes.append({"Level": "INFO", "Message": f"Result: {status} ({n_rows} rows, {n_wells} wells, {n_cols} columns)"})
    _save_report(summary, coverage, column_notes, review_rows, notes, master_wells=master[KEY])
    log.info("Finished with %d problem(s).", problems)
    return 1 if problems else 0


def _save_report(summary, coverage, column_notes, review_rows, notes, master_wells=None) -> None:
    cov = pd.DataFrame(coverage).fillna(0).astype(int) if coverage else pd.DataFrame()
    if not cov.empty:
        cov.index.name = "Standard_Well"
        cov = cov.reset_index()
        cov = cov[~cov["Standard_Well"].str.upper().str.startswith("REVIEW_NEEDED")]
        src_cols = [c for c in cov.columns if c != "Standard_Well"]
        cov["Missing from"] = cov[src_cols].apply(lambda r: ", ".join(c for c in src_cols if r[c] == 0), axis=1)
        cov = cov.sort_values("Missing from", ascending=False)
    sheets = {
        "Summary": pd.DataFrame(summary),
        **({"Test_Sheet_Units": pd.DataFrame(UNIT_REPORT)} if UNIT_REPORT else {}),
        "Coverage_Rows_Per_Well": cov,
        "Combined_Columns": pd.DataFrame(column_notes, columns=["Column", "Combined with", "Rows where both files had a value", "…of which values differ"]),
        "Unmatched_Wells": pd.DataFrame(review_rows, columns=["Source", "Label", "Rows"]),
        "Run_Notes": pd.DataFrame(notes, columns=["Level", "Message"]),
    }
    try:
        write_report(sheets, os.path.join(OUTPUT_DIR, REPORT_NAME))
        log.info("Report saved: %s", REPORT_NAME)
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not write report (%s)", exc)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    sys.exit(run())
