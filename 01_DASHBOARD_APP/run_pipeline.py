"""
Daily update runner  (run on ONE computer – the "host" PC)
===========================================================
Team members only drop the new Excel files into   02_UPLOAD_NEW_DATA/   (any file name that
contains DATABASE, Monthly, Cumulative DFL or Operation Mode is recognised).

This script then:
  1. waits until the dropped files are fully synced and valid Excel files,
  2. archives the previous raw files and installs the new ones (files that were NOT dropped keep
     their previous version, so partial updates are fine),
  3. runs   01_match_well_names.py  ->  02_merge_data.py ,
  4. archives the dropped files, writes Last_Update_Status.txt (readable by everybody) and a log.

If the Drop folder is empty it does nothing, so it is safe to schedule every 15-30 minutes.
Exit code: 0 = nothing to do / success,  1 = failed (the old master CSV is kept).
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from datetime import datetime

try:                                  # shared recognition of gross / net rate headers (BFPD, BOPD, M3L/day, Total fluid ...)
    import well_test_units as wtu
except Exception:  # noqa: BLE001
    wtu = None

APP_DIR = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.dirname(APP_DIR)
SOURCE_DIR = os.path.join(BASE, "03_DATA", "SOURCE_FILES")
PROCESSED_DIR = os.path.join(BASE, "03_DATA", "PROCESSED")
DROP_DIR = os.path.join(BASE, "02_UPLOAD_NEW_DATA")
ARCHIVE_DIR = os.path.join(BASE, "04_ARCHIVE")
LOG_DIR = os.path.join(BASE, "05_LOGS")
STATUS_FILE = os.path.join(BASE, "00_START_HERE", "Last_Update_Status.txt")
LOCK_FILE = os.path.join(LOG_DIR, "running.lock")

def _pick(*names):
    """First script name that exists in this folder (works with either naming)."""
    return next((n for n in names if os.path.exists(os.path.join(APP_DIR, n))), names[0])


STEPS = [_pick("01_match_well_names.py", "Wells Name Matching.py"),
         _pick("02_merge_data.py", "merge_data.py")]
SETTLE_SECONDS = 90          # a dropped file must be at least this old (avoids half-synced files)
LOCK_STALE_MINUTES = 30
KEEP_ARCHIVE_DAYS = 60
KEEP_LOG_DAYS = 90
EXTENSIONS = (".xlsx", ".xlsm")
IGNORE_PREFIXES = ("~$", "cleaned_", "master_", "well_matching_report")

# canonical working-file name -> keywords that must ALL appear in the dropped file's name
SOURCES = [
    ("Petrobel Wells Operation Mode", ["operation", "mode"]),
    ("Petrobel Cumulative DFL", ["dfl"]),
    ("Monthly", ["monthly"]),
    ("DATABASE", ["database"]),
]
OPTIONAL_SRP = "SRP Test Results.xlsx"


class Logger:
    def __init__(self, path):
        self.fh = open(path, "a", encoding="utf-8")

    def __call__(self, msg=""):
        line = f"{datetime.now():%H:%M:%S}  {msg}"
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode(), flush=True)
        self.fh.write(line + "\n")
        self.fh.flush()


def is_srp_workbook(path: str) -> bool:
    try:
        import pandas as pd
        with pd.ExcelFile(path) as book:
            for sheet in book.sheet_names:
                frame = pd.read_excel(book, sheet_name=sheet, nrows=10)
                keys = {"".join(ch for ch in str(c).upper() if ch.isalnum()) for c in frame.columns}
                # water-cut header may read "ACT W.C", "ACT W.C %" or "ACT W.C PCT": all normalise to ACTWC / ACTWCPCT
                if wtu is not None and wtu.looks_like_srp(keys):
                    return True
                if {"WELL", "ACTM3D", "ACTNET"}.issubset(keys) and ({"ACTWC"} & keys or {"ACTWCPCT"} & keys):
                    return True
    except Exception:
        pass
    return False


def classify(filename: str, path: str | None = None):
    if path and is_srp_workbook(path):
        return OPTIONAL_SRP
    stem = os.path.splitext(filename)[0].lower()
    if "srp" in stem and "test" in stem:        # files saved by the dashboard uploader are named "SRP Test Results.xlsx"
        return OPTIONAL_SRP
    for canon, keywords in SOURCES:
        if all(k in stem for k in keywords):
            return canon
    return None


def scan_drop():
    chosen, extras, ignored, not_ready = {}, [], [], []
    if not os.path.isdir(DROP_DIR):
        return chosen, extras, ignored, not_ready
    for name in sorted(os.listdir(DROP_DIR)):
        path = os.path.join(DROP_DIR, name)
        if not os.path.isfile(path) or name.lower().startswith(IGNORE_PREFIXES) or name.lower().endswith((".txt", ".ini", ".tmp")):
            continue
        canon = classify(name, path) if name.lower().endswith(EXTENSIONS) else None
        if canon is None:
            ignored.append(name)
            continue
        age = time.time() - os.path.getmtime(path)
        if age < SETTLE_SECONDS or os.path.getsize(path) == 0 or not zipfile.is_zipfile(path):
            not_ready.append(name)
            continue
        if canon in chosen:  # keep the newest file per source, archive the rest
            old = chosen[canon]
            if os.path.getmtime(path) > os.path.getmtime(old):
                extras.append(old)
                chosen[canon] = path
            else:
                extras.append(path)
        else:
            chosen[canon] = path
    return chosen, extras, ignored, not_ready


def working_files(canon: str):
    if canon == OPTIONAL_SRP:
        return [p for p in glob.glob(os.path.join(SOURCE_DIR, "*.xls*")) if is_srp_workbook(p)]
    return glob.glob(os.path.join(SOURCE_DIR, f"{canon}.xls[xm]"))


def write_status(lines):
    with open(STATUS_FILE, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def read_summary():
    """Pull a short summary out of the files written by steps 1 and 2 (best effort)."""
    out = {}
    try:
        with open(os.path.join(PROCESSED_DIR, "data_status.json"), encoding="utf-8") as fh:
            out["master"] = json.load(fh)
    except Exception:  # noqa: BLE001
        pass
    try:
        import pandas as pd

        rep = os.path.join(PROCESSED_DIR, "Well_Matching_Report.xlsx")
        summ = pd.read_excel(rep, sheet_name="Summary")
        out["check_names"] = int(summ["Names to check"].sum())
        rev = pd.read_excel(rep, sheet_name="Review_Needed")
        rev = rev[rev["Priority"].astype(str).str.startswith("Check")]
        out["review_names"] = (rev.groupby("Raw name")["Rows"].sum().sort_values(ascending=False).head(10).index.astype(str).tolist()
                               if len(rev) else [])
    except Exception:  # noqa: BLE001
        pass
    return out


def cleanup_old():
    cutoff = time.time() - KEEP_ARCHIVE_DAYS * 86400
    if os.path.isdir(ARCHIVE_DIR):
        for d in os.listdir(ARCHIVE_DIR):
            p = os.path.join(ARCHIVE_DIR, d)
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
    cutoff = time.time() - KEEP_LOG_DAYS * 86400
    for p in glob.glob(os.path.join(LOG_DIR, "update_*.log")):
        if os.path.getmtime(p) < cutoff:
            os.remove(p)


def acquire_lock() -> bool:
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) > LOCK_STALE_MINUTES * 60:
        os.remove(LOCK_FILE)
    try:
        os.close(os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def main() -> int:
    for d in (DROP_DIR, SOURCE_DIR, PROCESSED_DIR, ARCHIVE_DIR, LOG_DIR, os.path.dirname(STATUS_FILE)):
        os.makedirs(d, exist_ok=True)

    chosen, extras, ignored, not_ready = scan_drop()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    nice_time = datetime.now().strftime("%d %b %Y, %H:%M")

    if not chosen:
        if not_ready:
            print("Files are still syncing or are not valid Excel files yet – will check again next run:", ", ".join(not_ready))
        elif ignored:
            write_status(["Dashboard data status", "=" * 21, "Result : NOTHING PROCESSED", f"Time   : {nice_time}",
                          "Reason : these files in 02_UPLOAD_NEW_DATA were not recognised (baseline files need familiar names; "
                          "SRP test files are recognised by their Well / ACT M3/D / ACT W.C / ACT NET headers; Excel .xlsx/.xlsm only):",
                          *[f"   - {n}" for n in ignored]])
            print("Unrecognised files in the Drop folder:", ", ".join(ignored))
        else:
            print("No new files in 02_UPLOAD_NEW_DATA – nothing to do.")
        return 0
    if not_ready:
        print("Some dropped files are not ready yet (", ", ".join(not_ready), ") – waiting so all files update together.")
        return 0

    if not acquire_lock():
        print("Another update is already running – exiting.")
        return 0

    log = Logger(os.path.join(LOG_DIR, f"update_{stamp}.log"))
    run_archive = os.path.join(ARCHIVE_DIR, stamp)
    failed_reason = None
    archive_warnings = []
    try:
        log(f"Update started – {len(chosen)} new file(s): " + ", ".join(os.path.basename(p) for p in chosen.values()))

        # 1) install new raw files (previous ones are archived first)
        prev_dir = os.path.join(run_archive, "previous_working_files")
        os.makedirs(prev_dir, exist_ok=True)
        for canon, src in chosen.items():
            for old in working_files(canon):
                shutil.move(old, os.path.join(prev_dir, os.path.basename(old)))
            dest_name = os.path.basename(src) if canon == OPTIONAL_SRP else canon + os.path.splitext(src)[1].lower()
            dest = os.path.join(SOURCE_DIR, dest_name)
            shutil.copyfile(src, dest)  # copyfile -> fresh modified time, so it is picked as "newest"
            log(f"  installed {os.path.basename(src)}  ->  {os.path.basename(dest)}")

        def has_cleaned(canon):      # no raw file kept, but an earlier cleaned copy exists (e.g. a DATABASE that was only supplied cleaned)
            return os.path.exists(os.path.join(PROCESSED_DIR, f"Cleaned_{canon}.xlsx"))
        reused = [canon for canon, _ in SOURCES if canon not in chosen and (working_files(canon) or has_cleaned(canon))]
        missing = [canon for canon, _ in SOURCES if not working_files(canon) and not has_cleaned(canon)]
        for canon in reused:
            log(f"  (no new file for '{canon}' – keeping the previous version)")
        if missing:
            failed_reason = "No file available for: " + ", ".join(missing)
            log("ERROR: " + failed_reason)

        # 2) run the steps
        if not failed_reason:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            for step in STEPS:
                log(f"Running {step} ...")
                proc = subprocess.run([sys.executable, os.path.join(APP_DIR, step)], cwd=BASE, capture_output=True,
                                      text=True, encoding="utf-8", errors="replace", env=env, timeout=3600)
                for line in (proc.stdout + proc.stderr).splitlines():
                    log("   " + line)
                if proc.returncode != 0:
                    failed_reason = f"{step} reported a problem (see the log)"
                    break

        # 3) archive dropped files on success
        if not failed_reason:
            used_dir = os.path.join(run_archive, "dropped_files")
            os.makedirs(used_dir, exist_ok=True)
            for p in list(chosen.values()) + extras:
                dest = os.path.join(used_dir, os.path.basename(p))
                moved = False
                for attempt in range(3):
                    try:
                        shutil.move(p, dest)
                        moved = True
                        break
                    except PermissionError:
                        if attempt < 2:
                            time.sleep(2)
                if not moved:
                    warning = f"Could not archive {os.path.basename(p)} because Windows still has it open; it remains in 02_UPLOAD_NEW_DATA."
                    archive_warnings.append(warning)
                    log("WARNING: " + warning)
            cleanup_old()
    except Exception as exc:  # noqa: BLE001
        failed_reason = f"unexpected error: {exc}"
        log("ERROR: " + failed_reason)
    finally:
        try:
            os.remove(LOCK_FILE)
        except OSError:
            pass

    summary = read_summary()
    log_rel = os.path.join("05_LOGS", f"update_{stamp}.log")
    if failed_reason:
        write_status(["Dashboard data status", "=" * 21, "Result : FAILED - the dashboard still shows the PREVIOUS data",
                      f"Time   : {nice_time}", f"Reason : {failed_reason}",
                      f"Details: {log_rel}", "",
                      "Your dropped files were left in 02_UPLOAD_NEW_DATA. Fix the problem (or ask the dashboard owner) and the next run will retry."])
        log("FAILED: " + failed_reason)
        return 1

    m = summary.get("master", {})
    lines = ["Dashboard data status", "=" * 21, "Result : SUCCESS", f"Time   : {nice_time}",
             "Updated: " + ", ".join(chosen)]
    if reused:
        lines.append("Reused : " + ", ".join(reused) + "  (no new file dropped)")
    if m:
        lines.append(f"Master : {m.get('rows', '?'):,} rows | {m.get('wells', '?')} wells | {m.get('columns', '?')} columns")
    sr = m.get("srp_tests")
    if sr:
        lines.append(f"SRP tests: {sr.get('added', 0)} new test(s) added (dated {sr.get('test_date', '?')}), "
                     f"{sr.get('same_as_last_test', 0)} same as the last test (ignored), {sr.get('already_added', 0)} already added earlier"
                     + (f", {sr['zero_rate_ignored']} with zero gross and net (ignored)" if sr.get('zero_rate_ignored') else ""))
    for note in m.get("unit_conversions", []):
        lines.append("Units  : " + note)
    for warning in archive_warnings:
        lines.append("Archive warning: " + warning)
    if summary.get("check_names"):
        lines.append(f"Check  : {summary['check_names']} well name(s) look like master wells but could not be matched.")
        if summary.get("review_names"):
            lines.append("         e.g. " + "; ".join(summary["review_names"]))
        lines.append("         (see 03_DATA/PROCESSED/Well_Matching_Report.xlsx; reviewed mappings belong in 03_DATA/well_aliases.csv)")
    lines += ["Details: " + log_rel]
    write_status(lines)
    log("SUCCESS")
    return 0


if __name__ == "__main__":
    if "--no-wait" in sys.argv:   # files uploaded inside the dashboard are used immediately
        SETTLE_SECONDS = 0
    sys.exit(main())

