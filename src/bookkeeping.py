"""Separate cost and token bookkeeping store.

Every API call, no matter its role (simulated human persona rating, judge
evaluation, or prompt-engineering trial), appends exactly one row here. This
store is deliberately kept distinct from the evaluation databases so cost
accounting never contaminates the analysis tables and vice versa.
"""
from __future__ import annotations

import csv
import threading
import time
from pathlib import Path
from typing import Any

from .config import load_prices

# Serializes appends to the cost log across concurrent workers.
_BOOK_LOCK = threading.Lock()

COST_COLUMNS = [
    "timestamp",
    "role",            # human_persona | judge | prompt_eng | connectivity
    "model",
    "prompt_version",
    "item_id",
    "persona_id",      # blank for judge calls
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "token_source",    # api | estimated
    "est_cost_usd",
    "latency_s",
    "ok",              # 1 success, 0 failure
]


def price_for(model: str, prices: dict[str, Any] | None = None) -> dict[str, float]:
    prices = prices or load_prices()
    table = prices.get("models", {})
    if model in table:
        return table[model]
    return prices.get("default", {"input": 1.0, "output": 3.0})


def estimate_cost(model: str, input_tokens: int, output_tokens: int,
                  prices: dict[str, Any] | None = None) -> float:
    p = price_for(model, prices)
    return (input_tokens / 1_000_000.0) * float(p["input"]) + \
           (output_tokens / 1_000_000.0) * float(p["output"])


def _ensure_header(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with open(path, "w", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=COST_COLUMNS).writeheader()


def aggregate(store_path: str | Path) -> dict[str, Any]:
    """Summarize the bookkeeping store: totals, per-model, and per-role.

    Returns a dict of pandas objects and scalars for reporting.
    """
    import pandas as pd
    p = Path(store_path)
    if not (p.exists() and p.stat().st_size > 0):
        return {"empty": True}
    df = pd.read_csv(p)
    per_model = df.groupby("model").agg(
        calls=("est_cost_usd", "size"),
        input_tokens=("input_tokens", "sum"),
        output_tokens=("output_tokens", "sum"),
        cost_usd=("est_cost_usd", "sum"),
        mean_latency_s=("latency_s", "mean"),
    ).reset_index()
    per_role = df.groupby("role").agg(
        calls=("est_cost_usd", "size"),
        cost_usd=("est_cost_usd", "sum"),
    ).reset_index()
    # A judge evaluation is any call in which a judge model scored a passage.
    # In this study the full-data judge scoring is logged under the "prompt_eng"
    # role (the best prompt version's scores are reused as the final judge
    # scores) and the "judge" role holds only the handful of connectivity smoke
    # calls. Both are genuine judge evaluations, so the per-evaluation cost must
    # divide the total spend by every judge-scoring call, not by the smoke calls
    # alone. Counting only role=="judge" divided by ~14 smoke rows and produced a
    # wildly inflated figure that did not match the per-model table.
    judge_roles = ("judge", "prompt_eng")
    n_judge_evals = int(df["role"].isin(judge_roles).sum())
    total_cost = float(df["est_cost_usd"].sum())
    return {
        "empty": False,
        "total_calls": int(len(df)),
        "total_input_tokens": int(df["input_tokens"].sum()),
        "total_output_tokens": int(df["output_tokens"].sum()),
        "total_cost_usd": total_cost,
        "n_judge_evals": n_judge_evals,
        "cost_per_judge_eval": (total_cost / n_judge_evals)
                               if n_judge_evals else float("nan"),
        "api_usage_share": float((df["token_source"] == "api").mean()),
        "per_model": per_model,
        "per_role": per_role,
    }


def project_cost(cfg: dict[str, Any], prices: dict[str, Any] | None = None
                 ) -> dict[str, Any]:
    """Pre-run planning estimate of call volume and dollar cost by stage.

    Uses assumed per-call token counts from config, so it does not need any API
    calls. The pipeline is: persona panel over every passage, then a full-data
    prompt-engineering experiment where each judge model scores every passage at
    each prompt version. The best version's scores are reused as the model's
    final judge scores, so there is NO separate final judge run. A small
    fraction of calls trigger a cheap JSON-repair call.
    """
    prices = prices or load_prices()
    plan = cfg["cost_planning"]
    tin, tout = plan["assumed_input_tokens"], plan["assumed_output_tokens"]
    repair_rate = float(plan.get("assumed_repair_rate", 0.0))
    n_items = cfg["dataset"]["n_items"]
    judge_models = cfg["judge"]["models"]
    persona_model = cfg["personas"]["model"]
    raters = cfg["personas"]["raters_per_item"]
    versions = cfg["prompt_eng"].get("max_iters", 1)
    repair_model = cfg.get("repair", {}).get("model", persona_model)

    def call_cost(model: str) -> float:
        return estimate_cost(model, tin, tout, prices)

    # Human panel: every passage rated by `raters` personas, all on the persona
    # model.
    human_calls = n_items * raters
    human_cost = human_calls * call_cost(persona_model)

    # Judge scoring == full-data prompt engineering: each judge model scores
    # every passage at each of `versions` prompt versions. This is an upper
    # bound; the loop can stop a model early once it plateaus.
    judge_calls = len(judge_models) * versions * n_items
    judge_cost = sum(versions * n_items * call_cost(m) for m in judge_models)

    # JSON-repair fallback: a small fraction of all model calls fail to parse
    # and are re-sent to a cheap repair model.
    repair_calls = int(round(repair_rate * (human_calls + judge_calls)))
    repair_cost = repair_calls * call_cost(repair_model)

    total_calls = human_calls + judge_calls + repair_calls
    total_cost = human_cost + judge_cost + repair_cost

    # Per-item, single-version, blended judge cost, for at-scale extrapolation.
    per_item_one_version = sum(call_cost(m) for m in judge_models) / len(judge_models)
    return {
        "assumed_input_tokens": tin,
        "assumed_output_tokens": tout,
        "assumed_repair_rate": repair_rate,
        "n_items": n_items,
        "versions_per_model": versions,
        "human_panel": {"calls": human_calls, "cost_usd": human_cost},
        "prompt_eng_full_data": {"calls": judge_calls, "cost_usd": judge_cost},
        "json_repair": {"calls": repair_calls, "cost_usd": repair_cost},
        "total_calls": total_calls,
        "total_cost_usd": total_cost,
        "judge_cost_per_model_all_versions": {
            m: versions * n_items * call_cost(m) for m in judge_models},
        "judge_cost_per_item_per_version_blended": per_item_one_version,
        "judge_per_1k_items_one_version_blended": per_item_one_version * 1_000,
        "judge_per_1m_items_one_version_blended": per_item_one_version * 1_000_000,
    }


def log_call(store_path: str | Path, *, role: str, model: str,
             prompt_version: str, item_id: str, input_tokens: int,
             output_tokens: int, token_source: str, latency_s: float,
             ok: bool, persona_id: str = "",
             prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append one bookkeeping row and return it."""
    path = Path(store_path)
    total = int(input_tokens) + int(output_tokens)
    cost = estimate_cost(model, input_tokens, output_tokens, prices)
    row = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": model,
        "prompt_version": prompt_version,
        "item_id": item_id,
        "persona_id": persona_id,
        "input_tokens": int(input_tokens),
        "output_tokens": int(output_tokens),
        "total_tokens": total,
        "token_source": token_source,
        "est_cost_usd": round(cost, 8),
        "latency_s": round(float(latency_s), 3),
        "ok": 1 if ok else 0,
    }
    with _BOOK_LOCK:
        _ensure_header(path)
        with open(path, "a", newline="", encoding="utf-8") as fh:
            csv.DictWriter(fh, fieldnames=COST_COLUMNS).writerow(row)
    return row
