"""Pre-check: does Poe honor temperature for claude-haiku-4.5?
Also probe token usage on the real judge prompt to project cost.
Uses log=False so nothing is written to the committed api_cost_log.csv.
"""
import sys
sys.path.insert(0, "/home/k1/public/content_quality_evaluation_llm")

from src.config import load_config, load_prices
from src import bookkeeping, rubric
from src.evaluate import load_judge_prompt
from src.poe_client import PoeClient
import pandas as pd

cfg = load_config()
prices = load_prices()
client = PoeClient(cfg, prices, mock=False)

MODEL = "claude-haiku-4.5"

# --- Part A: creative prompt, temperature honoring ---
sys_c = "You are a creative writing assistant."
usr_c = ("Invent one short, original, whimsical sentence about the ocean. "
         "Just the sentence, nothing else.")

def run_creative(temp, n):
    client.temperature = temp
    outs = []
    for i in range(n):
        r = client.complete(model=MODEL, system=sys_c, user=usr_c, role="precheck",
                            item_id="precheck", prompt_version="creative", log=False)
        outs.append(r.text.strip())
    return outs

print("=== TEMP 0 creative (4 draws) ===")
t0 = run_creative(0.0, 4)
for o in t0:
    print(" -", o)
print("distinct temp0:", len(set(t0)))

print("\n=== TEMP 1.0 creative (4 draws) ===")
t1 = run_creative(1.0, 4)
for o in t1:
    print(" -", o)
print("distinct temp1:", len(set(t1)))

# --- Part B: token probe on the real judge prompt for item_0105 ---
data = pd.read_csv("/home/k1/public/content_quality_evaluation_llm/data/wiki_sample.csv")
passage = str(data[data["item_id"] == "item_0105"].iloc[0]["text"])
system = load_judge_prompt(MODEL, "v3")
user = rubric.build_user_prompt(passage, 1, 10)

client.temperature = 0.0
r = client.complete(model=MODEL, system=system, user=user, role="precheck",
                    item_id="item_0105", prompt_version="v3", log=False)
cost = bookkeeping.estimate_cost(MODEL, r.input_tokens, r.output_tokens, prices)
print("\n=== JUDGE PROMPT TOKEN PROBE (item_0105) ===")
print("input_tokens:", r.input_tokens, "output_tokens:", r.output_tokens,
      "source:", r.token_source)
print("est cost per call: $%.5f" % cost)
print("projected 3300-call cost: $%.2f" % (cost * 3300))
print("parsed:", rubric.parse_rubric(r.text, 1, 10))
print("raw text:\n", r.text)
