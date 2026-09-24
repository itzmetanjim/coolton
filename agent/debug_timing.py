"""`[!DEBUG]`: per-turn timing breakdown, posted in the thread after the reply.

A message containing `[!DEBUG]` (escape it as `\\[!DEBUG]` to send it literally)
gets an AgentDeps.debug_timer. Everything a turn spends time on records into it:

- turn phases (setup, the agent run, posting the reply, saving history) —
  listeners/events/turn.py;
- every provider attempt and retry backoff sleep — the fallback loop in
  agent.agent._run_with_provider_chain;
- every model request and tool call — build_timing_hooks, a pydantic-ai
  capability added to the run only when the timer exists.

format_report() then turns it into a total, a per-category breakdown, the
provider attempts, and a chronological timeline.
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

_DEBUG_DIRECTIVE_RE = re.compile(r"(\\)?\[!DEBUG\]", re.IGNORECASE)
# Keeps the report well under Slack's markdown_text size limit on long turns.
_MAX_TIMELINE_ROWS = 60


def extract_debug_directive(text: str) -> tuple[str, bool]:
    """(text without any live `[!DEBUG]`, whether one was found). A leading
    backslash escapes it: only the backslash is dropped and no timing runs."""
    found = False

    def _sub(match: re.Match) -> str:
        nonlocal found
        if match.group(1):
            return match.group(0)[1:]
        found = True
        return ""

    cleaned = _DEBUG_DIRECTIVE_RE.sub(_sub, text)
    return (cleaned.strip() if found else cleaned), found


@dataclass
class Span:
    category: str  # "phase", "model", "tool", "attempt", "backoff"
    label: str
    start: float
    end: float
    detail: str = ""

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class TurnTimer:
    started_at: float = field(default_factory=time.perf_counter)
    spans: list[Span] = field(default_factory=list)
    current_provider: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, category: str, label: str, start: float, end: float, detail: str = "") -> None:
        with self._lock:
            self.spans.append(Span(category, label, start, end, detail))

    def _of(self, category: str) -> list[Span]:
        with self._lock:
            return [s for s in self.spans if s.category == category]

    def format_report(self) -> str:
        total = time.perf_counter() - self.started_at
        phases = self._of("phase")
        models, tools = self._of("model"), self._of("tool")
        attempts, backoffs = self._of("attempt"), self._of("backoff")

        lines = [f"**:stopwatch: [!DEBUG] timing — {total:.2f}s total**", "", "**Where the time went**"]
        run = next((p for p in phases if p.label == "agent run"), None)
        for phase in phases:
            lines.append(f"• {phase.label}: {_fmt(phase.duration)} ({_pct(phase.duration, total)})")
            if phase is run:
                model_time = sum(s.duration for s in models)
                tool_time = sum(s.duration for s in tools)
                backoff_time = sum(s.duration for s in backoffs)
                other = max(0.0, phase.duration - model_time - tool_time - backoff_time)
                lines.append(f"    ◦ model requests: {_fmt(model_time)} across {len(models)} call(s)")
                lines.append(f"    ◦ tool calls: {_fmt(tool_time)} across {len(tools)} call(s)"
                             + (" — overlapping calls ran in parallel, so this can exceed wall time" if _overlaps(tools) else ""))
                if backoffs:
                    lines.append(f"    ◦ retry backoff (sleeping between attempts): {_fmt(backoff_time)}")
                lines.append(f"    ◦ other (prompt/tool setup, provider setup, status updates, hooks): {_fmt(other)}")
        unaccounted = total - sum(p.duration for p in phases)
        if unaccounted > 0.05:
            lines.append(f"• other turn overhead: {_fmt(unaccounted)} ({_pct(unaccounted, total)})")

        if len(attempts) > 1 or any(a.detail for a in attempts):
            lines += ["", "**Provider attempts**"]
            for a in attempts:
                lines.append(f"• {a.label} — {_fmt(a.duration)} — {a.detail or 'ok'}")

        if models:
            lines += ["", "**Slowest model requests**"]
            for s in sorted(models, key=lambda s: s.duration, reverse=True)[:5]:
                lines.append(f"• {s.label} — {_fmt(s.duration)}" + (f" ({s.detail})" if s.detail else ""))
        if tools:
            lines += ["", "**Tool time by tool**"]
            by_tool: dict[str, list[float]] = {}
            for s in tools:
                by_tool.setdefault(s.label, []).append(s.duration)
            for name, durations in sorted(by_tool.items(), key=lambda kv: sum(kv[1]), reverse=True):
                lines.append(f"• `{name}` — {_fmt(sum(durations))}" + (f" over {len(durations)} calls" if len(durations) > 1 else ""))

        timeline = sorted(
            (s for s in self.spans if s.category != "phase"), key=lambda s: s.start,
        )
        if timeline:
            rows = []
            for s in timeline:
                offset = s.start - self.started_at
                kind = {"model": "model", "tool": "tool", "attempt": "attempt", "backoff": "backoff"}.get(s.category, s.category)
                row = f"+{offset:7.2f}s  {s.duration:7.2f}s  {kind:<8} {s.label}"
                if s.detail and s.category != "model":
                    row += f"  [{s.detail[:80]}]"
                rows.append(row)
            if len(rows) > _MAX_TIMELINE_ROWS:
                rows = rows[:_MAX_TIMELINE_ROWS] + [f"... {len(rows) - _MAX_TIMELINE_ROWS} more"]
            lines += ["", "**Timeline** (start offset, duration)", "```\n" + "\n".join(rows) + "\n```"]
        return "\n".join(lines)


def _fmt(seconds: float) -> str:
    return f"{seconds * 1000:.0f}ms" if seconds < 1 else f"{seconds:.2f}s"


def _pct(part: float, total: float) -> str:
    return f"{(part / total * 100) if total > 0 else 0:.0f}%"


def _overlaps(spans: list[Span]) -> bool:
    ordered = sorted(spans, key=lambda s: s.start)
    return any(b.start < a.end for a, b in zip(ordered, ordered[1:]))


def build_timing_hooks(timer: TurnTimer):
    """A pydantic-ai capability timing every model request and tool call in a run."""
    from pydantic_ai.capabilities import Hooks

    hooks = Hooks()

    @hooks.on.model_request
    async def time_model_request(ctx, *, request_context, handler):
        start = time.perf_counter()
        model_name = getattr(getattr(ctx, "model", None), "model_name", "") or "model"
        label = f"{timer.current_provider} / {model_name}" if timer.current_provider else model_name
        try:
            response = await handler(request_context)
        except Exception as e:
            timer.record("model", label, start, time.perf_counter(), f"failed: {type(e).__name__}")
            raise
        usage = getattr(response, "usage", None)
        detail = ""
        if usage is not None:
            detail = f"in {getattr(usage, 'input_tokens', 0) or 0:,} / out {getattr(usage, 'output_tokens', 0) or 0:,} tokens"
        timer.record("model", label, start, time.perf_counter(), detail)
        return response

    @hooks.on.tool_execute
    async def time_tool(ctx, *, call, tool_def, args, handler):
        start = time.perf_counter()
        try:
            return await handler(args)
        finally:
            timer.record("tool", call.tool_name, start, time.perf_counter())

    return hooks
