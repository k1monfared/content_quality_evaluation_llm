"""Wikipedia passage acquisition.

Source: the Hugging Face datasets-server "rows" HTTP API, which streams rows
as JSON with no authentication and no heavy client library. The primary dataset
is English Wikipedia (wikimedia/wikipedia), from which we extract paragraph
level passages whose writing quality genuinely varies, which is exactly what a
quality judge should have to grade. WikiText-103 (raw), also derived from
verified-good Wikipedia articles, is the fallback if the primary is
unreachable. Both are licensed CC BY-SA (see configs/config.yaml).

The item is a single text passage, not a paired prompt and response. Only a
modest seeded, filtered sample is written to data/ and committed.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

_ROWS_URL = "https://datasets-server.huggingface.co/rows"
# datasets-server caps length at 100 rows per request, but full Wikipedia
# articles are large, so we page in small batches to stay under the timeout.
_PAGE = 25
_TIMEOUT_S = 60

# Markers that indicate a list item, table row, heading, or other non-prose
# fragment we do not want as an encyclopedic passage.
_LIST_PREFIXES = ("*", "-", "•", "#", "|", "=", ":", ";")
_ARTIFACT_MARKERS = ("@-@", "@,@", "@.@")


def _fetch_rows(dataset: str, config: str, split: str, pool: int) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while len(rows) < pool:
        length = min(_PAGE, pool - len(rows))
        qs = urllib.parse.urlencode(
            {"dataset": dataset, "config": config, "split": split,
             "offset": offset, "length": length}
        )
        req = urllib.request.Request(f"{_ROWS_URL}?{qs}",
                                     headers={"User-Agent": "curl/8"})
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            payload = json.load(resp)
        batch = payload.get("rows", [])
        if not batch:
            break
        rows.extend(r["row"] for r in batch)
        offset += length
        time.sleep(0.2)
    return rows


def _looks_like_prose(passage: str) -> bool:
    """Keep only clean prose paragraphs, dropping list and table fragments."""
    if not passage:
        return False
    if passage[0] in _LIST_PREFIXES:
        return False
    if any(m in passage for m in _ARTIFACT_MARKERS):
        return False
    # Table-like: several pipe separators or embedded tabs.
    if passage.count("|") >= 2 or "\t" in passage:
        return False
    # Must read like a sentence: contain a period and end on terminal
    # punctuation, and have a reasonable number of words.
    if "." not in passage:
        return False
    if passage[-1] not in ".!?\"')":
        return False
    if len(passage.split()) < 20:
        return False
    return True


def _extract_passages(raw: list[dict], min_chars: int, max_chars: int
                      ) -> list[str]:
    """Split each source row's text into paragraph-level passages and filter.

    Wikipedia article text separates paragraphs with newlines; WikiText rows are
    already one line each. Splitting on any run of newlines handles both.
    """
    seen: set[str] = set()
    out: list[str] = []
    for r in raw:
        text = str(r.get("text") or "")
        for block in re.split(r"\n+", text):
            passage = re.sub(r"\s+", " ", block).strip()
            if not (min_chars <= len(passage) <= max_chars):
                continue
            if not _looks_like_prose(passage):
                continue
            if passage in seen:
                continue
            seen.add(passage)
            out.append(passage)
    return out


def acquire(cfg: dict[str, Any]) -> pd.DataFrame:
    d = cfg["dataset"]
    min_chars, max_chars = d["min_chars"], d["max_chars"]
    try:
        raw = _fetch_rows(d["source"], d["source_config"], d["source_split"],
                          d["fetch_articles"])
        used = d["source"]
    except Exception as e:
        print(f"Primary dataset failed ({e}); using fallback {d['fallback']}")
        raw = _fetch_rows(d["fallback"], d["fallback_config"],
                          d["fallback_split"], d["fetch_articles"])
        used = d["fallback"]

    passages = _extract_passages(raw, min_chars, max_chars)
    df = pd.DataFrame({"text": passages})
    df = df.drop_duplicates(subset=["text"]).reset_index(drop=True)

    # Seeded sample that spans the full length band so quality and length both
    # vary. Sorting by length before sampling and drawing a shuffled subset
    # keeps a stable spread across short, medium, and long passages.
    n = min(d["n_items"], len(df))
    df = df.sample(n=n, random_state=d["seed"]).reset_index(drop=True)
    df.insert(0, "item_id", [f"item_{i:04d}" for i in range(len(df))])
    df["source_dataset"] = used
    df["char_count"] = df["text"].str.len()
    return df[["item_id", "text", "source_dataset", "char_count"]]


def save_sample(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)


def load_sample(path: str | Path) -> pd.DataFrame:
    return pd.read_csv(path)
