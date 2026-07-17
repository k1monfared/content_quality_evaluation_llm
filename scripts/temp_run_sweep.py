"""STEP 2 and 3: score item_0105 with claude-haiku-4.5 (v3 prompt) 300 times
per temperature in {0, 0.1, ..., 1.0}. Resumable cache, separate ledger,
never writes to the committed api_cost_log.csv (log=False). Cost guard at $17.5.
"""
import sys, os, json, csv, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, "/home/k1/public/content_quality_evaluation_llm")

from src.config import load_config, load_prices
from src import bookkeeping, rubric
from src.evaluate import load_judge_prompt
from src.poe_client import PoeClient
import pandas as pd

ROOT = "/home/k1/public/content_quality_evaluation_llm"
CACHE = os.path.join(ROOT, "outputs", "temperature_study_cache")
os.makedirs(CACHE, exist_ok=True)
DRAWS = os.path.join(CACHE, "draws.jsonl")
LEDGER = os.path.join(CACHE, "ledger.csv")

MODEL = "claude-haiku-4.5"
VERSION = "v3"
ITEM = "item_0105"
N = 300
TEMPS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
COST_GUARD = 17.5
MAX_WORKERS = 10

cfg = load_config()
prices = load_prices()
client = PoeClient(cfg, prices, mock=False)

data = pd.read_csv(os.path.join(ROOT, "data", "wiki_sample.csv"))
passage = str(data[data["item_id"] == ITEM].iloc[0]["text"])
system = load_judge_prompt(MODEL, VERSION)
user = rubric.build_user_prompt(passage, 1, 10)

_lock = threading.Lock()

def load_done():
    done = set()
    if os.path.exists(DRAWS):
        with open(DRAWS) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                done.add((round(float(d["temperature"]), 3), int(d["draw"])))
    return done

def load_cost():
    total = 0.0
    if os.path.exists(LEDGER):
        with open(LEDGER) as fh:
            r = csv.DictReader(fh)
            for row in r:
                total += float(row["cost"])
    return total

def append_ledger(temp, draw, itok, otok, cost, ok):
    newfile = not os.path.exists(LEDGER)
    with open(LEDGER, "a", newline="") as fh:
        w = csv.writer(fh)
        if newfile:
            w.writerow(["temperature", "draw", "input_tokens", "output_tokens", "cost", "ok"])
        w.writerow([temp, draw, itok, otok, "%.8f" % cost, 1 if ok else 0])

def append_draw(rec):
    with open(DRAWS, "a") as fh:
        fh.write(json.dumps(rec) + "\n")

def do_call(temp, draw):
    r = client.complete(model=MODEL, system=system, user=user, role="temp_study",
                        item_id=ITEM, prompt_version=VERSION, log=False)
    cost = bookkeeping.estimate_cost(MODEL, r.input_tokens, r.output_tokens, prices)
    parsed = rubric.parse_rubric(r.text, 1, 10) if r.ok else None
    return r, cost, parsed

def main():
    done = load_done()
    total_cost = load_cost()
    print(f"Resuming: {len(done)} draws already cached, ledger cost ${total_cost:.4f}",
          flush=True)

    for temp in TEMPS:
        tk = round(temp, 3)
        todo = [i for i in range(N) if (tk, i) not in done]
        if not todo:
            print(f"[temp {tk}] complete ({N}/{N} cached)", flush=True)
            continue
        client.temperature = temp  # all workers in this temp use same value
        print(f"[temp {tk}] running {len(todo)} draws", flush=True)
        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = {ex.submit(do_call, temp, i): i for i in todo}
            for fut in as_completed(futs):
                i = futs[fut]
                try:
                    r, cost, parsed = fut.result()
                except Exception as e:
                    print(f"  draw {i} raised {e}", flush=True)
                    continue
                with _lock:
                    total_cost += cost
                    append_ledger(tk, i, r.input_tokens, r.output_tokens, cost, r.ok)
                    if r.ok and parsed is not None:
                        rec = {"temperature": tk, "draw": i, "text": r.text,
                               "input_tokens": r.input_tokens,
                               "output_tokens": r.output_tokens, "cost": cost,
                               "ok": 1}
                        rec.update({k: parsed[k] for k in rubric.ALL_FIELDS})
                        rec["rationale"] = parsed.get("rationale", "")
                        append_draw(rec)
                        done.add((tk, i))
                completed += 1
                if total_cost >= COST_GUARD:
                    print(f"COST GUARD hit at ${total_cost:.4f}; stopping.", flush=True)
                    for f in futs:
                        f.cancel()
                    print(f"progress=STOP temp={tk} completed={completed}", flush=True)
                    return
        ok_ct = sum(1 for i in range(N) if (tk, i) in done)
        print(f"progress temp={tk} cached={ok_ct}/{N} total_cost=${total_cost:.4f}",
              flush=True)
    print(f"DONE all temps. total_cost=${total_cost:.4f}", flush=True)

if __name__ == "__main__":
    main()
