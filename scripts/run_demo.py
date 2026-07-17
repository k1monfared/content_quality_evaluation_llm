"""Phase A dry run: exercise the entire pipeline end to end with NO network.

Uses the deterministic mock client, writing to a throwaway outputs/_dryrun/
directory (and a throwaway prompt directory) so the real stores and versioned
prompts stay clean. It verifies, in the real run order:
  - the robustly connected round-robin assignment over all 45 persona pairs
    (reported on the full committed sample so the real min-shared count shows)
  - persona panel rating and caching on Wikipedia passages
  - the full-data, diagnosis-driven prompt-engineering loop (v1..v4) for all
    four judge models, scoring every demo passage at every version
  - reuse of each model's best-version scores as its final judge scores
  - bias normalization, human-human baseline, Monte-Carlo random human, the
    ratio to the human baseline, and P/R/F1
  - figure rendering (prompt-version agreement curve and the baseline ratio)
  - cost bookkeeping rows for every call

Usage:
    python scripts/run_demo.py
"""
import copy
import shutil

import _bootstrap  # noqa: F401
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import (assignment, bookkeeping, metrics, montecarlo, normalize,
                 personas, prompt_tuning, store)  # noqa: E402
from src.config import REPO_ROOT, load_config, load_prices, resolve_path  # noqa: E402
from src.poe_client import PoeClient  # noqa: E402

DEMO_N = 60  # passages exercised through the mock API (kept small for speed)


def _synth_passages(n):
    rows = []
    for i in range(n):
        rows.append({
            "item_id": f"item_{i:04d}",
            "text": ("This demonstration passage contains enough encyclopedic "
                     f"prose to resemble a real Wikipedia paragraph, variant {i}, "
                     "covering a topic in a few plain sentences.") * (1 + i % 3),
            "source_dataset": "synthetic",
            "char_count": 0,
        })
    df = pd.DataFrame(rows)
    df["char_count"] = df["text"].str.len()
    return df


def main():
    cfg = copy.deepcopy(load_config())
    dry = resolve_path("outputs/_dryrun")
    if dry.exists():
        shutil.rmtree(dry)
    dry.mkdir(parents=True, exist_ok=True)
    cfg["paths"]["human_store"] = str(dry / "human_panel.csv")
    cfg["paths"]["eval_store"] = str(dry / "judge_evaluations.csv")
    cfg["paths"]["bookkeeping_store"] = str(dry / "api_cost_log.csv")
    prompt_root = dry / "prompts" / "judge"
    figures_dir = dry / "figures"

    n_personas = cfg["personas"]["count"]

    # Load the committed Wikipedia sample if present, else synthesize passages.
    sample_path = resolve_path(cfg["paths"]["data_csv"])
    from src import data as data_mod
    if sample_path.exists():
        items_full = data_mod.load_sample(sample_path).reset_index(drop=True)
    else:
        items_full = _synth_passages(cfg["dataset"]["n_items"])

    # 0) Assignment design on the FULL committed sample: report the real
    #    co-rating robustness (min items shared by any persona pair).
    full_assign = assignment.build_assignment(items_full["item_id"].tolist(), n_personas)
    full_design = assignment.design_summary(full_assign, n_personas)
    assert full_design["connected"], "full assignment not connected"
    assert full_design["complete_graph"], "full co-rating graph is not complete"

    # Work on a small subset for the mock API exercise.
    items = items_full.head(DEMO_N).reset_index(drop=True)
    client = PoeClient(cfg, load_prices(), mock=True)

    # 1) assignment + panel on the demo subset
    assign = assignment.build_assignment(items["item_id"].tolist(), n_personas)
    dsum = assignment.design_summary(assign, n_personas)
    assert dsum["connected"], "demo assignment not connected"
    n_h = personas.run_panel(client, items, assign, cfg)
    assert n_h == 2 * len(items), f"expected {2*len(items)} ratings, got {n_h}"

    # 2) full-data prompt engineering for all judge models (v1..v4)
    human = store.load(cfg["paths"]["human_store"])
    human = human[human["ok"] == 1].copy()
    human = normalize.add_reviewer_noise(human, cfg)
    fit = normalize.fit_additive(human, "overall")
    norm = normalize.normalize_ratings(human, fit, "overall")
    tgt = norm.groupby("item_id")["normalized"].mean().rename("human").reset_index()
    base = (REPO_ROOT / "prompts" / "judge" / "v1" / "default.txt").read_text(encoding="utf-8")
    history_rows, best_versions = prompt_tuning.engineer_prompts(
        client, cfg, items, tgt, base, prompt_root)
    hist = pd.DataFrame(history_rows)
    versions_seen = hist.groupby("model")["version"].nunique()
    assert (versions_seen >= 4).all(), "expected at least 4 versions (v1..v4) per model"

    ev = store.load(cfg["paths"]["eval_store"])
    assert (ev["ok"] == 1).all(), "some judge calls failed to parse"

    # 3) normalization + metrics, reusing each model's best-version scores
    wide = normalize.to_wide(norm, fit)
    hh = metrics.human_human_corr(wide)
    resample = montecarlo.resample_matrix(wide, 200, cfg["analysis"]["seed"])

    headline_rows = []
    good_thr = cfg["analysis"]["good_threshold"]
    for model in cfg["judge"]["models"]:
        bver = best_versions[model]
        sub_ev = ev[(ev["model"] == model) & (ev["prompt_version"] == bver)]
        piv = sub_ev.groupby("item_id")["overall"].mean().rename(model)
        merged = wide.merge(piv, on="item_id").dropna(subset=[model])
        if len(merged) < 5:
            continue
        rs = montecarlo.resample_matrix(merged, 200, cfg["analysis"]["seed"])
        jscore = merged[model].to_numpy(dtype=float)
        lh = metrics.judge_human_corr(jscore, rs)
        hh_sub = metrics.human_human_corr(merged)
        pr = metrics.prf1(jscore, rs.mean(axis=0), good_thr, good_thr)
        headline_rows.append({
            "model": model, "version": bver, "n_items": len(merged),
            "judge_human_corr": round(lh, 3), "human_human_corr": round(hh_sub, 3),
            "ratio_to_human_baseline": round(lh / hh_sub, 3) if hh_sub else np.nan,
            "f1": round(pr["f1"], 3),
        })
    headline = pd.DataFrame(headline_rows)

    # 4) figure rendering (validates the plotting path)
    figures_dir.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7, 4))
    for model, grp in hist.groupby("model"):
        grp = grp.sort_values("iter")
        ax.plot(grp["iter"], grp["pearson"], marker="o", label=model)
    ax.set_xlabel("prompt version")
    ax.set_ylabel("correlation with human overall (full data)")
    ax.set_title("Judge-human agreement across prompt versions")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(figures_dir / "prompt_versions.png", dpi=120)
    plt.close(fig)

    # 5) cost bookkeeping
    agg = bookkeeping.aggregate(cfg["paths"]["bookkeeping_store"])
    assert not agg.get("empty"), "no bookkeeping rows"

    print("DRY RUN OK")
    print(f"  full sample={len(items_full)} passages  personas={n_personas}")
    print(f"  full co-rating graph: connected={full_design['connected']} "
          f"complete={full_design['complete_graph']} "
          f"pairs_covered={full_design['pairs_covered']}/{full_design['total_possible_pairs']}")
    print(f"  min items shared by any persona pair={full_design['min_items_shared_by_any_pair']} "
          f"max={full_design['max_items_shared_by_any_pair']}")
    print(f"  ratings per persona: min={full_design['min_ratings']} "
          f"max={full_design['max_ratings']}")
    print(f"  demo subset={len(items)}  panel ratings={n_h}  "
          f"judge evals={len(ev)} across versions")
    print(f"  versions per model: {dict(versions_seen)}  best={best_versions}")
    print(f"  human-human baseline corr={hh:.3f}")
    if not headline.empty:
        print("  best-version judge agreement (demo, mock scores):")
        print(headline.to_string(index=False))
    print(f"  bookkeeping calls={agg['total_calls']}  "
          f"tokens={agg['total_input_tokens']}in/{agg['total_output_tokens']}out  "
          f"est_cost=${agg['total_cost_usd']:.4f}")
    print(f"  wrote figure {figures_dir / 'prompt_versions.png'}")
    print(f"  (throwaway outputs in {dry})")


if __name__ == "__main__":
    main()
