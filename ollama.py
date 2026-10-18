"""Ollama: free open models running on this computer, used when no API key is set.

Install from https://ollama.com, then download a model, e.g. `ollama pull qwen3:8b`
(about 5 GB; runs on most laptops) or `ollama pull gpt-oss:20b` (needs ~16 GB of
GPU or system memory; writes noticeably better lessons). OLLAMA_URL and
OLLAMA_MODEL in .env change the server address and the model.
"""
from __future__ import annotations

import os
import threading

import requests

DEFAULT_URL = "http://localhost:11434"
# Best first. A model not on this list is still used if it's the only one installed.
PREFERRED = ["gpt-oss:20b", "qwen3:30b", "qwen3:14b", "gemma3:12b", "qwen3:8b", "llama3.1:8b", "qwen3:4b"]

_cache: dict = {}
_lock = threading.Lock()


def url() -> str:
    return (os.getenv("OLLAMA_URL") or DEFAULT_URL).strip().rstrip("/")


def installed(refresh: bool = False) -> list[str] | None:
    """Model names on the local Ollama server, or None if it isn't running."""
    with _lock:
        if not refresh and "models" in _cache:
            return _cache["models"]
    try:
        resp = requests.get(f"{url()}/api/tags", timeout=2)
        resp.raise_for_status()
        models = [m["name"] for m in resp.json().get("models") or []]
    except (requests.RequestException, ValueError, KeyError):
        models = None
    with _lock:
        _cache["models"] = models
    return models


def pick_model(configured: str | None = None) -> str | None:
    """OLLAMA_MODEL, else the configured model, else the best installed one."""
    models = installed()
    if not models:
        return None
    wanted = (os.getenv("OLLAMA_MODEL") or "").strip() or (configured if configured not in (None, "", "auto") else "")
    if wanted:
        return wanted
    names = set(models) | {m.removesuffix(":latest") for m in models}
    return next((m for m in PREFERRED if m in names), models[0])


def status() -> dict:
    models = installed(refresh=True)
    return {"running": models is not None, "models": models or [], "model": pick_model() if models else None,
            "url": url()}
