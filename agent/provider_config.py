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


def get_all_tags() -> list[str]:
    """All distinct model tags declared in providers.json, sorted."""
    tags: set[str] = set()
    for m in _get_models():
        tags.update(m.get("tags") or [])
    return sorted(tags)


def get_min_context_window(tag: str | None = None, default: int = 128_000) -> int:
    """Smallest declared `context_window` among models actually reachable for
    this turn's provider fallback chain — same tag + env-var-present
    filtering as build_provider_order, so a size derived from this always
    fits whichever provider in the chain ends up serving the turn.

    BYOK is deliberately excluded: a user's own endpoint is an unknown
    quantity with no declared context_window. `default` is a conservative
    floor used when nothing reachable declares one (missing data, or no
    provider env vars set at all, e.g. in tests).
    """
    pmap = _provider_map()
    windows: list[int] = []
    for model_entry in _get_models():
        if tag and tag not in (model_entry.get("tags") or []):
            continue
        pconf = pmap.get(model_entry["provider"])
        if not pconf:
            continue
        env_var = pconf.get("api_key_env_var_name")
        if not (env_var and os.environ.get(env_var)):
            continue
        window = model_entry.get("context_window")
        if window:
            windows.append(window)
    return min(windows) if windows else default


def is_vision_model(model_name: str) -> bool:
    """True if `model_name` matches a model tagged "vision" in providers.json.

    `model_name` is expected in the form pydantic_ai's `RunContext.model.model_name`
    reports it: for HCAI-style models (built directly as an OpenAIChatModel) that's
    the raw `providers.json` "model" string; for provider-prefixed string models
    (e.g. "anthropic:claude-sonnet-4-6") pydantic_ai strips the "anthropic:" prefix
    before exposing model_name, so both forms are checked here.

    BYOK/unknown models (not present in providers.json at all) are treated as
    non-vision — same conservative default as get_min_context_window's BYOK
    exclusion: better to block a tool that needs real pixels than guess.
    """
    if not model_name:
        return False
    m = model_name.lower()
    for entry in _get_models():
        if "vision" not in (entry.get("tags") or []):
            continue
        raw = entry["model"].lower()
        if m == raw or m == raw.split(":", 1)[-1]:
            return True
    return False


_TAG_DIRECTIVE_RE = re.compile(r"(\\)?\[!WITH:([^\]]*)\]")


def extract_tag_directive(text: str) -> tuple[str, str | None, str | None]:
    """Parse a `[!WITH:tag]` directive out of `text`, forcing the provider
    fallback chain to only try models carrying that tag for the turn.

    A backslash immediately before the directive escapes it: only the
    backslash is stripped, the bracket text is left in place for the model to
    see literally, and no filter is applied.

    Returns (cleaned_text, tag, error):
    - `tag` is the matched tag (lowercased), or None if no live directive was
      found.
    - `error` is a ready-to-send message if a live directive named an unknown
      tag. When set, the caller should not proceed with the turn — nothing
      else about `cleaned_text`/`tag` is meaningful in that case.
    """
    found_tag: str | None = None
    invalid_tag: str | None = None
    known_tags = get_all_tags()

    def _sub(match: re.Match) -> str:
        nonlocal found_tag, invalid_tag
        escaped, raw_tag = match.group(1), match.group(2).strip()
        if escaped:
            return match.group(0)[1:]  # drop only the leading backslash
        if found_tag is None and invalid_tag is None:
            if raw_tag.lower() in known_tags:
                found_tag = raw_tag.lower()
            else:
                invalid_tag = raw_tag
        return ""

    cleaned = _TAG_DIRECTIVE_RE.sub(_sub, text)

    if invalid_tag is not None:
        available = ", ".join(f"`{t}`" for t in known_tags) or "(none configured)"
        error = (
            f"Invalid tag `{invalid_tag}`. Current tags are: {available}. "
            f"Use a backslash before it (like `\\[!WITH:{invalid_tag}]`) to send it literally instead."
        )
        return cleaned, None, error

    return cleaned, found_tag, None


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


def build_provider_order(user_id: str | None = None, tag: str | None = None) -> list[tuple[str, dict]]:
    """Build the provider fallback order from JSON config.

    Returns list of (provider_name, config_dict) pairs, same shape as the
    old _build_provider_order. BYOK is always prepended when a user endpoint
    exists. Entries whose env var is unset are skipped.

    `tag`, when given, restricts the order to only models tagged with it
    (see extract_tag_directive) — BYOK is excluded in that case since a
    user's own endpoint carries no tag classification.
    """
    from agent.agent import get_user_text_endpoint

    provider_order: list[tuple[str, dict]] = []

    # BYOK
    if user_id and not tag:
        user_endpoint = get_user_text_endpoint(user_id)
        if user_endpoint:
            provider_order.append(("byok", user_endpoint))

    pmap = _provider_map()
    for model_idx, model_entry in enumerate(_get_models()):
        if tag and tag not in (model_entry.get("tags") or []):
            continue
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
    """Build the vision provider chain for analyze_image (the OpenAI-compatible
    image-captioning fallback for non-vision-running-model turns) directly from
    the "vision" tag on providers.json's `models` — no separate hand-maintained
    list. That tag is already the single source of truth is_vision_model() (the
    computer_use gate) and the `[!WITH:vision]` fallback-chain filter both use;
    a second list here meant the same fact ("is this model vision-capable") had
    to be kept in sync by hand in two places.

    Only providers with a real `api_url` qualify — this hits models directly
    via OpenAI-compatible chat/completions (see agent/tools/vision.py), which
    plain env-var-based providers (anthropic, openai, google, groq, mistral —
    talked to through their native SDKs, not raw HTTP) can't serve.

    Returns list of (provider_label, base_url, api_key, model) tuples.
    """
    pmap = _provider_map()
    chain: list[tuple[str, str, str, str]] = []

    for entry in _get_models():
        if "vision" not in (entry.get("tags") or []):
            continue
        pid = entry["provider"]
        pconf = pmap.get(pid)
        if not pconf or not pconf.get("api_url"):
            continue

        env_var = pconf.get("api_key_env_var_name")
        api_key = os.environ.get(env_var) if env_var else None
        if not api_key:
            continue

        chain.append((pid, pconf["api_url"], api_key, entry["model"]))

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
