"""
Petrobel Digitalization dashboard (stand-alone, complete)
---------------------------------------------------------
The "Digitalization" section of the Petrobel Well Operations Dashboard as a project of its own, together with the data pipeline that
builds its data from the Excel files and an "Update data" tab to upload new versions of them.

Run with:   START_DASHBOARD.bat   (or: streamlit run app.py   from this folder)
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from html import escape

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dash_core as core
from dash_core import DAYFIRST, NOT_A_WELL, WELL_COL, resolve_columns, run_status_map, status_limit
from digital_priority import render_digitalization
from runtime_history import load_live_runtime_history, merge_live_runtime

# ──────────────────────────────────────────────────────────────────────────────
# Settings
# ──────────────────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
START_DIR = os.path.join(PROJECT_DIR, "00_START_HERE")
DATA_DIR = os.environ.get("DASHBOARD_DATA_DIR") or os.path.join(PROJECT_DIR, "03_DATA", "PROCESSED")
CSV_PATH = os.path.join(DATA_DIR, "Master_Dashboard_Database.csv")
STATUS_JSON = os.path.join(DATA_DIR, "data_status.json")
ALLOW_UPLOAD = True       # show the "Update data" tab (upload the Excel files inside the dashboard)

# Colour palette
RED, RED_DARK = "#CE1141", "#9C0D31"          # Weatherford red
_BASE = st.get_option("theme.base")            # "light", "dark" or None (= follows the browser setting)
DARK = _BASE == "dark"
if DARK:    # dark page -> WHITE text on charcoal
    INK, WHITE, GREY, LIGHT, SOFT = "#FFFFFF", "#1A1A1A", "#B9BABD", "#3E3E43", "#2C2C30"
else:       # white page -> BLACK text
    INK, WHITE, GREY, LIGHT, SOFT = "#1A1A1A", "#FFFFFF", "#66686C", "#E4E4E6", "#F4F4F5"
if _BASE is None:   # no theme configured yet: create the light one for the next start
    try:
        _cfg = os.path.join(SCRIPT_DIR, ".streamlit", "config.toml")
        if not os.path.exists(_cfg):
            os.makedirs(os.path.dirname(_cfg), exist_ok=True)
            with open(_cfg, "w", encoding="utf-8") as _fh:
                _fh.write('[theme]\nbase = "light"\nprimaryColor = "#CE1141"\nbackgroundColor = "#FFFFFF"\n'
                          'secondaryBackgroundColor = "#F4F4F5"\ntextColor = "#1A1A1A"\nfont = "sans serif"\n')
    except OSError:
        pass
GOOD, WARN, BAD, MUTED = "#2E7D4F", "#D9930D", RED, GREY   # status signals: green/amber only mark running/warning, red = brand red
STATUS_COLORS = {"RUNNING": GOOD, "STOPPED": BAD, "NO DATA": MUTED}

st.set_page_config(
    page_title="Petrobel Digitalization",
    page_icon="🛢️",
    layout="wide",
    initial_sidebar_state="expanded",
)

_VER = tuple(int(p) for p in re.findall(r"\d+", st.__version__)[:2])
STRETCH = {"width": "stretch"} if _VER >= (1, 50) else {"use_container_width": True}

# ──────────────────────────────────────────────────────────────────────────────
# Styling
# ──────────────────────────────────────────────────────────────────────────────
_LIGHT_VARS = """--bg:#FFFFFF;--surface:#FFFFFF;--soft:#F4F4F5;--line:#E4E4E6;--text:#1A1A1A;--muted:#66686C;
  --good-text:#2E7D4F;--bad-text:#CE1141;--callout-bg:#FBEAEE;--callout-k:#9C0D31;--tab-sel-bg:#FBEAEE;--tab-sel-color:#CE1141;
  --shadow:0 2px 10px rgba(26,26,26,.07);"""
_DARK_VARS = """--bg:#1A1A1A;--surface:#242427;--soft:#2C2C30;--line:#3E3E43;--text:#FFFFFF;--muted:#B9BABD;
  --good-text:#6FD69A;--bad-text:#FF7A9A;--callout-bg:rgba(206,17,65,.20);--callout-k:#FFB3C4;--tab-sel-bg:rgba(206,17,65,.22);--tab-sel-color:#FFFFFF;
  --shadow:0 2px 10px rgba(0,0,0,.35);"""
_css_vars = (":root{" + _DARK_VARS + "}") if DARK else (":root{" + _LIGHT_VARS + "}" + (
    "@media (prefers-color-scheme: dark){:root{" + _DARK_VARS + "}}" if _BASE is None else ""))

CSS = """
<style>
/*VARS*/
:root{--red:#CE1141;--red-dark:#9C0D31;--good:#2E7D4F;--warn:#D9930D;--bad:#CE1141;}
.stApp{background:var(--bg);color:var(--text);}
header[data-testid="stHeader"]{background:transparent;}
#MainMenu,footer{visibility:hidden;}
.block-container{padding-top:1.2rem;padding-bottom:2rem;max-width:1500px;}
section[data-testid="stSidebar"]{background:var(--soft);border-right:1px solid var(--line);}
h1,h2,h3,h4{font-family:'Segoe UI',Inter,system-ui,sans-serif;color:var(--text);letter-spacing:.2px;}

.brand{display:flex;align-items:center;gap:10px;padding:6px 0 14px 0;}
.brand .logo{width:38px;height:38px;border-radius:10px;display:grid;place-items:center;font-size:20px;background:var(--red);box-shadow:0 4px 12px rgba(206,17,65,.30);}
.brand .name{font-weight:700;font-size:16px;line-height:1.1;color:var(--text);}
.brand .tag{font-size:11px;color:var(--muted);letter-spacing:.8px;text-transform:uppercase;}

.hero{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;
  background:linear-gradient(120deg,#1A1A1A 0%,#26262A 65%,#34343A 100%);border-bottom:4px solid var(--red);
  border-radius:14px;padding:20px 26px;margin-bottom:14px;box-shadow:0 10px 26px rgba(26,26,26,.20);}
.hero .eyebrow{font-size:11px;letter-spacing:1.6px;color:#E6E6E8;font-weight:600;text-transform:uppercase;}
.hero .eyebrow:before{content:"";display:inline-block;width:10px;height:10px;background:var(--red);margin-right:8px;transform:rotate(45deg);}
.hero h1{margin:2px 0 2px 0;font-size:30px;font-weight:700;color:#FFFFFF;}
.hero .sub{color:#C4C5C8;font-size:13px;}
.hero-right{text-align:right;}
.hero-right .asof{color:#C4C5C8;font-size:12px;margin-top:8px;line-height:1.5;}

.logos{display:flex;gap:10px;align-items:center;justify-content:flex-end;margin-bottom:10px;}
.logo-chip{background:#FFFFFF;border-radius:10px;padding:6px 12px;display:flex;align-items:center;border:1px solid #E4E4E6;}

.pill{display:inline-block;padding:6px 14px;border-radius:999px;font-weight:700;font-size:13px;letter-spacing:.6px;border:1px solid;}
.pill.good{color:#A8E6C0;background:rgba(46,125,79,.30);border-color:rgba(168,230,192,.55);}
.pill.bad{color:#FFC2D0;background:rgba(206,17,65,.35);border-color:rgba(255,194,208,.6);}
.pill.neutral{color:#E0E1E3;background:rgba(116,118,122,.35);border-color:rgba(224,225,227,.45);}

.kpi{background:var(--surface);border:1px solid var(--line);border-left:4px solid var(--red);border-radius:12px;
  padding:14px 16px;height:100%;min-height:112px;box-shadow:var(--shadow);}
.kpi-label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:700;}
.kpi-value{font-size:26px;font-weight:700;color:var(--text);margin-top:4px;line-height:1.15;word-break:break-word;}
.kpi-value.sm{font-size:15px;line-height:1.35;font-weight:600;color:var(--text);}
.kpi-delta{font-size:12px;margin-top:4px;font-weight:700;}
.kpi-delta.good{color:var(--good-text);} .kpi-delta.bad{color:var(--bad-text);} .kpi-delta.flat{color:var(--muted);}
.kpi-sub{font-size:11px;color:var(--muted);margin-top:2px;}
.kpi.good{border-left-color:var(--good);} .kpi.bad{border-left-color:var(--red);} .kpi.warn{border-left-color:var(--warn);}

.section-title{font-size:13px;font-weight:800;letter-spacing:1.2px;text-transform:uppercase;color:var(--text);
  margin:18px 0 8px 2px;display:flex;align-items:center;gap:8px;}
.section-title:before{content:"";width:4px;height:15px;border-radius:2px;background:var(--red);display:inline-block;}

.info-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;}
.info-item{background:var(--soft);border:1px solid var(--line);border-radius:12px;padding:12px 14px;}
.info-item .k{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;font-weight:600;}
.info-item .v{font-size:15px;font-weight:700;margin-top:3px;color:var(--text);word-break:break-word;}

.flag{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border-radius:10px;margin-bottom:8px;
  font-size:13px;border:1px solid var(--line);background:var(--surface);color:var(--text);}
.flag.bad{border-left:4px solid var(--red);} .flag.warn{border-left:4px solid var(--warn);}
.flag.good{border-left:4px solid var(--good);} .flag.info{border-left:4px solid #74767A;}

.callout{background:var(--callout-bg);border:1px solid rgba(206,17,65,.30);border-radius:12px;padding:14px 16px;}
.callout .k{font-size:11px;color:var(--callout-k);text-transform:uppercase;letter-spacing:1px;font-weight:800;}
.callout .v{font-size:14px;margin-top:6px;line-height:1.5;white-space:pre-wrap;color:var(--text);}

.stTabs [data-baseweb="tab-list"]{gap:4px;border-bottom:1px solid var(--line);}
.stTabs [data-baseweb="tab"]{height:42px;padding:0 16px;border-radius:10px 10px 0 0;color:var(--muted);font-weight:700;}
.stTabs [aria-selected="true"]{color:var(--tab-sel-color)!important;background:var(--tab-sel-bg);}
.stTabs [data-baseweb="tab-highlight"]{background-color:var(--red)!important;}
.stButton>button,.stDownloadButton>button{border-radius:8px;font-weight:600;border:1px solid var(--line);}
.stButton>button:hover,.stDownloadButton>button:hover{border-color:var(--red);color:var(--red);}
div[data-testid="stVerticalBlockBorderWrapper"]{border-color:var(--line)!important;border-radius:12px;}
div[data-testid="stSidebar"] div[data-testid="stPopover"]>button{width:100%;border-radius:10px;min-height:42px;font-weight:800;background:#ce1141;color:#fff;border-color:#ce1141;}
div[data-testid="stSidebar"] div[data-testid="stPopoverBody"]{width:min(280px,calc(100vw - 32px))!important;min-width:0!important;max-width:calc(100vw - 32px)!important;max-height:min(52vh,480px);overflow-y:auto;}
</style>
"""
st.markdown(CSS.replace("/*VARS*/", _css_vars), unsafe_allow_html=True)


def logo_uri(stem: str):
    """Data-URI of assets/<stem>.png|jpg|svg|webp next to app.py (None if the file is missing)."""
    for ext in ("png", "jpg", "jpeg", "svg", "webp"):
        p = os.path.join(SCRIPT_DIR, "assets", f"{stem}.{ext}")
        if os.path.exists(p):
            mime = "image/svg+xml" if ext == "svg" else f"image/{'jpeg' if ext in ('jpg', 'jpeg') else ext}"
            with open(p, "rb") as fh:
                return f"data:{mime};base64," + base64.b64encode(fh.read()).decode()
    return None


def logos_html(height: int = 44) -> str:
    chips = []
    for stem, label in (("petrobel", "PETROBEL"), ("weatherford", "WEATHERFORD")):
        uri = logo_uri(stem)
        inner = (f'<img src="{uri}" style="height:{height}px;display:block" alt="{label}">' if uri else
                 f'<span style="font-weight:800;letter-spacing:1px;color:#1A1A1A;font-size:13px">{label}</span>')
        chips.append(f'<div class="logo-chip" style="height:{height + 16}px">{inner}</div>')
    return '<div class="logos">' + "".join(chips) + "</div>"


def fnum(v, d=1, unit="") -> str:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return ftext(v)
    return "—" if np.isnan(x) else f"{x:,.{d}f}{unit}"


def ftext(v) -> str:
    if v is None or (not isinstance(v, (list, tuple, np.ndarray)) and pd.isna(v)):
        return "—"
    s = str(v).strip()
    return s if s and s.lower() != "nan" else "—"


def fdate(ts) -> str:
    return "—" if ts is None or pd.isna(ts) else pd.Timestamp(ts).strftime("%d %b %Y")


def kpi(label, value, sub="", delta="", tone="", small=False) -> str:
    cls = "kpi-value sm" if small else "kpi-value"
    return (
        f'<div class="kpi {tone}"><div class="kpi-label">{escape(label)}</div>'
        f'<div class="{cls}">{escape(str(value))}</div>{delta}'
        f'<div class="kpi-sub">{escape(sub)}</div></div>'
    )


def section(title: str):
    st.markdown(f'<div class="section-title">{escape(title)}</div>', unsafe_allow_html=True)


def style_fig(fig, height=360, legend=True):
    fig.update_layout(
        template="plotly_dark" if DARK else "plotly_white",
        height=height,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=30, b=10),
        font=dict(family="Segoe UI, Inter, sans-serif", size=12, color=INK),
        hovermode="x unified",
        showlegend=legend,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, bgcolor="rgba(0,0,0,0)"),
    )
    fig.update_xaxes(gridcolor=LIGHT, zeroline=False)
    fig.update_yaxes(gridcolor=LIGHT, zeroline=False)
    return fig


def plot(fig, key):
    st.plotly_chart(fig, key=key, theme=None, **STRETCH)


def table(data, **kwargs):
    st.dataframe(data, hide_index=True, **STRETCH, **kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# Upload password (optional): put a password in 00_START_HERE/Upload_Password.txt to protect the Update data tab
# ──────────────────────────────────────────────────────────────────────────────
UPLOAD_PASSWORD_FILE = os.path.join(START_DIR, "Upload_Password.txt")


def upload_password():
    """Read the upload-only password from the editable one-line file in 00_START_HERE."""
    try:
        with open(UPLOAD_PASSWORD_FILE, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def password_ok(entered: str, stored: str) -> bool:
    """Accept a plain password; retain compatibility with existing PBKDF2 hashes."""
    stored = stored.strip()
    if stored.startswith("pbkdf2:"):
        try:
            _, iterations, salt, digest = stored.split(":")
            test = hashlib.pbkdf2_hmac("sha256", entered.encode("utf-8"), bytes.fromhex(salt), int(iterations)).hex()
            return hmac.compare_digest(test, digest.lower())
        except ValueError:
            return False
    return hmac.compare_digest(entered.encode("utf-8"), stored.encode("utf-8"))


PROTECT_UPLOAD = bool(upload_password())


def norm(s) -> str:
    """Normalise a column name: collapse whitespace/newlines, upper-case."""
    return core.norm(s)


# Uploaded files are identified by their CONTENT (headers), with the file name as a second clue.
UPLOAD_KINDS = {
    "SRP_TESTS": ("SRP Test Results", "Production Test Results / Water Cut Samples (SRP)", {"srp", "production", "test", "results", "water", "cut", "samples"}),
    "DATABASE": ("DATABASE", "DATABASE (well tests)", {"database", "db", "tests", "test"}),
    "Monthly": ("Monthly", "Monthly", {"monthly", "month", "report"}),
    "DFL": ("Petrobel Cumulative DFL", "Cumulative DFL", {"dfl", "cumulative", "fluid", "level"}),
    "OPMODE": ("Petrobel Wells Operation Mode", "Wells Operation Mode", {"operation", "mode", "operations", "modes"}),
}
_WELL_HEADERS = {"WELL NO.", "WELL NO", "WELL NAME", "WELL"}


@st.cache_data(show_spinner=False)
def sniff_excel(name: str, data: bytes) -> dict:
    """Header facts of the first sheet: which well-name header, which header texts, how many date-like headers."""
    try:
        head = pd.read_excel(io.BytesIO(data), header=None, nrows=60)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}
    for r in range(len(head)):
        row = head.iloc[r]
        well_hdr = next((norm(v) for v in row if isinstance(v, str) and norm(v) in _WELL_HEADERS), None)
        if well_hdr:
            texts = {norm(v) for v in row if isinstance(v, str)}
            dates = sum(1 for v in row if isinstance(v, (pd.Timestamp, datetime)) or (isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}", v.strip())))
            return {"well_header": well_hdr, "texts": texts, "dates": dates, "header_row": r}
    return {"well_header": "", "texts": set(), "dates": 0, "header_row": None}


try:                                  # shared recognition of gross / net rate headers (BFPD, BOPD, M3L/day, Total fluid ...)
    import well_test_units as _wtu
except Exception:  # noqa: BLE001
    _wtu = None


def upload_scores(name: str, info: dict) -> dict:
    import difflib

    toks = set(re.sub(r"[^a-z0-9]+", " ", os.path.splitext(name)[0].lower()).split())
    stem = " ".join(sorted(toks))
    texts, wh = info.get("texts", set()), info.get("well_header", "")
    content = dict.fromkeys(UPLOAD_KINDS, 0.0)
    compact = {re.sub(r"[^A-Z0-9]+", "", t) for t in texts}
    if ({"WELL", "ACTM3D", "ACTNET"}.issubset(compact) and ({"ACTWC"} & compact or {"ACTWCPCT"} & compact)) or (_wtu is not None and _wtu.looks_like_srp(compact)):
        content["SRP_TESTS"] = 1.0
    if wh.startswith("WELL NO"):
        content["DFL"] = 0.8
    if any(k in t for t in texts for k in ("LIQUID LEVEL", "BHP", "PUMP DEPTH", "NLAP", "NET LIQUID")):
        content["DFL"] = max(content["DFL"], 0.9) if wh.startswith("WELL NO") else max(content["DFL"], 0.5)
    if any("DATE OF TEST" in t or "NET OIL" in t or t.startswith("W.C") for t in texts):
        content["DATABASE"] = 0.9
    if info.get("dates", 0) >= 5:
        content["Monthly"] = 0.9
    if wh and info.get("dates", 0) < 5 and max(content.values()) == 0:
        content["OPMODE"] = 0.6
    out = {}
    for k, (canon, _, kw) in UPLOAD_KINDS.items():
        ns_ = 1.0 if toks & kw else difflib.SequenceMatcher(None, stem, canon.lower()).ratio() * 0.8
        out[k] = 0.65 * content[k] + 0.35 * ns_
    return out


def assign_uploads(files: list) -> list:
    """files = [(name, sniff_info)] -> [(kind or None, confidence)], each kind used at most once."""
    scores = [upload_scores(n, i) for n, i in files]
    pairs = sorted(((s[k], idx, k) for idx, s in enumerate(scores) for k in s), reverse=True)
    out, used_files, used_kinds = [(None, 0.0)] * len(files), set(), set()
    for sc, idx, k in pairs:
        if sc >= 0.3 and idx not in used_files and k not in used_kinds:
            out[idx] = (k, sc)
            used_files.add(idx)
            used_kinds.add(k)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Data layer (cached)
# ──────────────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner="Loading master database…")
def load_csv(path: str, mtime: float) -> pd.DataFrame:
    return pd.read_csv(path, low_memory=False)


detect_date_columns = st.cache_data(show_spinner=False)(core.detect_date_columns)
build_hours_matrix = st.cache_data(show_spinner=False)(core.build_hours_matrix)
build_tests = st.cache_data(show_spinner=False)(core.build_tests)
build_dfl = st.cache_data(show_spinner=False)(core.build_dfl)


def load_history(name: str):
    """Full-history CSV written by the merge step (Test_History.csv / DFL_History.csv); None when it is not there."""
    p = os.path.join(DATA_DIR, name)
    if not os.path.exists(p):
        return None
    h = load_csv(p, os.path.getmtime(p))
    if WELL_COL not in h.columns:
        return None
    h = h.dropna(subset=[WELL_COL]).copy()
    h[WELL_COL] = h[WELL_COL].astype(str).str.strip()
    bad = h[WELL_COL].str.upper().str.contains("REVIEW_NEEDED", na=False) | h[WELL_COL].str.upper().isin(NOT_A_WELL)
    return h[~bad].reset_index(drop=True)


def read_status() -> dict:
    try:
        with open(STATUS_JSON, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# ──────────────────────────────────────────────────────────────────────────────
# Load data
# ──────────────────────────────────────────────────────────────────────────────
data_status = read_status()
st.sidebar.markdown(logos_html(30), unsafe_allow_html=True)
st.sidebar.markdown(
    '<div class="brand"><div class="logo">🛢️</div><div><div class="name">Petrobel</div>'
    '<div class="tag">Digitalization</div></div></div>',
    unsafe_allow_html=True,
)

if not os.path.exists(CSV_PATH):
    st.sidebar.error("The master database has not been built yet.")
    st.info("There is no data yet. Put the Excel files in 03_DATA/SOURCE_FILES and run Update_Data_From_Folder.bat, or upload them in the "
            "**Update data** tab below.")
    (tab_dig, tab_update) = st.tabs(["🛰️ Digitalization", "🔄 Update data"])
    with tab_dig:
        st.caption("Waiting for data.")
    exec_update_only = True
else:
    exec_update_only = False

if not exec_update_only:
    mtime = os.path.getmtime(CSV_PATH)
    raw = load_csv(CSV_PATH, mtime)
    data_key = f"{mtime}-{len(raw)}"
    if WELL_COL not in raw.columns:
        st.error(f"Column '{WELL_COL}' was not found in the master database.")
        st.stop()

    raw = raw.dropna(subset=[WELL_COL]).copy()
    raw[WELL_COL] = raw[WELL_COL].astype(str).str.strip()
    review_mask = raw[WELL_COL].str.upper().str.contains("REVIEW_NEEDED", na=False) | raw[WELL_COL].str.upper().isin(NOT_A_WELL)
    df = raw[~review_mask].reset_index(drop=True)
    if df.empty:
        st.error("No valid wells found after removing REVIEW_NEEDED / UNKNOWN rows.")
        st.stop()

    M = resolve_columns(df.columns)
    date_cols = tuple(detect_date_columns(tuple(df.columns)))
    hours_all = build_hours_matrix(df, data_key, date_cols)

    test_src, dfl_src = load_history("Test_History.csv"), load_history("DFL_History.csv")
    test_src = df if test_src is None else test_src
    dfl_src = df if dfl_src is None else dfl_src
    M_test, M_dfl = resolve_columns(test_src.columns), resolve_columns(dfl_src.columns)
    if core.DFL_DATE_COLUMN in dfl_src.columns:
        M_dfl["dfl_date"] = core.DFL_DATE_COLUMN
    if core.NLAP_COLUMN in dfl_src.columns:
        M_dfl["nlap"] = core.NLAP_COLUMN
    if M_dfl.get("dfl_date") is None:
        M_dfl["dfl_date"] = core.auto_date_col(dfl_src, exclude=() if dfl_src is not df else {M_dfl.get("test_date")})
    tests_all = build_tests(test_src, data_key + f"-t{len(test_src)}", tuple((k, v) for k, v in M_test.items() if v))
    dfl_all = build_dfl(dfl_src, data_key + f"-d{len(dfl_src)}", tuple((k, v) for k, v in M_dfl.items() if v))

    # saved daily running hours from the live API (only if such a file exists) are overlaid on the workbook hours
    hours_all = merge_live_runtime(hours_all, load_live_runtime_history(DATA_DIR))
    status_map = run_status_map(hours_all)
    STATUS_DOT = {"RUNNING": "🟢", "STOPPED": "🔴", "NO DATA": "⚪"}

    # ──────────────────────────────────────────────────────────────────────────
    # Sidebar controls
    # ──────────────────────────────────────────────────────────────────────────
    mask = pd.Series(True, index=df.index)
    with st.sidebar.expander("🔎 Filters", expanded=False):
        for label, key in [("Location", "location"), ("Zone", "zone"), ("Art. lift type", "lift"), ("Vendor", "vendor")]:
            col = M.get(key)
            if col:
                opts = sorted(df[col].dropna().astype(str).str.strip().unique())
                chosen = st.multiselect(label, opts, key=f"flt_{key}")
                if chosen:
                    mask &= df[col].astype(str).str.strip().isin(chosen)

    scope_wells = sorted(df.loc[mask, WELL_COL].unique())
    status_counts = pd.Series([status_map.get(w, "NO DATA") for w in scope_wells], dtype="object").value_counts()
    run_filter = st.sidebar.radio(
        "Motor Status",
        ["All", "RUNNING", "STOPPED", "NO DATA"],
        horizontal=True,
        key="flt_run_status",
        format_func=lambda o: f"All ({len(scope_wells)})" if o == "All" else f"{o.title()} ({int(status_counts.get(o, 0))})",
    )
    if run_filter != "All":
        mask &= df[WELL_COL].map(lambda w: status_map.get(w, "NO DATA")) == run_filter

    wells = sorted(df.loc[mask, WELL_COL].unique())
    if not wells:
        st.warning("No wells match the selected filters.")
        st.stop()

    selected = st.sidebar.selectbox(
        f"Well ({len(wells)})", wells, key="well",
        format_func=lambda w: f"{STATUS_DOT[status_map.get(w, 'NO DATA')]} {w}",
    )

    if st.sidebar.button("↻ Refresh data"):
        st.cache_data.clear()
        st.rerun()

    if data_status.get("updated_at"):
        try:
            st.sidebar.caption(f"Data updated: {datetime.fromisoformat(data_status['updated_at']):%d %b %Y, %H:%M} · {data_status.get('wells', '?')} wells")
        except ValueError:
            pass

    # ──────────────────────────────────────────────────────────────────────────
    # Selected well and hero banner
    # ──────────────────────────────────────────────────────────────────────────
    well_df = df[df[WELL_COL] == selected]
    info = well_df.ffill().iloc[-1]

    def val(key):
        col = M.get(key)
        return info[col] if col else np.nan

    status = status_map.get(selected, "NO DATA")
    status_cls = {"RUNNING": "good", "STOPPED": "bad"}.get(status, "neutral")
    rt = hours_all.loc[selected].dropna() if (not hours_all.empty and selected in hours_all.index) else pd.Series(dtype=float)
    rt = rt[rt.index <= status_limit()]
    last_reading = rt.index[-1] if len(rt) else None
    wt = tests_all[tests_all["Well"] == selected].sort_values("Date")
    wd = dfl_all[dfl_all["Well"] == selected].sort_values("Date")
    last_test = wt["Date"].iloc[-1] if len(wt) else None
    last_dfl = wd["Date"].iloc[-1] if len(wd) else None

    sub_parts = [ftext(val(k)) for k in ("location", "zone", "lift")]
    sub_line = " · ".join(p for p in sub_parts if p != "—") or "Well operations overview"
    st.markdown(
        f"""
<div class="hero">
  <div>
    <div class="eyebrow">Petrobel · Digitalization</div>
    <h1>{escape(selected)}</h1>
    <div class="sub">{escape(sub_line)}</div>
  </div>
  <div class="hero-right">
    {logos_html()}
    <span class="pill {status_cls}">● {status}</span>
    <div class="asof">Last running-hours reading: {fdate(last_reading)}<br>Last test: {fdate(last_test)} · Last DFL: {fdate(last_dfl)}</div>
  </div>
</div>
""",
        unsafe_allow_html=True,
    )

    _flash = st.session_state.pop("flash", None)
    if _flash:
        (st.success if _flash[0] else st.error)(_flash[1])

    (tab_dig, tab_update) = st.tabs(["🛰️ Digitalization", "🔄 Update data"])
    with tab_dig:
        render_digitalization(
            df, tests_all, dfl_all, status_map, WELL_COL, M, selected,
            ui=dict(kpi=kpi, section=section, plot=plot, table=table, style_fig=style_fig, STRETCH=STRETCH),
            data_dir=DATA_DIR,
        )

# ══════════════════════════════ UPDATE DATA ═══════════════════════════════════
PIPELINE = os.path.join(SCRIPT_DIR, "run_pipeline.py")
DROP_DIR = os.path.join(PROJECT_DIR, "02_UPLOAD_NEW_DATA")
STATUS_FILE = os.path.join(START_DIR, "Last_Update_Status.txt")
with tab_update:
    section("Update the dashboard data")
    _team_pw = upload_password() if PROTECT_UPLOAD else None
    if not ALLOW_UPLOAD or not os.path.exists(PIPELINE) or os.environ.get("DASHBOARD_DATA_DIR"):
        st.info("Uploading is available in the hosted dashboard (the one that has run_pipeline.py next to it). "
                "You can also drop the Excel files into the 02_UPLOAD_NEW_DATA folder.")
    elif PROTECT_UPLOAD and not _team_pw:
        st.error("Upload password is missing. Set a password in 00_START_HERE/Upload_Password.txt, then reload this page.")
    elif PROTECT_UPLOAD and not st.session_state.get("upd_ok"):
        st.caption("Updating the data changes the dashboard for everyone, so it needs the upload password.")
        with st.form("upd_form"):
            _pw = st.text_input("Upload password", type="password")
            if st.form_submit_button("Unlock"):
                if password_ok(_pw, _team_pw):
                    st.session_state["upd_ok"] = True
                    st.rerun()
                else:
                    st.error("Incorrect password.")
    else:
        st.caption("Upload the Excel files that changed, with any file names and in any order. Each file is identified from its "
                   "contents (and its name); check the result below and correct it if needed. Files you do not upload keep their previous version.")
        files = st.file_uploader("Excel files", type=["xlsx", "xlsm"], accept_multiple_files=True, key="up_many")
        kinds = list(UPLOAD_KINDS)
        picks, problems = {}, []
        if files:
            sniffs = [sniff_excel(f.name, f.getvalue()) for f in files]
            auto = assign_uploads([(f.name, s) for f, s in zip(files, sniffs)])
            h = st.columns([3, 3, 1])
            h[0].markdown("**File**"); h[1].markdown("**Identified as**"); h[2].markdown("**Match**")
            for n, (f, s, (kind, conf)) in enumerate(zip(files, sniffs, auto)):
                c = st.columns([3, 3, 1])
                c[0].write(f"📄 {f.name}")
                options = kinds + ["IGNORE"]
                pick = c[1].selectbox("Identified as", options, index=options.index(kind) if kind else len(options) - 1,
                                      key=f"pick_{n}_{f.name}", label_visibility="collapsed",
                                      format_func=lambda k_: "Ignore this file" if k_ == "IGNORE" else UPLOAD_KINDS[k_][1])
                c[2].caption(f"{conf:.0%}" if kind else ("unreadable" if s.get("error") else "unknown"))
                picks[n] = pick
            chosen = [k_ for k_ in picks.values() if k_ != "IGNORE"]
            dup = sorted({UPLOAD_KINDS[k_][1] for k_ in chosen if chosen.count(k_) > 1})
            if dup:
                problems.append("More than one file is set to: " + ", ".join(dup) + ". Change one of them or ignore it.")
            missing = [UPLOAD_KINDS[k_][1] for k_ in kinds if k_ not in chosen]
            if missing and chosen:
                st.info("Not uploaded (previous version kept): " + ", ".join(missing))
            for p_ in problems:
                st.error(p_)
            if not chosen:
                problems.append("No file selected.")
        if st.button("Save files and update dashboard", type="primary", disabled=(not files) or bool(problems)):
            os.makedirs(DROP_DIR, exist_ok=True)
            for n, f in enumerate(files):
                if picks[n] != "IGNORE":
                    with open(os.path.join(DROP_DIR, UPLOAD_KINDS[picks[n]][0] + (os.path.splitext(f.name)[1].lower() or ".xlsx")), "wb") as fh:
                        fh.write(f.getbuffer())
            with st.spinner("Updating: matching well names and merging the sheets…"):
                proc = subprocess.run([sys.executable, PIPELINE, "--no-wait"], cwd=PROJECT_DIR, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", timeout=900)
            st.cache_data.clear()
            status_txt = ""
            try:
                with open(STATUS_FILE, encoding="utf-8") as fh:
                    status_txt = fh.read()
            except OSError:
                pass
            st.session_state["update_log"] = (proc.stdout + proc.stderr)[-6000:]
            st.session_state["flash"] = (proc.returncode == 0, "Data updated from the uploaded files." if proc.returncode == 0
                                         else "The update failed, so the previous data is still shown. See Update data > log.")
            st.session_state["update_status"] = status_txt
            st.rerun()
        # The pipeline can update status outside this Streamlit session; don't
        # keep showing the upload-time snapshot after a file lock is resolved.
        try:
            with open(STATUS_FILE, encoding="utf-8") as fh:
                st.session_state["update_status"] = fh.read()
        except OSError:
            pass
        if st.session_state.get("update_status"):
            st.code(st.session_state["update_status"], language=None)
        if st.session_state.get("update_log"):
            with st.expander("Update log"):
                st.code(st.session_state["update_log"], language=None)

        with st.expander("Remove SRP well tests uploaded by mistake"):
            try:
                import srp_cleanup
            except Exception as exc_:  # noqa: BLE001
                st.caption(f"srp_cleanup.py was not found in the app folder ({exc_}).")
            else:
                st.caption("Takes back the well tests that an SRP sheet added on one upload date, and the uploaded sheet itself, so the next update "
                           "cannot add them again. A backup is kept in 04_ARCHIVE. Then upload the correct SRP sheet as usual.")
                dates_ = srp_cleanup.uploaded_dates(PROJECT_DIR)
                if not dates_:
                    st.caption("No SRP tests were found in the test history.")
                else:
                    pick_ = st.selectbox("Upload date to remove", [f"{d_}  ({n_} well tests)" for d_, n_ in dates_], key="srp_rm_date")
                    sure_ = st.checkbox("Yes, remove these tests and the uploaded SRP sheet", key="srp_rm_sure")
                    if st.button("Remove and rebuild the data", disabled=not sure_, key="srp_rm_go"):
                        with st.spinner("Removing the tests and rebuilding the master table…"):
                            res_ = srp_cleanup.remove_srp_tests(PROJECT_DIR, pick_.split()[0], app_dir=SCRIPT_DIR)
                        st.cache_data.clear()
                        st.session_state["srp_removed"] = res_
                        st.rerun()
                r_ = st.session_state.get("srp_removed")
                if r_:
                    ok_ = r_.get("rebuilt") in (True, None)
                    (st.success if ok_ else st.error)(
                        f"Removed {r_['tests_removed']} well test(s) of {r_['wells']} well(s) dated {r_['date']}"
                        + (f" and the file(s): {', '.join(r_['files_removed'])}" if r_["files_removed"] else "")
                        + (". The data was rebuilt." if r_.get("rebuilt") else ". The rebuild did not finish: " + r_.get("message", ""))
                        + f" Backup: {r_['backup']}")
