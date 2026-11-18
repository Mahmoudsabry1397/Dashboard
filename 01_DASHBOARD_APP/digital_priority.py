def build_priority(inputs: pd.DataFrame, threshold: float = 4.0, vsd_pct: float = 48.0, ropc_pct: float = 45.0, base: str = "matrix",
                   missing: str = "median", max_test_age: int = 0) -> tuple[pd.DataFrame, dict]:
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

    src_cols = {"net_oil": "net_oil", "gross": "gross", "nlap": "nlap", "rod": "goodman_pct", "beam": "beam_pct", "gbx": "gbx_pct"}
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

    # Base total score from all parameters
    d["Total score"] = d[[f"s_{k}" for k, *_ in PARAMS]].sum(axis=1, min_count=1).where(matrix)

    # High Rod Stress Bonus (+10 points if Rod Loaded % > 90%)
    d["High Rod Stress Bonus"] = 0.0
    high_rod_mask = matrix & (d["goodman_pct"] > 90.0)
    d.loc[high_rod_mask, "High Rod Stress Bonus"] = 10.0
    d.loc[high_rod_mask, "Total score"] += 10.0

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
        stress_note = " (+10 pts Rod Stress Bonus)" if d.loc[idx, "High Rod Stress Bonus"] > 0 else ""
        if i < n_v:
            d.loc[idx, ["Recommendation", "Reason"]] = ["VSD", f"Top {vsd_pct:g}% of priority ranking{stress_note}"]
        elif i < n_v + n_r:
            d.loc[idx, ["Recommendation", "Reason"]] = ["ROPC", f"Next {ropc_pct:g}% of priority ranking{stress_note}"]
        else:
            d.loc[idx, ["Recommendation", "Reason"]] = ["PT sensor", f"Remaining wells of priority ranking{stress_note}"]
    meta.update({"n_vsd": n_v, "n_ropc": n_r, "n_pt_matrix": len(order) - n_v - n_r})
    return d, meta
