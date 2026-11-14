"""
STEP 1 — Well-name standardisation
==================================
Reads the raw Excel sheets, maps every well name to the reference well list (= the wells in the
Petrobel Wells Operation Mode file, read fresh on every run) and writes
Cleaned_<file>.xlsx (same columns as before + 'Standard_Well') for step 2.

It also writes  Well_Matching_Report.xlsx  so you can see exactly what was matched, what
needs a human decision, and which master wells are missing from each file.

To fix a REVIEW_NEEDED name permanently, add one line to  well_aliases.csv :
        raw_name,standard_well
        112-132 NEW SRP,112-132
(no code changes needed).

Exit code: 0 = all good, 1 = something needs attention (missing file / column / unreadable file).
"""
from __future__ import annotations

import glob
import logging
import os
import re
import sys

import pandas as pd

try:  # thefuzz is optional – falls back to the standard library
    from thefuzz import fuzz

    def similarity(a: str, b: str) -> int:
        return int(fuzz.ratio(a, b))
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher

    def similarity(a: str, b: str) -> int:
        return round(100 * SequenceMatcher(None, a, b).ratio())

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
INPUT_DIR = os.path.join(PROJECT_DIR, "03_DATA", "SOURCE_FILES")
OUTPUT_DIR = os.path.join(PROJECT_DIR, "03_DATA", "PROCESSED")
try:                                  # shared recognition of gross / net rate headers (BFPD, BOPD, M3L/day, Total fluid ...)
    import well_test_units as wtu
except Exception:  # noqa: BLE001
    wtu = None

CLEANED_PREFIX = "Cleaned_"
REPORT_NAME = "Well_Matching_Report.xlsx"
ALIAS_FILE = os.path.join(PROJECT_DIR, "03_DATA", "well_aliases.csv")
STANDARD_COL = "Standard_Well"

FUZZY_THRESHOLD = 80      # minimum similarity (0-100) for a typo-level fuzzy match
FUZZY_MIN_GAP = 5         # best match must beat the runner-up by this much, otherwise -> review

# "col"     : the column holding the well name (matched ignoring line-breaks / extra spaces)
# "header"  : header row used if auto-detection can't find the column (0-based)
# "pattern" : optional file-name pattern, e.g. "Monthly*.xlsx" -> newest matching file is used
# "sheet"   : optional sheet name/index (default first sheet)
# "required": set False if the file is optional (missing file will not raise an error)
FILE_CONFIGS = {
    "DATABASE.xlsx": {"col": "Well\n Name", "header": 4, "pattern": "DATABASE.xls[xm]"},
    "Monthly.xlsx": {"col": "Well Name", "header": 0, "pattern": "Monthly.xls[xm]", "all_sheets": True},
    "SRP Test Results.xlsx": {"col": "Well", "header": 0, "required": False, "signature": "srp_test"},
    "Petrobel Cumulative DFL.xlsx": {"col": "Well No.", "header": 10, "pattern": "Petrobel Cumulative DFL.xls[xm]"},
    "Petrobel Wells Operation Mode.xlsx": {"col": "Well Name", "header": 0, "pattern": "Petrobel Wells Operation Mode.xls[xm]"},
}

# Built-in LAST-RESORT list, used only if the Operation Mode file cannot be read.
# Normally the reference wells are read from the Operation Mode Excel file (see load_reference_wells).
MASTER_WELLS_FALLBACK = [
    "112-132", "112-161", "112-169", "112-41", "113-155", "113-A63", "113-A64",
    "ARS-01", "BLSW-01", "112-100", "112-107", "112-111", "112-114", "112-116",
    "112-130", "112-135", "112-136", "112-14", "112-140", "112-144", "112-147",
    "112-150", "112-153", "112-159", "112-168", "112-170", "112-175", "112-176",
    "112-179", "112-182", "112-187", "112-190", "112-52", "112-53", "112-63",
    "112-64", "112-67", "112-69", "112-72", "112-84", "112-92", "113-105",
    "113-108", "113-11", "113-110", "113-117", "113-122", "113-124", "113-133",
    "113-144", "113-145", "113-158", "113-170", "113-174", "113-175", "113-208",
    "113-215", "113-219", "113-29", "113-32", "113-64", "113-65", "113-66",
    "113-69", "113-73", "113-78", "113-80", "113-86", "113-88", "113-91",
    "113-A10", "113-A17", "113-A28", "113-A33", "113-A43", "113-A58", "113-A60",
    "113-A70", "113-A78", "AR-05", "ARM-16", "ARM-18", "ARM-26", "ARM-31",
    "ARM-37", "ARS-09", "BAY-05", "BAY-06", "BAY-08", "BLNE-01", "BLNW-03",
    "BLNW-04", "BLNW-06", "BLS-20", "SID-28", "SID-37", "SID-41", "113-A36",
    "112-173", "112-196", "112-172",
]

# Equipment / status words that are not part of the well name
SUFFIXES_TO_REMOVE = {"DOL", "VFD", "LUFKIN", "RPOC", "ESP", "SRP", "PUMP", "CHOKED", "FLOW"}

# Quick inline overrides (raw text -> standard name). Prefer well_aliases.csv for day-to-day use.
MANUAL_ALIASES: dict[str, str] = {}

log = logging.getLogger("well_matching")

# ──────────────────────────────────────────────────────────────────────────────
# NAME NORMALISATION
# ──────────────────────────────────────────────────────────────────────────────
_TOKEN_RE = re.compile(r"[A-Z]+|\d+")


def norm_header(s) -> str:
    """Normalise a column header: collapse whitespace/line-breaks, upper-case."""
    return re.sub(r"\s+", " ", str(s)).strip().upper()


def tokenize(raw) -> list[str]:
    """
    '113-A63', '113 a63', '113_A63', '113–A63' -> ['113', 'A', '63']
    Numbers lose leading zeros, so 'BAY-5' == 'BAY-05'. Equipment tags are dropped.
    """
    if raw is None or (not isinstance(raw, str) and pd.isna(raw)):
        return []
    tokens = [str(int(t)) if t.isdigit() else t for t in _TOKEN_RE.findall(str(raw).upper())]
    return [t for t in tokens if t not in SUFFIXES_TO_REMOVE]


def clean_well_name(raw_name) -> str:
    """Readable cleaned label (used in REVIEW_NEEDED labels and the report)."""
    if raw_name is None or (not isinstance(raw_name, str) and pd.isna(raw_name)):
        return ""
    text = str(raw_name).strip().upper().replace("_", "-")
    for suffix in SUFFIXES_TO_REMOVE:
        text = re.sub(r"\b" + re.escape(suffix) + r"\b", "", text)
    return re.sub(r"\s+", " ", text).strip(" -")


def _contains(seq: list[str], sub: tuple[str, ...]) -> bool:
    n = len(sub)
    return n > 0 and any(tuple(seq[i:i + n]) == sub for i in range(len(seq) - n + 1))


# ──────────────────────────────────────────────────────────────────────────────
# MATCHER
# ──────────────────────────────────────────────────────────────────────────────
class WellMatcher:
    """
    Matching order (first hit wins):
      1. alias   – listed in well_aliases.csv / MANUAL_ALIASES
      2. exact   – same name after normalisation (separators, case, leading zeros, equipment tags)
      3. contained – exactly one master well appears inside a longer text (e.g. 'WELL 112-132 (ST)')
      4. fuzzy   – typo-level match; only against master wells with the SAME numbers, so that
                   112-132 can never be 'fuzzy-matched' to 112-133, and the winner must be unambiguous
      5. review  – everything else -> 'REVIEW_NEEDED: <cleaned name>'
    """

    def __init__(self, master: list[str], aliases: dict[str, str], threshold: int = FUZZY_THRESHOLD):
        self.master = list(dict.fromkeys(master))
        self.threshold = threshold
        self.tokens = {w: tuple(tokenize(w)) for w in self.master}
        self.by_tokens: dict[tuple, str] = {}
        for w, tk in self.tokens.items():
            if tk in self.by_tokens:
                raise ValueError(f"Master list has two names that normalise the same: {w} / {self.by_tokens[tk]}")
            self.by_tokens[tk] = w
        self.aliases: dict[tuple, str] = {}
        for raw, target in aliases.items():
            if target not in self.tokens:
                log.warning("Alias ignored – '%s' is not in the master list (alias: '%s')", target, raw)
                continue
            self.aliases[tuple(tokenize(raw))] = target
        self._cache: dict[str, dict] = {}

    def match(self, raw) -> dict:
        key = "" if raw is None or (not isinstance(raw, str) and pd.isna(raw)) else str(raw)
        if key not in self._cache:
            self._cache[key] = self._match(key)
        return self._cache[key]

    def _match(self, raw: str) -> dict:
        toks = tokenize(raw)
        cleaned = clean_well_name(raw)
        res = {"raw": raw, "cleaned": cleaned, "standard": None, "method": "review", "score": 0,
               "suggestion": "", "suggestion_score": 0}
        if not toks:
            res["method"] = "blank"
            return res
        tkey = tuple(toks)

        if tkey in self.aliases:
            return {**res, "standard": self.aliases[tkey], "method": "alias", "score": 100}
        if tkey in self.by_tokens:
            return {**res, "standard": self.by_tokens[tkey], "method": "exact", "score": 100}

        hits = [w for w, tk in self.tokens.items() if _contains(toks, tk)]
        if len(hits) == 1:
            return {**res, "standard": hits[0], "method": "contained", "score": 95}

        text = "-".join(toks)
        digits = tuple(t for t in toks if t.isdigit())
        scored = sorted(
            ((similarity(text, "-".join(tk)), w) for w, tk in self.tokens.items()
             if tuple(t for t in tk if t.isdigit()) == digits),
            reverse=True,
        )
        if scored and scored[0][0] >= self.threshold and (len(scored) == 1 or scored[0][0] - scored[1][0] >= FUZZY_MIN_GAP):
            return {**res, "standard": scored[0][1], "method": "fuzzy", "score": scored[0][0]}

        best = max(((similarity(text, "-".join(tk)), w) for w, tk in self.tokens.items()), default=(0, ""))
        res.update(suggestion=best[1], suggestion_score=best[0])
        return res

    def label(self, raw) -> str:
        r = self.match(raw)
        if r["standard"]:
            return r["standard"]
        return f"REVIEW_NEEDED: {r['cleaned'] or '(blank)'}"


REFERENCE_SOURCE = "Petrobel Wells Operation Mode.xlsx"   # key of FILE_CONFIGS whose well-name column defines the reference wells
NOT_REFERENCE = {"", "NAN", "NONE", "UNKNOWN", "TOTAL", "TOTALS", "GRAND TOTAL", "SUM", "AVERAGE"}


def load_reference_wells() -> list[str]:
    """Reference (standard) well names = the well-name column of the Operation Mode file."""
    cfg = FILE_CONFIGS[REFERENCE_SOURCE]
    path = locate_file(REFERENCE_SOURCE, cfg)
    if path is None:
        log.warning("Reference file '%s' not found - using the built-in fallback list (%d wells).", REFERENCE_SOURCE, len(MASTER_WELLS_FALLBACK))
        return list(MASTER_WELLS_FALLBACK)
    try:
        sheet = cfg.get("sheet", 0)
        df = pd.read_excel(path, sheet_name=sheet, header=find_header_row(path, sheet, cfg["col"], cfg.get("header", 0)))
        col = next(c for c in df.columns if norm_header(c) == norm_header(cfg["col"]))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not read the reference wells from %s (%s) - using the built-in fallback list.", os.path.basename(path), exc)
        return list(MASTER_WELLS_FALLBACK)

    wells, seen = [], {}
    for raw in df[col].dropna():
        name = clean_well_name(raw)
        key = tuple(tokenize(name))
        if not key or name.upper() in NOT_REFERENCE:
            continue
        if key in seen:
            if seen[key] != name:
                log.warning("Reference list: '%s' and '%s' are the same well - keeping '%s'.", name, seen[key], seen[key])
            continue
        seen[key] = name
        wells.append(name)
    if len(wells) < 5:
        log.warning("Only %d reference wells found in %s - using the built-in fallback list.", len(wells), os.path.basename(path))
        return list(MASTER_WELLS_FALLBACK)
    log.info("Reference wells: %d, read from %s", len(wells), os.path.basename(path))
    return wells


def load_aliases() -> dict[str, str]:
    aliases = dict(MANUAL_ALIASES)
    if os.path.exists(ALIAS_FILE):
        try:
            df = pd.read_csv(ALIAS_FILE, dtype=str, comment="#").dropna()
            df.columns = [c.strip().lower() for c in df.columns]
            aliases.update(dict(zip(df["raw_name"].str.strip(), df["standard_well"].str.strip())))
            log.info("Loaded %d alias(es) from %s", len(df), os.path.basename(ALIAS_FILE))
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not read %s (%s) – continuing without it", ALIAS_FILE, exc)
    return aliases


# ──────────────────────────────────────────────────────────────────────────────
# FILE HANDLING
# ──────────────────────────────────────────────────────────────────────────────
def locate_file(name: str, cfg: dict) -> str | None:
    if cfg.get("signature") == "srp_test":
        hits = []
        for p in glob.glob(os.path.join(INPUT_DIR, "*.xls*")):
            if os.path.basename(p).startswith((CLEANED_PREFIX, "~$")):
                continue
            try:
                book = pd.ExcelFile(p)
                for sheet in book.sheet_names:
                    probe = pd.read_excel(p, sheet_name=sheet, nrows=10)
                    keys = {re.sub(r"[^A-Z0-9]+", "", norm_header(c)) for c in probe.columns}
                    if {"WELL", "ACTM3D", "ACTWC", "ACTNET"}.issubset(keys) or (wtu is not None and wtu.looks_like_srp(keys)):
                        hits.append(p)
                        break
            except Exception:
                continue
        return max(hits, key=os.path.getmtime) if hits else None
    pattern = cfg.get("pattern", name)
    hits = [
        p for p in glob.glob(os.path.join(INPUT_DIR, pattern))
        if not os.path.basename(p).startswith((CLEANED_PREFIX, "~$"))
    ]
    return max(hits, key=os.path.getmtime) if hits else None


def find_header_row(path: str, sheet, col_name: str, hint: int, scan_rows: int = 40) -> int:
    """Find the row that contains the well-name header, so a shifted header never breaks the run."""
    try:
        probe = pd.read_excel(path, sheet_name=sheet, header=None, nrows=scan_rows)
    except Exception:  # noqa: BLE001
        return hint
    target = norm_header(col_name)
    for i, row in probe.iterrows():
        if any(pd.notna(v) and norm_header(v) == target for v in row):
            return int(i)
    return hint


def save_excel_atomic(df: pd.DataFrame, path: str) -> None:
    tmp = path[:-5] + ".tmp.xlsx"  # keep a real .xlsx extension so pandas picks the right engine
    df.to_excel(tmp, index=False, engine="openpyxl")
    os.replace(tmp, path)  # the old file is only replaced once the new one is fully written


def process_source_file(path: str, cfg: dict, matcher: WellMatcher):
    """Returns (cleaned_df, detail_df, header_row) or raises ValueError with a readable message."""
    if cfg.get("all_sheets"):
        frames, details, headers = [], [], []
        book = pd.ExcelFile(path)
        for sheet_name in book.sheet_names:
            try:
                header = find_header_row(path, sheet_name, cfg["col"], cfg.get("header", 0))
                part = pd.read_excel(path, sheet_name=sheet_name, header=header)
                well_col = next((c for c in part.columns if norm_header(c) == norm_header(cfg["col"])), None)
                if well_col is None:
                    continue
                part = part.dropna(how="all").reset_index(drop=True)
                part[STANDARD_COL] = part[well_col].map(matcher.label)
                frames.append(part)
                headers.append(f"{sheet_name}: {header}")
                counts = part[well_col].map(lambda x: "" if pd.isna(x) else str(x)).value_counts(dropna=False)
                details.extend([{**matcher.match(raw), "rows": int(n)} for raw, n in counts.items()])
            except Exception as exc:
                log.warning("Skipping Monthly sheet %s: %s", sheet_name, exc)
        if not frames:
            raise ValueError("No Monthly sheet contained a Well Name column")
        df = pd.concat(frames, ignore_index=True, sort=False)
        # Newer sheet rows can contain only a few dates. Keep each well's latest
        # nonblank value per column so earlier date columns are retained.
        def latest_nonblank(series):
            values = series.dropna()
            return values.iloc[-1] if len(values) else pd.NA
        df = df.groupby(STANDARD_COL, sort=False, as_index=False).agg(latest_nonblank)
        return df, pd.DataFrame(details), "; ".join(headers)

    sheet = cfg.get("sheet", 0)
    header = find_header_row(path, sheet, cfg["col"], cfg.get("header", 0)) if cfg.get("auto_header", True) else cfg.get("header", 0)
    df = pd.read_excel(path, sheet_name=sheet, header=header)

    target = norm_header(cfg["col"])
    well_col = next((c for c in df.columns if norm_header(c) == target), None)
    if well_col is None:
        raise ValueError(f"column '{cfg['col']}' not found. Available: {list(df.columns)}")

    df = df.dropna(how="all").reset_index(drop=True)  # drop fully empty rows
    if cfg.get("signature") == "srp_test":
        # the test date of an SRP result file is the day it was uploaded (the file's modified time, set when it is installed)
        df["DATE OF TEST"] = pd.Timestamp.fromtimestamp(os.path.getmtime(path)).normalize()
    df[STANDARD_COL] = df[well_col].map(matcher.label)

    counts = df[well_col].map(lambda x: "" if pd.isna(x) else str(x)).value_counts(dropna=False)
    detail = pd.DataFrame([{**matcher.match(raw), "rows": int(n)} for raw, n in counts.items()])
    return df, detail, header


# ──────────────────────────────────────────────────────────────────────────────
# REPORT
# ──────────────────────────────────────────────────────────────────────────────
PRIORITY_ORDER = {"Check - similar to a master well": 0, "Blank name": 1, "Probably not a master well": 2}


def review_priority(method: str, score: int) -> str:
    """Triage of unmatched names: does it look like one of YOUR wells written differently?"""
    if method == "blank":
        return "Blank name"
    return "Check - similar to a master well" if score >= FUZZY_THRESHOLD else "Probably not a master well"


def build_report(results: dict, matcher: WellMatcher, notes: list[dict]) -> dict[str, pd.DataFrame]:
    summary_rows, review, auto, coverage = [], [], [], {}
    for fname, r in results.items():
        d = r["detail"]
        by_method = d.groupby("method")["rows"].sum()
        total = int(d["rows"].sum())
        n_review = int(by_method.get("review", 0) + by_method.get("blank", 0))
        matched = d[d["standard"].notna()]
        summary_rows.append({
            "File": fname, "Source file used": r["source"], "Header row": r["header"], "Rows": total,
            "Exact": int(by_method.get("exact", 0)), "Alias": int(by_method.get("alias", 0)),
            "Contained": int(by_method.get("contained", 0)), "Fuzzy": int(by_method.get("fuzzy", 0)),
            "Review needed (rows)": n_review,
            "Review %": round(100 * n_review / total, 1) if total else 0.0,
            "Unmatched names": int(d["standard"].isna().sum()),
            "Names to check": int(sum(review_priority(x.method, x.suggestion_score).startswith("Check")
                                      for x in d[d["standard"].isna()].itertuples())),
            "Distinct wells matched": matched["standard"].nunique(),
        })
        review += [
            {"File": fname, "Priority": review_priority(x.method, x.suggestion_score), "Raw name": x.raw,
             "Cleaned": x.cleaned, "Rows": x.rows, "Closest master well": x.suggestion, "Similarity": x.suggestion_score}
            for x in d[d["standard"].isna()].itertuples()
        ]
        auto += [
            {"File": fname, "Raw name": x.raw, "Matched to": x.standard, "Method": x.method, "Score": x.score, "Rows": x.rows}
            for x in d[d["method"].isin(["fuzzy", "contained"])].itertuples()
        ]
        coverage[fname] = matched.groupby("standard")["rows"].sum()

    review_cols = ["File", "Priority", "Raw name", "Cleaned", "Rows", "Closest master well", "Similarity"]
    review_sheet = pd.DataFrame(review, columns=review_cols)
    if len(review_sheet):
        review_sheet["_p"] = review_sheet["Priority"].map(PRIORITY_ORDER)
        review_sheet = review_sheet.sort_values(["_p", "Rows"], ascending=[True, False]).drop(columns="_p")

    cov = pd.DataFrame(coverage).reindex(matcher.master).fillna(0).astype(int)
    cov.index.name = "Master well"
    cov = cov.reset_index()
    sheets = {
        "Summary": pd.DataFrame(summary_rows),
        "Review_Needed": review_sheet,
        "Auto_Matched_To_Check": pd.DataFrame(auto, columns=["File", "Raw name", "Matched to", "Method", "Score", "Rows"]),
        "Coverage_Rows_Per_Well": cov,
    }
    if notes:
        sheets["Run_Notes"] = pd.DataFrame(notes)
    return sheets


def write_report(sheets: dict[str, pd.DataFrame], path: str) -> None:
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
    matcher = WellMatcher(load_reference_wells(), load_aliases())
    results, notes, problems = {}, [], 0

    for name, cfg in FILE_CONFIGS.items():
        path = locate_file(name, cfg)
        if path is None and os.path.exists(os.path.join(OUTPUT_DIR, f"{CLEANED_PREFIX}{name}")):
            log.info("%s: no source file, the existing %s%s is kept", name, CLEANED_PREFIX, name)
            continue
        if path is None:
            msg = "file not found"
            (log.error if cfg.get("required", True) else log.warning)("%s: %s", name, msg)
            notes.append({"File": name, "Level": "ERROR" if cfg.get("required", True) else "WARNING", "Message": msg})
            problems += int(cfg.get("required", True))
            continue
        log.info("Processing %s ...", os.path.basename(path))
        try:
            df, detail, header = process_source_file(path, cfg, matcher)
            out_path = os.path.join(OUTPUT_DIR, f"{CLEANED_PREFIX}{name}")
            save_excel_atomic(df, out_path)
        except Exception as exc:  # noqa: BLE001
            log.error("%s: %s", name, exc)
            notes.append({"File": name, "Level": "ERROR", "Message": str(exc)})
            problems += 1
            continue

        results[name] = {"detail": detail, "source": os.path.basename(path), "header": header}
        total = int(detail["rows"].sum())
        unmatched = detail[detail["standard"].isna()]
        n_review = int(unmatched["rows"].sum())
        n_check = int(sum(review_priority(x.method, x.suggestion_score).startswith("Check") for x in unmatched.itertuples()))
        log.info("   %d rows | %d wells matched | %d row(s) not matched (%d names, %d look like master wells) -> %s%s",
                 total, detail["standard"].nunique(), n_review, len(unmatched), n_check, CLEANED_PREFIX, name)
        if n_check:
            msg = f"{n_check} unmatched name(s) look like master wells - see {REPORT_NAME} > Review_Needed (Check)"
            log.warning("   %s", msg)
            notes.append({"File": name, "Level": "WARNING", "Message": msg})

    if results:
        write_report(build_report(results, matcher, notes), os.path.join(OUTPUT_DIR, REPORT_NAME))
        log.info("Report saved: %s", REPORT_NAME)

    log.info("Finished with %d problem(s).", problems)
    return 1 if problems else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    sys.exit(run())
