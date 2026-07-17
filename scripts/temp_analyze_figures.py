"""STEP 4: analyze the temperature sweep draws and produce figures.
Reads outputs/temperature_study_cache/draws.jsonl, writes figures to docs/images/
and summary tables to outputs/temperature_study_cache/.
"""
import sys, os, json
sys.path.insert(0, "/home/k1/public/content_quality_evaluation_llm")
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = "/home/k1/public/content_quality_evaluation_llm"
CACHE = os.path.join(ROOT, "outputs", "temperature_study_cache")
DRAWS = os.path.join(CACHE, "draws.jsonl")
IMG = os.path.join(ROOT, "docs", "images")
os.makedirs(IMG, exist_ok=True)

DIMS = ["clarity", "neutrality", "verifiability", "coverage", "structure",
        "readability", "informativeness"]

rows = []
with open(DRAWS) as fh:
    for line in fh:
        line = line.strip()
        if line:
            rows.append(json.loads(line))
df = pd.DataFrame(rows)
df["temperature"] = df["temperature"].round(3)
def na_count(r):
    n = r.get("na_dims")
    return len(n) if isinstance(n, list) else 0
df["n_na"] = df.apply(na_count, axis=1)
df["any_na"] = df["n_na"] > 0
print("loaded valid draws:", len(df))
print("per-temp counts:\n", df.groupby("temperature").size().to_string())

temps = sorted(df["temperature"].unique())
t0 = df[df["temperature"] == 0.0]["overall"]
t0_val = t0.iloc[0] if t0.nunique() == 1 else t0.mean()

recs = []
for t in temps:
    sub = df[df["temperature"] == t]["overall"].dropna()
    q1, q3 = np.percentile(sub, [25, 75])
    na_rate = df[df["temperature"] == t]["any_na"].mean()
    recs.append({
        "temperature": t, "n": len(sub),
        "mean_overall": round(sub.mean(), 3),
        "std_overall": round(sub.std(ddof=1), 3),
        "iqr_overall": round(q3 - q1, 3),
        "min": sub.min(), "max": sub.max(),
        "n_distinct": sub.nunique(), "mode": sub.mode().iloc[0],
        "delta_mean_vs_t0": round(sub.mean() - t0_val, 3),
        "coverage_na_rate": round(na_rate, 3),
    })
summ = pd.DataFrame(recs)
summ.to_csv(os.path.join(CACHE, "summary_table.csv"), index=False)
pd.set_option("display.width", 220)
print("\nSUMMARY TABLE (overall):")
print(summ.to_string(index=False))

dim_recs = []
for t in temps:
    sub = df[df["temperature"] == t]
    row = {"temperature": t}
    for d in DIMS:
        row[d + "_std"] = round(sub[d].astype(float).std(ddof=1), 3)
    dim_recs.append(row)
dimsumm = pd.DataFrame(dim_recs)
dimsumm.to_csv(os.path.join(CACHE, "dimension_std_by_temp.csv"), index=False)
print("\nDIMENSION STD BY TEMP:")
print(dimsumm.to_string(index=False))

print("\nTEMP-0 overall distribution:",
      df[df["temperature"] == 0.0]["overall"].value_counts().to_dict())
print("TEMP-0 distinct raw texts =",
      df[df["temperature"] == 0.0]["text"].nunique())

INK = "#1b2a4a"; ACC = "#c0392b"; GRID = "#d9dde3"; FILL = "#8fa8c8"

fig, ax = plt.subplots(figsize=(11, 6))
data_by_t = [df[df["temperature"] == t]["overall"].dropna().values for t in temps]
positions = list(range(len(temps)))
parts = ax.violinplot(data_by_t, positions=positions, widths=0.85,
                      showmeans=False, showextrema=False)
for pc in parts["bodies"]:
    pc.set_facecolor(FILL); pc.set_edgecolor(INK); pc.set_alpha(0.55)
bp = ax.boxplot(data_by_t, positions=positions, widths=0.22, patch_artist=True,
                showfliers=True, medianprops=dict(color=ACC, linewidth=1.6),
                flierprops=dict(marker="o", markersize=3, markerfacecolor="#666",
                                markeredgecolor="none", alpha=0.35))
for box in bp["boxes"]:
    box.set(facecolor="white", edgecolor=INK, linewidth=1.0)
for w in bp["whiskers"]:
    w.set(color=INK, linewidth=1.0)
for c in bp["caps"]:
    c.set(color=INK, linewidth=1.0)
means = [d.mean() for d in data_by_t]
ax.plot(positions, means, "-", color=ACC, linewidth=1.4, marker="D",
        markersize=5, label="mean overall", zorder=5)
ax.axhline(t0_val, color="#7f8c8d", linestyle="--", linewidth=1.2,
           label=f"temp 0 value ({t0_val:.2f})")
ax.set_xticks(positions)
ax.set_xticklabels([f"{t:g}" for t in temps])
ax.set_xlabel("temperature"); ax.set_ylabel("overall score (1 to 10)")
ax.set_title("Distribution of the overall score by temperature\n"
             "item_0105, claude-haiku-4.5 judge (prompt v3), 300 draws per temperature")
ax.grid(axis="y", color=GRID, linewidth=0.8); ax.set_axisbelow(True)
ax.legend(loc="upper left", frameon=False)
fig.tight_layout()
fig.savefig(os.path.join(IMG, "temperature_overall_distributions.png"), dpi=150)
plt.close(fig)

fig, ax = plt.subplots(figsize=(9, 5.2))
ax.errorbar(summ["temperature"], summ["mean_overall"], yerr=summ["std_overall"],
            fmt="-o", color=INK, ecolor=FILL, elinewidth=1.4, capsize=3,
            markersize=5, label="mean overall +/- 1 SD")
ax.axhline(t0_val, color=ACC, linestyle="--", linewidth=1.3,
           label=f"temp 0 value ({t0_val:.2f})")
ax.scatter([0.0], [t0_val], color=ACC, zorder=6, s=55)
ax.set_xlabel("temperature"); ax.set_ylabel("mean overall score")
ax.set_title("Mean overall score versus temperature\nitem_0105, claude-haiku-4.5 judge")
ax.grid(color=GRID, linewidth=0.8); ax.set_axisbelow(True)
ax.legend(frameon=False)
fig.tight_layout()
fig.savefig(os.path.join(IMG, "temperature_mean_overall.png"), dpi=150)
plt.close(fig)

fig, ax = plt.subplots(figsize=(9, 5.2))
ax.plot(summ["temperature"], summ["std_overall"], "-o", color=INK,
        markersize=5, label="standard deviation")
ax.plot(summ["temperature"], summ["iqr_overall"], "-s", color=ACC,
        markersize=5, label="interquartile range")
ax.set_xlabel("temperature"); ax.set_ylabel("spread of overall score")
ax.set_title("Spread of the overall score versus temperature\nitem_0105, claude-haiku-4.5 judge")
ax.grid(color=GRID, linewidth=0.8); ax.set_axisbelow(True)
ax.legend(frameon=False)
fig.tight_layout()
fig.savefig(os.path.join(IMG, "temperature_spread.png"), dpi=150)
plt.close(fig)

print("\nfigures written to", IMG)
for f in ("temperature_overall_distributions.png", "temperature_mean_overall.png",
          "temperature_spread.png"):
    print(" -", f)
