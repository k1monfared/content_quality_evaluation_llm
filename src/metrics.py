"""Metrics: agreement, the headline correlation ratio, bootstrap CIs, and the
secondary precision, recall, and F1 against a thresholded label.

The headline number for each judge is

    ratio = corr(judge, random-human) / corr(rater A, rater B)

the ratio to the human baseline: how the judge's agreement compares to the
human agreement baseline. A ratio at or above 1 means the judge tracks the
debiased human as reliably as two noisy humans track each other. It is a
baseline, not an unbeatable cap: judges can and do meet or exceed it.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from scipy import stats


def pearson(x: np.ndarray, y: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if len(x) < 3 or np.std(x) == 0 or np.std(y) == 0:
        return float("nan")
    return float(np.corrcoef(x, y)[0, 1])


def icc_a1(score_a: np.ndarray, score_b: np.ndarray) -> float:
    """ICC(A,1) style two-rater agreement (absolute agreement, single rater).

    A robustness companion to the Pearson human-human baseline.
    """
    a = np.asarray(score_a, dtype=float)
    b = np.asarray(score_b, dtype=float)
    n = len(a)
    if n < 3:
        return float("nan")
    m = np.vstack([a, b]).T
    grand = m.mean()
    row_means = m.mean(axis=1)
    col_means = m.mean(axis=0)
    ss_rows = 2 * np.sum((row_means - grand) ** 2)
    ss_cols = n * np.sum((col_means - grand) ** 2)
    ss_total = np.sum((m - grand) ** 2)
    ss_err = ss_total - ss_rows - ss_cols
    ms_rows = ss_rows / (n - 1)
    ms_cols = ss_cols / 1
    ms_err = ss_err / (n - 1)
    denom = ms_rows + ms_cols / n + (2 / n) * (ms_cols - ms_err)
    if denom == 0:
        return float("nan")
    return float((ms_rows - ms_err) / denom)


def human_human_corr(wide: pd.DataFrame) -> float:
    return pearson(wide["score_a"].to_numpy(), wide["score_b"].to_numpy())


def judge_human_corr(judge_scores: np.ndarray, random_human: np.ndarray) -> float:
    """Pearson correlation between a judge and the Monte-Carlo random human.

    judge_scores and random_human are both shape (n_items,). random_human is the
    per-item average of the 1000 random-pick evaluation sets (see montecarlo).
    """
    return pearson(np.asarray(judge_scores, dtype=float),
                   np.asarray(random_human, dtype=float))


def bootstrap_ratio(judge_scores: np.ndarray, wide: pd.DataFrame,
                    random_human: np.ndarray, n_boot: int = 2000, seed: int = 13
                    ) -> dict[str, float]:
    """Bootstrap over items to get a CI for the correlation ratio.

    Resamples items with replacement. For each bootstrap draw it recomputes both
    the human-human correlation and the judge to Monte-Carlo-random-human
    correlation on the resampled items, and takes their ratio.
    """
    rng = np.random.default_rng(seed)
    a = wide["score_a"].to_numpy(dtype=float)
    b = wide["score_b"].to_numpy(dtype=float)
    rh = np.asarray(random_human, dtype=float)
    n = len(a)
    ratios = []
    for _ in range(n_boot):
        idx = rng.integers(0, n, size=n)
        hh = pearson(a[idx], b[idx])
        jh = pearson(judge_scores[idx], rh[idx])
        if hh and not np.isnan(hh) and hh != 0 and not np.isnan(jh):
            ratios.append(jh / hh)
    ratios = np.array(ratios)
    if len(ratios) == 0:
        return {"ratio_mean": float("nan"), "ci_low": float("nan"),
                "ci_high": float("nan")}
    return {
        "ratio_mean": float(np.mean(ratios)),
        "ci_low": float(np.percentile(ratios, 2.5)),
        "ci_high": float(np.percentile(ratios, 97.5)),
    }


def prf1(judge_scores: np.ndarray, human_label: np.ndarray,
         judge_threshold: float, human_threshold: float) -> dict[str, float]:
    """Precision, recall, F1 for the judge's good/bad call against the
    thresholded random-human label."""
    y_true = (np.asarray(human_label, dtype=float) >= human_threshold).astype(int)
    y_pred = (np.asarray(judge_scores, dtype=float) >= judge_threshold).astype(int)
    tp = int(np.sum((y_pred == 1) & (y_true == 1)))
    fp = int(np.sum((y_pred == 1) & (y_true == 0)))
    fn = int(np.sum((y_pred == 0) & (y_true == 1)))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0.0)
    return {"precision": precision, "recall": recall, "f1": f1,
            "positives_true": int(y_true.sum()), "positives_pred": int(y_pred.sum())}
