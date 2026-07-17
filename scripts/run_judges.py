"""Optional: run every judge model over every passage at its best prompt version.

With the full-data prompt-engineering stage, every model already scores every
passage at each version, so the best version's scores are the final scores and
this script is normally unnecessary. It remains as a convenience to (re)fill any
missing best-version evaluations. Reads each model's selected best version from
the file written by run_prompt_eng.py. Cached and resumable: passages already
scored at the best version are reused. Every call is logged to the cost store.

Usage:
    python scripts/run_judges.py
    python scripts/run_judges.py --limit 8
    python scripts/run_judges.py --mock
"""
import argparse
import json

import _bootstrap  # noqa: F401
from src import data, evaluate
from src.config import load_config, load_prices, resolve_path
from src.poe_client import PoeClient


def best_versions(cfg) -> dict:
    path = resolve_path(cfg["judge"]["best_versions_file"])
    if path.exists():
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    return {m: cfg["judge"]["best_prompt_version"] for m in cfg["judge"]["models"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    items = data.load_sample(resolve_path(cfg["paths"]["data_csv"]))
    client = PoeClient(cfg, load_prices(), mock=args.mock)
    versions = best_versions(cfg)

    total = 0
    for model in cfg["judge"]["models"]:
        version = versions.get(model, cfg["judge"]["best_prompt_version"])
        n = evaluate.run_judges(client, items, cfg, models=[model],
                                version=version, limit=args.limit)
        print(f"{model} @ {version}: {n} new evaluations")
        total += n
    print(f"Total new judge evaluations: {total} "
          f"(store: {cfg['paths']['eval_store']})")


if __name__ == "__main__":
    main()
