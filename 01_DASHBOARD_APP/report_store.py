"""
report_store.py: turn parsed Echometer reports into Optimizer inputs and keep them in the data folder.
Pure Python (pandas / numpy): no Streamlit, no import of the other optimizer modules, so it can be imported anywhere.

Stores (CSV in the dashboard data folder)
  Echometer_General.csv   one row per imported general-data page: configuration facts, rod string (JSON), production
  Echometer_DFL.csv       one row per acoustic survey
  Echometer_Dyna.csv      one row per dynamometer acquisition: results, valve test, Goodman tables, stresses, torque
Cards go to the existing Dyno_Cards.csv; configuration to SRP_Engineering_Configuration.csv; rods to SRP_Rod_Strings.csv.
"""
from __future__ import annotations

import difflib
import json
import os
import re

import numpy as np
import pandas as pd

GENERAL_FILE, DFL_FILE, DYNA_FILE = "Echometer_General.csv", "Echometer_DFL.csv", "Echometer_Dyna.csv"
LB_KN, FT, IN, HP_KW, INLB_KNM = 4.4482216e-3, 0.3048, 0.0254, 0.7457, 1.129848e-4
PSI_KPA = 6.894757
CARD_COLS = ["Well", "Timestamp", "Type", "Seq", "Position_m", "Load_kN", "SPM"]
QRANK = {"Measured": 0, "Manufacturer specification": 1, "Historical": 2, "Calculated": 3, "Assumed": 4}
STALE_PRESSURE_DAYS = 90

TUBING_ID_IN = {2.375: 1.995, 2.875: 2.441, 3.5: 2.992, 4.0: 3.476, 4.5: 3.958}      # standard-weight tubing


# ──────────────────────────────────────────────────────────────────────────────
# Well matching
# ──────────────────────────────────────────────────────────────────────────────
def _norm(s) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(s).upper())


def match_well(report_id: str, wells) -> tuple[str | None, float, str]:
    """Best dashboard well for a report's well id: exact, normalised, contained, then fuzzy."""
    wells = list(wells)
    if report_id in wells:
        return report_id, 1.0, "exact"
    n = _norm(report_id)
    for w in wells:
        if _norm(w) == n:
            return w, 0.98, "same after removing punctuation"
    contained = [w for w in wells if n and (n in _norm(w) or _norm(w) in n) and len(_norm(w)) >= 4]
    if len(contained) == 1:
        return contained[0], 0.85, "name contained"
    best, score = None, 0.0
    for w in wells:
        r = difflib.SequenceMatcher(None, n, _norm(w)).ratio()
        if r > score:
            best, score = w, r
    return (best, score, "closest name") if score >= 0.8 else (None, score, "no match")


# ──────────────────────────────────────────────────────────────────────────────
# Rod type -> dashboard material
# ──────────────────────────────────────────────────────────────────────────────
def rod_material(rtype) -> tuple[str, str, str]:
    """(material, quality, note). Echometer's N97 is evaluated in the 'H' (high-strength) Goodman column of its own reports."""
    t = str(rtype or "").upper().replace(" ", "")
    if re.search(r"FIBER|^FG|FRP|FIBERGLASS", t):
        return "Fiberglass", "Manufacturer specification", ""
    if re.search(r"SINKER|^SB|KBAR|WEIGHT", t):
        return "Sinker bar (steel)", "Manufacturer specification", ""
    if re.search(r"N-?97|NORRIS97|^HS$|^H$|HIGH", t):
        return "High-strength steel", "Manufacturer specification", "N97 is shown against the H column in the Echometer Goodman table"
    for g, name in (("C", "Steel Grade C"), ("D", "Steel Grade D"), ("K", "Steel Grade K")):
        if t in (g, "API" + g, "GRADE" + g):
            return name, "Manufacturer specification", ""
    return "Steel Grade D", "Assumed", f"Rod type '{rtype}' is not recognised: grade D assumed, please confirm"


def rods_from_report(r: dict) -> pd.DataFrame:
    rows = []
    for t in r["rods"]:
        mat, q, note = rod_material(t.get("type"))
        rows.append({"Order": t["taper"], "Length_m": round(t["length_ft"] * FT, 2), "Diameter_mm": round(t["diameter_in"] * 25.4, 2),
                     "Material": mat, "Quality": q, "Type_in_report": t.get("type"), "Note": note})
    return pd.DataFrame(rows)


# ──────────────────────────────────────────────────────────────────────────────
# Configuration from the report
# ──────────────────────────────────────────────────────────────────────────────
def _pressure_quality(date, method, today=None) -> str:
    today = today or pd.Timestamp.today().normalize()
    if date is None or pd.isna(date):
        return "Historical"
    age = (today - pd.Timestamp(date)).days
    if age <= STALE_PRESSURE_DAYS and method and str(method).lower() not in ("none", "nan"):
        return "Measured"
    return "Historical"


def config_from_report(r: dict, today=None) -> dict:
    """{config field: (value, quality, note)} for the SRP engineering configuration."""
    g, d, dfl, tq = r["general"], r["dyna"], r["dfl"], r["torque"]
    out: dict = {}

    def put(k, v, q, note=""):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return
        out[k] = (v, q, note)

    if g.get("plunger_in"):
        put("pump_dia_mm", round(g["plunger_in"] * 25.4, 3), "Manufacturer specification")
    put("pump_depth_m", g.get("pump_depth_m") or dfl.get("pump_depth_m"), "Historical", "pump intake depth, measured depth")
    if g.get("stroke_in"):
        put("stroke_m", round(g["stroke_in"] * IN, 4), "Measured", "measured stroke length")
    put("spm", d.get("spm"), "Measured", "from the dynamometer acquisition")
    put("unit_model", g.get("unit_api"), "Manufacturer specification", f"{g.get('manufacturer', '')} {g.get('unit_class', '')}".strip())
    if tq.get("gearbox_rating_inlb"):
        put("gearbox_rating_kNm", round(tq["gearbox_rating_inlb"] * INLB_KNM, 2), "Manufacturer specification", "gearbox rating on the torque page")
    if g.get("rated_hp"):
        put("motor_kW", round(g["rated_hp"] * HP_KW, 1), "Manufacturer specification")
    if g.get("cbe_klb"):
        put("cbe90_kN", round(g["cbe_klb"] * 1000 * LB_KN, 2), "Measured", "counterbalance effect with the weights level (90 degrees)")
    cw = g.get("counterweights_lb") or tq.get("counterweights_lb")
    if cw:
        put("counterweight_kN", round(cw * LB_KN, 2), "Manufacturer specification")
    if g.get("tubing_od_in"):
        put("tubing_od_mm", round(g["tubing_od_in"] * 25.4, 2), "Manufacturer specification")
        near = min(TUBING_ID_IN, key=lambda k: abs(k - g["tubing_od_in"]))
        if abs(near - g["tubing_od_in"]) < 0.01:
            put("tubing_id_mm", round(TUBING_ID_IN[near] * 25.4, 2), "Assumed", "standard-weight tubing ID assumed: confirm the tubing weight")
    comment = str(g.get("comment") or "").lower()
    if re.search(r"un-?anchor|no anchor", comment):
        put("tubing_anchored", "No", "Historical", "from the report comment")
    elif g.get("anchor_depth_m") or "anchor" in comment:
        put("tubing_anchored", "Yes", "Historical", "from the anchor depth / report comment")
    if g.get("water_sg") or dfl.get("water_sg"):
        put("sg_water", g.get("water_sg") or dfl.get("water_sg"), "Historical")
    api = g.get("oil_api") or dfl.get("oil_api")
    if api:
        put("sg_oil", round(141.5 / (131.5 + api), 4), "Calculated", f"from {api:g} deg API")
    pr = g.get("static_bhp_psi") or dfl.get("static_bhp_psi")
    if pr:
        q = _pressure_quality(g.get("static_bhp_date"), g.get("static_bhp_method"), today)
        put("res_pressure_psi", pr, q, f"static BHP{' dated ' + g['static_bhp_date'].strftime('%Y-%m-%d') if g.get('static_bhp_date') else ''}"
                                         f"{'' if g.get('static_bhp_method') else ', method not stated'}")
        if str(dfl.get("ipr_method", "")).lower().startswith("vogel"):
            put("bubble_point_psi", pr, "Assumed", "Echometer used a Vogel IPR: treated as saturated (bubble point = reservoir pressure)")
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Records for the stores
# ──────────────────────────────────────────────────────────────────────────────
def _json(x):
    def conv(o):
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
        if isinstance(o, dict):
            return {str(k): conv(v) for k, v in o.items()}
        if isinstance(o, (list, tuple)):
            return [conv(v) for v in o]
        return o
    return json.dumps(conv(x))


def general_record(r: dict, well: str, src: str) -> dict:
    g = dict(r["general"])
    for k in ("static_bhp_date", "producing_bhp_date", "production_date"):
        if g.get(k) is not None:
            g[k] = pd.Timestamp(g[k])
    g.update({"Well": well, "Report_well": r["well_id"], "Source_file": src, "Imported_at": pd.Timestamp.now().floor("s"),
              "Rods_json": _json(r["rods"])})
    return g


def stroke_variability(overlay: list[dict]) -> dict:
    """Spread of PPRL/MPRL over the overlaid strokes of one acquisition (a steady well repeats closely)."""
    if not overlay:
        return {}
    hi = np.array([float(np.max(o["load_klb"])) for o in overlay]) * 1000
    lo = np.array([float(np.min(o["load_klb"])) for o in overlay]) * 1000
    return {"Strokes": len(overlay), "PPRL_spread_pct": float((hi.max() - hi.min()) / hi.mean() * 100), "MPRL_spread_pct": float((lo.max() - lo.min()) / max(lo.mean(), 1) * 100)}


def dyna_record(r: dict, well: str, src: str) -> dict | None:
    d, tq = r["dyna"], r["torque"]
    ts = r["ts"].get("dyna")
    if not d or ts is None:
        return None
    rec = {"Well": well, "Report_well": r["well_id"], "Timestamp": pd.Timestamp(ts), "Valve_timestamp": pd.Timestamp(r["ts"]["valve"]) if r["ts"].get("valve") else pd.NaT,
           "Source_file": src, "Imported_at": pd.Timestamp.now().floor("s")}
    for k, v in d.items():
        rec[k] = _json(v) if isinstance(v, (dict, list)) else v
    for k, v in tq.items():
        rec["tq_" + k] = v
    rec.update(stroke_variability(r["cards"]["overlay"]))
    sc, pc = r["cards"]["surface"], r["cards"]["pump"]
    rec["Surface_card_verified"] = bool(sc and sc.get("verified"))
    rec["Pump_card_verified"] = bool(pc and pc.get("verified"))
    return rec


def dfl_record(r: dict, well: str, src: str) -> dict | None:
    d, ts = r["dfl"], r["ts"].get("dfl")
    if not d or ts is None or d.get("liquid_level_m") is None:
        return None
    rec = dict(d)
    pump = d.get("pump_depth_m") or r["general"].get("pump_depth_m")
    rec.update({"Well": well, "Report_well": r["well_id"], "Timestamp": pd.Timestamp(ts), "Source_file": src, "Imported_at": pd.Timestamp.now().floor("s"),
                "Pump_depth_m": pump})
    return rec


def cards_from_report(r: dict, well: str) -> pd.DataFrame:
    """Verified surface and pump cards in the Dyno_Cards.csv layout (m, kN)."""
    ts, spm = r["ts"].get("dyna"), r["dyna"].get("spm")
    rows = []
    if ts is None:
        return pd.DataFrame(columns=CARD_COLS)
    for kind, label in (("surface", "Surface"), ("pump", "Pump")):
        c = r["cards"][kind]
        if c is None or not c.get("verified"):
            continue
        n = len(c["pos_in"])
        rows.append(pd.DataFrame({"Well": well, "Timestamp": pd.Timestamp(ts), "Type": label, "Seq": np.arange(n),
                                  "Position_m": np.asarray(c["pos_in"]) * IN, "Load_kN": np.asarray(c["load_klb"]) * 1000 * LB_KN, "SPM": spm}))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=CARD_COLS)


# ──────────────────────────────────────────────────────────────────────────────
# Store IO
# ──────────────────────────────────────────────────────────────────────────────
def _is_date_col(c: str) -> bool:
    return c in ("Timestamp", "Valve_timestamp", "Imported_at", "Date") or c.endswith("_date")


def _coerce_dates(df: pd.DataFrame) -> pd.DataFrame:
    for c in df.columns:
        if _is_date_col(c):
            try:
                df[c] = pd.to_datetime(df[c], errors="coerce", format="mixed")
            except (TypeError, ValueError):           # older pandas without format="mixed"
                df[c] = df[c].apply(lambda v: pd.to_datetime(v, errors="coerce"))
    return df


def _read(path):
    if os.path.exists(path):
        try:
            return _coerce_dates(pd.read_csv(path))
        except Exception:
            pass
    return pd.DataFrame()


def upsert(path: str, rows: list[dict], key: list[str]):
    new = _coerce_dates(pd.DataFrame(rows))
    if new.empty:
        return
    both = pd.concat([_read(path), new], ignore_index=True)
    both = _coerce_dates(both).drop_duplicates(key, keep="last")
    both.to_csv(path, index=False)


def load_all(data_dir: str) -> dict:
    return {name: _read(os.path.join(data_dir, f)) for name, f in (("general", GENERAL_FILE), ("dfl", DFL_FILE), ("dyna", DYNA_FILE))}


def merge_report_dfl(dfl_all: pd.DataFrame, store: pd.DataFrame) -> pd.DataFrame:
    """Append imported acoustic surveys to the dashboard DFL table (same columns). Dashboard rows win on the same well and date."""
    if store is None or store.empty:
        return dfl_all
    rows = []
    for _, s in store.iterrows():
        pump = s.get("Pump_depth_m")
        lvl = s.get("liquid_level_m")
        if pd.isna(lvl):
            continue
        rows.append({"Well": s["Well"], "Date": pd.Timestamp(s["Timestamp"]).normalize(),
                     "NLAP": max(float(pump) - float(lvl), 0.0) if pd.notna(pump) else np.nan, "Liquid level": float(lvl),
                     "Casing pressure": s.get("casing_pressure_psi", np.nan), "BHP @ pump": s.get("pump_intake_psi", np.nan),
                     "Pump depth": float(pump) if pd.notna(pump) else np.nan})
    if not rows:
        return dfl_all
    add = pd.DataFrame(rows)
    both = pd.concat([dfl_all, add.reindex(columns=dfl_all.columns)], ignore_index=True)
    both["Well"] = both["Well"].astype(str)
    both = both.drop_duplicates(["Well", "Date"], keep="first")
    return both.sort_values(["Well", "Date"]).reset_index(drop=True)


def latest_measured(dyna: pd.DataFrame) -> dict:
    """Latest dynamometer acquisition per well in the engine's measured-card format."""
    out = {}
    if dyna is None or dyna.empty:
        return out
    for w, g in dyna.sort_values("Timestamp").groupby("Well"):
        s = g.iloc[-1]
        if pd.isna(s.get("pprl_lb")):
            continue
        out[w] = {"Well": w, "Timestamp": s["Timestamp"], "Type": "Surface", "PPRL_kN": float(s["pprl_lb"]) * LB_KN,
                  "MPRL_kN": float(s["mprl_lb"]) * LB_KN if pd.notna(s.get("mprl_lb")) else np.nan,
                  "Power_kW": float(s["prhp"]) * HP_KW if pd.notna(s.get("prhp")) else np.nan, "SPM": s.get("spm"), "source": "Echometer report"}
    return out


def latest_production(general: pd.DataFrame) -> dict:
    out = {}
    if general is None or general.empty or "production_date" not in general:
        return out
    for w, g in general.dropna(subset=["production_date"]).sort_values("production_date").groupby("Well"):
        s = g.iloc[-1]
        if pd.notna(s.get("oil_m3d")) and pd.notna(s.get("water_m3d")):
            out[w] = {"oil_m3d": float(s["oil_m3d"]), "water_m3d": float(s["water_m3d"]), "date": pd.Timestamp(s["production_date"]),
                      "tubing_psi": float(s["tubing_pressure_psi"]) if pd.notna(s.get("tubing_pressure_psi", np.nan)) else None}
    return out


def latest_dfl(dfl: pd.DataFrame, dyna: pd.DataFrame | None = None) -> dict:
    """Latest imported acoustic survey per well (producing/static BHP at formation depth, intake pressure); pump intake from the dyna as a fallback."""
    out = {}
    if dfl is not None and not dfl.empty:
        for w, g in dfl.sort_values("Timestamp").groupby("Well"):
            s_ = g.iloc[-1]
            out[w] = {"Timestamp": pd.Timestamp(s_["Timestamp"]), "producing_bhp_psi": s_.get("producing_bhp_psi"), "static_bhp_psi": s_.get("static_bhp_psi"),
                      "pump_intake_psi": s_.get("pump_intake_psi"), "method": "acoustic"}
    if dyna is not None and not dyna.empty:
        for w, g in dyna.sort_values("Timestamp").groupby("Well"):
            s_ = g.iloc[-1]
            if w not in out and pd.notna(s_.get("pump_intake_psi", np.nan)):
                out[w] = {"Timestamp": pd.Timestamp(s_["Timestamp"]), "producing_bhp_psi": None, "static_bhp_psi": None, "pump_intake_psi": float(s_["pump_intake_psi"]),
                          "method": "dynamometer analysis"}
    for w, v in out.items():
        for k in ("producing_bhp_psi", "static_bhp_psi", "pump_intake_psi"):
            if v.get(k) is not None and pd.isna(v[k]):
                v[k] = None
    return out


def jload(x, default=None):
    try:
        return json.loads(x) if isinstance(x, str) else default
    except Exception:
        return default
