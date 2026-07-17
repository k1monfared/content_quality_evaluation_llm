"""Bounded concurrent execution for the API stages.

The persona ratings and judge evaluations are independent per item and per
model, and temperature 0 makes them order-independent, so we can run them in a
thread pool without affecting results or reproducibility. The CSV stores are
made thread-safe by locks in store.py and bookkeeping.py, and the caller
deduplicates by call key before submitting, so nothing is double called or
double charged. Retry and exponential backoff live in the API client.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Iterable


def map_concurrent(fn: Callable[[Any], Any], tasks: Iterable[Any],
                   max_workers: int = 10) -> list[Any]:
    """Run fn over tasks in a bounded thread pool, returning results.

    Exceptions in a task are captured and returned in place of a result so one
    failure never aborts the whole batch.
    """
    tasks = list(tasks)
    if not tasks:
        return []
    results: list[Any] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(fn, t) for t in tasks]
        for fut in as_completed(futures):
            try:
                results.append(fut.result())
            except Exception as e:  # keep going, record the error
                results.append({"error": str(e)})
    return results
