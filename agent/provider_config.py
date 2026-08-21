"""JSON-driven provider/model configuration.

Reads providers.json from the repo root and exposes helpers for:
- Building the provider fallback order
- Building the vision provider chain
- Setting env vars for a provider
- Resolving display names
- Getting the first viable model string
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_JSON_PATH = Path(__file__).resolve().parent.parent / "providers.json"
_providers: dict[str, Any] | None = None


def _load() -> dict[str, Any]:
    global _providers
    if _providers is None:
        _providers = json.loads(_JSON_PATH.read_text())
    return _providers


def _load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load config from a specific path (for testing)."""
    global _providers
    p = Path(path) if path else _JSON_PATH
    _providers = json.loads(p.read_text())
    return _providers


def _reset():
    """Reset cached config (for testing)."""
    global _providers
    _providers = None


def _get_providers() -> list[dict]:
    return _load()["providers"]


def _get_models() -> list[dict]:
    return _load()["models"]


def _get_vision_models() -> list[dict]:
    return _load().get("vision_models", [])


def _provider_map() -> dict[str, dict]:
    return {p["id"]: p for p in _get_providers()}


def _make_provider_name(provider_id: str, model_index: int) -> str:
    """Generate a unique name for each (provider, model) entry."""
    count = sum(1 for m in _get_models() if m["provider"] == provider_id)
    if count <= 1:
        return provider_id
    idx = 0
    for i, m in enumerate(_get_models()):
        if m["provider"] == provider_id:
            if i == model_index:
                return f"{provider_id}_{idx}"
            idx += 1
    return f"{provider_id}_{model_index}"


def build_provider_order(user_id: str | None = None) -> list[tuple[str, dict]]:
    """Build the provider fallback order from JSON config.

    Returns list of (provider_name, config_dict) pairs, same shape as the
    old _build_provider_order. BYOK is always prepended when a user endpoint
    exists. Entries whose env var is unset are skipped.
    """
    from agent.agent import get_user_text_endpoint

    provider_order: list[tuple[str, dict]] = []

    # BYOK
    if user_id:
        user_endpoint = get_user_text_endpoint(user_id)
        if user_endpoint:
            provider_order.append(("byok", user_endpoint))

    pmap = _provider_map()
    for model_idx, model_entry in enumerate(_get_models()):
        pid = model_entry["provider"]
        pconf = pmap.get(pid)
        if not pconf:
            continue

        env_var = pconf.get("api_key_env_var_name")
        api_key = os.environ.get(env_var) if env_var else None
        if not api_key:
            continue  # Provider needs its env var set

        name = _make_provider_name(pid, model_idx)
        config: dict[str, Any] = {
            "model": model_entry["model"],
            "base_url": pconf.get("api_url"),
            "api_key": api_key or "",
            "display": get_provider_display(model_entry, pmap),
        }
        if pconf.get("max_retries"):
            config["max_retries"] = pconf["max_retries"]
        provider_order.append((name, config))

    return provider_order


def build_vision_chain() -> list[tuple[str, str, str, str]]:
    """Build the vision provider chain from JSON config.

    Returns list of (provider_label, base_url, api_key, model) tuples.
    """
    pmap = _provider_map()
    chain: list[tuple[str, str, str, str]] = []

    for entry in _get_vision_models():
        pid = entry["provider"]
        pconf = pmap.get(pid)
        if not pconf:
            continue

        env_var = pconf.get("api_key_env_var_name")
        api_key = os.environ.get(env_var) if env_var else None
        if not api_key:
            continue

        chain.append((pid, pconf.get("api_url") or "", api_key, entry["model"]))

    return chain


def apply_provider_env(provider_name: str, api_key: str) -> None:
    """Set the provider API key env var for pydantic-ai.

    Looks up the provider's api_key_env_var_name from the JSON config.
    Skips BYOK and providers with a base_url (HCAI-style).
    """
    if not api_key or provider_name == "byok":
        return

    # Find the provider config by name, including aliases. Generated names look
    # like "<provider_id>_<index>" (see _make_provider_name) — strip the trailing
    # index for an exact match rather than prefix-matching, which could pick the
    # wrong provider if one id happens to prefix another (dict order is unordered).
    pmap = _provider_map()
    pconf = pmap.get(provider_name)
    if pconf is None:
        base = re.sub(r"_\d+$", "", provider_name)
        pconf = pmap.get(base)
    if pconf is None:
        for pc in pmap.values():
            if provider_name in (pc.get("aliases") or []):
                pconf = pc
                break
    if not pconf:
        return

    if pconf.get("api_url"):
        return  # HCAI-style: uses base_url + api_key, not env var

    env_var = pconf.get("pydantic_ai_env_var") or pconf.get("api_key_env_var_name")
    if env_var:
        os.environ[env_var] = api_key


def get_provider_display(model_entry: dict, provider_map: dict | None = None) -> str:
    """Get display name for a model entry.

    Uses the 'display' field if present, otherwise auto-generates from
    provider displayname + model name.
    """
    if model_entry.get("display"):
        return model_entry["display"]

    pmap = provider_map or _provider_map()
    pconf = pmap.get(model_entry["provider"], {})
    displayname = pconf.get("displayname", model_entry["provider"])
    return f"{displayname} {model_entry['model']}"


def get_model_from_config(user_id: str | None = None) -> str:
    """Get the first viable model string from config.

    Replaces the old get_model() function. Returns a string model name
    for standard providers, or a configured OpenAIChatModel for base_url
    providers (HCAI/BYOK).
    """
    for model_entry in _get_models():
        pid = model_entry["provider"]
        pconf = _provider_map().get(pid, {})
        env_var = pconf.get("api_key_env_var_name")
        api_key = os.environ.get(env_var) if env_var else None

        if not api_key:
            continue

        model_name = model_entry["model"]

        if pconf.get("api_url"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider

            return OpenAIChatModel(
                model_name,
                provider=OpenAIProvider(
                    base_url=pconf["api_url"],
                    api_key=api_key,
                ),
            )

        return model_name

    raise RuntimeError(
        "No AI provider configured. Set at least one supported provider key: "
        + ", ".join(
            p.get("api_key_env_var_name", p["id"])
            for p in _get_providers()
            if p.get("api_key_env_var_name")
        )
        + "."
    )
