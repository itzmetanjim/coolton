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

# ModelMessage count (not conversation "turns" — tool calls/returns each add
# their own message) that triggers a compaction pass.
COMPACTION_MESSAGE_THRESHOLD = 60
# Most recent messages kept verbatim after compaction, so near-term context
# (the last few exchanges, in-flight tool results) survives untouched.
KEEP_TAIL_MESSAGES = 20

_TRANSCRIPT_CHAR_LIMIT = 20000
_PART_CHAR_LIMIT = 2000


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


def maybe_compact_history(messages: list[ModelMessage], deps) -> list[ModelMessage]:
    """Return `messages` unchanged if short enough, otherwise a compacted list: one
    synthetic summary message covering everything before the tail, plus the tail
    kept verbatim. Never raises — falls back to the untouched history on any error."""
    if len(messages) <= COMPACTION_MESSAGE_THRESHOLD:
        return messages

    head, tail = messages[:-KEEP_TAIL_MESSAGES], messages[-KEEP_TAIL_MESSAGES:]
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
        "Compacted thread history: %d messages -> 1 summary + %d tail messages",
        len(messages), len(tail),
    )
    return [summary_message, *tail]
