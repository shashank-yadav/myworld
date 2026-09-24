"""Token usage normalization and cost estimation.

If an integration reports a cost (``attrs["cost_usd"]`` or ``usage.cost``), that wins.
Otherwise cost is estimated from the price table below, which can be extended or
overridden with ``$AOPS_HOME/pricing.json``::

    {"gpt-5": {"input": 1.25, "output": 10.0, "cache_read": 0.125}}

Prices are USD per million tokens. Models not in the table are reported as unpriced
rather than guessed.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse

from .store import default_home

# Anthropic first-party list prices (per 1M tokens). Cache writes are billed at 1.25x
# input and cache reads at 0.1x input unless stated.
_CLAUDE = {
    "claude-fable-5-1": (10.0, 50.0, 0.25),
    "claude-mythos-5-1": (10.0, 50.0, None),
    "claude-fable-5": (10.0, 50.0, None),
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-opus-5": (5.0, 25.0, None),
    "claude-opus-4-8": (5.0, 25.0, None),
    "claude-opus-4-7": (5.0, 25.0, None),
    "claude-opus-4-6": (5.0, 25.0, None),
    "claude-sonnet-5": (2.0, 10.0, None),
    "claude-sonnet-4-6": (3.0, 15.0, None),
    "claude-haiku-4-5": (1.0, 5.0, None),
}

DEFAULT_PRICES: dict[str, dict[str, float]] = {
    model: {
        "input": inp,
        "output": out,
        "cache_read": cache_read if cache_read is not None else inp * 0.1,
        "cache_write": inp * 1.25,
    }
    for model, (inp, out, cache_read) in _CLAUDE.items()
}


@lru_cache(maxsize=1)
def price_table() -> dict[str, dict[str, float]]:
    table = dict(DEFAULT_PRICES)
    path = default_home() / "pricing.json"
    try:
        table.update(json.loads(path.read_text()))
    except (OSError, ValueError):
        pass
    return table


def canonical_model(model: str | None) -> str:
    """'anthropic/claude-opus-5-20260401' -> 'claude-opus-5'; 'claude-opus-4-5@20251101' -> 'claude-opus-4-5'."""
    if not model:
        return ""
    m = model.strip().lower().rsplit("/", 1)[-1]
    m = m.removeprefix("anthropic.").removeprefix("us.anthropic.")
    m = m.split("@", 1)[0]
    m = re.sub(r"-(\d{8})(-v\d+(:\d+)?)?$", "", m)
    m = m.replace(".", "-") if m.startswith("claude") else m
    return m


def lookup(model: str | None) -> dict[str, float] | None:
    table = price_table()
    m = canonical_model(model)
    if m in table:
        return table[m]
    # longest known prefix, so 'claude-opus-5-fast' still prices as claude-opus-5
    best = max((k for k in table if m.startswith(k)), key=len, default=None)
    return table[best] if best else None


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def normalize_usage(usage: Any) -> dict[str, int]:
    """Map Anthropic / OpenAI / OpenClaw usage shapes to one dict.

    ``input`` excludes cached tokens so the four buckets never double count.
    """
    if not isinstance(usage, dict):
        return {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    cache_read = _int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or usage.get("cacheRead")
                      or usage.get("cache_read") or usage.get("cached_tokens"))
    cache_write = _int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens")
                       or usage.get("cacheWrite") or usage.get("cache_write"))
    output = _int(usage.get("output_tokens") or usage.get("completion_tokens") or usage.get("output"))
    if "prompt_tokens" in usage:  # OpenAI: prompt_tokens includes cached tokens
        details = usage.get("prompt_tokens_details") or {}
        cache_read = cache_read or _int(details.get("cached_tokens"))
        inp = max(_int(usage["prompt_tokens"]) - cache_read, 0)
    else:
        inp = _int(usage.get("input_tokens") or usage.get("input"))
    return {"input": inp, "output": output, "cache_read": cache_read, "cache_write": cache_write}


_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0", "[::1]", "host.docker.internal")


def is_local(base_url: Any = None, provider: Any = None) -> bool:
    """Models served on this machine (Ollama, LM Studio, llama.cpp, vLLM on localhost) cost nothing per token."""
    if str(provider or "").lower() in ("ollama", "lmstudio", "llamacpp"):
        return True
    host = urlparse(str(base_url or "")).hostname or ""
    return host in _LOCAL_HOSTS or host.endswith(".local")


def estimate_cost(model: str | None, usage: Any, reported: Any = None, *, base_url: Any = None,
                  provider: Any = None) -> float | None:
    """USD cost of one LLM call, or None if the model is unpriced and no cost was reported."""
    for v in (reported, usage.get("cost") if isinstance(usage, dict) else None):
        if isinstance(v, dict):
            v = v.get("total")
        if isinstance(v, (int, float)):
            return float(v)
    if is_local(base_url, provider):
        return 0.0
    prices = lookup(model)
    if prices is None:
        return None
    u = normalize_usage(usage)
    return sum(u[k] * prices.get(k, 0.0) for k in u) / 1_000_000
