"""Phase B analysis: normalize the panel, compute the headline correlation
ratio per judge with bootstrap CIs, the secondary precision/recall/F1, and the
full cost and token accounting. Writes result CSVs into outputs/.

Usage:
    python scripts/analyze.py
"""
import json

import _bootstrap  # noqa: F401
import numpy as np
import pandas as pd

from src import bookkeeping, failures, metrics, montecarlo, normalize, store
from src.config import load_config, load_prices, resolve_path
from src.rubric import DIMENSIONS


def best_versions(cfg) -> dict:
    path = resolve_path(cfg["judge"]["best_versions_file"])
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {m: cfg["judge"]["best_prompt_version"] for m in cfg["judge"]["models"]}


def _load_wide(cfg):
    human = store.load(cfg["paths"]["human_store"])
    human = human[human["ok"] == 1].copy()
    if human.empty:
        raise SystemExit("Human panel store is empty. Run run_human_panel.py.")
    # Add the seeded reviewer noise so the panel models a realistic
    # inter-reviewer agreement (about 0.70). No API calls: the noise is a
    # deterministic transform of the committed cache.
    human = normalize.add_reviewer_noise(human, cfg)
    fit = normalize.fit_additive(human, "overall")
    norm = normalize.normalize_ratings(human, fit, "overall")
    wide = normalize.to_wide(norm, fit)
    out = resolve_path(cfg["paths"]["normalized_csv"])
    wide.to_csv(out, index=False)
    # persist fitted biases too
    pd.DataFrame(
        [{"persona_id": k, "rater_bias": v} for k, v in fit["rater_bias"].items()]
    ).to_csv(resolve_path(cfg["paths"]["results_dir"]) / "rater_biases.csv", index=False)
    return wide, fit


def _judge_scores(cfg, versions):
    """Per-item overall score for each model, each at its own best version."""
    ev = store.load(cfg["paths"]["eval_store"])
    ev = ev[ev["ok"] == 1]
    frames = []
    for model, version in versions.items():
        sub = ev[(ev["model"] == model) & (ev["prompt_version"] == version)]
        if sub.empty:
            continue
        frames.append(sub.groupby("item_id")["overall"].mean().rename(model))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, axis=1)


def rating_levels(cfg, versions, results_dir):
    """Raw rating levels: how favorably each judge and each human reference scores.

    Uses the real (raw, integer) overall and dimension scores, so it measures the
    generosity of each rater on the 1 to 10 scale rather than the bias-normalized
    values. Writes rating_levels.csv (overall per rater plus the min and max human
    band) and rating_levels_dimensions.csv (per-dimension means).
    """
    dims = list(DIMENSIONS)
    cols = dims + ["overall"]
    human = store.load(cfg["paths"]["human_store"])
    human = human[human["ok"] == 1].copy()
    human = normalize.add_reviewer_noise(human, cfg)
    item_ids, a_rows, b_rows = [], [], []
    for it, grp in human.groupby("item_id"):
        grp = grp.sort_values("persona_id")
        if len(grp) < 2:
            continue
        item_ids.append(it)
        a_rows.append({c: float(grp.iloc[0][c]) for c in cols})
        b_rows.append({c: float(grp.iloc[1][c]) for c in cols})
    A, B = pd.DataFrame(a_rows, index=item_ids), pd.DataFrame(b_rows, index=item_ids)
    ao, bo = A["overall"].to_numpy(), B["overall"].to_numpy()
    rng = np.random.default_rng(cfg["analysis"]["seed"])
    picks = rng.integers(0, 2, size=(cfg["analysis"]["mc_resamples"], len(ao)))
    frac_a = (picks == 0).mean(axis=0)
    rh_o = frac_a * ao + (1 - frac_a) * bo

    ev = store.load(cfg["paths"]["eval_store"])
    ev = ev[ev["ok"] == 1]
    jtab = {}
    for m in cfg["judge"]["models"]:
        v = versions.get(m)
        sub = ev[(ev["model"] == m) & (ev["prompt_version"] == v)]
        if not sub.empty:
            jtab[m] = sub.groupby("item_id")[cols].mean()

    # per-item overall for every rater, for the box-plot distributions
    overall_cols = {"item_id": item_ids}
    for m in cfg["judge"]["models"]:
        if m in jtab:
            overall_cols[m] = jtab[m]["overall"].reindex(item_ids).to_numpy()
    overall_cols["human_1"] = ao
    overall_cols["human_2"] = bo
    overall_cols["random_human"] = rh_o
    pd.DataFrame(overall_cols).to_csv(
        results_dir / "rating_levels_overall.csv", index=False)

    rows = []
    for m in cfg["judge"]["models"]:
        if m in jtab:
            rows.append({"rater": m, "kind": "judge",
                         "mean_overall": round(float(jtab[m]["overall"].mean()), 3)})
    rows += [
        {"rater": "human_1", "kind": "human", "mean_overall": round(float(ao.mean()), 3)},
        {"rater": "human_2", "kind": "human", "mean_overall": round(float(bo.mean()), 3)},
        {"rater": "two_human_average", "kind": "human_ref",
         "mean_overall": round(float((ao.mean() + bo.mean()) / 2), 3)},
        {"rater": "random_human", "kind": "human_ref",
         "mean_overall": round(float(rh_o.mean()), 3)},
        {"rater": "min_of_two_humans", "kind": "human_ref",
         "mean_overall": round(float(np.minimum(ao, bo).mean()), 3)},
        {"rater": "max_of_two_humans", "kind": "human_ref",
         "mean_overall": round(float(np.maximum(ao, bo).mean()), 3)},
    ]
    levels = pd.DataFrame(rows)
    levels.to_csv(results_dir / "rating_levels.csv", index=False)

    h_dim = {d: float((A[d].mean() + B[d].mean()) / 2) for d in dims}
    drows = []
    for d in dims:
        row = {"dimension": d, "two_human_average": round(h_dim[d], 3)}
        for m in cfg["judge"]["models"]:
            if m in jtab:
                row[m] = round(float(jtab[m][d].mean()), 3)
        drows.append(row)
    pd.DataFrame(drows).to_csv(results_dir / "rating_levels_dimensions.csv", index=False)

    print("\nRating levels (raw overall, how favorably each rater scores):")
    print(levels.to_string(index=False))
    return levels


def main():
    cfg = load_config()
    prices = load_prices()
    results_dir = resolve_path(cfg["paths"]["results_dir"])
    results_dir.mkdir(parents=True, exist_ok=True)

    wide, fit = _load_wide(cfg)
    hh = metrics.human_human_corr(wide)
    icc = metrics.icc_a1(wide["score_a"].to_numpy(), wide["score_b"].to_numpy())
    print(f"Human-human baseline: pearson={hh:.3f} icc={icc:.3f} "
          f"(n={len(wide)} items)")

    n_mc = cfg["analysis"]["mc_resamples"]
    seed = cfg["analysis"]["seed"]
    versions = best_versions(cfg)
    piv = _judge_scores(cfg, versions)
    rating_levels(cfg, versions, results_dir)

    # ---- Monte-Carlo random human vs the exact simple average ----
    # The random human is the average of n_mc seeded random-pick evaluation sets.
    # It converges to, but at a finite iteration count is not identical to, the
    # exact per-item mean of the two humans. Quantify the residual.
    rh_full = montecarlo.random_human(wide, n_mc, seed)
    avg_full = montecarlo.simple_average(wide)
    resid = np.abs(rh_full - avg_full)
    rh_cmp = {
        "n_resamples": int(n_mc),
        "n_items": int(len(wide)),
        "mean_abs_diff_vs_simple_average": round(float(resid.mean()), 4),
        "max_abs_diff_vs_simple_average": round(float(resid.max()), 4),
        "corr_with_simple_average": round(float(metrics.pearson(rh_full, avg_full)), 6),
    }
    with open(results_dir / "random_human_comparison.json", "w", encoding="utf-8") as fh:
        json.dump(rh_cmp, fh, indent=2)
    print("\nMonte-Carlo random human vs exact simple average of the two humans:")
    print(json.dumps(rh_cmp, indent=2))

    good_thr = cfg["analysis"]["good_threshold"]
    headline_rows, prf_rows = [], []
    for model in cfg["judge"]["models"]:
        if model not in piv.columns:
            continue
        merged = wide.merge(piv[[model]], on="item_id", how="inner").dropna(subset=[model])
        if len(merged) < 5:
            continue
        # Monte-Carlo random human aligned to the merged item order.
        rh = montecarlo.random_human(merged, n_mc, seed)
        jscore = merged[model].to_numpy(dtype=float)
        lh = metrics.judge_human_corr(jscore, rh)
        hh_sub = metrics.human_human_corr(merged)
        boot = metrics.bootstrap_ratio(jscore, merged, rh,
                                       cfg["analysis"]["bootstrap_iters"], seed)
        headline_rows.append({
            "model": model, "version": versions.get(model, ""),
            "n_items": len(merged),
            "judge_human_corr": round(lh, 3),
            "human_human_corr": round(hh_sub, 3),
            "ratio": round(lh / hh_sub, 3) if hh_sub else np.nan,
            "ratio_boot_mean": round(boot["ratio_mean"], 3),
            "ratio_ci_low": round(boot["ci_low"], 3),
            "ratio_ci_high": round(boot["ci_high"], 3),
        })
        pr = metrics.prf1(jscore, rh, good_thr, good_thr)
        prf_rows.append({"model": model, **{k: round(v, 3) if isinstance(v, float) else v
                                            for k, v in pr.items()}})

    headline = pd.DataFrame(headline_rows).sort_values("ratio", ascending=False)
    prf = pd.DataFrame(prf_rows)
    headline.to_csv(results_dir / "headline_results.csv", index=False)
    prf.to_csv(results_dir / "secondary_prf1.csv", index=False)
    print("\nHeadline (LLM-human / human-human correlation ratio):")
    print(headline.to_string(index=False))
    print("\nSecondary (precision/recall/F1 vs thresholded random human):")
    print(prf.to_string(index=False))

    # ---- cost accounting ----
    agg = bookkeeping.aggregate(cfg["paths"]["bookkeeping_store"])
    if not agg.get("empty"):
        agg["per_model"].to_csv(results_dir / "cost_per_model.csv", index=False)
        scalars = {k: v for k, v in agg.items()
                   if k not in ("per_model", "per_role", "empty")}
        with open(results_dir / "cost_summary.json", "w", encoding="utf-8") as fh:
            json.dump(scalars, fh, indent=2)
        print("\nCost accounting (actuals from bookkeeping store):")
        print(json.dumps(scalars, indent=2))
        print(agg["per_model"].to_string(index=False))

    # ---- parse success and failure reporting ----
    rep = failures.failure_report(cfg)
    if not rep.get("empty"):
        rep["per_model"].to_csv(results_dir / "failure_rates.csv", index=False)
        with open(results_dir / "failure_report.json", "w", encoding="utf-8") as fh:
            json.dump({"overall": rep["overall"], "reason_counts": rep["reason_counts"],
                       "n_failed_records": rep["n_failed_records"]}, fh, indent=2)
        print("\nParse success and failure:")
        print(json.dumps(rep["overall"], indent=2))
        print(rep["per_model"].to_string(index=False))
        if rep["reason_counts"]:
            print("Failure reasons (model|reason -> count):", rep["reason_counts"])

    # ---- pre-run projection is always available ----
    proj = bookkeeping.project_cost(cfg, prices)
    with open(results_dir / "cost_projection.json", "w", encoding="utf-8") as fh:
        json.dump(proj, fh, indent=2)

    if not headline.empty:
        best = headline.iloc[0]
        print(f"\nBest judge by correlation ratio: {best['model']} "
              f"(ratio={best['ratio']}, "
              f"95% CI [{best['ratio_ci_low']}, {best['ratio_ci_high']}])")


if __name__ == "__main__":
    main()
