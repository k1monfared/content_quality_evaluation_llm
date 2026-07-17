"""Phase C dimension analysis: internal consistency and dimension reduction.

Phase B measures how well each judge tracks the direct, holistic human overall
score. This module studies the seven-dimension rubric behind that overall
through two distinct analyses. Everything is a recompute over the committed
stores. No API calls are made.

Two composites of the dimensions are used throughout:

  - flat: the equal-weight average of the dimensions.
  - fitted: a least-squares fit of a group's own direct overall on that group's
    dimension scores, so the weights reconstruct the holistic overall.

Analysis one (internal consistency, descriptive): per rater (the two humans, the
random human, and each judge), fit weights by regressing that rater's direct
overall on its own dimension scores, and compare both composites to that rater's
own direct overall. This says whether each rater is internally consistent and
whether the flat or the fitted composite better reconstructs its own overall.

Analysis two (dimension reduction, the actual pruning): a good judge is one
whose dimension-based evaluation tracks the human holistic judgment, so the
rubric is pruned by how well the LLM composite matches the Monte-Carlo
random-human overall, not by contribution to the human overall. Using the
composite type that is more internally consistent for the LLMs, we search for
the dimension subset that maximises the LLM-composite-to-random-human-overall
correlation, aggregated across the four judges.

The human side is worked in the study's normalised space (per-rater additive
bias removed, as the baseline is computed). Pearson is invariant to per-variable
shift and scale, so the raw LLM composites compare cleanly against it.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd

from . import metrics, montecarlo, normalize
from .rubric import DIMENSIONS

# The refined rubric selected by analysis two (see analyze_dimensions.py): the
# dimension subset whose LLM composite best tracks the random-human overall.
# Clarity, structure, and informativeness are dropped because removing them
# raises that LLM-composite-to-random-human match rather than lowering it.
PRUNED_DIMENSIONS = ["neutrality", "verifiability", "coverage", "readability"]
DROPPED_DIMENSIONS = ["clarity", "structure", "informativeness"]


def llm_long(judge_tab: dict[str, pd.DataFrame], models: list[str]) -> pd.DataFrame:
    """Pooled per-item LLM dimension and overall rows across the judge models."""
    return pd.concat([judge_tab[m].reset_index() for m in models],
                     ignore_index=True)


def internal_consistency(long: pd.DataFrame, dims: list[str],
                         target: str = "overall") -> dict[str, Any]:
    """Flat vs fitted composite correlation with a group's own direct overall."""
    fit = fit_weights(long, dims, target)
    return {
        "flat_corr": round(fit["flat_corr"], 4),
        "fitted_corr": round(fit["fitted_corr"], 4),
        "better": "fitted" if fit["fitted_corr"] >= fit["flat_corr"] else "flat",
    }


def llm_composite_match(judge_tab: dict[str, pd.DataFrame], models: list[str],
                        dims: list[str], rh_overall: pd.Series, ctype: str,
                        pooled_long: pd.DataFrame) -> float:
    """Mean over judges of corr(LLM composite of dims, random-human overall).

    For the fitted composite the weights reconstruct the pooled LLM direct
    overall on the same dimension subset. This measures how well each judge's
    own dimension-based evaluation tracks the human holistic judgment.
    """
    dims = list(dims)
    if ctype == "fitted":
        fit = fit_weights(pooled_long, dims, "overall")
        w, b = fit["weights"], fit["intercept"]
    items = list(rh_overall.index)
    corrs = []
    for m in models:
        frame = judge_tab[m]
        common = [it for it in items if it in frame.index]
        X = frame.loc[common, dims].to_numpy(float)
        comp = X.mean(axis=1) if ctype == "flat" else b + X @ np.array([w[d] for d in dims])
        corrs.append(metrics.pearson(comp, rh_overall.loc[common].to_numpy(float)))
    return float(np.mean(corrs))


def select_dimensions(judge_tab: dict[str, pd.DataFrame], models: list[str],
                      all_dims: list[str], rh_overall: pd.Series, ctype: str,
                      pooled_long: pd.DataFrame) -> dict[str, Any]:
    """Exhaustive subset search maximising the LLM-composite-to-random-human match.

    Returns the full-rubric match, the best subset and its match, and a per-
    dimension drop-one delta from the full rubric (positive means dropping the
    dimension improves the match, so the dimension hurts).
    """
    full_match = llm_composite_match(judge_tab, models, all_dims, rh_overall,
                                     ctype, pooled_long)
    best_subset, best_match = list(all_dims), full_match
    # Exhaustive search over every non-empty subset. Track the single best subset,
    # the best achievable match at each subset size (to confirm the greedy refined
    # set is optimal for its size), and the overall top subsets.
    best_by_size: dict[int, dict[str, Any]] = {}
    all_scored: list[tuple[float, list[str]]] = []
    for k in range(1, len(all_dims) + 1):
        for S in combinations(all_dims, k):
            r = llm_composite_match(judge_tab, models, list(S), rh_overall,
                                    ctype, pooled_long)
            all_scored.append((r, list(S)))
            if k not in best_by_size or r > best_by_size[k]["match"]:
                best_by_size[k] = {"dims": list(S), "match": round(r, 4)}
            if r > best_match:
                best_subset, best_match = list(S), r
    top_subsets = [{"dims": s, "match": round(r, 4)}
                   for r, s in sorted(all_scored, key=lambda t: t[0], reverse=True)[:8]]
    best_by_size_list = [{"size": k, "dims": best_by_size[k]["dims"],
                          "match": best_by_size[k]["match"]}
                         for k in sorted(best_by_size)]
    drop_one = {}
    for d in all_dims:
        rem = [x for x in all_dims if x != d]
        drop_one[d] = round(
            llm_composite_match(judge_tab, models, rem, rh_overall, ctype,
                                pooled_long) - full_match, 4)

    # Greedy backward elimination for an interpretable path: at each step remove
    # the dimension whose removal most improves the match, until no removal helps.
    cur, cur_r = list(all_dims), full_match
    greedy = [{"dropped": None, "dims": list(cur), "match": round(cur_r, 4)}]
    while len(cur) > 1:
        cand = None
        for d in cur:
            rem = [x for x in cur if x != d]
            r = llm_composite_match(judge_tab, models, rem, rh_overall, ctype,
                                    pooled_long)
            if cand is None or r > cand[1]:
                cand = (d, r, rem)
        if cand[1] >= cur_r:
            cur, cur_r = cand[2], cand[1]
            greedy.append({"dropped": cand[0], "dims": list(cur),
                           "match": round(cur_r, 4)})
        else:
            break

    refined = [d for d in all_dims if d in best_subset]
    return {
        "composite_type": ctype,
        "full_match": round(full_match, 4),
        "refined_dims": refined,
        "dropped_dims": [d for d in all_dims if d not in best_subset],
        "refined_match": round(best_match, 4),
        "improvement": round(best_match - full_match, 4),
        "drop_one_delta": drop_one,
        "greedy_path": greedy,
        "best_by_size": best_by_size_list,
        "top_subsets": top_subsets,
    }


def build_normalized(human: pd.DataFrame, cfg: dict[str, Any],
                     cols: list[str]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Normalise each column for per-rater additive bias.

    Returns (wide_by_col, long) where wide_by_col[c] has columns item_id, c_a,
    c_b (the two raters' normalised scores per item) and long has one row per
    rating with a normalised column per name in cols.
    """
    wide_by_col: dict[str, pd.DataFrame] = {}
    long: pd.DataFrame | None = None
    for c in cols:
        fit = normalize.fit_additive(human, c)
        nr = normalize.normalize_ratings(human, fit, c)
        w = normalize.to_wide(nr, fit).rename(
            columns={"score_a": c + "_a", "score_b": c + "_b"})
        wide_by_col[c] = w[["item_id", c + "_a", c + "_b"]]
        piece = nr[["item_id", "persona_id", "normalized"]].rename(
            columns={"normalized": c})
        long = piece if long is None else long.merge(
            piece, on=["item_id", "persona_id"])
    return wide_by_col, long


def fit_weights(long: pd.DataFrame, dims: list[str], target: str = "overall"
                ) -> dict[str, Any]:
    """Least-squares fit of target on dims, pooled over ratings.

    Returns the intercept, the per-dimension weights, and the correlation of
    both the flat and the fitted composite with the target.
    """
    X = long[dims].to_numpy(float)
    y = long[target].to_numpy(float)
    A = np.column_stack([np.ones(len(X)), X])
    coef, *_ = np.linalg.lstsq(A, y, rcond=None)
    intercept = float(coef[0])
    weights = {d: float(w) for d, w in zip(dims, coef[1:])}
    flat = X.mean(axis=1)
    fitted = A @ coef
    return {
        "dims": list(dims),
        "intercept": intercept,
        "weights": weights,
        "flat_corr": float(metrics.pearson(flat, y)),
        "fitted_corr": float(metrics.pearson(fitted, y)),
    }


def composite_series(frame: pd.DataFrame, dims: list[str], weights: dict[str, float],
                     intercept: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (flat, fitted) composite vectors for a frame carrying columns dims."""
    X = frame[dims].to_numpy(float)
    flat = X.mean(axis=1)
    fitted = intercept + X @ np.array([weights[d] for d in dims])
    return flat, fitted


def univariate_corr(long: pd.DataFrame, dims: list[str], target: str = "overall"
                    ) -> dict[str, float]:
    y = long[target].to_numpy(float)
    return {d: float(metrics.pearson(long[d].to_numpy(float), y)) for d in dims}


def correlation_matrix(long: pd.DataFrame, dims: list[str]) -> pd.DataFrame:
    return long[dims].corr()


def inter_rater(wide_by_col: dict[str, pd.DataFrame], col: str) -> float:
    w = wide_by_col[col]
    return float(metrics.pearson(w[col + "_a"].to_numpy(), w[col + "_b"].to_numpy()))


def composite_inter_rater(wide_by_col: dict[str, pd.DataFrame], dims: list[str],
                          weights: dict[str, float] | None = None,
                          intercept: float = 0.0) -> float:
    """Inter-rater reliability of a composite (flat if weights is None)."""
    def side(sfx: str) -> np.ndarray:
        cols = [wide_by_col[d][d + sfx].to_numpy(float) for d in dims]
        M = np.vstack(cols)
        if weights is None:
            return M.mean(axis=0)
        return intercept + np.array([weights[d] for d in dims]) @ M
    return float(metrics.pearson(side("_a"), side("_b")))


def drop_one(long: pd.DataFrame, dims: list[str], target: str = "overall"
             ) -> pd.DataFrame:
    """For each dim, remove it and record the composite-to-target agreement."""
    full = fit_weights(long, dims, target)
    rows = []
    for d in dims:
        rem = [x for x in dims if x != d]
        r = fit_weights(long, rem, target)
        rows.append({
            "dropped": d,
            "flat_corr_without": round(r["flat_corr"], 4),
            "flat_delta": round(r["flat_corr"] - full["flat_corr"], 4),
            "fitted_corr_without": round(r["fitted_corr"], 4),
            "fitted_delta": round(r["fitted_corr"] - full["fitted_corr"], 4),
        })
    return pd.DataFrame(rows)


def best_worst_bounds(wide_overall: pd.DataFrame, judge_overall: pd.Series,
                      model: str, n_resamples: int = 500, seed: int = 13
                      ) -> dict[str, float]:
    """Best- and worst-case agreement of a judge against the two humans.

    For each item, take the judge's absolute distance to each of the two human
    overall scores. The nearer human is the best case, the farther the worst
    case. The random-human result assumes users split evenly between the two
    reviewers (optimistic), and is computed with the same Monte-Carlo random
    human as the headline ratio so the numbers line up. The worst case bounds
    how bad it gets if every user happens to draw the less-agreeing reviewer.
    """
    merged = wide_overall.merge(judge_overall.rename("j"), on="item_id",
                                how="inner").dropna(subset=["j"])
    J = merged["j"].to_numpy(float)
    A = merged["score_a"].to_numpy(float)
    B = merged["score_b"].to_numpy(float)
    da, db = np.abs(J - A), np.abs(J - B)
    closer = np.where(da <= db, A, B)
    farther = np.where(da <= db, B, A)
    rh = montecarlo.random_human(merged, n_resamples, seed)
    return {
        "model": model,
        "n_items": int(len(merged)),
        "random_corr": round(metrics.judge_human_corr(J, rh), 3),
        "best_corr": round(float(metrics.pearson(J, closer)), 3),
        "worst_corr": round(float(metrics.pearson(J, farther)), 3),
        "random_mad": round(float(np.mean(np.abs(J - rh))), 3),
        "best_mad": round(float(np.mean(np.minimum(da, db))), 3),
        "worst_mad": round(float(np.mean(np.maximum(da, db))), 3),
    }
