"""Build the robustly connected, balanced two-rater assignment and report it.

Items are dealt round-robin over all 45 persona pairs, so the co-rating graph
is complete and every persona pair shares about a dozen items.

Usage:
    python scripts/build_assignment.py
"""
import json

import _bootstrap  # noqa: F401
from src import assignment, data
from src.config import load_config, resolve_path


def main():
    cfg = load_config()
    items = data.load_sample(resolve_path(cfg["paths"]["data_csv"]))
    n_personas = cfg["personas"]["count"]
    assign = assignment.build_assignment(items["item_id"].tolist(), n_personas)
    out = resolve_path(cfg["paths"]["assignment_csv"])
    out.parent.mkdir(parents=True, exist_ok=True)
    assign.to_csv(out, index=False)
    summary = assignment.design_summary(assign, n_personas)
    print(f"Wrote assignment for {len(assign)} items to {out}")
    print("Design summary:")
    print(json.dumps(summary, indent=2))
    if not summary["connected"]:
        raise SystemExit("ERROR: assignment graph is not connected.")


if __name__ == "__main__":
    main()
