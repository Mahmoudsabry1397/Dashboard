"""
runtime_history.py: saved daily running hours from the live API (Live_Runtime_History.csv), overlaid on the workbook running-hours matrix.
Same two functions as in the full dashboard's live_data.py, without the API connection or the live page.
"""
from __future__ import annotations

import os

import pandas as pd

RUNTIME_COLUMNS = ["Well", "Date", "Hours", "Updated at", "Source"]


def _read_csv(path, columns):
    if not os.path.exists(path):
        return pd.DataFrame(columns=columns)
    try:
        return pd.read_csv(path, low_memory=False)
    except (OSError, ValueError, pd.errors.ParserError):
        return pd.DataFrame(columns=columns)


def load_live_runtime_history(data_dir):
    """Load saved daily API runtime records for merging into the dashboard matrix."""
    frame = _read_csv(os.path.join(data_dir, "Live_Runtime_History.csv"), RUNTIME_COLUMNS)
    if not all(column in frame.columns for column in ("Well", "Date", "Hours")):
        return pd.DataFrame(columns=RUNTIME_COLUMNS)
    frame = frame.dropna(subset=["Well", "Date", "Hours"]).copy()
    frame["Well"] = frame["Well"].astype(str).str.strip()
    frame["Date"] = pd.to_datetime(frame["Date"], errors="coerce").dt.normalize()
    frame["Hours"] = pd.to_numeric(frame["Hours"], errors="coerce")
    frame = frame.dropna(subset=["Date", "Hours"])
    return frame.drop_duplicates(["Well", "Date"], keep="last")


def merge_live_runtime(hours_matrix, runtime_history):
    """Overlay saved live daily hours on the workbook-derived well/date matrix."""
    if runtime_history is None or runtime_history.empty:
        return hours_matrix
    live = runtime_history.pivot_table(index="Well", columns="Date", values="Hours", aggfunc="last")
    if hours_matrix is None or hours_matrix.empty:
        combined = live
    else:
        dates = sorted(set(hours_matrix.columns).union(live.columns))
        wells = sorted(set(hours_matrix.index).union(live.index))
        combined = hours_matrix.reindex(index=wells, columns=dates).combine_first(live.reindex(index=wells, columns=dates))
        # API daily runtime is the most recent source for dates it reports.
        combined.update(live.reindex(index=wells, columns=dates))
    combined.index.name = hours_matrix.index.name if hours_matrix is not None else "Well"
    return combined.sort_index(axis=1)
