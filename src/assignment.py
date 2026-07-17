"""Connected, balanced two-rater assignment design.

Each passage is rated by exactly two of the ten personas. Which two matters a
great deal for the later bias correction.

Why the design must be connected
--------------------------------
We later fit an additive model  score = item_quality + rater_bias + noise.
Rater biases are only comparable if the personas are linked into a single
component. Picture the personas as nodes and "co-rated an item" as edges. If
that graph split into two islands (say personas 1..5 only ever rate one pile
of items and 6..10 only the other), there would be no shared item to tie the
two islands together, so we could add any constant to one island's biases and
subtract it from its item qualities with no change in fit. The biases across
islands would be unidentifiable and the normalized scores would not live on a
common scale.

Robustly connected, not minimally connected
-------------------------------------------
Rather than a fragile ring of adjacent pairs (which links the personas with
only a spanning-tree-thin set of shared items), we deal items round-robin over
ALL C(10, 2) = 45 unordered persona pairs. With 500 items each of the 45 pairs
co-rates about 11 items, so the co-occurrence graph is the complete graph on
ten nodes: every pair of raters shares roughly a dozen items. Each persona
appears in exactly nine pairs, so its load is about 9 * 11 = 100 ratings,
keeping every rater's bias estimated from a similar, generous number of
observations.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

import pandas as pd


def all_pairs(n_personas: int) -> list[tuple[int, int]]:
    """Every unordered persona pair, C(n, 2) of them, lowest id first."""
    return [(a, b) for a, b in combinations(range(1, n_personas + 1), 2)]


def build_assignment(item_ids: list[str], n_personas: int = 10) -> pd.DataFrame:
    """Return a dataframe with columns item_id, rater_a, rater_b.

    Items are dealt round-robin across all C(n_personas, 2) persona pairs, so
    the co-rating graph is complete and each pair co-rates a balanced share of
    the items. rater_a is always the lower persona id, so downstream "first vs
    second rater" comparisons are well defined.
    """
    pairs = all_pairs(n_personas)
    rows = []
    for k, item_id in enumerate(item_ids):
        a, b = pairs[k % len(pairs)]
        lo, hi = sorted((a, b))
        rows.append({"item_id": item_id, "rater_a": lo, "rater_b": hi})
    return pd.DataFrame(rows)


def design_summary(assignment: pd.DataFrame, n_personas: int) -> dict[str, Any]:
    counts = {p: 0 for p in range(1, n_personas + 1)}
    pair_counts: dict[tuple[int, int], int] = {}
    adj = {p: set() for p in range(1, n_personas + 1)}
    for _, r in assignment.iterrows():
        a, b = int(r["rater_a"]), int(r["rater_b"])
        counts[a] += 1
        counts[b] += 1
        key = tuple(sorted((a, b)))
        pair_counts[key] = pair_counts.get(key, 0) + 1
        adj[a].add(b)
        adj[b].add(a)

    # connectivity check over the co-rating graph
    seen = set()
    stack = [1]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adj[node] - seen)

    total_pairs = n_personas * (n_personas - 1) // 2
    covered_pairs = len(pair_counts)
    min_shared = min(pair_counts.values()) if pair_counts else 0
    return {
        "items": len(assignment),
        "ratings_per_persona": counts,
        "connected": len(seen) == n_personas,
        "min_ratings": min(counts.values()),
        "max_ratings": max(counts.values()),
        "total_possible_pairs": total_pairs,
        "pairs_covered": covered_pairs,
        "complete_graph": covered_pairs == total_pairs,
        "min_items_shared_by_any_pair": min_shared,
        "max_items_shared_by_any_pair": max(pair_counts.values()) if pair_counts else 0,
    }
