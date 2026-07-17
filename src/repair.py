"""JSON-repair fallback.

When a rater or judge reply cannot be parsed or validated, we do not throw it
away immediately. We send only the raw reply text to a cheap model (a Claude
Haiku on Poe) and ask it to reformat it into schema-correct JSON, then parse
that. If it still fails, the caller marks the item as failed. The repair call
is logged in the cost ledger like any other call (role "json_repair").
"""
from __future__ import annotations

from typing import Any

from . import rubric
from .poe_client import PoeClient

_SYSTEM = (
    "You convert another model's reply into a single valid JSON object that "
    "matches a required schema. Output only the JSON object and nothing else. "
    "Preserve the original scores and reasons wherever they appear in the "
    "reply. If a required field is genuinely missing, infer the most reasonable "
    "value from the text. Every score must be an integer from 1 to 10."
)


def repair_to_json(raw_text: str, client: PoeClient, cfg: dict[str, Any], *,
                   item_id: str, model: str, version: str) -> dict[str, Any] | None:
    """Try to recover a valid rubric record from an unparseable reply."""
    rcfg = cfg.get("repair", {})
    if not rcfg.get("enabled", False) or client.mock:
        return None
    if not raw_text or raw_text.startswith("__ERROR__"):
        return None
    repair_model = rcfg.get("model", "claude-haiku-4.5")
    user = ("Required JSON schema:\n" + rubric.json_instruction()
            + "\n\nReply to convert into that schema:\n" + str(raw_text)[:4000])
    res = client.complete(model=repair_model, system=_SYSTEM, user=user,
                          role="json_repair", item_id=item_id,
                          prompt_version=version)
    return rubric.parse_rubric(res.text, cfg["rubric"]["scale_min"],
                               cfg["rubric"]["scale_max"])
