"""
digital_priority.py: which wells get PT sensors, ROPC or VSD.
"""
from __future__ import annotations

import io
import json
import os
import re

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import report_store as rsto

TECH_ORDER = ["VSD", "ROPC", "PT sensor"]
COLORS = {"VSD": "#CE1141", "ROPC": "#E08A00", "PT sensor": "#1F7A8C", "Not assessed": "#9AA0A6"}
EMOJI = {"VSD": "🟥 VSD", "ROPC": "🟧 ROPC", "PT sensor": "🟦 PT sensor", "Not assessed": "⬜ Not assessed"}
OVERRIDES_FILE = "Digitalization_DynaOverrides.csv"
SETTINGS_FILE = "Digitalization_Settings.json"
BBL = 6.28981
FT = 3.28084

# Default settings matching your custom ratios (VSD: 48%, ROPC: 45%)
DEFAULT_SETTINGS = {
    "thr": 4.0,
    "vsd": 48,
    "ropc": 45,
    "age": 0,
    "base": "Ranked wells (net oil above the threshold)",
    "miss": "The median score of the other wells",
}

PARAMS = [("net_oil", "Net oil", 10.0, 30.0), ("gross", "Gross rate", 0.0, 10.0), ("nlap", "NLAP", 0.0, 10.0),
          ("rod", "Rod loaded", 0.0, 10.0), ("beam", "Beam loading", 0.0, 10.0), ("gbx", "Gearbox torque", 0.0, 10.0)]

def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"

PARAM_COLORS = {"net_oil": "#2F80ED", "gross": "#CE1141", "nlap": "#1F7A8C", "rod": "#E08A00", "beam": "#7B61C9", "gbx": "#4C9F70"}

_LOAD = r"(?:l[o0]a?d(?:ed|ing|ng|s)?|l[o0]d(?:ed|ing)?)"
_ROD = [r"\brods?\b[\s.\-:]*(?:string[\s.\-:]*)?" + _LOAD + r"\b", r"\bgoodman\b(?:[\s.\-:]*(?:diagram|ratio|" + _LOAD + r"))?"]
_BEAM = [r"\bbeam\b[\s.\-:]*(?:" + _LOAD + r")?"]
_GBX = [r"(?:\bg\s*[./\-]?\s*b\b\.?|\bgear\s*-?\s*box\b|\bgbx\b|\bg/box\b)[\s.\-:]*(?:(?:torque|trq|tq)\b[\s.\-:]*)?(?:" + _LOAD + r")?",
        r"\b(?:torque|trq)\b[\s.\-:]*" + _LOAD + r"\b"]
_SEP = r"(?:[\s:=\-.,;|/>(\[{~\u2248\u00b1]|loaded|loading|load|torque|stress|max|maximum|value|ratio|is|at|of|about|around|approx\.?|approximately|near|nearly|up|to|percent|pct|%)*?"
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*(%|pct\b|percent\b|psi\b|kn\b|lbs?\b|kg\b)?"
_NOT_PCT = {"psi", "kn", "lb", "lbs", "kg"}
_DIGITS = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669\u066b\u066c\u066a\uff05\u00a0\u2013\u2014\u2212",
                        "0123456789.,%% ---")

def _clean_text(text: str) -> str:
    t = str(text).lower().translate(_DIGITS)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)
    t = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", t)
    return t

def _find(t: str, names, lo: float, hi: float):
    for nm in names:
        for m in re.finditer(nm + _SEP + _NUM + _UNIT, t):
            raw, unit = m.group(1), m.group(2)
            if unit in _NOT_PCT:
                continue
            v = float(raw)
            if unit is None and "." in raw and 0 < v <= 1.5:
                v *= 100
            if lo <= v <= hi:
                return v
    return None

def parse_dyno_text(text) -> dict:
    out = {}
    if not isinstance(text, str) or not text.strip():
        return out
    t = _clean_text(text)
    for key, names, hi in (("goodman_pct", _ROD, 400.0), ("beam_pct", _BEAM, 300.0), ("gbx_pct", _GBX, 300.0)):
        v = _find(t, names, 0.0, hi)
        if v is not None:
            out[key] = v
    return out

def _wkey(x) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(x).upper())

_MENTION = r"rods?\s*[.\-:]*\s*l[o0]|beam\s*[.\-:]*\s*l[o0]|g\s*[./\-]?\s*b\b\.?\s*(?:torque|l[o0])|gear\s*-?\s*box\s*(?:torque|l[o0])"

def _dyno_columns(df: pd.DataFrame, primary) -> list:
    cols = [primary] if primary in df.columns else []
    for c in df.columns:
        if c not in cols and any(k in _wkey(c) for k in ("DYNA", "DYNO")):
            cols.append(c)
    for c in df.columns:
        if c in cols or not (pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])):
            continue
        try:
            if df[c].str.contains(_MENTION, case=False, regex=True, na=False).any():
                cols.append(c)
        except Exception:
            pass
    return cols

def _echometer_latest(dyna: pd.DataFrame) -> dict:
    out = {}
    if dyna is None or dyna.empty:
        return out
    for w, g in dyna.sort_values("Timestamp").groupby("Well"):
        s = g.iloc[-1]
        rec = {"date": pd.Timestamp(s["Timestamp"])}
        gt = rsto.jload(s.get("goodman_tapers"), None)
        if isinstance(gt, dict):
            top = gt.get("1.0") or gt.get("1")
            vals = [v for v in (top or []) if v is not None]
            if vals:
                rec["goodman_pct"] = max(vals)
        if pd.notna(s.get("beam_loading_pct", np.nan)):
            rec["beam_pct"] = float(s["beam_loading_pct"])
        pe, rt = s.get("tq_peak_existing_inlb", np.nan), s.get("tq_gearbox_rating_inlb", np.nan)
        if pd.notna(pe) and pd.notna(rt) and rt > 0:
            rec["gbx_pct"] = float(pe) / float(rt) * 100
        out[w] = rec
    return out

def load_overrides(data_dir: str) -> pd.DataFrame:
    p = os.path.join(data_dir, OVERRIDES_FILE)
    cols = ["Well", "goodman_pct", "beam_pct", "gbx_pct", "Note"]
    if os.path.exists(p):
        try:
            return pd.read_csv(p).reindex(columns=cols)
        except Exception:
            pass
    return pd.DataFrame(columns=cols)

def load_settings(data_dir: str) -> dict:
    cfg = DEFAULT_SETTINGS.copy()
    if not data_dir:
        return cfg
    p = os.path.join(data_dir, SETTINGS_FILE)
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cfg.update(saved)
        except Exception:
            pass
    return cfg

def save_settings(data_dir: str, cfg: dict) -> tuple[bool, str]:
    try:
        os.makedirs(data_dir or ".", exist_ok=True)
        p = os.path.join(data_dir, SETTINGS_FILE)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        return True, ""
    except OSError as exc:
        return False, str(exc)

def collect_inputs(wells, status_map: dict, tests_all: pd.DataFrame, dfl_all: pd.DataFrame, df: pd.DataFrame, M: dict, wellcol: str,
                   data_dir: str = "", today=None) -> pd
