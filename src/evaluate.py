"""The judge pipeline.

evaluate(content, model, version) is the single entry point. It sends the
content to a model with that model's current prompt, parses the JSON reply
into rubric fields, appends the record to the judge CSV database, and returns
it. Every evaluation is persisted, so re-runs reuse cached results and cost
nothing. Temperature is 0 (set in config) for reproducibility.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from . import failures, repair, rubric, store
from .config import REPO_ROOT
from .poe_client import PoeClient

JUDGE_PROMPT_DIR = REPO_ROOT / "prompts" / "judge"

EVAL_COLUMNS = [
    "item_id", "model", "prompt_version",
    "clarity", "neutrality", "verifiability", "coverage", "structure",
    "readability", "informativeness",
    "overall", "rationale", "ok", "repaired", "timestamp",
]


def load_judge_prompt(model: str, version: str, prompt_dir=None) -> str:
    """Return the system prompt for a model and version.

    Preferred layout is per-model iterative versions at
    <prompt_dir>/<model>/<version>.txt (written by the tuning loop). Falls back
    to the shared layout <prompt_dir>/<version>/<model or default>.txt.
    prompt_dir defaults to prompts/judge under the repo root; it can be
    redirected (for example to a throwaway directory in a dry run).
    """
    base = Path(prompt_dir) if prompt_dir else JUDGE_PROMPT_DIR
    per_model = base / model / f"{version}.txt"
    if per_model.exists():
        return per_model.read_text(encoding="utf-8")
    specific = base / version / f"{model}.txt"
    default = base / version / "default.txt"
    if specific.exists():
        return specific.read_text(encoding="utf-8")
    if default.exists():
        return default.read_text(encoding="utf-8")
    raise FileNotFoundError(f"No judge prompt for model={model} version={version}")


def evaluate(content: dict[str, Any], model: str, version: str,
             client: PoeClient, cfg: dict[str, Any],
             role: str = "judge", persist: bool = True,
             prompt_dir=None) -> dict[str, Any]:
    """Evaluate one passage with one model and prompt version."""
    system = load_judge_prompt(model, version, prompt_dir)
    user = rubric.build_user_prompt(str(content["text"]),
                                    cfg["rubric"]["scale_min"], cfg["rubric"]["scale_max"])
    res = client.complete(model=model, system=system, user=user, role=role,
                          item_id=str(content["item_id"]), prompt_version=version)
    smin, smax = cfg["rubric"]["scale_min"], cfg["rubric"]["scale_max"]
    parsed = rubric.parse_rubric(res.text, smin, smax)
    repaired = 0
    if parsed is None:
        fixed = repair.repair_to_json(res.text, client, cfg,
                                      item_id=str(content["item_id"]),
                                      model=model, version=version)
        if fixed is not None:
            parsed, repaired = fixed, 1
    row = {"item_id": content["item_id"], "model": model, "prompt_version": version,
           "ok": 1 if parsed else 0, "repaired": repaired,
           "timestamp": pd.Timestamp.now().isoformat(timespec="seconds")}
    if parsed:
        row.update(parsed)
    elif persist:
        failures.log_failure(
            cfg["paths"]["failure_store"], role=role, model=model,
            prompt_version=version, item_id=str(content["item_id"]),
            failure_reason=rubric.classify_failure(res.text),
            raw_response=res.text, text=str(content.get("text", "")))
    if persist:
        store.append_row(cfg["paths"]["eval_store"], row, EVAL_COLUMNS)
    return row


def run_judges(client: PoeClient, items: pd.DataFrame, cfg: dict[str, Any],
               models: list[str] | None = None, version: str | None = None,
               limit: int | None = None) -> int:
    """Run each model over every item at one prompt version, concurrently.

    Cached (item, model, version) keys are skipped before submission, so the
    run is resumable and never double charges.
    """
    from . import concurrency
    models = models or cfg["judge"]["models"]
    version = version or cfg["judge"]["best_prompt_version"]
    path = cfg["paths"]["eval_store"]
    done = store.existing_keys(path, ["item_id", "model", "prompt_version"])

    tasks = []
    for model in models:
        for _, it in items.iterrows():
            if (str(it["item_id"]), model, version) in done:
                continue
            tasks.append((model, {"item_id": it["item_id"],
                                  "text": it["text"]}))
    if limit is not None:
        tasks = tasks[:limit]

    def work(task):
        model, content = task
        return evaluate(content, model, version, client, cfg)

    results = concurrency.map_concurrent(work, tasks, cfg["api"].get("max_workers", 10))
    return len([r for r in results if isinstance(r, dict) and "error" not in r])
