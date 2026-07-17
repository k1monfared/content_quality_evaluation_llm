"""STEP 2 and 3 (v2): lenient-parse runner.

Scores item_0105 with claude-haiku-4.5 (v3 prompt) up to 300 valid draws per
temperature in {0, 0.1, ..., 1.0}. A draw is valid when a numeric overall score
is present. At higher temperature the model sometimes writes a non-numeric
"N/A" for the coverage dimension; the lenient parser keeps that draw (overall
plus the other numeric dimensions) and records which dimensions were N/A, so the
schema-violation rate becomes part of the consistency finding instead of a lost
draw. Reuses whatever is already cached, uses log=False so the committed
api_cost_log.csv is untouched, and self-stops if the separate ledger approaches
the budget cap.
"""
import sys, os, json, csv, re, threading
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
MAX_PASSES = 6  # bounded retry rounds per temperature to converge to N valid

cfg = load_config()
prices = load_prices()
client = PoeClient(cfg, prices, mock=False)

data = pd.read_csv(os.path.join(ROOT, "data", "wiki_sample.csv"))
passage = str(data[data["item_id"] == ITEM].iloc[0]["text"])
system = load_judge_prompt(MODEL, VERSION)
user = rubric.build_user_prompt(passage, 1, 10)

_lock = threading.Lock()


def parse_lenient(text):
    """Return dict with all rubric fields (None where non-numeric) plus the list
    of N/A dimensions, or None if no numeric overall score can be recovered."""
    if not text or text.startswith("__ERROR__"):
        return None
    m = re.search(r"\{.*\}", text.strip(), re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    out = {}
    na = []
    for f in rubric.ALL_FIELDS:
        if f not in obj:
            if f == "overall":
                return None
            out[f] = None
            na.append(f)
            continue
        v = obj[f]
        raw = v.get("score") if isinstance(v, dict) else v
        try:
            out[f] = max(1.0, min(10.0, float(raw)))
        except (TypeError, ValueError):
            if f == "overall":
                return None
            out[f] = None
            na.append(f)
    ov = obj.get("overall")
    out["rationale"] = str(ov.get("reason", ""))[:300] if isinstance(ov, dict) else ""
    out["_na"] = na
    return out


def load_done():
    done = set()
    if os.path.exists(DRAWS):
        with open(DRAWS) as fh:
            for line in fh:
                line = line.strip()
                if line:
                    d = json.loads(line)
                    done.add((round(float(d["temperature"]), 3), int(d["draw"])))
    return done


def load_cost():
    total = 0.0
    if os.path.exists(LEDGER):
        with open(LEDGER) as fh:
            for row in csv.DictReader(fh):
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
    parsed = parse_lenient(r.text) if r.ok else None
    return r, cost, parsed


def main():
    done = load_done()
    total_cost = load_cost()
    print(f"Resuming: {len(done)} valid draws cached, ledger cost ${total_cost:.4f}",
          flush=True)

    for temp in TEMPS:
        tk = round(temp, 3)
        client.temperature = temp
        for pass_i in range(MAX_PASSES):
            have = sum(1 for i in range(N) if (tk, i) in done)
            if have >= N:
                break
            # assign call slots to the not-yet-valid indices
            todo = [i for i in range(N) if (tk, i) not in done]
            print(f"[temp {tk}] pass {pass_i}: have {have}/{N}, attempting {len(todo)}",
                  flush=True)
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = {ex.submit(do_call, tk, i): i for i in todo}
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
                        if parsed is not None and (tk, i) not in done:
                            rec = {"temperature": tk, "draw": i, "text": r.text,
                                   "input_tokens": r.input_tokens,
                                   "output_tokens": r.output_tokens, "cost": cost,
                                   "ok": 1}
                            for k in rubric.ALL_FIELDS:
                                rec[k] = parsed[k]
                            rec["rationale"] = parsed["rationale"]
                            rec["na_dims"] = parsed["_na"]
                            append_draw(rec)
                            done.add((tk, i))
                    if total_cost >= COST_GUARD:
                        print(f"COST GUARD hit at ${total_cost:.4f}; stopping.", flush=True)
                        return
        have = sum(1 for i in range(N) if (tk, i) in done)
        print(f"progress temp={tk} valid={have}/{N} total_cost=${total_cost:.4f}",
              flush=True)
    print(f"DONE all temps. total_cost=${total_cost:.4f}", flush=True)


if __name__ == "__main__":
    main()
