"""Phase B: run the simulated human panel.

Each item is rated by its two assigned personas. Every rating is cached in the
human panel store, so this is safe to re-run and resume. Every call is also
logged to the cost bookkeeping store.

Usage:
    python scripts/run_human_panel.py            # full panel
    python scripts/run_human_panel.py --limit 4  # smoke test, first 4 ratings
    python scripts/run_human_panel.py --mock      # no network, deterministic
"""
import argparse

import _bootstrap  # noqa: F401
from src import assignment, data, personas
from src.config import load_config, load_prices, resolve_path
from src.poe_client import PoeClient


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    items = data.load_sample(resolve_path(cfg["paths"]["data_csv"]))
    assign_path = resolve_path(cfg["paths"]["assignment_csv"])
    if not assign_path.exists():
        raise SystemExit("Run scripts/build_assignment.py first.")
    import pandas as pd
    assign = pd.read_csv(assign_path)

    client = PoeClient(cfg, load_prices(), mock=args.mock)
    n = personas.run_panel(client, items, assign, cfg, limit=args.limit)
    print(f"Wrote {n} new persona ratings to {cfg['paths']['human_store']}")


if __name__ == "__main__":
    main()
