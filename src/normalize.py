"""Rater-bias normalization via a two-way additive model.

We assume each observed overall score decomposes as

    score(item, rater) = item_quality(item) + rater_bias(rater) + noise

and fit it by least squares over the connected assignment. Because the
personas form a single connected component, all rater biases are estimated on
one common scale (see assignment.py for why connectivity is required). We then
center the biases to sum to zero and subtract each rater's bias from its raw
scores. After this step the two scores an item received are on a comparable
scale, so their disagreement reflects genuine quality uncertainty rather than
one rater simply being harsher than the other.

The fit uses the minimum-norm least-squares solution (numpy.linalg.lstsq) to
handle the built-in rank deficiency (item and rater effects share an additive
constant), followed by explicit centering of the rater effects.
"""
from __future__ import annotations

import hashlib
from typing import Any

import numpy as np
import pandas as pd


def _row_noise(seed: int, col: str, item_id: Any, persona_id: Any,
               sd: float) -> float:
    """One deterministic Gaussian draw keyed by seed, column, item, and rater.

    Keying on the identity of the rating rather than on row position makes the
    injected noise reproducible regardless of the order the ratings are loaded
    in, so the noisier panel is identical on every re-run.
    """
    key = f"{seed}|{col}|{item_id}|{persona_id}".encode("utf-8")
    digest = hashlib.sha256(key).digest()
    sub_seed = int.from_bytes(digest[:8], "big")
    return float(np.random.default_rng(sub_seed).normal(0.0, sd))


def add_reviewer_noise(ratings: pd.DataFrame, cfg: dict[str, Any]
                       ) -> pd.DataFrame:
    """Add seeded reviewer noise to the cached rubric scores.

    Real subjective review agrees far less than the raw persona panel, so we
    model a more realistic inter-reviewer agreement by adding an independent,
    deterministic, seeded noise term to each rating's numeric rubric scores.
    After the Gaussian term is added, each score is rounded to the nearest whole
    number and clipped to the rubric scale, so a noisy reviewer still returns an
    integer 1 to 10 rating exactly as a real reviewer would. The noise standard
    deviation is calibrated (see configs/config.yaml) so the two-reviewer Pearson
    baseline lands at most 0.70 after this rounding. The transform is applied in
    memory from the committed cache, so it makes no API calls and does not touch
    the cost ledger. When extra_noise_sd is zero the ratings are returned
    unchanged.
    """
    hp = cfg.get("human_panel", {}) or {}
    sd = float(hp.get("extra_noise_sd", 0.0) or 0.0)
    if sd <= 0.0 or ratings.empty:
        return ratings
    seed = int(hp.get("noise_seed", 0))
    cols = hp.get("noise_columns", ["overall"])
    rubric = cfg.get("rubric", {})
    lo = float(rubric.get("scale_min", 1))
    hi = float(rubric.get("scale_max", 10))
    out = ratings.copy()
    items = out["item_id"].tolist()
    personas = out["persona_id"].tolist()
    for col in cols:
        if col not in out.columns:
            continue
        noise = np.array([_row_noise(seed, col, it, pid, sd)
                          for it, pid in zip(items, personas)])
        # Round to the nearest integer and clip to the rubric scale so a noisy
        # reviewer still gives a whole-number 1 to 10 rating.
        out[col] = np.rint(out[col].astype(float) + noise)
        out[col] = out[col].clip(lo, hi)
    return out


def fit_additive(ratings: pd.DataFrame, score_col: str = "overall"
                 ) -> dict[str, Any]:
    """Fit score = item_quality + rater_bias by least squares.

    ratings needs columns item_id, persona_id, and score_col. Returns the
    estimated item qualities, centered rater biases, and the grand mean.
    """
    ratings = ratings.dropna(subset=[score_col]).copy()
    items = sorted(ratings["item_id"].unique())
    raters = sorted(ratings["persona_id"].unique())
    item_ix = {v: i for i, v in enumerate(items)}
    rater_ix = {v: j for j, v in enumerate(raters)}

    n = len(ratings)
    ni, nr = len(items), len(raters)
    X = np.zeros((n, ni + nr))
    y = ratings[score_col].to_numpy(dtype=float)
    for row_i, (_, r) in enumerate(ratings.iterrows()):
        X[row_i, item_ix[r["item_id"]]] = 1.0
        X[row_i, ni + rater_ix[r["persona_id"]]] = 1.0

    beta, *_ = np.linalg.lstsq(X, y, rcond=None)
    item_q = beta[:ni]
    rater_b = beta[ni:]

    # Center rater biases to sum to zero; fold the shift into item qualities so
    # fitted values are unchanged.
    shift = rater_b.mean()
    rater_b_centered = rater_b - shift
    item_q_adj = item_q + shift

    return {
        "grand_mean": float(y.mean()),
        "item_quality": {items[i]: float(item_q_adj[i]) for i in range(ni)},
        "rater_bias": {int(raters[j]): float(rater_b_centered[j]) for j in range(nr)},
    }


def normalize_ratings(ratings: pd.DataFrame, fit: dict[str, Any],
                      score_col: str = "overall") -> pd.DataFrame:
    """Add a normalized score column: raw minus that rater's centered bias."""
    out = ratings.dropna(subset=[score_col]).copy()
    bias = fit["rater_bias"]
    out["normalized"] = out.apply(
        lambda r: float(r[score_col]) - bias.get(int(r["persona_id"]), 0.0), axis=1
    )
    return out


def to_wide(norm_ratings: pd.DataFrame, fit: dict[str, Any]) -> pd.DataFrame:
    """One row per item with its two normalized scores, ordered by persona id."""
    rows = []
    for item_id, grp in norm_ratings.groupby("item_id"):
        grp = grp.sort_values("persona_id")
        if len(grp) < 2:
            continue
        first, second = grp.iloc[0], grp.iloc[1]
        rows.append({
            "item_id": item_id,
            "rater_a": int(first["persona_id"]),
            "score_a": float(first["normalized"]),
            "rater_b": int(second["persona_id"]),
            "score_b": float(second["normalized"]),
            "item_quality_hat": fit["item_quality"].get(item_id, np.nan),
        })
    return pd.DataFrame(rows)
