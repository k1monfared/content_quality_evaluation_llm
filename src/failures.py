"""Dedicated failure store and failure-rate reporting.

Every evaluation that cannot be turned into valid scores, even after the
JSON-repair attempt, is recorded here with enough context to analyze later:
the passage id and an excerpt, the raw response, the model, the prompt version,
and a failure reason (api_error, empty, refusal, no_json, schema_error,
repair_failed). This makes it easy to check whether a category of passage, for
example sensitive material that a model refuses to score, drives the failures.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from . import store

FAILURE_COLUMNS = [
    "timestamp", "role", "model", "prompt_version", "item_id", "persona_id",
    "failure_reason", "raw_response", "text_excerpt",
]


def log_failure(path: str | Path, *, role: str, model: str, prompt_version: str,
                item_id: str, failure_reason: str, raw_response: str,
                persona_id: str = "", text: str = "") -> None:
    row = {
        "timestamp": pd.Timestamp.now().isoformat(timespec="seconds"),
        "role": role, "model": model, "prompt_version": prompt_version,
        "item_id": item_id, "persona_id": persona_id,
        "failure_reason": failure_reason,
        "raw_response": str(raw_response)[:2000],
        "text_excerpt": str(text)[:300],
    }
    store.append_row(path, row, FAILURE_COLUMNS)


def load_failures(cfg: dict[str, Any]) -> pd.DataFrame:
    return store.load(cfg["paths"]["failure_store"])


def failure_report(cfg: dict[str, Any]) -> dict[str, Any]:
    """Failure percentage overall and per model, plus reason and category
    breakdowns. Rates are computed from the evaluation store's ok flag, which
    counts an evaluation as ok if it parsed directly or after repair.
    """
    ev = store.load(cfg["paths"]["eval_store"])
    if ev.empty:
        return {"empty": True}
    ev = ev.copy()
    ev["ok"] = pd.to_numeric(ev["ok"], errors="coerce").fillna(0).astype(int)
    if "repaired" in ev.columns:
        ev["repaired"] = pd.to_numeric(ev["repaired"], errors="coerce").fillna(0).astype(int)
    else:
        ev["repaired"] = 0

    attempts = len(ev)
    ok = int(ev["ok"].sum())
    overall = {
        "attempts": attempts,
        "ok": ok,
        "failures": attempts - ok,
        "failure_rate": round(1 - ok / attempts, 4) if attempts else 0.0,
        "parse_success_rate": round(ok / attempts, 4) if attempts else 0.0,
        "repaired_recoveries": int(ev["repaired"].sum()),
    }
    per_model = ev.groupby("model").agg(
        attempts=("ok", "size"), ok=("ok", "sum"), repaired=("repaired", "sum")
    ).reset_index()
    per_model["failures"] = per_model["attempts"] - per_model["ok"]
    per_model["failure_rate"] = (per_model["failures"] / per_model["attempts"]).round(4)

    fails = store.load(cfg["paths"]["failure_store"])
    reason_counts = {}
    if not fails.empty and "failure_reason" in fails.columns:
        reason_counts = fails.groupby(["model", "failure_reason"]).size().to_dict()
        reason_counts = {f"{k[0]}|{k[1]}": int(v) for k, v in reason_counts.items()}
    return {
        "empty": False,
        "overall": overall,
        "per_model": per_model,
        "reason_counts": reason_counts,
        "n_failed_records": 0 if fails.empty else len(fails),
    }
