"""Project the cost and call volume of the full study before running it.

Uses only the config and the price table. No API calls. Covers the persona
panel, the full-data prompt-engineering experiment (which doubles as the final
judge scoring), and the JSON-repair fallback, plus a per-item at-scale judge
projection (per 1,000 and per 1,000,000 items, single prompt version).

Usage:
    python scripts/estimate_cost.py
"""
import json

import _bootstrap  # noqa: F401
from src import bookkeeping
from src.config import load_config, load_prices


def main():
    cfg = load_config()
    prices = load_prices()
    proj = bookkeeping.project_cost(cfg, prices)
    print("Planned call volume and estimated cost (planning assumptions):\n")
    print(json.dumps(proj, indent=2))
    print("\nNote: dollar figures use the configurable price table in "
          "configs/prices.yaml. Poe bills in compute points, so treat these "
          "as planning approximations.")


if __name__ == "__main__":
    main()
