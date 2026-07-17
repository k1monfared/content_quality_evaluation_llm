"""Tiny append-only CSV store used as the reproducible database.

Both the human panel and the judge pipeline persist every parsed record here.
A record is keyed so that re-runs skip work that is already cached, which makes
the whole study resumable and cheap to iterate on.
"""
from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

# Serializes appends so concurrent workers never interleave rows in a CSV.
_WRITE_LOCK = threading.Lock()


def load(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.exists() and p.stat().st_size > 0:
        return pd.read_csv(p)
    return pd.DataFrame()


def existing_keys(path: str | Path, key_cols: list[str]) -> set[tuple]:
    df = load(path)
    if df.empty or not set(key_cols).issubset(df.columns):
        return set()
    return set(tuple(str(v) for v in row) for row in df[key_cols].itertuples(index=False))


def append_row(path: str | Path, row: dict[str, Any], columns: Iterable[str]) -> None:
    p = Path(path)
    with _WRITE_LOCK:
        p.parent.mkdir(parents=True, exist_ok=True)
        write_header = not (p.exists() and p.stat().st_size > 0)
        with open(p, "a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(columns))
            if write_header:
                w.writeheader()
            w.writerow({k: row.get(k, "") for k in columns})
