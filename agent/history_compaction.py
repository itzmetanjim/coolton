"""Compress long-running thread history instead of letting it grow unbounded.

thread_context/store.py persists every ModelMessage a thread has ever produced.
A thread that stays alive a long time (kevinton keeps replying in it, a
scheduled task posts into it, or someone just has a long conversation) pays to
re-send that entire raw history on every subsequent turn, and eventually pushes
real recent context out of the model's window entirely. This mirrors the idea
behind gorkie's "Observational Memory": once a thread crosses a size threshold,
fold everything except a recent tail into one dense summary message.

Deliberately NOT pydantic_ai's native `CompactionPart` — that is provider-native
(Anthropic/OpenAI specific) and "must be round-tripped back to the same
provider", which is incompatible with coolton's whole-chain provider fallback.
A plain text summary works identically regardless of which provider answers the
next turn.
"""

import logging

from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart

logger = logging.getLogger(__name__)

# What triggers a compaction pass, and how much survives it, both scale off
# the smallest context window actually reachable in the provider fallback
# chain (see get_min_context_window in provider_config.py, and
# _compaction_budget below) — NOT a fixed constant. Providers.json has models
# ranging from ~131K to 1M+ tokens; a threshold sized for the small end wastes
# most of a 1M-token model's headroom, while one sized for the large end would
# risk overflowing a 131K one. Deriving it per-turn from whichever models
# could actually serve this turn keeps it safe either way.
#
# Fractions of that context window: trigger compaction once accumulated
# history would use more than this share of it, and keep this share as a
# verbatim tail. The rest of the window is headroom this module doesn't
# account for — system prompt, tool definitions, the new turn's own content,
# and the model's output budget.
_COMPACTION_TRIGGER_FRACTION = 0.35
_KEEP_TAIL_FRACTION = 0.10
# Floors so a small/misconfigured context_window (or the get_min_context_window
# fallback default) never shrinks these to something impractically small.
_MIN_COMPACTION_TOKEN_THRESHOLD = 20_000
_MIN_KEEP_TAIL_TOKENS = 6_000

# Coolton's provider fallback chain spans several real tokenizers (Anthropic,
# OpenAI-compatible, Groq, ...) with no single correct token count between
# them. A chars/4 estimate is the standard rough heuristic for English text —
# cheap, dependency-free, and provider-agnostic. This decides "is this thread
# getting long", not anything billed, so it doesn't need to be exact.
_CHARS_PER_TOKEN = 4

_TRANSCRIPT_CHAR_LIMIT = 20000
_PART_CHAR_LIMIT = 2000


def _compaction_budget(context_window: int) -> tuple[int, int]:
    """(trigger_threshold, keep_tail_tokens) for a given context window."""
    threshold = max(int(context_window * _COMPACTION_TRIGGER_FRACTION), _MIN_COMPACTION_TOKEN_THRESHOLD)
    keep_tail = max(int(context_window * _KEEP_TAIL_FRACTION), _MIN_KEEP_TAIL_TOKENS)
    return threshold, keep_tail


def _message_size_chars(message: ModelMessage) -> int:
    """Rough character size of one message's content — including tool call
    args, not just text/tool-return content, since a large `code_mode` script
    or sandbox command is real context weight too."""
    total = 0
    for part in getattr(message, "parts", []):
        for attr in ("content", "args"):
            value = getattr(part, attr, None)
            if value:
                total += len(value) if isinstance(value, str) else len(str(value))
    return total


def _estimate_tokens(messages: list[ModelMessage]) -> int:
    return sum(_message_size_chars(m) for m in messages) // _CHARS_PER_TOKEN


def _render_for_summary(messages: list[ModelMessage]) -> str:
    """Flatten a slice of ModelMessage history into plain text for the summarizer."""
    lines = []
    for msg in messages:
        kind = type(msg).__name__
        for part in getattr(msg, "parts", []):
            content = getattr(part, "content", None)
            if not content:
                continue
            text = content if isinstance(content, str) else str(content)
            lines.append(f"[{kind}/{type(part).__name__}] {text[:_PART_CHAR_LIMIT]}")
    return "\n".join(lines)[:_TRANSCRIPT_CHAR_LIMIT]


def _summarize(transcript: str, deps) -> str:
    """Summarize via the summarizer subagent; fall back to a direct model call
    through the same providers.json fallback chain — never a hardcoded model."""
    task = (
        "This is an EARLIER PORTION of a longer Slack conversation, being compressed to "
        "save context for a bot that is still in the middle of it. Summarize it densely: "
        "preserve decisions, action items, open questions, concrete facts (ids, links, "
        "numbers, file/tool names), and anything a continuation would need. Drop small talk "
        "and redundant tool-call chatter. Plain text, no preamble.\n\n" + transcript
    )
    try:
        from agent.subagents import run_subagent
        summary = run_subagent("summarizer", task, deps)
        if summary:
            return summary
    except Exception:
        logger.exception("History compaction via summarizer subagent failed, falling back")

    from pydantic_ai.direct import model_request_sync
    from agent.provider_config import get_model_from_config

    response = model_request_sync(
        get_model_from_config(),
        [ModelRequest(parts=[UserPromptPart(content=task)])],
    )
    return "".join(p.content for p in response.parts if hasattr(p, "content"))


def _has_pending_tool_call(message: ModelMessage) -> bool:
    return any(
        getattr(part, "part_kind", None) == "tool-call"
        for part in getattr(message, "parts", [])
    )


def _safe_split_index(messages: list[ModelMessage], keep_tail_tokens: int) -> int:
    """Find a head/tail split that keeps roughly `keep_tail_tokens` of the most
    recent messages verbatim, without ever separating a tool call from its
    return.

    Walks backward from the end accumulating each message's estimated token
    size until the tail budget is spent (always keeping at least the last
    message, however large, so the tail is never empty). Landing that
    boundary right between a ModelResponse's ToolCallPart(s) and the
    ModelRequest immediately after it carrying the matching ToolReturnPart(s)
    would leave an orphaned function_call_output with no matching
    function_call — every provider rejects that on the next turn ("No tool
    call found for function call output with call_id ..."). So after finding
    the token-budget boundary, walk it further left past any ModelResponse
    that still has an unresolved tool call sitting right at that point.
    """
    tail_tokens = 0
    split = len(messages)
    while split > 0:
        candidate_tokens = _message_size_chars(messages[split - 1]) // _CHARS_PER_TOKEN
        if tail_tokens > 0 and tail_tokens + candidate_tokens > keep_tail_tokens:
            break
        tail_tokens += candidate_tokens
        split -= 1
    while split > 0 and _has_pending_tool_call(messages[split - 1]):
        split -= 1
    return split


def maybe_compact_history(messages: list[ModelMessage], deps) -> list[ModelMessage]:
    """Return `messages` unchanged if small enough, otherwise a compacted list: one
    synthetic summary message covering everything before the tail, plus the tail
    kept verbatim. Never raises — falls back to the untouched history on any error."""
    from agent.provider_config import get_min_context_window

    total_tokens = _estimate_tokens(messages)
    context_window = get_min_context_window(getattr(deps, "provider_tag_filter", None))
    threshold, keep_tail_tokens = _compaction_budget(context_window)
    if total_tokens <= threshold:
        return messages

    split = _safe_split_index(messages, keep_tail_tokens)
    head, tail = messages[:split], messages[split:]
    transcript = _render_for_summary(head)
    if not transcript:
        return messages

    try:
        summary = _summarize(transcript, deps)
    except Exception:
        logger.exception("History compaction summarization failed; keeping full history")
        return messages

    if not summary.strip():
        return messages

    summary_message = ModelRequest(
        parts=[
            UserPromptPart(
                content=(
                    f"[Earlier conversation summary — {len(head)} older messages compressed "
                    "to save context. Treat this as prior context, not a new request.]\n"
                    f"{summary.strip()}"
                )
            )
        ]
    )
    logger.info(
        "Compacted thread history: ~%d tokens (%d messages) -> 1 summary + "
        "%d tail messages (~%d tokens)",
        total_tokens, len(messages), len(tail), _estimate_tokens(tail),
    )
    return [summary_message, *tail]
