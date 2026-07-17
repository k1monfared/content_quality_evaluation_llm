"""STEP 1: find the passage with maximum judge disagreement (no API)."""
import sys
import pandas as pd
import numpy as np

sys.path.insert(0, "/home/k1/public/content_quality_evaluation_llm")

EVAL = "/home/k1/public/content_quality_evaluation_llm/outputs/judge_evaluations.csv"
DATA = "/home/k1/public/content_quality_evaluation_llm/data/wiki_sample.csv"

df = pd.read_csv(EVAL)
print("total rows:", len(df))
print("distinct models:", sorted(df["model"].unique()))
print("distinct prompt_versions:", sorted(df["prompt_version"].unique()))
print("ok values:", df["ok"].value_counts().to_dict())
print()

# The four judges of the study (per config) each at their best prompt version.
best = {"gpt-5.2": "v1", "claude-haiku-4.5": "v3",
        "gemini-2.5-flash": "v3", "perplexity-sonar": "v2"}
print("best versions:", best)

# Keep only best-version rows per judge model, ok==1
mask = False
parts = []
for m, v in best.items():
    sub = df[(df["model"] == m) & (df["prompt_version"] == v) & (df["ok"] == 1)]
    parts.append(sub)
    print(f"  {m} {v}: {len(sub)} rows")
jf = pd.concat(parts, ignore_index=True)

dims = ["clarity", "neutrality", "verifiability", "coverage", "structure",
        "readability", "informativeness"]

# Pivot overall by item x model
piv = jf.pivot_table(index="item_id", columns="model", values="overall", aggfunc="mean")
# Keep items scored by all four judges
piv4 = piv.dropna()
print("\nitems scored by all four judges:", len(piv4))

overall_range = piv4.max(axis=1) - piv4.min(axis=1)
overall_var = piv4.var(axis=1, ddof=0)
overall_std = piv4.std(axis=1, ddof=0)

res = pd.DataFrame({"range": overall_range, "var": overall_var, "std": overall_std})
res = res.sort_values("var", ascending=False)
print("\nTop 10 by overall variance:")
print(res.head(10).to_string())

top_item = res.index[0]
print("\n=== MAX-DISAGREEMENT ITEM (by overall variance):", top_item, "===")
print("Overall scores by judge:")
print(piv4.loc[top_item].to_string())

# Cross-check across all dimensions: mean per-dimension variance across judges
def dim_spread(item):
    sub = jf[jf["item_id"] == item]
    # one row per model
    vals = sub.groupby("model")[dims].mean()
    return vals.var(ddof=0).mean(), vals

print("\nCross-check: top items ranked by mean across-dimension variance:")
alldim = {}
for item in piv4.index:
    sub = jf[jf["item_id"] == item].groupby("model")[dims + ["overall"]].mean()
    if len(sub) < 4:
        continue
    alldim[item] = sub[dims].var(ddof=0).mean()
alldim_s = pd.Series(alldim).sort_values(ascending=False)
print(alldim_s.head(10).to_string())

# Show dimension detail for the top overall-variance item
print("\nPer-dimension + overall scores for", top_item, ":")
detail = jf[jf["item_id"] == top_item].groupby("model")[dims + ["overall"]].mean()
print(detail.to_string())

# passage text
data = pd.read_csv(DATA)
print("\ndata columns:", list(data.columns))
row = data[data["item_id"] == top_item]
if len(row):
    txt = row.iloc[0]["text"]
    print("\nPASSAGE TEXT for", top_item, ":\n", txt)
    print("\npassage char length:", len(str(txt)))

# rationales
print("\nRationales for", top_item, ":")
for _, r in jf[jf["item_id"] == top_item].iterrows():
    print(f"[{r['model']}] overall={r['overall']}: {str(r['rationale'])[:200]}")
