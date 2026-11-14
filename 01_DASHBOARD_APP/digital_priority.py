"""
digital_priority.py: which wells get PT sensors, ROPC or VSD.

Rules (as specified)
  1. Only RUNNING wells are assessed.
  2. A running well whose LAST well test has net oil <= threshold (default 4 m3/d) is a PT sensor automatically.
  3. Running wells above the threshold go into a priority matrix. Each parameter is scored from the well's position in the
     distribution of the matrix wells (lowest = bottom of the range, highest = top, linear in between):
        gross rate, NLAP, rod loaded, beam loading, gearbox torque        0 to 10
        net oil                                                            10 to 30
  4. Total score = sum of the six scores. The top 30% of matrix wells are recommended VSD, the next 30% ROPC, the rest PT sensors.

Dynamometer values (rod loaded %, beam loading %, gearbox torque %) come from the last dynamometer result, in this order:
  manual entry (this section)  >  numbers read from the master "DYNAMOMETER TEST RESULTS" text  >  imported Echometer report.
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
BBL = 6.28981
FT = 3.28084

# key, label, lowest score, highest score
PARAMS = [("net_oil", "Net oil", 10.0, 30.0), ("gross", "Gross rate", 0.0, 10.0), ("nlap", "NLAP", 0.0, 10.0),
          ("rod", "Rod loaded", 0.0, 10.0), ("beam", "Beam loading", 0.0, 10.0), ("gbx", "Gearbox torque", 0.0, 10.0)]
def _rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


PARAM_COLORS = {"net_oil": "#2F80ED", "gross": "#CE1141", "nlap": "#1F7A8C", "rod": "#E08A00", "beam": "#7B61C9", "gbx": "#4C9F70"}


# ══════════════════════════════════════════════════════════════════════════════
# Reading numbers out of the master dynamometer text
# ══════════════════════════════════════════════════════════════════════════════
# Parameter names, tolerant of spelling and punctuation: "Rod Loaded", "rod loding", "Rods load", "G.B Torque loaded", "GB torque", "Gear box load", "Beam loading"
_LOAD = r"(?:l[o0]a?d(?:ed|ing|ng|s)?|l[o0]d(?:ed|ing)?)"
_ROD = [r"rods?[\s.\-:]*(?:string[\s.\-:]*)?" + _LOAD + r"", r"goodman(?:[\s.\-:]*(?:diagram|ratio|" + _LOAD + r"))?"]
_BEAM = [r"beam[\s.\-:]*(?:" + _LOAD + r")?"]
_GBX = [r"(?:g\s*[./\-]?\s*b\.?|gear\s*-?\s*box|gbx|g/box)[\s.\-:]*(?:(?:torque|trq|tq)[\s.\-:]*)?(?:" + _LOAD + r")?",
        r"(?:torque|trq)[\s.\-:]*" + _LOAD + r""]
# what may sit between a name and its number: "Rod Loaded : 85 %", "Beam loading = ~60", "G.B Torque loaded (72%)", "up to 80"
_SEP = r"(?:[\s:=\-.,;|/>(\[{~≈±]|loaded|loading|load|torque|stress|max|maximum|value|ratio|is|at|of|about|around|approx\.?|approximately|near|nearly|up|to|percent|pct|%)*?"
_NUM = r"(\d+(?:\.\d+)?)"
_UNIT = r"\s*(%|pct|percent|psi|kn|lbs?|kg)?"
_NOT_PCT = {"psi", "kn", "lb", "lbs", "kg"}
_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٫٬٪％ –—−",
                        "0123456789.,%% ---")


def _clean_text(text: str) -> str:
    t = str(text).lower().translate(_DIGITS)
    t = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", t)          # 27,500 -> 27500
    t = re.sub(r"(?<=\d),(?=\d{1,2}(?!\d))", ".", t)       # 85,5 -> 85.5
    return t


def _find(t: str, names, lo: float, hi: float):
    """First number that follows one of the name patterns, as a percentage within [lo, hi]. A 0.85-style fraction becomes 85."""
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
    """Extract rod loaded, beam loading and gearbox torque (all in %) from the free text of one dynamometer result cell.

    The cell holds everything together and each value follows its parameter name, e.g.
        'Rod Loaded : 85 %, Beam loading : 60 %, G.B Torque loaded : 72 %'
    Spelling variants, other separators, brackets, "~", "approx", fractions (0.85) and decimal commas are tolerated.
    Rod loaded is returned as goodman_pct (the stored key name is kept so saved manual values still work).
    """
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


_MENTION = r"rods?\s*[.\-:]*\s*l[o0]|beam\s*[.\-:]*\s*l[o0]|g\s*[./\-]?\s*b\.?\s*(?:torque|l[o0])|gear\s*-?\s*box\s*(?:torque|l[o0])"


def _dyno_columns(df: pd.DataFrame, primary) -> list:
    """The configured dynamometer column first, then any other column named like a dynamometer result, then any text column that actually mentions the parameters."""
    cols = [primary] if primary in df.columns else []
    for c in df.columns:
        if c not in cols and any(k in _wkey(c) for k in ("DYNA", "DYNO")):
            cols.append(c)
    for c in df.columns:
        if c in cols or not (pd.api.types.is_object_dtype(df[c]) or pd.api.types.is_string_dtype(df[c])):   # pandas 3 uses a string dtype, not object
            continue
        try:
            if df[c].str.contains(_MENTION, case=False, regex=True, na=False).any():
                cols.append(c)
        except Exception:
            pass
    return cols


# ══════════════════════════════════════════════════════════════════════════════
# Assemble the inputs for every well
# ══════════════════════════════════════════════════════════════════════════════
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


def collect_inputs(wells, status_map: dict, tests_all: pd.DataFrame, dfl_all: pd.DataFrame, df: pd.DataFrame, M: dict, wellcol: str,
                   data_dir: str = "", today=None) -> pd.DataFrame:
    today = pd.Timestamp(today or pd.Timestamp.today()).normalize()
    rows = {w: {"Well": w, "Status": status_map.get(w, "NO DATA")} for w in wells}
    # last test with a net oil value
    t = tests_all.dropna(subset=["Net Oil"]).sort_values("Date")
    for w, g in t.groupby("Well"):
        if w in rows:
            s = g.iloc[-1]
            rows[w].update({"last_test": pd.Timestamp(s["Date"]), "net_oil": float(s["Net Oil"]), "gross": float(s["Rate"]) if pd.notna(s["Rate"]) else np.nan})
    # last NLAP (dashboard DFL plus imported acoustic surveys)
    dfl = dfl_all
    if data_dir:
        dfl = rsto.merge_report_dfl(dfl_all, rsto.load_all(data_dir)["dfl"])
    for w, g in dfl.dropna(subset=["NLAP"]).sort_values("Date").groupby("Well"):
        if w in rows:
            s = g.iloc[-1]
            rows[w].update({"nlap": float(s["NLAP"]), "nlap_date": pd.Timestamp(s["Date"])})
    # dynamometer values: master text, then Echometer, then manual
    text_by_well, parsed_by_well, srcinfo_by_well = {}, {}, {}
    primary = M.get("dyno") if M else None
    dcols = _dyno_columns(df, primary) if M else []
    if dcols:
        dcol = M.get("test_date") if M.get("test_date") in df.columns else None
        keys = df[wellcol].map(_wkey)
        for w, g in df.groupby(keys):
            if not w:
                continue
            # most recent result first; a parameter missing from it is taken from the next most recent one that has it
            texts = {c: g[c].tolist() for c in dcols}
            n = len(g)
            dates = pd.to_datetime(g[dcol], errors="coerce").tolist() if dcol else [pd.NaT] * n
            parsed, info, newest_text, found_text = {}, {}, None, None
            for pos in range(n - 1, -1, -1):
                for c in dcols:
                    v = texts[c][pos]
                    if not (isinstance(v, str) and v.strip()):
                        continue
                    if newest_text is None:
                        newest_text = v
                    p = parse_dyno_text(v)
                    if p and found_text is None:
                        found_text = v                      # newest cell that actually holds readable values
                    for k, val_ in p.items():
                        if k not in parsed:
                            parsed[k], info[k] = val_, (dates[pos], c)
                if all(k in parsed for k in ("goodman_pct", "beam_pct", "gbx_pct")):
                    break
            if newest_text is not None:
                text_by_well[w] = found_text or newest_text
                parsed_by_well[w] = parsed
                srcinfo_by_well[w] = info
    emet = _echometer_latest(rsto.load_all(data_dir)["dyna"]) if data_dir else {}
    ov = load_overrides(data_dir) if data_dir else pd.DataFrame(columns=["Well"])
    ov_by = {r["Well"]: r for _, r in ov.iterrows()} if len(ov) else {}
    for w, r in rows.items():
        src, dates = {}, None
        wk = _wkey(w)
        parsed = parsed_by_well.get(wk, {})
        for k, v in parsed.items():
            dt, col = srcinfo_by_well.get(wk, {}).get(k, (pd.NaT, primary))
            r[k], src[k] = v, ("Master text" + (f" {dt:%Y-%m-%d}" if dt is not None and pd.notna(dt) else "") + ("" if col == primary else f" [{col}]"))
        r["dyno_text"] = text_by_well.get(wk)
        e = emet.get(w)
        if e:
            for k in ("goodman_pct", "beam_pct", "gbx_pct"):
                if k in e and not str(src.get(k, "")).startswith("Master text"):   # the dynamometer test text wins over the Echometer import
                    r[k], src[k] = e[k], f"Echometer {e['date']:%Y-%m-%d}"
            dates = e["date"]
        o = ov_by.get(w)
        if o is not None:
            for k in ("goodman_pct", "beam_pct", "gbx_pct"):
                if pd.notna(o.get(k)):
                    r[k], src[k] = float(o[k]), "Manual"
        r["dyno_sources"] = ", ".join(sorted(set(src.values()))) if src else ""
        r["dyno_date"] = dates
    out = pd.DataFrame(list(rows.values()))
    for c in ("net_oil", "gross", "nlap", "goodman_pct", "beam_pct", "gbx_pct"):
        if c not in out:
            out[c] = np.nan
    for c in ("last_test", "nlap_date", "dyno_date"):
        if c not in out:
            out[c] = pd.NaT
    out["days_since_test"] = (today - pd.to_datetime(out["last_test"])).dt.days
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Scoring (pure)
# ══════════════════════════════════════════════════════════════════════════════
def minmax_score(s: pd.Series, lo: float, hi: float) -> pd.Series:
    """Linear position of each value between the lowest (-> lo) and highest (-> hi) value of the series. NaN stays NaN."""
    mn, mx = s.min(), s.max()
    if pd.isna(mn):
        return pd.Series(np.nan, index=s.index)
    if mx == mn:                                               # nothing to discriminate: everyone gets the middle score
        return s.where(s.isna(), (lo + hi) / 2)
    return lo + (hi - lo) * (s - mn) / (mx - mn)


def split_counts(n: int, vsd_pct: float, ropc_pct: float) -> tuple[int, int]:
    n_v = int(np.floor(vsd_pct / 100 * n + 0.5))
    n_r = int(np.floor(ropc_pct / 100 * n + 0.5))
    n_r = min(n_r, max(n - n_v, 0))
    return min(n_v, n), n_r


def build_priority(inputs: pd.DataFrame, threshold: float = 4.0, vsd_pct: float = 30.0, ropc_pct: float = 30.0, base: str = "matrix",
                   missing: str = "median", max_test_age: int = 0) -> tuple[pd.DataFrame, dict]:
    """Returns (one row per well with scores and recommendation, meta)."""
    d = inputs.copy()
    d["Recommendation"] = "Not assessed"
    d["Reason"] = ""
    running = d["Status"] == "RUNNING"
    stale = (d["days_since_test"] > max_test_age) if max_test_age else pd.Series(False, index=d.index)
    has_test = d["net_oil"].notna() & ~stale
    d.loc[~running, "Reason"] = d["Status"].map({"STOPPED": "Not running (stopped)", "NO DATA": "Not running (no run data)"}).fillna("Not running")
    d.loc[running & d["net_oil"].isna(), "Reason"] = "No well test with net oil"
    d.loc[running & d["net_oil"].notna() & stale, "Reason"] = f"Last test older than {max_test_age} days"
    assessed = running & has_test
    auto = assessed & (d["net_oil"] <= threshold)
    matrix = assessed & (d["net_oil"] > threshold)
    d.loc[auto, ["Recommendation", "Reason"]] = ["PT sensor", f"Net oil at or below {threshold:g} m3/d: PT sensor automatically"]

    src_cols = {"net_oil": "net_oil", "gross": "gross", "nlap": "nlap", "rod": "goodman_pct", "beam": "beam_pct", "gbx": "gbx_pct"}   # rod loaded (%)
    for key, label, lo, hi in PARAMS:
        d[f"s_{key}"] = np.nan
        raw = d.loc[matrix, src_cols[key]]
        d.loc[matrix, f"s_{key}"] = minmax_score(raw, lo, hi)
    meta = {"threshold": threshold, "n_running": int(running.sum()), "n_assessed": int(assessed.sum()), "n_auto": int(auto.sum()),
            "n_matrix": int(matrix.sum()), "missing_policy": missing, "unavailable": []}
    filled = pd.DataFrame(False, index=d.index, columns=[k for k, *_ in PARAMS])
    for key, label, lo, hi in PARAMS:
        col = d.loc[matrix, f"s_{key}"]
        if col.notna().sum() == 0:
            meta["unavailable"].append(label)
        gap = matrix & d[f"s_{key}"].isna()
        if gap.any():
            fill = (col.median() if missing == "median" and col.notna().any() else lo)
            d.loc[gap, f"s_{key}"] = fill
            filled.loc[gap, key] = True
    d["Total score"] = d[[f"s_{k}" for k, *_ in PARAMS]].sum(axis=1, min_count=1).where(matrix)
    d["Data gaps"] = ""
    d.loc[matrix, "Data gaps"] = filled.loc[matrix].apply(lambda r: ", ".join(lbl for (k, lbl, *_), f in zip(PARAMS, r) if f), axis=1)

    order = d[matrix].sort_values(["Total score", "net_oil"], ascending=False).index
    d["Rank"] = np.nan
    d.loc[order, "Rank"] = np.arange(1, len(order) + 1)
    n_base = int(running.sum()) if base == "running" else int(matrix.sum())
    n_v, n_r = split_counts(n_base, vsd_pct, ropc_pct)
    n_v = min(n_v, len(order))
    n_r = min(n_r, len(order) - n_v)
    for i, idx in enumerate(order):
        if i < n_v:
            d.loc[idx, ["Recommendation", "Reason"]] = ["VSD", f"Top {vsd_pct:g}% of the priority ranking"]
        elif i < n_v + n_r:
            d.loc[idx, ["Recommendation", "Reason"]] = ["ROPC", f"Next {ropc_pct:g}% of the priority ranking"]
        else:
            d.loc[idx, ["Recommendation", "Reason"]] = ["PT sensor", "Remaining wells of the priority ranking"]
    meta.update({"n_vsd": n_v, "n_ropc": n_r, "n_pt_matrix": len(order) - n_v - n_r})
    return d, meta


# ══════════════════════════════════════════════════════════════════════════════
# Presentation table
# ══════════════════════════════════════════════════════════════════════════════
def detail_table(d: pd.DataFrame, meta: dict) -> pd.DataFrame:
    out = pd.DataFrame({
        "Rank": d["Rank"], "Well": d["Well"], "Recommendation": d["Recommendation"].map(EMOJI), "Why": d["Reason"], "Total score": d["Total score"].round(1),
        "Net oil (m3/d)": d["net_oil"].round(1), "Net oil (BOPD)": (d["net_oil"] * BBL).round(0), "Gross (BLPD)": (d["gross"] * BBL).round(0),
        "NLAP (ft)": (d["nlap"] * FT).round(0), "Rod loaded (%)": d["goodman_pct"].round(1),
        "Beam loading (%)": d["beam_pct"].round(1), "Gearbox torque (%)": d["gbx_pct"].round(1),
    })
    for key, label, lo, hi in PARAMS:
        out[f"Score: {label}"] = d[f"s_{key}"].round(1)
    out["Missing values filled for"] = d["Data gaps"]
    out["Dyna source"] = d["dyno_sources"]
    out["Last test"] = pd.to_datetime(d["last_test"]).dt.date
    out["Days since test"] = d["days_since_test"]
    out["Status"] = d["Status"]
    return out.sort_values(["Rank", "Well"], na_position="last").reset_index(drop=True)


# ══════════════════════════════════════════════════════════════════════════════
# Charts
# ══════════════════════════════════════════════════════════════════════════════
def fig_donut(d: pd.DataFrame, style_fig):
    counts = d["Recommendation"].value_counts()
    labels = [t for t in TECH_ORDER if counts.get(t, 0) > 0]
    fig = go.Figure(go.Pie(labels=labels, values=[int(counts[t]) for t in labels], hole=0.62, sort=False, marker=dict(colors=[COLORS[t] for t in labels], line=dict(color="#FFFFFF", width=3)),
                           textinfo="label+value", hovertemplate="%{label}: %{value} wells (%{percent})<extra></extra>"))
    fig.add_annotation(text=f"<b>{int(sum(counts.get(t, 0) for t in TECH_ORDER))}</b><br>wells", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False, font=dict(size=22))
    return style_fig(fig, 340, legend=False)


def fig_sankey(d: pd.DataFrame, meta: dict, n_total: int, style_fig):
    n_notrun = n_total - meta["n_running"]
    n_notest = meta["n_running"] - meta["n_assessed"]
    labels = ["All wells", "Running", "Not running", "No usable test", f"Net oil ≤ {meta['threshold']:g} m³/d", "Priority matrix", "VSD", "ROPC", "PT sensor"]
    colors = ["#5F6368", "#2E7D4F", "#9AA0A6", "#BDC1C6", COLORS["PT sensor"], "#1A1A1A", COLORS["VSD"], COLORS["ROPC"], COLORS["PT sensor"]]
    links = [(0, 1, meta["n_running"]), (0, 2, n_notrun), (1, 3, n_notest), (1, 4, meta["n_auto"]), (1, 5, meta["n_matrix"]),
             (4, 8, meta["n_auto"]), (5, 6, meta["n_vsd"]), (5, 7, meta["n_ropc"]), (5, 8, meta["n_pt_matrix"])]
    links = [l for l in links if l[2] > 0]
    fig = go.Figure(go.Sankey(arrangement="snap", node=dict(label=labels, color=colors, pad=22, thickness=20, line=dict(color="#FFFFFF", width=1)),
                              link=dict(source=[l[0] for l in links], target=[l[1] for l in links], value=[l[2] for l in links],
                                        color=[_rgba(colors[l[1]], 0.35) for l in links])))
    return style_fig(fig, 340, legend=False)


def _num(v, spec: str, unit: str = "") -> str:
    return "n/a" if v is None or pd.isna(v) else f"{v:{spec}}{unit}"


def _hover_cards(m: pd.DataFrame) -> list[str]:
    """One tooltip text per well: every parameter's value and its score (shown whichever bar segment is pointed at)."""
    cards = []
    for _, r in m.iterrows():
        gaps = {g.strip() for g in str(r.get("Data gaps") or "").split(",") if g.strip()}
        values = {"net_oil": _num(r["net_oil"] * BBL if pd.notna(r["net_oil"]) else np.nan, ",.0f", " BOPD"),
                  "gross": _num(r["gross"] * BBL if pd.notna(r["gross"]) else np.nan, ",.0f", " BLPD"),
                  "nlap": _num(r["nlap"] * FT if pd.notna(r["nlap"]) else np.nan, ",.0f", " ft"),
                  "rod": _num(r["goodman_pct"], ".1f", " %"), "beam": _num(r["beam_pct"], ".1f", " %"), "gbx": _num(r["gbx_pct"], ".1f", " %")}
        lines = [f"<b>{r['Well']}</b>  ·  {r['Recommendation']}  ·  rank {int(r['Rank'])}",
                 f"<b>Total score: {r['Total score']:.1f}</b>", "──────────────────"]
        for key, label, lo, hi in PARAMS:
            note = "  (no data, score filled)" if label in gaps else ""
            lines.append(f"{label}: {values[key]}  →  score <b>{r[f's_{key}']:.1f}</b> (of {lo:g}–{hi:g}){note}")
        cards.append("<br>".join(lines))
    return cards


def fig_ranking(d: pd.DataFrame, style_fig, meta: dict | None = None):
    m = d[d["Rank"].notna()].sort_values("Rank")
    if m.empty:
        return None
    ylab = [f"{w}  ·  {r}" for w, r in zip(m["Well"], m["Recommendation"])]
    cards = _hover_cards(m)
    fig = go.Figure()
    for key, label, lo, hi in PARAMS:
        fig.add_bar(y=ylab, x=m[f"s_{key}"], orientation="h", name=label, marker_color=PARAM_COLORS[key],
                    customdata=cards, hovertemplate="%{customdata}<extra></extra>")
    # cut lines between the groups
    tot = m["Total score"].tolist()
    recs = m["Recommendation"].tolist()
    for a, b, text in (("VSD", "ROPC", "VSD | ROPC"), ("ROPC", "PT sensor", "ROPC | PT")):
        ia = [i for i, r in enumerate(recs) if r == a]
        ib = [i for i, r in enumerate(recs) if r == b]
        if ia and ib:
            fig.add_shape(type="line", xref="x", yref="y", x0=0, x1=max(tot) * 1.02, y0=ia[-1] + 0.5, y1=ia[-1] + 0.5, line=dict(color="#444", width=1.5, dash="dot"))
            fig.add_annotation(x=max(tot) * 1.02, y=ia[-1] + 0.5, text=text, showarrow=False, xanchor="right", yanchor="bottom", font=dict(size=11, color="#444"))
    fig.update_layout(barmode="stack", xaxis_title="Total priority score (net oil 10-30 + five parameters 0-10 each)", yaxis=dict(autorange="reversed", type="category"),
                      hoverlabel=dict(bgcolor="#FFFFFF", bordercolor="#888888", font=dict(color="#111111", size=12), align="left", namelength=-1))
    fig = style_fig(fig, max(380, 24 * len(m) + 120), legend=True)
    fig.update_layout(hovermode="closest")          # one card for the pointed well (style_fig sets "x unified")
    return fig


def fig_heatmap(d: pd.DataFrame, style_fig):
    m = d[d["Rank"].notna()].sort_values("Rank")
    if m.empty:
        return None
    cols = [f"s_{k}" for k, *_ in PARAMS]
    labels = [lbl for _, lbl, *_ in PARAMS]
    raw = m[cols].to_numpy(float)
    norm = np.column_stack([(raw[:, i] - lo) / (hi - lo) for i, (_, _, lo, hi) in enumerate(PARAMS)])
    ylab = [f"{w}  ·  {r}" for w, r in zip(m["Well"], m["Recommendation"])]
    fig = go.Figure(go.Heatmap(z=norm, x=labels, y=ylab, zmin=0, zmax=1, colorscale=[[0, "#F4F4F5"], [0.5, "#F2B6C4"], [1, "#CE1141"]],
                               text=np.round(raw, 1), texttemplate="%{text}" if len(m) <= 45 else None, hovertemplate="%{y}<br>%{x}: %{text}<extra></extra>",
                               colorbar=dict(title=dict(text="position<br>in range"), tickvals=[0, 1], ticktext=["lowest", "highest"])))
    fig.update_yaxes(autorange="reversed", type="category")
    return style_fig(fig, max(380, 24 * len(m) + 120), legend=False)


def fig_scatter(d: pd.DataFrame, style_fig):
    m = d[d["Rank"].notna()]
    if m.empty:
        return None
    fig = go.Figure()
    for t in TECH_ORDER:
        g = m[m["Recommendation"] == t]
        if g.empty:
            continue
        fig.add_scatter(x=g["net_oil"] * BBL, y=g["Total score"], mode="markers+text", text=g["Well"], textposition="top center", name=t,
                        marker=dict(size=np.clip((g["gross"].fillna(g["gross"].median()) * BBL) / max(m["gross"].max() * BBL, 1) * 38 + 10, 10, 48), color=COLORS[t],
                                    line=dict(color="#FFFFFF", width=1.5), opacity=0.9),
                        hovertemplate="%{text}<br>Net oil %{x:,.0f} BOPD<br>Total score %{y:.1f}<extra></extra>")
    fig.update_layout(xaxis_title="Net oil, last test (BOPD)", yaxis_title="Total priority score")
    return style_fig(fig, 420, legend=True)


def fig_radar(d: pd.DataFrame, well: str, style_fig):
    m = d[d["Rank"].notna()]
    if m.empty or well not in set(m["Well"]):
        return None
    labels = [lbl for _, lbl, *_ in PARAMS]
    def pct(row):
        return [(row[f"s_{k}"] - lo) / (hi - lo) * 100 for k, _, lo, hi in PARAMS]
    me = pct(m[m["Well"] == well].iloc[0])
    med = [float(np.median([pct(r)[i] for _, r in m.iterrows()])) for i in range(len(PARAMS))]
    fig = go.Figure()
    fig.add_scatterpolar(r=med + med[:1], theta=labels + labels[:1], name="Median well", line=dict(color="#9AA0A6", dash="dot"), fill="toself", fillcolor="rgba(154,160,166,0.15)")
    rec = m[m["Well"] == well].iloc[0]["Recommendation"]
    fig.add_scatterpolar(r=me + me[:1], theta=labels + labels[:1], name=well, line=dict(color=COLORS[rec], width=3), fill="toself", fillcolor=_rgba(COLORS[rec], 0.2))
    fig.update_layout(polar=dict(radialaxis=dict(range=[0, 100], ticksuffix="%")))
    return style_fig(fig, 400, legend=True)


# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════
def _excel_bytes(sheets: dict) -> bytes | None:
    try:
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as xw:
            for name, df_ in sheets.items():
                df_.to_excel(xw, sheet_name=name[:31], index=False)
        return buf.getvalue()
    except Exception:
        return None


def render_digitalization(df, tests_all, dfl_all, status_map, wellcol, M, selected, ui, data_dir):
    kpi, section, plot, table, style_fig = ui["kpi"], ui["section"], ui["plot"], ui["table"], ui["style_fig"]
    st.markdown("#### Digitalization deployment priority")
    st.caption("Which running wells should get PT sensors, ROPC or VSD. Wells at or below the net-oil threshold on their last test get a PT sensor automatically. "
               "The rest are scored against each other (position in the distribution of those wells: lowest = bottom of the range, highest = top) on gross rate, NLAP, "
               "rod loaded, beam loading, gearbox torque (0 to 10 each) and net oil (10 to 30). The highest 30% of the total are VSD candidates, the next 30% ROPC, the rest PT sensors.")
    all_wells = sorted({str(w) for w in df[wellcol].dropna().astype(str)})

    with st.expander("⚙️ Settings"):
        c = st.columns(4)
        thr = c[0].number_input("Automatic PT sensor at or below (net oil, m³/d)", 0.0, 100.0, 4.0, 0.5, key="dig_thr")
        c[0].caption(f"≈ {thr * BBL:,.0f} BOPD")
        vsd = c[1].number_input("VSD: top share of ranked wells (%)", 0, 100, 30, 5, key="dig_vsd")
        ropc = c[2].number_input("ROPC: next share (%)", 0, 100, 30, 5, key="dig_ropc")
        age = c[3].number_input("Ignore tests older than (days, 0 = never)", 0, 1000, 0, 30, key="dig_age")
        c2 = st.columns(2)
        base = c2[0].radio("Shares are taken of", ["Ranked wells (net oil above the threshold)", "All running wells"], key="dig_base")
        miss = c2[1].radio("A well with a missing dynamometer value gets", ["The median score of the other wells", "The lowest score"], key="dig_miss")
        if vsd + ropc > 100:
            st.warning("VSD and ROPC shares add up to more than 100%: the ROPC group is reduced to fit.")

    inputs = collect_inputs(all_wells, status_map, tests_all, dfl_all, df, M, wellcol, data_dir)
    res, meta = build_priority(inputs, thr, vsd, ropc, "running" if base.startswith("All") else "matrix", "median" if miss.startswith("The median") else "lowest", age)
    tbl = detail_table(res, meta)

    n_pt = meta["n_auto"] + meta["n_pt_matrix"]
    k = st.columns(5)
    k[0].markdown(kpi("Running wells", str(meta["n_running"]), f"of {len(all_wells)} wells"), unsafe_allow_html=True)
    k[1].markdown(kpi("VSD candidates", str(meta["n_vsd"]), "highest priority scores", tone="bad"), unsafe_allow_html=True)
    k[2].markdown(kpi("ROPC candidates", str(meta["n_ropc"]), "next priority scores", tone="warn"), unsafe_allow_html=True)
    k[3].markdown(kpi("PT sensors", str(n_pt), f"{meta['n_auto']} automatic + {meta['n_pt_matrix']} ranked"), unsafe_allow_html=True)
    k[4].markdown(kpi("Not assessed", str(len(all_wells) - meta["n_assessed"]), "stopped, no data or no test"), unsafe_allow_html=True)
    if selected in set(res["Well"]):
        r = res[res["Well"] == selected].iloc[0]
        st.markdown(f"**Selected well {selected}:** {EMOJI[r['Recommendation']]}" + (f", rank {int(r['Rank'])} of {meta['n_matrix']} with score {r['Total score']:.1f}" if pd.notna(r["Rank"]) else f" ({r['Reason']})"))
    if meta["unavailable"]:
        st.warning("No ranked well has a value for: " + ", ".join(meta["unavailable"]) + ". That parameter adds nothing to the ranking until values are added "
                   "below (manual entry) or Echometer reports are imported.")
    if meta["n_matrix"] == 0:
        st.info("No running well is above the net-oil threshold, so there is nothing to rank.")

    c1, c2 = st.columns([3, 2])
    with c1, st.container(border=True):
        plot(fig_sankey(res, meta, len(all_wells), style_fig), "dig_sankey")
    with c2, st.container(border=True):
        plot(fig_donut(res, style_fig), "dig_donut")

    t1, t2, t3, t4, t5 = st.tabs(["🏆 Ranking", "🔥 Score heatmap", "🎯 Net oil vs score", "🕸️ Well profile", "📊 Selection Analytics"])
    with t1:
        f = fig_ranking(res, style_fig, meta)
        if f is not None:
            with st.container(border=True):
                plot(f, "dig_rank")
            st.caption("Each bar is the total score, split by parameter. Dotted lines mark where VSD ends and ROPC ends.")
    with t2:
        f = fig_heatmap(res, style_fig)
        if f is not None:
            with st.container(border=True):
                plot(f, "dig_heat")
            st.caption("Colour shows the position inside each parameter's own range (net oil 10 to 30, the others 0 to 10); the number is the score.")
    with t3:
        f = fig_scatter(res, style_fig)
        if f is not None:
            with st.container(border=True):
                plot(f, "dig_scatter")
            st.caption("Bubble size is the gross rate. High score at low net oil means the well is loaded mechanically, not just productive.")
    with t4:
        ws = st.selectbox("Well", sorted(res[res["Rank"].notna()]["Well"]), index=(sorted(res[res["Rank"].notna()]["Well"]).index(selected) if selected in set(res[res["Rank"].notna()]["Well"]) else 0), key="dig_radar_well") if meta["n_matrix"] else None
        if ws:
            f = fig_radar(res, ws, style_fig)
            if f is not None:
                with st.container(border=True):
                    plot(f, "dig_radar")
                st.caption("Each axis is the well's score as a share of the parameter's range, against the median ranked well.")
    with t5:
        render_selection_analytics(res, ui)

    section("Detailed table")
    fl = st.multiselect("Show", ["VSD", "ROPC", "PT sensor", "Not assessed"], default=["VSD", "ROPC", "PT sensor"], key="dig_filter")
    view = tbl[tbl["Recommendation"].isin([EMOJI[x] for x in fl])] if fl else tbl
    table(view, column_config={"Total score": st.column_config.ProgressColumn("Total score", format="%.1f", min_value=10, max_value=80)})
    d1, d2 = st.columns(2)
    d1.download_button("⬇ Download table (CSV)", tbl.to_csv(index=False).encode("utf-8"), file_name="digitalization_priority.csv", mime="text/csv", key="dig_csv")
    xb = _excel_bytes({"Priority": tbl, "Inputs": inputs.drop(columns=["dyno_text"], errors="ignore")})
    if xb:
        d2.download_button("⬇ Download Excel", xb, file_name="digitalization_priority.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", key="dig_xlsx")

    with st.expander("✍️ Fill or correct dynamometer values (rod loaded, beam loading, gearbox torque)"):
        st.caption("Values here override the dynamometer text and the Echometer import. Leave blank to keep what was found. "
                   "Rod loaded, beam loading and gearbox torque are all in %.")
        pool = res[res["Rank"].notna() | ((res["Status"] == "RUNNING") & res["net_oil"].notna() & (res["net_oil"] > thr))]
        only_gaps = st.checkbox("Only wells with a missing value", True, key="dig_only_gaps")
        ed = pool[["Well", "goodman_pct", "beam_pct", "gbx_pct"]].copy()
        if only_gaps:
            ed = ed[ed[["goodman_pct", "beam_pct", "gbx_pct"]].isna().any(axis=1)]
        ov = load_overrides(data_dir)
        base_ov = ov.set_index("Well") if len(ov) else pd.DataFrame(columns=["goodman_pct", "beam_pct", "gbx_pct", "Note"])
        show = pd.DataFrame({"Well": ed["Well"],
                             "Found: rod loaded (%)": ed["goodman_pct"].values, "Found: beam (%)": ed["beam_pct"].values, "Found: gearbox (%)": ed["gbx_pct"].values,
                             "Rod loaded (%)": [base_ov["goodman_pct"].get(w, np.nan) for w in ed["Well"]],
                             "Beam loading (%)": [base_ov["beam_pct"].get(w, np.nan) for w in ed["Well"]], "Gearbox torque (%)": [base_ov["gbx_pct"].get(w, np.nan) for w in ed["Well"]]})
        edited = st.data_editor(show, hide_index=True, key="dig_editor", disabled=["Well", "Found: rod loaded (%)", "Found: beam (%)", "Found: gearbox (%)"], **ui.get("STRETCH", {}))
        if st.button("💾 Save manual values", key="dig_save"):
            new = pd.DataFrame({"Well": edited["Well"], "goodman_pct": edited["Rod loaded (%)"],
                                "beam_pct": edited["Beam loading (%)"], "gbx_pct": edited["Gearbox torque (%)"], "Note": "manual"})
            new = new.dropna(subset=["goodman_pct", "beam_pct", "gbx_pct"], how="all")
            keep = ov[~ov["Well"].isin(edited["Well"])] if len(ov) else ov
            out = pd.concat([keep, new], ignore_index=True)
            try:
                out.to_csv(os.path.join(data_dir, OVERRIDES_FILE), index=False)
                st.success("Saved. The ranking updates now.")
                st.rerun()
            except OSError as exc:
                st.error(f"Could not save: {exc}")

    with st.expander("Wells not assessed"):
        na = res[res["Recommendation"] == "Not assessed"][["Well", "Status", "Reason", "last_test", "days_since_test"]].rename(columns={"last_test": "Last test", "days_since_test": "Days since test"})
        table(na) if len(na) else st.caption("Every well was assessed.")
    with st.expander("How the dynamometer values were found"):
        src = res[res["Status"] == "RUNNING"][["Well", "goodman_pct", "beam_pct", "gbx_pct", "Data gaps", "dyno_sources", "dyno_text"]].rename(
            columns={"goodman_pct": "Rod loaded (%)", "beam_pct": "Beam loading (%)", "gbx_pct": "Gearbox torque (%)", "Data gaps": "Missing (score filled)", "dyno_sources": "Source", "dyno_text": "Master text read"})
        table(src)
        st.caption("Order of preference: manual entry, numbers read from the master 'DYNAMOMETER TEST RESULTS' text, imported Echometer report. "
                   "If a value was read wrongly from the text, correct it above.")



def render_selection_analytics(matrix_df: pd.DataFrame, ui: dict):
    """Render advanced charts and visualizations to support artificial lift selection."""
    ui["section"]("Artificial Lift Selection Analytics & Decision Support")
    
    if matrix_df.empty:
        st.info("No matrix data available for analytics.")
        return

    # 1. High-level Metrics
    col1, col2, col3, col4 = st.columns(4)
    total_wells = len(matrix_df)
    vsd_count = len(matrix_df[matrix_df["Recommendation"] == "VSD"])
    ropc_count = len(matrix_df[matrix_df["Recommendation"] == "ROPC"])
    pt_count = len(matrix_df[matrix_df["Recommendation"] == "PT sensor"])
    
    with col1:
        st.markdown(ui["kpi"]("Assessed Wells", total_wells, "Active running wells", tone="neutral"), unsafe_allow_html=True)
    with col2:
        st.markdown(ui["kpi"]("VSD Candidates", vsd_count, f"{vsd_count/total_wells:.1%}" if total_wells else "0%", tone="bad"), unsafe_allow_html=True)
    with col3:
        st.markdown(ui["kpi"]("ROPC Candidates", ropc_count, f"{ropc_count/total_wells:.1%}" if total_wells else "0%", tone="warn"), unsafe_allow_html=True)
    with col4:
        st.markdown(ui["kpi"]("PT Sensor Candidates", pt_count, f"{pt_count/total_wells:.1%}" if total_wells else "0%", tone="good"), unsafe_allow_html=True)

    st.markdown("<br>", unsafe_allow_html=True)

    # 2. Scatter Plot: Net Oil vs Total Score color-coded by Recommendation
    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Priority Score vs. Net Oil Rate**")
        fig_scatter = go.Figure()
        for tech in TECH_ORDER:
            sub = matrix_df[matrix_df["Recommendation"] == tech]
            if not sub.empty:
                fig_scatter.add_trace(go.Scatter(
                    x=sub["net_oil"],
                    y=sub["Total score"],
                    mode="markers",
                    name=EMOJI.get(tech, tech),
                    marker=dict(size=10, color=COLORS.get(tech, "#9AA0A6")),
                    text=sub["Well"],
                    hovertemplate="<b>%{text}</b><br>Net Oil: %{x:.1f} m³/d<br>Total Score: %{y:.1f}<extra></extra>"
                ))
        fig_scatter.update_xaxes(title="Net Oil Rate (m³/d)")
        fig_scatter.update_yaxes(title="Total Priority Score")
        ui["plot"](fig_scatter, key="analytics_score_vs_oil")

    # 3. Technology Breakdown Donut Chart
    with col_b:
        st.markdown("**Recommendation Share**")
        counts = matrix_df["Recommendation"].value_counts()
        fig_pie = go.Figure(go.Pie(
            labels=[EMOJI.get(t, t) for t in counts.index],
            values=counts.values,
            hole=0.4,
            marker=dict(colors=[COLORS.get(t, "#9AA0A6") for t in counts.index])
        ))
        fig_pie.update_layout(margin=dict(l=20, r=20, t=20, b=20))
        ui["plot"](fig_pie, key="analytics_tech_share")

    st.markdown("---")

    # 4. Engineering Parameters Distribution Box Plots
    st.markdown("**Engineering Parameters Distribution by Recommendation Tier**")
    param_choice = st.selectbox(
        "Select Parameter to Compare",
        options=[("rod", "Rod Loaded (%)"), ("beam", "Beam Loading (%)"), ("gbx", "Gearbox Torque (%)"), ("nlap", "NLAP (m)"), ("gross", "Gross Rate (m³/d)"), ("net_oil", "Net Oil (m³/d)")],
        format_func=lambda x: x[1],
        key="analytics_box_param"
    )[0]

    fig_box = go.Figure()
    for tech in TECH_ORDER:
        sub = matrix_df[matrix_df["Recommendation"] == tech]
        if not sub.empty and param_choice in sub.columns:
            fig_box.add_trace(go.Box(
                y=sub[param_choice],
                name=EMOJI.get(tech, tech),
                marker_color=COLORS.get(tech, "#9AA0A6")
            ))
    fig_box.update_yaxes(title=param_choice.replace("_", " ").title())
    ui["plot"](fig_box, key=f"analytics_box_{param_choice}")
