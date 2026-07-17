"""Iterative, diagnosis-driven prompt refinement.

The loop starts from a minimal judge prompt, runs it on the full evaluated
passage set, compares the judge scores to the simulated human panel, diagnoses
where they disagree, and appends targeted corrective guidance to produce the
next prompt version. It repeats up to a version cap. Every version is saved and
scored so the improvement is visible, and each model keeps its own best version.

The refinement is automatic and reproducible: the diagnosis is computed from
the data, and the corrective text is selected from a library keyed to the
observed failure mode. This is the machine-driven analogue of a human reading
the disagreements and editing the prompt.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from scipy.stats import pearsonr, spearmanr


def _z(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    s = x.std()
    return (x - x.mean()) / s if s > 0 else x - x.mean()


def diagnose(judge: np.ndarray, human: np.ndarray, lengths: np.ndarray
             ) -> dict[str, Any]:
    """Compare judge scores to the human target on the tuning sample.

    Reports calibration failure modes (level bias, spread) and ranking failure
    modes (length sensitivity, overall correlation). Pearson correlation is
    invariant to level and scale, so calibration fixes help the secondary
    good/bad metric while the ranking fixes are what can move correlation.
    """
    j = np.asarray(judge, dtype=float)
    h = np.asarray(human, dtype=float)
    L = np.asarray(lengths, dtype=float)
    if len(j) < 2:
        # Not enough scored items to diagnose (for example a version whose
        # calls all failed to parse). Report neutral diagnostics.
        return {"n": int(len(j)), "pearson": float("nan"),
                "spearman": float("nan"), "level_bias": 0.0,
                "spread_ratio": float("nan"), "length_resid_corr": 0.0,
                "mae": float("nan")}
    corr = float(pearsonr(j, h)[0]) if j.std() > 0 and h.std() > 0 else float("nan")
    spear = float(spearmanr(j, h)[0]) if len(j) > 2 else float("nan")
    spread_ratio = float(j.std() / h.std()) if h.std() > 0 else float("nan")
    resid = _z(j) - _z(h)  # where the judge over- or under-rates vs humans
    length_corr = (float(pearsonr(L, resid)[0])
                   if L.std() > 0 and resid.std() > 0 else 0.0)
    return {
        "n": int(len(j)),
        "pearson": corr,
        "spearman": spear,
        "level_bias": float(j.mean() - h.mean()),
        "spread_ratio": spread_ratio,
        "length_resid_corr": length_corr,
        "mae": float(np.mean(np.abs(j - h))),
    }


# Corrective guidance keyed to a failure mode. Ranking fixes come first because
# they are the ones that can raise correlation.
CORRECTIONS: dict[str, str] = {
    "length_over": (
        "Do not reward a passage just for being long. A concise passage that "
        "covers its topic well deserves as high a score as a longer one, and "
        "padding or repetition should lower the score."
    ),
    "length_under": (
        "Do not penalize a thorough passage for its length when the extra "
        "detail is relevant and genuinely informative to the reader."
    ),
    "substance": (
        "Judge substance before style. First decide whether the passage is "
        "accurate, neutral, and informative, and let that dominate the overall "
        "score. Treat surface polish as secondary."
    ),
    "compressed": (
        "Use the full 1 to 10 range and separate passages clearly. Weak, "
        "average, and strong passages should land in visibly different score "
        "bands rather than clustering in the middle."
    ),
    "over_dispersed": (
        "Avoid extreme scores unless they are clearly warranted. Most passages "
        "are neither excellent nor terrible and belong in the middle of the "
        "range."
    ),
    "too_generous": (
        "You have been scoring too generously. Be more critical and reserve "
        "scores of 8 or higher for genuinely excellent passages."
    ),
    "too_harsh": (
        "You have been scoring too harshly. Give credit for what a passage "
        "does well and do not over-penalize small imperfections."
    ),
}


def choose_correction(diag: dict[str, Any], applied: list[str]) -> str | None:
    """Pick the most salient not-yet-applied correction, or None to stop."""
    candidates: list[str] = []
    lc = diag["length_resid_corr"]
    if lc > 0.20:
        candidates.append("length_over")
    elif lc < -0.20:
        candidates.append("length_under")
    sr = diag["spread_ratio"]
    if not np.isnan(sr):
        if sr < 0.75:
            candidates.append("compressed")
        elif sr > 1.40:
            candidates.append("over_dispersed")
    lb = diag["level_bias"]
    if lb > 0.5:
        candidates.append("too_generous")
    elif lb < -0.5:
        candidates.append("too_harsh")
    # Substance is the general-purpose ranking nudge, tried once if nothing
    # sharper is outstanding.
    candidates.append("substance")
    for key in candidates:
        if key not in applied:
            return key
    return None


def render_prompt(base: str, applied: list[str]) -> str:
    """Base prompt plus the accumulated corrective guidance block."""
    if not applied:
        return base
    lines = [base.rstrip(), "", "Additional calibration guidance learned from "
             "reviewer disagreement:"]
    for key in applied:
        lines.append(f"- {CORRECTIONS[key]}")
    lines.append("")
    lines.append("Return only the requested JSON object.")
    return "\n".join(lines)


def engineer_prompts(client, cfg: dict[str, Any], items, human_tgt,
                     base_prompt: str, prompt_root, models: list[str] | None = None,
                     length_col: str = "char_count"):
    """Full-data, diagnosis-driven prompt engineering for every judge model.

    Each model scores EVERY evaluated passage at each prompt version, its scores
    are compared to the human overall target, the divergence is diagnosed, and
    the next version appends targeted corrective guidance. The loop runs up to
    cfg["prompt_eng"]["max_iters"] versions (at least four are expected) and
    stops early only once no further correction applies. Each version prompt is
    written under prompt_root/<model>/vN.txt and every evaluation is cached in
    cfg eval_store, so the best version's scores are the model's final scores
    with no separate judge run.

    Parameters
    ----------
    items : DataFrame with columns item_id, text, and `length_col`.
    human_tgt : DataFrame with columns item_id and "human" (target overall).

    Returns (history_rows, best_versions) where history_rows is a list of
    per-version diagnostic dicts and best_versions maps model -> best version.
    """
    from pathlib import Path

    import numpy as np

    from . import concurrency, evaluate, store

    models = models or cfg["judge"]["models"]
    max_iters = cfg["prompt_eng"]["max_iters"]
    prompt_root = Path(prompt_root)

    sample = items.merge(human_tgt, on="item_id")
    lengths = sample[length_col].to_numpy(dtype=float)
    human_vec = sample["human"].to_numpy(dtype=float)

    history_rows: list[dict[str, Any]] = []
    best_versions: dict[str, str] = {}
    for model in models:
        applied: list[str] = []
        prompt_text = base_prompt
        best_corr, best_ver = -2.0, "v1"
        model_dir = prompt_root / model
        model_dir.mkdir(parents=True, exist_ok=True)
        for it in range(1, max_iters + 1):
            version = f"v{it}"
            (model_dir / f"{version}.txt").write_text(prompt_text, encoding="utf-8")

            done = store.existing_keys(cfg["paths"]["eval_store"],
                                       ["item_id", "model", "prompt_version"])
            todo = [row for _, row in sample.iterrows()
                    if (str(row["item_id"]), model, version) not in done]

            def _eval(row, _model=model, _version=version):
                content = {"item_id": row["item_id"], "text": row["text"]}
                return evaluate.evaluate(content, _model, _version, client, cfg,
                                         role="prompt_eng", prompt_dir=prompt_root)

            concurrency.map_concurrent(_eval, todo,
                                       cfg["api"].get("max_workers", 10))

            ev = store.load(cfg["paths"]["eval_store"])
            ev = ev[(ev["model"] == model) & (ev["prompt_version"] == version)
                    & (ev["ok"] == 1)]
            jmap = ev.set_index("item_id")["overall"]
            jvec = sample["item_id"].map(jmap).to_numpy(dtype=float)
            mask = ~np.isnan(jvec)
            diag = diagnose(jvec[mask], human_vec[mask], lengths[mask])

            history_rows.append({
                "model": model, "version": version, "iter": it,
                "n": diag["n"], "pearson": round(diag["pearson"], 3),
                "spearman": round(diag["spearman"], 3),
                "level_bias": round(diag["level_bias"], 3),
                "spread_ratio": round(diag["spread_ratio"], 3),
                "length_resid_corr": round(diag["length_resid_corr"], 3),
                "mae": round(diag["mae"], 3),
                "correction_applied": applied[-1] if applied else "(base)",
            })

            if not np.isnan(diag["pearson"]) and diag["pearson"] > best_corr:
                best_corr, best_ver = diag["pearson"], version

            if it == max_iters:
                break
            # Prefer the diagnosis-driven correction; if none is salient, fall
            # back to the next unused correction so the loop still produces at
            # least the configured number of versions (at least v1..v4). Only
            # stop early once every correction has been applied.
            key = choose_correction(diag, applied)
            if key is None:
                key = next((k for k in CORRECTIONS if k not in applied), None)
            if key is None:
                break
            applied.append(key)
            prompt_text = render_prompt(base_prompt, applied)

        best_versions[model] = best_ver
        print(f"{model}: best={best_ver} (full-data pearson={best_corr:.3f})")

    return history_rows, best_versions
