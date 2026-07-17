"""Thin wrapper over the Poe OpenAI-compatible Chat Completions endpoint.

Responsibilities:
  - one place that talks to the network, with retries and temperature 0
  - measure latency and capture token usage from the response
  - write one bookkeeping row per call, for every role, always
  - offer a deterministic mock mode so the whole pipeline can be dry-run and
    unit-checked without spending a cent

Parsing of the model output into rubric fields lives in evaluate.py, because
that logic is shared by the judge and the personas.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from . import bookkeeping, tokens
from .config import get_api_key, load_prices


def _is_rate_limit(exc: Exception) -> bool:
    """Best-effort detection of provider rate limiting or throttling."""
    if exc.__class__.__name__ in ("RateLimitError", "APIStatusError"):
        code = getattr(exc, "status_code", None)
        if code == 429:
            return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg \
        or "overloaded" in msg


@dataclass
class ChatResult:
    text: str
    input_tokens: int
    output_tokens: int
    token_source: str
    latency_s: float
    ok: bool


class PoeClient:
    def __init__(self, cfg: dict[str, Any], prices: dict[str, Any] | None = None,
                 mock: bool = False):
        self.cfg = cfg
        self.prices = prices or load_prices()
        self.mock = mock
        self.book_path = cfg["paths"]["bookkeeping_store"]
        api = cfg.get("api", {})
        self.temperature = api.get("temperature", 0)
        self.max_tokens = api.get("max_tokens", 700)
        self.timeout = api.get("request_timeout_s", 90)
        self.max_retries = api.get("max_retries", 4)
        # Optional hooks for an adaptive concurrency controller (see batch.py).
        # on_rate_limit() may return extra seconds to sleep; on_success() is
        # called after each successful call so the controller can recover.
        self.on_rate_limit = None
        self.on_success = None
        self._client = None
        if not mock:
            import openai  # imported lazily so mock/dry runs need no network stack
            key = get_api_key(cfg)
            if not key:
                raise SystemExit(
                    "No Poe API key found. Set POE_API_KEY or use mock mode."
                )
            self._client = openai.OpenAI(
                api_key=key, base_url=api.get("base_url", "https://api.poe.com/v1")
            )

    # -- public ---------------------------------------------------------------
    def complete(self, *, model: str, system: str, user: str, role: str,
                 item_id: str, prompt_version: str, persona_id: str = "",
                 log: bool = True) -> ChatResult:
        """Run one completion and record it in the bookkeeping store."""
        t0 = time.time()
        if self.mock:
            res = self._mock_complete(model, system, user, t0)
        else:
            res = self._real_complete(model, system, user, t0)
        if log:
            bookkeeping.log_call(
                self.book_path, role=role, model=model,
                prompt_version=prompt_version, item_id=item_id,
                persona_id=persona_id, input_tokens=res.input_tokens,
                output_tokens=res.output_tokens, token_source=res.token_source,
                latency_s=res.latency_s, ok=res.ok, prices=self.prices,
            )
        return res

    # -- internals ------------------------------------------------------------
    def _real_complete(self, model: str, system: str, user: str,
                       t0: float) -> ChatResult:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                resp = self._client.chat.completions.create(
                    model=model, messages=messages,
                    temperature=self.temperature, max_tokens=self.max_tokens,
                    timeout=self.timeout,
                )
                text = resp.choices[0].message.content or ""
                in_tok, out_tok, source = tokens.usage_from_response(resp)
                if source == "estimated":
                    in_tok = tokens.estimate_tokens(system + "\n" + user)
                    out_tok = tokens.estimate_tokens(text)
                if self.on_success:
                    self.on_success()
                return ChatResult(text, in_tok, out_tok, source,
                                  time.time() - t0, True)
            except Exception as e:  # broad on purpose: retry any transient error
                last_err = e
                backoff = min(2 ** attempt, 15)
                # On rate limiting, let an adaptive controller shrink the active
                # concurrency and add extra delay before we retry.
                if _is_rate_limit(e) and self.on_rate_limit:
                    extra = self.on_rate_limit() or 0
                    backoff += extra
                time.sleep(backoff)
        # all retries failed: record an estimated, failed call
        in_tok = tokens.estimate_tokens(system + "\n" + user)
        return ChatResult(f"__ERROR__: {last_err}", in_tok, 0, "estimated",
                          time.time() - t0, False)

    def _mock_complete(self, model: str, system: str, user: str,
                       t0: float) -> ChatResult:
        """Deterministic fake judgment so dry runs exercise the full pipeline.

        Scores are a stable function of the content hash and the model/persona
        name, so different raters disagree and re-runs are identical. The
        overall score is drawn from its own slice of the hash, independent of
        the dimension scores, mirroring the real rubric where overall is a
        direct holistic judgment and not the average of the dimensions.
        """
        from . import rubric  # local import avoids any import cycle
        seed = int(hashlib.sha256((model + system + user).encode()).hexdigest(), 16)
        dims = rubric.DIMENSIONS
        obj = {}
        for i, d in enumerate(dims):
            s = 1 + (seed >> (i * 5)) % 10
            obj[d] = {"reason": "mock reason", "score": s}
        # Independent draw for overall, not a function of the dimension scores.
        overall = 1 + (seed >> 61) % 10
        obj["overall"] = {"reason": "mock overall reason", "score": overall}
        text = json.dumps(obj)
        in_tok = tokens.estimate_tokens(system + "\n" + user)
        out_tok = tokens.estimate_tokens(text)
        return ChatResult(text, in_tok, out_tok, "estimated",
                          time.time() - t0, True)
