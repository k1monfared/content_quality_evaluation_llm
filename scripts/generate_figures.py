"""Generate figures from the analysis outputs (Phase B).

Produces, when the underlying CSVs exist:
  - headline_ratio.png     ratio to the human baseline per judge with 95% CI
  - cost_per_model.png      total estimated cost per model
  - calibration.png         best judge overall vs mean random-human overall
  - prompt_versions.png     correlation with human overall by version and model

Usage:
    python scripts/generate_figures.py
"""
import json  # noqa: E402

import _bootstrap  # noqa: F401
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from src import store  # noqa: E402
from src.config import load_config, load_prices, resolve_path  # noqa: E402


def _save(fig, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    print(f"Wrote {path}")


def main():
    cfg = load_config()
    rd = resolve_path(cfg["paths"]["results_dir"])
    fd = resolve_path(cfg["paths"]["figures_dir"])

    hl = rd / "headline_results.csv"
    if hl.exists():
        df = pd.read_csv(hl)
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.arange(len(df))
        lo = df["ratio_boot_mean"] - df["ratio_ci_low"]
        hi = df["ratio_ci_high"] - df["ratio_boot_mean"]
        ax.bar(x, df["ratio_boot_mean"], color="#4C72B0")
        ax.errorbar(x, df["ratio_boot_mean"], yerr=[lo, hi], fmt="none",
                    ecolor="#333", capsize=5)
        ax.axhline(1.0, color="#C44E52", linestyle="--", label="human baseline")
        ax.set_xticks(x)
        ax.set_xticklabels(df["model"], rotation=20, ha="right")
        ax.set_ylabel("LLM-human / human-human correlation")
        ax.set_title("Judge agreement relative to the human baseline")
        ax.legend()
        _save(fig, fd / "headline_ratio.png")

    cpm = rd / "cost_per_model.csv"
    if cpm.exists():
        df = pd.read_csv(cpm)
        # Show only the models actually used in the study: the current judges
        # and the persona model, which are exactly the models in the price
        # table. This drops any retired or connectivity-only smoke-test models
        # (for example the old Pro tiers) that may still sit in the full ledger.
        priced = set(load_prices().get("models", {}))
        df = df[df["model"].isin(priced)].sort_values("cost_usd", ascending=False)
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.bar(df["model"], df["cost_usd"], color="#55A868")
        ax.set_ylabel("estimated cost (USD)")
        ax.set_title("Total estimated API cost per model")
        ax.set_xticklabels(df["model"], rotation=20, ha="right")
        _save(fig, fd / "cost_per_model.png")

    pe = rd / "prompt_eng_results.csv"
    if pe.exists():
        df = pd.read_csv(pe)
        fig, ax = plt.subplots(figsize=(7, 4))
        for model, grp in df.groupby("model"):
            grp = grp.sort_values("iter")
            ax.plot(grp["iter"], grp["pearson"], marker="o", label=model)
        ax.set_xlabel("prompt version")
        ax.set_ylabel("correlation with human overall (full data)")
        ax.set_title("Judge-human agreement across prompt versions")
        ax.legend(fontsize=8)
        _save(fig, fd / "prompt_versions.png")

    # calibration for the best model, if both stores present
    nb = resolve_path(cfg["paths"]["normalized_csv"])
    if hl.exists() and nb.exists():
        headline = pd.read_csv(hl)
        wide = pd.read_csv(nb)
        best_row = headline.sort_values("ratio", ascending=False).iloc[0]
        best = best_row["model"]
        best_ver = best_row.get("version", cfg["judge"]["best_prompt_version"])
        ev = store.load(cfg["paths"]["eval_store"])
        ev = ev[(ev["model"] == best) & (ev["ok"] == 1) &
                (ev["prompt_version"] == best_ver)]
        if not ev.empty:
            j = ev.groupby("item_id")["overall"].mean()
            wide = wide.assign(human=(wide["score_a"] + wide["score_b"]) / 2)
            m = wide.set_index("item_id").join(j.rename("judge"), how="inner").dropna()
            fig, ax = plt.subplots(figsize=(5, 5))
            ax.scatter(m["human"], m["judge"], alpha=0.6, color="#4C72B0")
            lims = [1, 10]
            ax.plot(lims, lims, "--", color="#999")
            ax.set_xlim(lims); ax.set_ylim(lims)
            ax.set_xlabel("human mean overall (normalized)")
            ax.set_ylabel(f"{best} overall")
            ax.set_title(f"Calibration of {best}")
            _save(fig, fd / "calibration.png")

    # dimension reduction (analysis 2): change in the LLM-composite-to-random-
    # human match when each dimension is removed, colored by kept or dropped.
    di = rd / "dimension_importance.csv"
    if di.exists():
        df = pd.read_csv(di).sort_values("llm_match_drop_one_delta", ascending=True)
        # Absolute match after removing only that one dimension, for labeling.
        full_match = None
        dr_path = rd / "dimension_reduction.json"
        if dr_path.exists():
            full_match = json.loads(dr_path.read_text()).get("full_match")
        fig, ax = plt.subplots(figsize=(8, 4.4))
        y = np.arange(len(df))
        deltas = df["llm_match_drop_one_delta"].to_numpy(float)
        colors = ["#55A868" if k else "#C44E52" for k in df["kept"]]
        ax.barh(y, deltas, color=colors)
        ax.axvline(0.0, color="#888", linewidth=0.8)
        span = max(abs(deltas.min()), abs(deltas.max())) or 0.001
        ax.set_xlim(-span * 1.9, span * 1.9)
        if full_match is not None:
            for yi, d in zip(y, deltas):
                off = span * 0.06
                ax.text(d + (off if d >= 0 else -off), yi,
                        f"{full_match + d:.4f}", va="center",
                        ha="left" if d >= 0 else "right", fontsize=8)
        ax.set_yticks(y)
        ax.set_yticklabels(df["dimension"])
        ax.set_xlabel("change in match when only that dimension is removed\n"
                      "(labels show the match after removal)")
        ax.set_title("Drop-one importance (green kept, red removed)\n"
                     "positive change means removal helps, so the dimension is redundant")
        _save(fig, fd / "dimension_importance.png")

    # rating levels: mean raw overall per judge, with the human band and average.
    rl = rd / "rating_levels.csv"
    if rl.exists():
        df = pd.read_csv(rl)
        judges = df[df["kind"] == "judge"]
        refs = df.set_index("rater")["mean_overall"]
        fig, ax = plt.subplots(figsize=(7, 4))
        x = np.arange(len(judges))
        ax.bar(x, judges["mean_overall"], color="#4C72B0", zorder=3)
        for xi, v in zip(x, judges["mean_overall"]):
            ax.text(xi, v + 0.03, f"{v:.2f}", ha="center", fontsize=8)
        lo, hi = refs["min_of_two_humans"], refs["max_of_two_humans"]
        avg = refs["two_human_average"]
        ax.axhspan(lo, hi, color="#C44E52", alpha=0.12, zorder=0,
                   label=f"human min to max band ({lo:.2f} to {hi:.2f})")
        ax.axhline(avg, color="#C44E52", linestyle="--", zorder=2,
                   label=f"two-human average ({avg:.2f})")
        ax.set_xticks(x)
        ax.set_xticklabels(judges["rater"], rotation=20, ha="right")
        ax.set_ylabel("mean overall score (raw 1 to 10)")
        ax.set_title("How favorably each judge scores")
        ax.legend(fontsize=8)
        _save(fig, fd / "rating_levels.png")

    # rating-level distributions: box plots of every rater's overall scores.
    rlo = rd / "rating_levels_overall.csv"
    if rlo.exists():
        df = pd.read_csv(rlo).drop(columns=["item_id"])
        order = [c for c in df.columns]
        fig, ax = plt.subplots(figsize=(8, 4.4))
        ax.boxplot([df[c].dropna().to_numpy() for c in order], tick_labels=order,
                   showmeans=True, meanprops={"marker": "D", "markerfacecolor": "#C44E52",
                                              "markeredgecolor": "#C44E52", "markersize": 5})
        ax.set_ylabel("overall score (raw 1 to 10)")
        ax.set_title("Distribution of overall scores by rater (red diamond is the mean)")
        ax.set_xticklabels(order, rotation=20, ha="right")
        ax.set_ylim(1, 10)
        _save(fig, fd / "rating_levels_box.png")

    # per-dimension favorability: each judge's mean minus the human reference,
    # as a diverging heatmap (red inflates the dimension, blue deflates it).
    rld = rd / "rating_levels_dimensions.csv"
    if rld.exists() and rl.exists():
        dim_df = pd.read_csv(rld)
        levels = pd.read_csv(rl).set_index("rater")["mean_overall"]
        dims = dim_df["dimension"].tolist()
        cols = dims + ["overall"]
        judge_models = [c for c in dim_df.columns if c != "dimension"
                        and c != "two_human_average"]
        human_ref = {d: float(dim_df.loc[dim_df["dimension"] == d,
                                         "two_human_average"].iloc[0]) for d in dims}
        human_ref["overall"] = float(levels["two_human_average"])
        rows = []
        for m in judge_models:
            row = {d: float(dim_df.loc[dim_df["dimension"] == d, m].iloc[0])
                   - human_ref[d] for d in dims}
            row["overall"] = float(levels[m]) - human_ref["overall"]
            rows.append((m, row))
        # order judges from most inflating to most deflating by the overall delta
        rows.sort(key=lambda r: r[1]["overall"], reverse=True)
        labels = [m for m, _ in rows]
        D = np.array([[r[c] for c in cols] for _, r in rows], float)
        span = float(np.abs(D).max())
        fig, ax = plt.subplots(figsize=(9, 3.8))
        im = ax.imshow(D, cmap="RdBu_r", vmin=-span, vmax=span, aspect="auto")
        ax.set_xticks(np.arange(len(cols)))
        ax.set_yticks(np.arange(len(labels)))
        ax.set_xticklabels(cols, rotation=30, ha="right", fontsize=8)
        ax.set_yticklabels(labels, fontsize=8)
        for i in range(len(labels)):
            for j in range(len(cols)):
                v = D[i, j]
                ax.text(j, i, f"{v:+.2f}", ha="center", va="center", fontsize=7,
                        color="#111" if abs(v) < span * 0.6 else "#fff")
        # separate the overall column from the dimensions with a light divider
        ax.axvline(len(dims) - 0.5, color="#444", linewidth=1.0)
        ax.set_title("Per-dimension favorability: judge mean minus the human "
                     "reference\nred inflates the dimension, blue deflates it")
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        cb.set_label("judge mean minus human mean (raw 1 to 10)", fontsize=8)
        _save(fig, fd / "rating_levels_dimensions.png")

    # internal consistency (analysis 1): per-rater flat vs fitted composite
    # correlation with each rater's own direct overall.
    ic = rd / "internal_consistency.json"
    if ic.exists():
        c = json.loads(ic.read_text())
        raters = c["raters"]
        labels = [r["rater"] for r in raters]
        flat = [r["flat_corr"] for r in raters]
        fitted = [r["fitted_corr"] for r in raters]
        x = np.arange(len(raters))
        w = 0.38
        fig, ax = plt.subplots(figsize=(9, 4.4))
        ax.bar(x - w / 2, flat, w, label="flat (equal weight)", color="#4C72B0")
        ax.bar(x + w / 2, fitted, w, label="fitted (least squares)", color="#55A868")
        for xi, (a, b) in enumerate(zip(flat, fitted)):
            ax.text(xi - w / 2, a + 0.004, f"{a:.3f}", ha="center", fontsize=7)
            ax.text(xi + w / 2, b + 0.004, f"{b:.3f}", ha="center", fontsize=7)
        ax.set_xticks(x)
        ax.set_xticklabels(labels, rotation=20, ha="right")
        ax.set_ylim(0.60, 1.0)
        ax.set_ylabel("correlation with own direct overall")
        ax.set_title("Internal consistency per rater: composite vs own direct overall")
        ax.legend(fontsize=8)
        _save(fig, fd / "composite_comparison.png")

    # inter-dimension correlation matrix (heatmap) on the normalized human dims.
    dc = rd / "dimension_correlation.csv"
    if dc.exists():
        cm = pd.read_csv(dc, index_col=0)
        dims = list(cm.columns)
        M = cm.to_numpy(float)
        fig, ax = plt.subplots(figsize=(6.4, 5.6))
        im = ax.imshow(M, cmap="RdBu_r", vmin=-1, vmax=1)
        ax.set_xticks(np.arange(len(dims)))
        ax.set_yticks(np.arange(len(dims)))
        ax.set_xticklabels(dims, rotation=40, ha="right", fontsize=8)
        ax.set_yticklabels(dims, fontsize=8)
        for i in range(len(dims)):
            for j in range(len(dims)):
                v = M[i, j]
                ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=7,
                        color="#111" if abs(v) < 0.6 else "#fff")
        ax.set_title("Inter-dimension correlation (normalized human scores)")
        fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
        _save(fig, fd / "dimension_correlation.png")

    # dimension reduction trajectory, subset-size confirmation, and fitted weights.
    dr = rd / "dimension_reduction.json"
    if dr.exists():
        red = json.loads(dr.read_text())

        # Backward-elimination trajectory: match after each cumulative removal.
        gp = red.get("greedy_path", [])
        if gp:
            xs = np.arange(len(gp))
            ys = [s["match"] for s in gp]
            labs = ["all seven" if s["dropped"] is None else f"- {s['dropped']}"
                    for s in gp]
            fig, ax = plt.subplots(figsize=(7, 4.2))
            ax.plot(xs, ys, marker="o", color="#4C72B0")
            for xi, yi in zip(xs, ys):
                ax.text(xi, yi + 0.0004, f"{yi:.4f}", ha="center", fontsize=8)
            ax.set_xticks(xs)
            ax.set_xticklabels(labs, rotation=20, ha="right")
            ax.set_ylabel("LLM-composite-to-random-human match")
            ax.set_xlabel("cumulative removal step")
            ax.set_title("Backward elimination: match rises then plateaus")
            _save(fig, fd / "backward_elimination.png")

        # Exhaustive-search confirmation: best achievable match by subset size.
        bs = red.get("best_by_size", [])
        if bs:
            sizes = [s["size"] for s in bs]
            matches = [s["match"] for s in bs]
            chosen = len(red.get("refined_dims", []))
            fig, ax = plt.subplots(figsize=(7, 4.2))
            colors = ["#55A868" if s == chosen else "#4C72B0" for s in sizes]
            ax.bar(sizes, matches, color=colors)
            for s, mv in zip(sizes, matches):
                ax.text(s, mv + 0.0004, f"{mv:.4f}", ha="center", fontsize=8)
            ax.set_xlabel("subset size (best subset of that size, exhaustive search)")
            ax.set_ylabel("best LLM-to-random-human match")
            ax.set_title("Best achievable match by subset size\n"
                         "(green is the chosen refined size)")
            ax.set_ylim(min(matches) - 0.004, max(matches) + 0.003)
            _save(fig, fd / "best_subset_by_size.png")

    # per-dimension fitted weights (full rubric reconstruction of the human overall).
    cr = rd / "composite_results.json"
    if cr.exists():
        comp = json.loads(cr.read_text())
        fw = comp["full_rubric"]["fitted_weights"]
        dims = list(fw.keys())
        vals = [fw[d] for d in dims]
        order = np.argsort(vals)
        dims = [dims[i] for i in order]
        vals = [vals[i] for i in order]
        refined = set(comp["refined_rubric"]["dims"])
        colors = ["#55A868" if d in refined else "#C44E52" for d in dims]
        fig, ax = plt.subplots(figsize=(7, 4.2))
        y = np.arange(len(dims))
        ax.barh(y, vals, color=colors)
        for yi, v in zip(y, vals):
            ax.text(v + 0.002, yi, f"{v:.3f}", va="center", fontsize=8)
        ax.set_yticks(y)
        ax.set_yticklabels(dims)
        ax.set_xlabel("fitted weight reconstructing the human overall (full rubric)")
        ax.set_title("Per-dimension fitted weights (green kept, red dropped)")
        _save(fig, fd / "fitted_weights.png")


if __name__ == "__main__":
    main()
