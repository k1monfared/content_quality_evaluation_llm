"""The ten biased simulated human raters (the ground-truth panel).

Each persona is a deliberately opinionated grading personality defined by a
prompt file in prompts/personas/. A single low-cost model plays every persona,
so the disagreement between raters comes from the prompts, not from model
identity. Their biases are removed later by the additive normalization step.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from . import failures, repair, rubric, store
from .config import REPO_ROOT
from .poe_client import PoeClient

PERSONA_DIR = REPO_ROOT / "prompts" / "personas"

HUMAN_COLUMNS = [
    "item_id", "persona_id", "persona_key", "model",
    "clarity", "neutrality", "verifiability", "coverage", "structure",
    "readability", "informativeness",
    "overall", "rationale", "ok", "repaired", "timestamp",
]


@dataclass
class Persona:
    pid: int
    key: str
    name: str
    file: str
    bias_hint: str  # short description of the intended bias, for documentation


PERSONAS: list[Persona] = [
    Persona(1, "lenient", "The Encourager", "persona_01_lenient.txt",
            "scores high, gives benefit of the doubt"),
    Persona(2, "harsh", "The Hard Marker", "persona_02_harsh.txt",
            "scores low, hard to impress"),
    Persona(3, "depth", "The Depth Seeker", "persona_03_depth.txt",
            "rewards long, thorough, detailed passages"),
    Persona(4, "brevity", "The Minimalist", "persona_04_brevity.txt",
            "rewards concise passages, punishes rambling"),
    Persona(5, "citations", "The Evidence Stickler", "persona_05_citations.txt",
            "rewards sources and concrete evidence"),
    Persona(6, "anti_hedge", "The Straight Shooter", "persona_06_anti_hedge.txt",
            "dislikes hedging and wants confident direct writing"),
    Persona(7, "grammar", "The Style Editor", "persona_07_grammar.txt",
            "weights writing quality and clarity heavily"),
    Persona(8, "practical", "The Pragmatist", "persona_08_practical.txt",
            "rewards concrete, specific, useful content"),
    Persona(9, "empathy", "The Warm Reader", "persona_09_empathy.txt",
            "rewards tone, warmth, and engagement"),
    Persona(10, "skeptic", "The Fact Checker", "persona_10_skeptic.txt",
            "distrusts confident claims, focuses on accuracy"),
]

BY_ID = {p.pid: p for p in PERSONAS}


def load_persona_prompt(persona: Persona) -> str:
    return (PERSONA_DIR / persona.file).read_text(encoding="utf-8")


def rate_item(client: PoeClient, persona: Persona, model: str,
              item_id: str, text: str,
              cfg: dict[str, Any]) -> dict[str, Any]:
    smin, smax = cfg["rubric"]["scale_min"], cfg["rubric"]["scale_max"]
    system = load_persona_prompt(persona)
    user = (rubric.build_content_block(text) + "\n\n"
            + rubric.rubric_block(smin, smax) + "\n\n"
            + rubric.anchors_block(smin, smax) + "\n\n"
            + rubric.json_instruction())
    res = client.complete(model=model, system=system, user=user,
                          role="human_persona", item_id=item_id,
                          prompt_version="persona", persona_id=str(persona.pid))
    parsed = rubric.parse_rubric(res.text, smin, smax)
    repaired = 0
    if parsed is None:
        fixed = repair.repair_to_json(res.text, client, cfg, item_id=item_id,
                                      model=model, version="persona")
        if fixed is not None:
            parsed, repaired = fixed, 1
    row = {"item_id": item_id, "persona_id": persona.pid, "persona_key": persona.key,
           "model": model, "ok": 1 if parsed else 0, "repaired": repaired,
           "timestamp": pd.Timestamp.now().isoformat(timespec="seconds")}
    if parsed:
        row.update(parsed)
    else:
        failures.log_failure(
            cfg["paths"]["failure_store"], role="human_persona", model=model,
            prompt_version="persona", item_id=item_id, persona_id=str(persona.pid),
            failure_reason=rubric.classify_failure(res.text),
            raw_response=res.text, text=text)
    return row


def run_panel(client: PoeClient, items: pd.DataFrame, assignment: pd.DataFrame,
              cfg: dict[str, Any], limit: int | None = None) -> int:
    """Rate every item with its two assigned personas, caching each rating.

    Runs concurrently with a bounded thread pool. Returns the number of new
    ratings written. Already-cached (item, persona) pairs are skipped before
    submission so the run is resumable and never double charges.
    """
    from . import concurrency
    model = cfg["personas"]["model"]
    path = cfg["paths"]["human_store"]
    done = store.existing_keys(path, ["item_id", "persona_id"])
    items_by_id = {r["item_id"]: r for _, r in items.iterrows()}

    tasks = []
    for _, a in assignment.iterrows():
        item_id = a["item_id"]
        if item_id not in items_by_id:
            continue
        for pid in (int(a["rater_a"]), int(a["rater_b"])):
            if (item_id, str(pid)) in done:
                continue
            tasks.append((item_id, pid))
    if limit is not None:
        tasks = tasks[:limit]

    def work(task):
        item_id, pid = task
        it = items_by_id[item_id]
        row = rate_item(client, BY_ID[pid], model, item_id,
                        str(it["text"]), cfg)
        store.append_row(path, row, HUMAN_COLUMNS)
        return row

    results = concurrency.map_concurrent(work, tasks, cfg["api"].get("max_workers", 10))
    return len([r for r in results if isinstance(r, dict) and "error" not in r])
