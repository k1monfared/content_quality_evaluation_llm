"""Monte-Carlo random-human synthesis.

Averaging an item's two scores directly would hide the disagreement between
raters and make the human panel look more consistent than it is. Instead we
build the random human by simulation: for each item we randomly pick one of its
two human evaluations, which yields one full "random human" evaluation set
across all items, and we repeat that draw many times (1000 by default, seeded
and reproducible). Averaging the repeated sets per item gives the Monte-Carlo
random human. By the law of large numbers it converges to the per-item mean of
the two humans, but at a finite number of iterations it is close to, and not
exactly equal to, that mean: a small seeded residual remains that shrinks as the
iteration count grows. Carrying the panel's real spread into the draw is what
makes it a plausible single reviewer rather than a smoothed consensus.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def resample_matrix(wide: pd.DataFrame, n_resamples: int, seed: int = 13
                    ) -> np.ndarray:
    """Return an array of shape (n_resamples, n_items).

    Row r, column i is one randomly chosen score for item i. Each row is one
    full random-human evaluation set across all items.
    """
    rng = np.random.default_rng(seed)
    a = wide["score_a"].to_numpy(dtype=float)
    b = wide["score_b"].to_numpy(dtype=float)
    n_items = len(wide)
    picks = rng.integers(0, 2, size=(n_resamples, n_items))
    return np.where(picks == 0, a[None, :], b[None, :])


def random_human(wide: pd.DataFrame, n_resamples: int = 1000, seed: int = 13
                 ) -> np.ndarray:
    """The Monte-Carlo random human: one score per item.

    Draws n_resamples full random-pick evaluation sets and averages them per
    item. Close to, but not exactly, the per-item mean of the two humans.
    """
    return resample_matrix(wide, n_resamples, seed).mean(axis=0)


def simple_average(wide: pd.DataFrame) -> np.ndarray:
    """The exact per-item mean of the two human evaluations."""
    return (wide["score_a"].to_numpy(dtype=float)
            + wide["score_b"].to_numpy(dtype=float)) / 2.0


def single_random_human(wide: pd.DataFrame, seed: int = 13) -> np.ndarray:
    """One random-human draw, one score per item (used for thresholded labels)."""
    return resample_matrix(wide, 1, seed)[0]
