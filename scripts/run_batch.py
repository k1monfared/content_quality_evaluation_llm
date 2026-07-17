"""Example: score a batch of content with the adaptive concurrent entry point.

This shows the interface a future user would call. It reads the content
sample, then hands the whole batch to evaluate_batch, which parallelizes the
calls, adapts to rate limiting, and caches every result so re-runs are cheap.

Usage:
    python scripts/run_batch.py                    # default concurrency
    python scripts/run_batch.py --max-workers 24   # push harder
    python scripts/run_batch.py --model gpt-5.2 --limit 20
    python scripts/run_batch.py --mock             # no network
"""
import argparse
import json

import _bootstrap  # noqa: F401
from src import batch, data
from src.config import load_config, load_prices, resolve_path
from src.poe_client import PoeClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=None,
                    help="model to run (repeatable); default is all judge models")
    ap.add_argument("--version", default=None)
    ap.add_argument("--max-workers", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    items = data.load_sample(resolve_path(cfg["paths"]["data_csv"]))
    if args.limit:
        items = items.head(args.limit)
    models = args.model or cfg["judge"]["models"]
    client = PoeClient(cfg, load_prices(), mock=args.mock)

    summary = batch.evaluate_batch(items, models, cfg, client=client,
                                   version=args.version or cfg["judge"]["best_prompt_version"],
                                   max_workers=args.max_workers)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
