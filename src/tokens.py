"""Token counting.

Preferred source is the API response usage block. When usage is missing we
fall back to a lightweight local estimate and mark it as estimated so the
bookkeeping stays honest about where each number came from.
"""
from __future__ import annotations

# Rough characters-per-token ratio for English prose. Good enough for a
# fallback estimate when the API does not return usage. Documented as an
# approximation in the README limitations.
_CHARS_PER_TOKEN = 4.0


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, round(len(text) / _CHARS_PER_TOKEN))


def usage_from_response(response) -> tuple[int, int, str]:
    """Return (input_tokens, output_tokens, source).

    source is "api" when the response carried usage, else "estimated".
    """
    usage = getattr(response, "usage", None)
    if usage is not None:
        pt = getattr(usage, "prompt_tokens", None)
        ct = getattr(usage, "completion_tokens", None)
        if pt is not None and ct is not None:
            return int(pt), int(ct), "api"
    return 0, 0, "estimated"
