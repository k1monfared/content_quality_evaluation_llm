"""Phase B: full-data, diagnosis-driven prompt engineering (at least 4 per model).

For each judge model the loop:
  1. starts from a minimal base prompt (version v1),
  2. scores EVERY evaluated passage at this version and caches the evaluations,
  3. compares the judge scores to the simulated human panel over the full data
     and diagnoses the failure modes (level bias, spread, length sensitivity,
     correlation with the human overall),
  4. appends targeted corrective guidance to produce the next version,
  5. repeats up to the version cap (at least four versions, v1..v4).

Every version is saved under prompts/judge/<model>/ and scored on the full
dataset. The per-version results table is written and printed, and each model's
best version (highest full-data correlation with the human overall) is recorded.
Those best-version scores are the model's final judge scores, so no separate
final judge run is needed.

Usage:
    python scripts/run_prompt_eng.py
    python scripts/run_prompt_eng.py --mock
"""
import argparse
import json

import _bootstrap  # noqa: F401
import pandas as pd

from src import data, normalize, prompt_tuning, store
from src.config import REPO_ROOT, load_config, load_prices, resolve_path
from src.poe_client import PoeClient


def human_target(cfg) -> pd.DataFrame:
    """Per-passage mean normalized human overall score (the tuning target)."""
    human = store.load(cfg["paths"]["human_store"])
    human = human[human["ok"] == 1].copy()
    if human.empty:
        raise SystemExit("Human panel is empty. Run run_human_panel.py first.")
    # Tune against the same seeded, noisier panel the analysis uses.
    human = normalize.add_reviewer_noise(human, cfg)
    fit = normalize.fit_additive(human, "overall")
    norm = normalize.normalize_ratings(human, fit, "overall")
    tgt = norm.groupby("item_id")["normalized"].mean().rename("human")
    return tgt.reset_index()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    items = data.load_sample(resolve_path(cfg["paths"]["data_csv"]))
    tgt = human_target(cfg)
    base = (REPO_ROOT / "prompts" / "judge" / "v1" / "default.txt").read_text(encoding="utf-8")

    client = PoeClient(cfg, load_prices(), mock=args.mock)
    prompt_root = REPO_ROOT / "prompts" / "judge"

    history_rows, best_versions = prompt_tuning.engineer_prompts(
        client, cfg, items, tgt, base, prompt_root)

    bv_path = resolve_path(cfg["judge"]["best_versions_file"])
    bv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(bv_path, "w", encoding="utf-8") as fh:
        json.dump(best_versions, fh, indent=2)

    table = pd.DataFrame(history_rows)
    out = resolve_path(cfg["paths"]["results_dir"]) / "prompt_eng_results.csv"
    table.to_csv(out, index=False)
    print("\nPer-version full-data results:")
    print(table.to_string(index=False))
    print(f"\nBest versions: {best_versions}")
    print(f"Wrote {out} and {bv_path}")


if __name__ == "__main__":
    main()
