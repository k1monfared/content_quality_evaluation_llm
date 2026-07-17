"""Configuration loading and API key resolution.

All paths are resolved relative to the repository root so scripts work no
matter what directory they are launched from.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    cfg_path = Path(path) if path else REPO_ROOT / "configs" / "config.yaml"
    with open(cfg_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_prices(path: str | Path | None = None) -> dict[str, Any]:
    price_path = Path(path) if path else REPO_ROOT / "configs" / "prices.yaml"
    with open(price_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def resolve_path(rel: str | Path) -> Path:
    """Turn a config-relative path into an absolute path under the repo root."""
    p = Path(rel)
    return p if p.is_absolute() else REPO_ROOT / p


def get_api_key(cfg: dict[str, Any] | None = None) -> str | None:
    """Return the Poe API key from the first environment variable that is set.

    Loads a local .env if present. Returns None if no candidate is set, so
    callers can fail early with a clear message instead of a stack trace.
    """
    load_dotenv(REPO_ROOT / ".env")
    candidates = ["POE_API_KEY", "POE_KEY", "POE_TOKEN", "POE_API_TOKEN"]
    if cfg:
        candidates = cfg.get("api", {}).get("key_env_candidates", candidates)
    for name in candidates:
        val = os.getenv(name)
        if val:
            return val
    return None


def require_api_key(cfg: dict[str, Any] | None = None) -> str:
    key = get_api_key(cfg)
    if not key:
        raise SystemExit(
            "No Poe API key found. Set POE_API_KEY (or one of "
            "POE_KEY, POE_TOKEN, POE_API_TOKEN) in your environment or in a "
            ".env file. See .env.example."
        )
    return key
