"""Tracing to Langfuse (https://langfuse.com), on when LANGFUSE_PUBLIC_KEY and
LANGFUSE_SECRET_KEY are set (LANGFUSE_BASE_URL picks the region or a self-hosted instance;
Langfuse's own default otherwise). Without them nothing here does anything.

Pydantic AI's OpenTelemetry instrumentation (Agent.instrument_all) records every model run
(prompts, replies, tool calls and their results, models, tokens, timings) and the Langfuse
client exports those spans in the background. run() tags a run with who it's for and its
conversation, so a thread's turns, their subagents and a huddle's replies group into one
Langfuse session. Known secrets are masked out of every span before it's sent
(agent.redact), as they are from coolton's own traces.
"""
from __future__ import annotations

import logging
import os
from contextlib import nullcontext

logger = logging.getLogger(__name__)

_enabled = False


def enabled() -> bool:
    return _enabled


def setup() -> None:
    """Start tracing if it's configured. Never raises: coolton runs the same without it."""
    global _enabled
    if _enabled or not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return
    try:
        from langfuse import Langfuse
        from pydantic_ai import Agent
        from pydantic_ai.models.instrumented import InstrumentationSettings

        Langfuse(mask_otel_spans=mask_secrets)
        # Images stay out: they'd make every span huge, and the text says what they were.
        Agent.instrument_all(InstrumentationSettings(include_binary_content=False))
        from agent import shutdown

        shutdown.on_sigterm(flush)
        _enabled = True
        logger.info("Langfuse tracing is on")
    except Exception:
        logger.exception("Langfuse tracing couldn't start; carrying on without it")


def run(*, name: str, user_id: str | None = None, channel_id: str = "", thread_ts: str = "", tags=()):
    """Tag the model runs inside this block (a context manager, also in async code)."""
    if not _enabled:
        return nullcontext()
    from langfuse import propagate_attributes

    session = f"{channel_id}:{thread_ts}" if channel_id else None
    return propagate_attributes(user_id=user_id or None, session_id=session, trace_name=name, tags=list(tags) or None)


def mask_secrets(*, params):
    """Langfuse's mask_otel_spans hook: every known secret in a span's text, as ***."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    from agent.redact import redact

    patches = {}
    for identifier, span in params.spans.items():
        changed = {}
        for key, value in span.attributes.items():
            if isinstance(value, str):
                masked = redact(value, notify=False)
                if masked != value:
                    changed[key] = masked
        if changed:
            patches[identifier] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches) if patches else None


def flush() -> None:
    """Send what's still buffered (before a restart)."""
    if _enabled:
        from langfuse import get_client

        get_client().flush()
