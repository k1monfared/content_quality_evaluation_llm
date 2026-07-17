"""Phase C: two dimension analyses, composites, best/worst-case bounds, and the
raw-data dashboard.

Pure recompute over the committed stores. No API calls.

Analysis 1 (internal consistency, descriptive): per rater (the two humans, the
random human, and each judge), whether the flat or the fitted composite better
reconstructs that rater's own direct overall. Analysis 2 (dimension reduction,
the pruning): using the composite type more internally consistent for the judges,
keep the dimension subset whose LLM composite best matches the Monte-Carlo
random-human overall.

Writes into outputs/:
  - internal_consistency.json     analysis 1: per-rater flat vs fitted composite
  - dimension_reduction.json      analysis 2: refined set and before/after match
  - composite_results.json        flat and fitted composites vs the human overall
  - dimension_importance.csv      per-dim descriptive stats plus the analysis-2 drop-one
  - dimension_correlation.csv     inter-dimension collinearity matrix
  - best_worst_bounds.csv         per-judge best/worst/random agreement bounds
  - dashboard_data.json           per-passage detail feeding the dashboard

and builds docs/index.html (the raw-data dashboard).

Usage:
    python scripts/analyze_dimensions.py
"""
import json

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from src import dimensions as dim
from src import montecarlo, normalize, store
from src.config import load_config, resolve_path
from src.rubric import DIMENSIONS


def _judge_dim_table(cfg, versions):
    """Per-item rubric scores for each judge at its best prompt version."""
    ev = store.load(cfg["paths"]["eval_store"])
    ev = ev[ev["ok"] == 1]
    cols = DIMENSIONS + ["overall"]
    out = {}
    for model, version in versions.items():
        sub = ev[(ev["model"] == model) & (ev["prompt_version"] == version)]
        if sub.empty:
            continue
        out[model] = sub.groupby("item_id")[cols].mean()
    return out


def main():
    cfg = load_config()
    results = resolve_path(cfg["paths"]["results_dir"])
    results.mkdir(parents=True, exist_ok=True)
    DIMS = list(DIMENSIONS)
    n_mc = int(cfg["analysis"]["mc_resamples"])
    seed = int(cfg["analysis"]["seed"])

    human = store.load(cfg["paths"]["human_store"])
    human = human[human["ok"] == 1].copy()
    human = normalize.add_reviewer_noise(human, cfg)
    # Every human dimension AND the overall is bias-normalized (per-rater additive
    # bias removed) via build_normalized, so the entire human side of the study
    # lives in normalized space. Judge (LLM) scores stay raw integers throughout.
    wide_by_col, long = dim.build_normalized(human, cfg, DIMS + ["overall"])

    # Judge per-item dimension tables and the Monte-Carlo random-human overall.
    with open(resolve_path(cfg["judge"]["best_versions_file"])) as fh:
        versions = json.load(fh)
    judge_tab = _judge_dim_table(cfg, versions)
    models = [m for m in cfg["judge"]["models"] if m in judge_tab]
    wide_overall = wide_by_col["overall"].rename(
        columns={"overall_a": "score_a", "overall_b": "score_b"})
    items = list(wide_overall["item_id"])
    rh_overall = pd.Series(montecarlo.random_human(wide_overall, n_mc, seed),
                           index=items)
    llm_pool = dim.llm_long(judge_tab, models)

    # ---- Per-rater normalized frames (humans and the random human) ----
    # One frame per rater carrying its normalized dimension scores and its own
    # normalized direct overall, one row per item. human_1 is the lower-persona-id
    # side per item, human_2 the higher. The random human is the Monte-Carlo
    # average of the two humans' normalized scores per item, built with the same
    # seeded draw as the headline random-human overall so every column is
    # consistent with it.
    cols = DIMS + ["overall"]
    h1_frame = _side_frame(wide_by_col, cols, "_a")
    h2_frame = _side_frame(wide_by_col, cols, "_b")
    rh_frame = _random_human_frame(h1_frame, h2_frame, cols, n_mc, seed)

    # ---- Analysis 1: internal consistency per rater (descriptive) ----
    # For each rater, does the flat or the fitted composite of its own dimensions
    # better reconstruct its own direct overall? Reported per rater: the two
    # humans, the random human, and each judge. The humans and the random human
    # use the normalized dimensions; the judges use their raw dimension scores.
    ic_frames = [("human_1", "human", h1_frame),
                 ("human_2", "human", h2_frame),
                 ("random_human", "human_ref", rh_frame)]
    ic_frames += [(m, "judge", judge_tab[m]) for m in models]
    ic_rows = []
    for name, kind, frame in ic_frames:
        ic = dim.internal_consistency(frame, DIMS, "overall")
        ic_rows.append({"rater": name, "kind": kind, **ic})
    internal = {"dims": DIMS, "raters": ic_rows}
    with open(results / "internal_consistency.json", "w", encoding="utf-8") as fh:
        json.dump(internal, fh, indent=2)

    # ---- Analysis 2: dimension reduction (drives the pruning) ----
    # Use the composite type that is more internally consistent for the judges
    # (mean over the four judges of the fitted vs the flat reconstruction of their
    # own overall). It is the fitted weighted composite on this run.
    j_flat = float(np.mean([r["flat_corr"] for r in ic_rows if r["kind"] == "judge"]))
    j_fitted = float(np.mean([r["fitted_corr"] for r in ic_rows if r["kind"] == "judge"]))
    ctype = "fitted" if j_fitted >= j_flat else "flat"
    reduction = dim.select_dimensions(judge_tab, models, DIMS, rh_overall,
                                      ctype, llm_pool)
    PRUNED = reduction["refined_dims"]
    DROPPED = reduction["dropped_dims"]
    with open(results / "dimension_reduction.json", "w", encoding="utf-8") as fh:
        json.dump(reduction, fh, indent=2)

    # ---- composites on the refined set (feed the dashboard and README) ----
    full = dim.fit_weights(long, DIMS)
    pruned = dim.fit_weights(long, PRUNED)
    composite = {
        "full_rubric": {
            "dims": DIMS,
            "flat_corr_with_overall": round(full["flat_corr"], 4),
            "fitted_corr_with_overall": round(full["fitted_corr"], 4),
            "fitted_intercept": round(full["intercept"], 4),
            "fitted_weights": {d: round(w, 4) for d, w in full["weights"].items()},
        },
        "refined_rubric": {
            "dims": PRUNED,
            "dropped": DROPPED,
            "flat_corr_with_overall": round(pruned["flat_corr"], 4),
            "fitted_corr_with_overall": round(pruned["fitted_corr"], 4),
            "fitted_intercept": round(pruned["intercept"], 4),
            "fitted_weights": {d: round(w, 4) for d, w in pruned["weights"].items()},
        },
    }

    # ---- descriptive dimension stats (collinearity, reliability, Part-2 drop-one) ----
    uni = dim.univariate_corr(long, DIMS)
    cmat = dim.correlation_matrix(long, DIMS)
    cmat.round(4).to_csv(results / "dimension_correlation.csv")
    drop_delta = reduction["drop_one_delta"]
    imp_rows = []
    for d in DIMS:
        imp_rows.append({
            "dimension": d,
            "fitted_weight_human_overall": round(full["weights"][d], 4),
            "univariate_corr_overall": round(uni[d], 4),
            "inter_rater_reliability": round(dim.inter_rater(wide_by_col, d), 4),
            "max_collinearity": round(float(cmat[d].drop(d).max()), 4),
            "llm_match_drop_one_delta": drop_delta[d],
            "kept": d in PRUNED,
        })
    pd.DataFrame(imp_rows).to_csv(results / "dimension_importance.csv", index=False)

    baseline_overall = dim.inter_rater(wide_by_col, "overall")
    composite["direct_overall_inter_rater"] = round(baseline_overall, 4)
    composite["full_rubric"]["flat_inter_rater"] = round(
        dim.composite_inter_rater(wide_by_col, DIMS), 4)
    composite["refined_rubric"]["flat_inter_rater"] = round(
        dim.composite_inter_rater(wide_by_col, PRUNED), 4)
    with open(results / "composite_results.json", "w", encoding="utf-8") as fh:
        json.dump(composite, fh, indent=2)

    # ---- best/worst-case bounds per judge ----
    bw_rows = []
    for m in models:
        bw_rows.append(dim.best_worst_bounds(
            wide_overall, judge_tab[m]["overall"], m,
            n_resamples=n_mc, seed=seed))
    bw = pd.DataFrame(bw_rows)
    bw.to_csv(results / "best_worst_bounds.csv", index=False)

    # ---- dashboard (composites use the refined set) ----
    # Humans and the random human are shown in normalized space (continuous),
    # judges as their raw integer ratings.
    _write_dashboard(cfg, judge_tab, pruned, DIMS, PRUNED, DROPPED,
                     h1_frame, h2_frame, rh_frame)

    # ---- console summary ----
    print("ANALYSIS 1: internal consistency per rater (composite vs own DIRECT overall):")
    print(f"  {'rater':16s} {'flat':>7s} {'fitted':>7s}  better")
    for r in ic_rows:
        print(f"  {r['rater']:16s} {r['flat_corr']:7.4f} {r['fitted_corr']:7.4f}  {r['better']}")
    print(f"  (judge composite type used for pruning: {ctype})")
    print(f"\nANALYSIS 2: dimension reduction ({ctype} LLM composite vs random-human overall):")
    print(f"  full {len(DIMS)}-dim match (mean over {len(models)} LLMs): r={reduction['full_match']:.4f}")
    print(f"  refined set ({len(PRUNED)} dims): {PRUNED}")
    print(f"  dropped: {DROPPED}")
    print(f"  refined match: r={reduction['refined_match']:.4f} "
          f"(improvement {reduction['improvement']:+.4f})")
    print("  per-dimension drop-one delta on the LLM match (positive means the dim hurts):")
    for d in DIMS:
        print(f"    {d:16s} {drop_delta[d]:+.4f}   kept={d in PRUNED}")
    print("\nCOMPOSITE vs direct human overall (normalised space, refined set weights):")
    print(f"  full rubric ({len(DIMS)} dims):    flat r={full['flat_corr']:.4f}  "
          f"fitted r={full['fitted_corr']:.4f}")
    print(f"  refined rubric ({len(PRUNED)} dims): flat r={pruned['flat_corr']:.4f}  "
          f"fitted r={pruned['fitted_corr']:.4f}")
    print("  refined fitted weights (reconstructing human overall):",
          {d: round(w, 3) for d, w in pruned["weights"].items()},
          f"intercept={pruned['intercept']:+.3f}")
    print("\nBEST/WORST-CASE BOUNDS per judge:")
    print(bw.to_string(index=False))
    print(f"\nDirect-overall inter-rater baseline: {baseline_overall:.3f}")
    print("Dashboard written to docs/index.html")


def _cell(frame, item_id, dims, pruned, weights, intercept, good_thr,
          raw=True):
    """One eval's cell payload for a single item.

    The per-dimension and overall scores are the rater's raw ratings, shown as
    given: integers for the two humans and the judges, and continuous only for
    the Monte-Carlo random human (raw=False), which is a derived average. The
    flat and fitted composites are always derived, continuous quantities.
    """
    if item_id not in frame.index:
        return None
    row = frame.loc[item_id]
    if raw:
        vals = {d: int(round(float(row[d]))) for d in dims}
        overall = int(round(float(row["overall"])))
    else:
        vals = {d: round(float(row[d]), 2) for d in dims}
        overall = round(float(row["overall"]), 2)
    X = np.array([float(row[d]) for d in pruned])
    flat = float(X.mean())
    fitted = intercept + float(X @ np.array([weights[d] for d in pruned]))
    out = {**vals, "flat": round(flat, 2), "fitted": round(fitted, 2),
           "overall": overall, "good": bool(float(overall) >= good_thr)}
    return out


def _side_frame(wide_by_col, cols, sfx):
    """Per-item normalized score frame for one reviewer side (a = lower id).

    Assembles one column per rubric field from the per-column normalized wide
    tables, so every human cell shown is bias-corrected rather than raw.
    """
    frame = None
    for c in cols:
        piece = wide_by_col[c].set_index("item_id")[[c + sfx]].rename(
            columns={c + sfx: c})
        frame = piece if frame is None else frame.join(piece)
    return frame


def _random_human_frame(a_frame, b_frame, cols, n_mc, seed):
    """Monte-Carlo random human on the normalized human scores, per column.

    For each item, average n_mc seeded random picks of one whole reviewer. The
    same seeded draw is used for every column and matches the headline random-
    human overall, so the random human's dimensions and overall are mutually
    consistent and consistent with the correlation target.
    """
    items = list(a_frame.index)
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, 2, size=(n_mc, len(items)))
    frac_a = (picks == 0).mean(axis=0)
    rh = pd.DataFrame(index=items, columns=cols, dtype=float)
    for c in cols:
        rh[c] = frac_a * a_frame[c].to_numpy() + (1 - frac_a) * b_frame[c].to_numpy()
    return rh


def _write_dashboard(cfg, judge_tab, pruned_fit, DIMS, PRUNED, DROPPED,
                     a_frame, b_frame, rh_frame):
    weights, intercept = pruned_fit["weights"], pruned_fit["intercept"]
    good_thr = float(cfg["analysis"]["good_threshold"])
    data_csv = pd.read_csv(resolve_path(cfg["paths"]["data_csv"])).set_index("item_id")

    # Human cells are bias-normalized (continuous) on every dimension and on the
    # overall; the random human is their Monte-Carlo average. All three are shown
    # as continuous values (raw=False). Judge cells stay raw integers.
    items = list(a_frame.index)

    judge_models = [m for m in cfg["judge"]["models"] if m in judge_tab]
    rows = []
    for it in items:
        text = str(data_csv.loc[it, "text"]) if it in data_csv.index else ""
        evals = {}
        evals["human_1"] = _cell(a_frame, it, DIMS, PRUNED, weights, intercept,
                                 good_thr, raw=False)
        evals["human_2"] = _cell(b_frame, it, DIMS, PRUNED, weights, intercept,
                                 good_thr, raw=False)
        rh = _cell(rh_frame, it, DIMS, PRUNED, weights, intercept, good_thr, raw=False)
        evals["random_human"] = rh
        h1o = evals["human_1"]["overall"]
        h2o = evals["human_2"]["overall"]
        rho = rh["overall"]
        rh_good = rh["good"]
        for m in judge_models:
            c = _cell(judge_tab[m], it, DIMS, PRUNED, weights, intercept, good_thr)
            if c is not None:
                jo = c["overall"]
                c["min_diff"] = round(min(abs(jo - h1o), abs(jo - h2o)), 2)
                c["max_diff"] = round(max(abs(jo - h1o), abs(jo - h2o)), 2)
                # Random-human delta: the per-item basis of the headline metric.
                c["rh_diff"] = round(abs(jo - rho), 2)
                # Good/bad decision agreement with the random human at the threshold.
                c["rh_match"] = bool(c["good"] == rh_good)
            evals[m] = c
        rows.append({"item_id": it, "text": text, "evals": evals})

    payload = {
        "dims": DIMS,
        "pruned": PRUNED,
        "dropped": dim.DROPPED_DIMENSIONS,
        "weights": {d: round(weights[d], 4) for d in PRUNED},
        "intercept": round(intercept, 4),
        "judges": judge_models,
        "human_names": ["human_1", "human_2", "random_human"],
        "good_threshold": good_thr,
        "rows": rows,
    }
    with open(resolve_path(cfg["paths"]["results_dir"]) / "dashboard_data.json",
              "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    from src.dashboard import render_html
    docs = resolve_path("docs")
    docs.mkdir(parents=True, exist_ok=True)
    html = render_html(payload)
    with open(docs / "index.html", "w", encoding="utf-8") as fh:
        fh.write(html)


if __name__ == "__main__":
    main()
