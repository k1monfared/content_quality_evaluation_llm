"""Shared rubric definition, prompt construction, and response parsing.

Both the simulated human personas and the judge models are asked for the same
JSON shape, so their scores land in the same columns and are directly
comparable. Every dimension is scored by first writing a short reason and then
an integer score from 1 to 10, in that order, so the reasoning is generated
before the number. The scale anchors are stated in the prompt so every rater
uses the same meaning of 1 and 10.

The item under review is a single encyclopedic passage (a Wikipedia paragraph).
The rubric measures the writing quality of that passage. The dimensions are
grounded in Wikipedia's own content policies and quality criteria (neutral
point of view, verifiability, comprehensiveness, clear well structured prose).
Some overlap between dimensions is expected: a later research phase prunes the
redundant or unhelpful ones. The overall score is a direct, independent
holistic judgment from each rater and is deliberately NOT computed as the
average of the dimension scores, so downstream analysis can compare it against
computed composites of the dimensions.
"""
from __future__ import annotations

import json
import re
from typing import Any

# Seven candidate dimensions for encyclopedic paragraph writing quality. Each
# is scored on an integer 1 to 10 scale. Overlap is intentional at this stage.
DIMENSIONS = [
    "clarity",
    "neutrality",
    "verifiability",
    "coverage",
    "structure",
    "readability",
    "informativeness",
]
ALL_FIELDS = DIMENSIONS + ["overall"]

DIMENSION_HELP = {
    "clarity": "Is the passage clear and easy to understand on a first read?",
    "neutrality": "Is the tone neutral and impartial, free of bias, promotion, "
                  "or editorializing?",
    "verifiability": "Do the claims appear sourced or attributable rather than "
                     "unsupported assertions?",
    "coverage": "Does the passage cover its topic adequately for its scope, "
                "without obvious gaps?",
    "structure": "Is it well organized and coherent, with ideas that flow "
                 "logically?",
    "readability": "Is the language fluent, grammatical, and in an appropriate "
                   "encyclopedic register?",
    "informativeness": "Does it convey substantive, useful information "
                       "efficiently rather than padding or triviality?",
}

DIMENSION_ENDPOINTS = {
    "clarity": "1 = confusing or impenetrable, 10 = immediately clear and easy "
               "to follow.",
    "neutrality": "1 = heavily biased or promotional, 10 = strictly neutral and "
                  "impartial.",
    "verifiability": "1 = unsupported or unverifiable claims, 10 = claims "
                     "clearly attributable to sources or concrete evidence.",
    "coverage": "1 = superficial or fragmentary, 10 = thorough and well rounded "
                "for its scope.",
    "structure": "1 = disjointed or incoherent, 10 = tightly organized and "
                 "coherent.",
    "readability": "1 = clumsy, ungrammatical, or awkward prose, 10 = fluent, "
                   "polished, encyclopedic prose.",
    "informativeness": "1 = vacuous or trivial, 10 = dense with relevant, "
                       "useful information.",
}


def rubric_block(scale_min: int = 1, scale_max: int = 10) -> str:
    lines = ["Score these dimensions of the passage plus an overall score:"]
    for d in DIMENSIONS:
        lines.append(f"  - {d}: {DIMENSION_HELP[d]}")
    lines.append("  - overall: your holistic judgment of the passage's writing "
                 "quality, decided directly and NOT as the average of the "
                 "dimensions above.")
    return "\n".join(lines)


def anchors_block(scale_min: int = 1, scale_max: int = 10) -> str:
    lines = [
        f"Every score is an integer from {scale_min} to {scale_max} on this scale:",
        f"  {scale_min} = unusable: incoherent, empty, or clearly not "
        "encyclopedic writing.",
        "  4 = poor to fair: readable but with real problems in clarity, "
        "neutrality, sourcing, or coverage.",
        "  7 = good: clear, neutral, and informative encyclopedic writing with "
        "only minor shortcomings.",
        f"  {scale_max} = exceptional: clear, neutral, well sourced, "
        "comprehensive, and polished.",
        "Per-dimension meaning of the endpoints:",
    ]
    for d in DIMENSIONS:
        lines.append(f"  - {d}: {DIMENSION_ENDPOINTS[d]}")
    return "\n".join(lines)


def json_instruction() -> str:
    example = {d: {"reason": "<short reason>", "score": "<integer 1 to 10>"}
               for d in DIMENSIONS}
    example["overall"] = {"reason": "<short reason>", "score": "<integer 1 to 10>"}
    shape = json.dumps(example, indent=2)
    return (
        "Respond with a single JSON object and nothing else. For every "
        "dimension and for overall, write a brief reason FIRST and then the "
        "integer score, in that order, as an object with keys \"reason\" then "
        "\"score\". Use this exact shape:\n" + shape
    )


def build_content_block(text: str) -> str:
    return f"PASSAGE TO EVALUATE:\n{text}"


def build_user_prompt(text: str, scale_min: int = 1,
                      scale_max: int = 10) -> str:
    return (build_content_block(text) + "\n\n"
            + rubric_block(scale_min, scale_max) + "\n\n"
            + anchors_block(scale_min, scale_max) + "\n\n"
            + json_instruction())


def _extract_score(value: Any) -> tuple[float | None, str]:
    """Return (score, reason) from either a nested object or a bare number."""
    if isinstance(value, dict):
        if "score" not in value:
            return None, ""
        raw = value["score"]
        reason = str(value.get("reason", ""))
    else:
        raw = value
        reason = ""
    try:
        return float(raw), reason
    except (TypeError, ValueError):
        return None, reason


def parse_rubric(text: str, scale_min: int = 1, scale_max: int = 10
                 ) -> dict[str, Any] | None:
    """Extract rubric scores from a model reply.

    Accepts the nested reason-then-score objects and also tolerates a flat
    number per field (useful for repaired responses). Returns None if it cannot
    be parsed or a required field is missing.
    """
    if not text or text.startswith("__ERROR__"):
        return None
    match = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    parsed: dict[str, Any] = {}
    overall_reason = ""
    for f in ALL_FIELDS:
        if f not in obj:
            return None
        score, reason = _extract_score(obj[f])
        if score is None:
            return None
        parsed[f] = max(scale_min, min(scale_max, score))
        if f == "overall":
            overall_reason = reason
    parsed["rationale"] = str(overall_reason)[:300]
    return parsed


def classify_failure(text: str) -> str:
    """Label why a response could not be turned into scores."""
    if text is None:
        return "empty"
    if text.startswith("__ERROR__"):
        return "api_error"
    if not text.strip():
        return "empty"
    low = text.lower()
    has_json = bool(re.search(r"\{.*\}", text, re.DOTALL))
    refusal_markers = ["i can't", "i cannot", "i'm unable", "i am unable",
                       "cannot assist", "can't help", "not able to",
                       "i won't", "i will not"]
    if not has_json:
        if any(m in low for m in refusal_markers):
            return "refusal"
        return "no_json"
    return "schema_error"
