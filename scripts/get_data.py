"""Download and sample the Wikipedia passage dataset.

Usage:
    python scripts/get_data.py

Writes a filtered sample to data/wiki_sample.csv. No API key needed. The source
is the Hugging Face datasets-server rows API (no authentication).
"""
import _bootstrap  # noqa: F401
from src import data
from src.config import load_config, resolve_path


def main():
    cfg = load_config()
    df = data.acquire(cfg)
    out = resolve_path(cfg["paths"]["data_csv"])
    data.save_sample(df, out)
    print(f"Wrote {len(df)} passages to {out}")
    print(f"Source dataset: {df['source_dataset'].iloc[0]}")
    print("Passage length chars: "
          f"min={df['char_count'].min()} "
          f"median={int(df['char_count'].median())} "
          f"max={df['char_count'].max()}")


if __name__ == "__main__":
    main()
