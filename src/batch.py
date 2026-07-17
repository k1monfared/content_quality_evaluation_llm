"""Batch judging entry point with adaptive concurrency.

This is the clean interface a future user calls to score a whole batch of
content in parallel. It layers three things on top of evaluate():

  1. Configurable concurrency. The default leaves headroom at
     max(1, cpu_count - 1). For these API calls the practical limit is
     usually the provider rate limit rather than the number of cores, so a user
     can safely set max_workers much higher (dozens) or lower.
  2. Adaptive slowdown. When the provider starts rate limiting (HTTP 429 or
     throttling), the controller shrinks the number of in-flight calls and adds
     exponential backoff, then recovers the concurrency gradually as calls
     start succeeding again. A rate-limited user degrades gracefully instead of
     failing.
  3. Thread-safe, deduplicated caching (inherited from evaluate() and the
     locked CSV stores), so a re-run only calls the items not already done and
     never double charges.
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import pandas as pd

from . import evaluate, store
from .poe_client import PoeClient


def default_max_workers() -> int:
    """Sensible default that leaves one core of headroom."""
    return max(1, (os.cpu_count() or 2) - 1)


class AdaptiveLimiter:
    """Gates the number of concurrent API calls and adapts to rate limiting.

    The worker pool may hold up to `hard_cap` threads, but only `current`
    of them may be making a call at once. On a rate-limit signal `current` is
    halved (down to 1) and a growing backoff is returned to the caller. After a
    streak of successes `current` grows back one step at a time toward the cap.
    """

    def __init__(self, hard_cap: int, recover_after: int = 8):
        self.hard_cap = max(1, hard_cap)
        self.current = self.hard_cap
        self.min_limit = 1
        self.recover_after = recover_after
        self._active = 0
        self._success_streak = 0
        self._rate_events = 0
        self._cond = threading.Condition()

    def acquire(self) -> None:
        with self._cond:
            while self._active >= self.current:
                self._cond.wait()
            self._active += 1

    def release(self) -> None:
        with self._cond:
            self._active -= 1
            self._cond.notify_all()

    def on_rate_limit(self) -> float:
        """Shrink concurrency and return extra seconds to back off."""
        with self._cond:
            self._rate_events += 1
            self._success_streak = 0
            self.current = max(self.min_limit, self.current // 2)
            self._cond.notify_all()
            # extra backoff grows with how many times we have been throttled
            return float(min(2 ** self._rate_events, 30))

    def on_success(self) -> None:
        with self._cond:
            self._success_streak += 1
            if self._success_streak >= self.recover_after and self.current < self.hard_cap:
                self.current += 1
                self._success_streak = 0
                self._cond.notify_all()

    def snapshot(self) -> dict[str, int]:
        with self._cond:
            return {"current_limit": self.current, "hard_cap": self.hard_cap,
                    "rate_limit_events": self._rate_events}


def evaluate_batch(items, models: list[str], cfg: dict[str, Any],
                   client: PoeClient | None = None, version: str | None = None,
                   max_workers: int | None = None, role: str = "judge",
                   prices: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score a batch of content items with one or more models, in parallel.

    Parameters
    ----------
    items : DataFrame or list of dicts, each with item_id and text.
    models : list of model names to run.
    cfg : loaded config.
    client : optional PoeClient. If omitted, one is created.
    version : prompt version to use. Defaults to config best_prompt_version.
    max_workers : concurrency cap. Defaults to max(1, cpu_count - 1). For API
        calls the real limit is usually the provider rate limit, so feel free
        to raise this well above core count, or lower it if you get throttled.
    role : bookkeeping role label for the calls.

    Returns a summary dict with counts and the final adaptive limiter state.
    Results themselves are cached to the CSV stores by evaluate().
    """
    if isinstance(items, pd.DataFrame):
        records = items.to_dict("records")
    else:
        records = list(items)
    version = version or cfg["judge"]["best_prompt_version"]
    workers = max_workers or default_max_workers()
    client = client or PoeClient(cfg, prices)

    # dedup against the cache before submitting anything
    done = store.existing_keys(cfg["paths"]["eval_store"],
                               ["item_id", "model", "prompt_version"])
    tasks = []
    for model in models:
        for rec in records:
            if (str(rec["item_id"]), model, version) in done:
                continue
            tasks.append((model, rec))

    limiter = AdaptiveLimiter(workers)
    client.on_rate_limit = limiter.on_rate_limit
    client.on_success = limiter.on_success

    written = 0
    lock = threading.Lock()

    def work(task):
        nonlocal written
        model, rec = task
        content = {"item_id": rec["item_id"], "text": rec["text"]}
        limiter.acquire()
        try:
            row = evaluate.evaluate(content, model, version, client, cfg, role=role)
        finally:
            limiter.release()
        if row.get("ok"):
            with lock:
                written += 1
        return row

    if tasks:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(work, t) for t in tasks]
            for _ in as_completed(futures):
                pass

    # detach hooks so the client can be reused elsewhere
    client.on_rate_limit = None
    client.on_success = None
    return {
        "requested": len(records) * len(models),
        "submitted": len(tasks),
        "written_ok": written,
        "skipped_cached": len(records) * len(models) - len(tasks),
        "limiter": limiter.snapshot(),
    }
