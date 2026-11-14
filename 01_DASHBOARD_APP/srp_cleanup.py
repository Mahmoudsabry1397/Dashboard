"""
srp_cleanup.py: take back an SRP well-test sheet that was uploaded by mistake.

What it does for one upload date (the date the false tests were added):
  1. backs up Test_History.csv, SRP_Test_State.json, SRP_Test_Comparison.csv and the cleaned SRP file (04_ARCHIVE/removed_srp_<time>/),
  2. deletes the well tests added from the SRP sheet on that date from Test_History.csv (the other tests of those wells are kept),
  3. forgets the SRP readings remembered for that date,
  4. moves the uploaded SRP sheet out of 03_DATA/SOURCE_FILES (and deletes its cleaned copy) so the next update cannot add it again,
  5. re-runs the merge step so the master table and history are rebuilt without it.
Then upload the correct SRP sheet in the dashboard as usual.

Command line (from the dashboard app folder):   python srp_cleanup.py 2026-11-13
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

import pandas as pd

try:
    import well_test_units as wtu
except Exception:  # noqa: BLE001
    wtu = None

SRP_PREFIX = "SRP | "


def _dirs(project_dir: str) -> dict:
    return {"processed": os.path.join(project_dir, "03_DATA", "PROCESSED"), "source": os.path.join(project_dir, "03_DATA", "SOURCE_FILES"),
            "archive": os.path.join(project_dir, "04_ARCHIVE")}


def _read_history(path: str) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8-sig")


def _srp_mask(h: pd.DataFrame) -> pd.Series:
    cols = [c for c in h.columns if c.startswith(SRP_PREFIX)]
    if not cols:
        return pd.Series(False, index=h.index)
    return h[cols].apply(lambda s: s.astype(str).str.strip().ne("")).any(axis=1)


def _row_dates(h: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(h["DATE OF TEST"], errors="coerce").dt.normalize()


def uploaded_dates(project_dir: str) -> list[tuple[str, int]]:
    """[(date, number of wells), ...] newest first, for the tests in the history that came from an SRP sheet."""
    path = os.path.join(_dirs(project_dir)["processed"], "Test_History.csv")
    if not os.path.exists(path):
        return []
    h = _read_history(path)
    if "DATE OF TEST" not in h.columns:
        return []
    h = h[_srp_mask(h)].copy()
    h["_d"] = _row_dates(h)
    h = h.dropna(subset=["_d"])
    cnt = h.groupby("_d").size().sort_index(ascending=False).head(15)
    return [(d.strftime("%Y-%m-%d"), int(n)) for d, n in cnt.items()]


def _is_srp_file(path: str) -> bool:
    name = os.path.basename(path).lower()
    if name.startswith(("~$", "cleaned_")):
        return False
    if "srp" in name and "test" in name:
        return True
    try:
        with pd.ExcelFile(path) as book:
            for sheet in book.sheet_names:
                frame = pd.read_excel(book, sheet_name=sheet, nrows=10)
                keys = {re.sub(r"[^A-Z0-9]", "", str(c).upper()) for c in frame.columns}
                if (wtu is not None and wtu.looks_like_srp(keys)) or {"WELL", "ACTM3D", "ACTNET"}.issubset(keys):
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def remove_srp_tests(project_dir: str, date_str: str, app_dir: str | None = None, rebuild: bool = True) -> dict:
    d = _dirs(project_dir)
    target = pd.Timestamp(date_str).normalize()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    backup = os.path.join(d["archive"], f"removed_srp_{stamp}")
    os.makedirs(backup, exist_ok=True)
    out = {"date": target.strftime("%Y-%m-%d"), "backup": backup, "tests_removed": 0, "wells": 0, "files_removed": [], "rebuilt": None, "message": ""}

    hist_path = os.path.join(d["processed"], "Test_History.csv")
    cleaned = os.path.join(d["processed"], "Cleaned_SRP Test Results.xlsx")
    for p in (hist_path, os.path.join(d["processed"], "SRP_Test_State.json"), os.path.join(d["processed"], "SRP_Test_Comparison.csv"), cleaned):
        if os.path.exists(p):
            shutil.copy2(p, os.path.join(backup, os.path.basename(p)))

    # 1) the tests
    if os.path.exists(hist_path):
        h = _read_history(hist_path)
        drop = _srp_mask(h) & (_row_dates(h) == target)
        out["tests_removed"], out["wells"] = int(drop.sum()), int(h.loc[drop, h.columns[0]].nunique())
        if drop.any():
            tmp = hist_path + ".tmp"
            h[~drop].to_csv(tmp, index=False, encoding="utf-8-sig")
            os.replace(tmp, hist_path)

    # 2) remembered readings of that date
    st_path = os.path.join(d["processed"], "SRP_Test_State.json")
    if os.path.exists(st_path):
        try:
            with open(st_path, encoding="utf-8") as fh:
                state = json.load(fh)
            state = {w: v for w, v in state.items() if str(v.get("date", ""))[:10] != out["date"]}
            with open(st_path, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        except Exception:  # noqa: BLE001
            os.remove(st_path)

    # 3) the uploaded sheet itself, so the next update cannot add it again
    for p in glob.glob(os.path.join(d["source"], "*.xls*")):
        if _is_srp_file(p):
            shutil.move(p, os.path.join(backup, os.path.basename(p)))
            out["files_removed"].append(os.path.basename(p))
    if os.path.exists(cleaned):
        os.remove(cleaned)
        out["files_removed"].append(os.path.basename(cleaned))

    # 4) rebuild the master table and history without it
    if rebuild:
        app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))
        step = next((os.path.join(app_dir, n) for n in ("02_merge_data.py", "merge_data.py") if os.path.exists(os.path.join(app_dir, n))), None)
        if step:
            proc = subprocess.run([sys.executable, step], cwd=project_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=1800)
            out["rebuilt"] = proc.returncode == 0
            if proc.returncode != 0:
                out["message"] = (proc.stdout + proc.stderr)[-1500:]
        else:
            out["rebuilt"] = False
            out["message"] = "merge_data.py was not found, so the master table was not rebuilt."
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        print("Dates with SRP tests in the history:", uploaded_dates(proj))
        print("Usage: python srp_cleanup.py YYYY-MM-DD")
        sys.exit(0)
    proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    res = remove_srp_tests(proj, sys.argv[1])
    print(res)
