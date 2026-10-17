"""The agent that talks for coolton in a huddle.

It only talks, or hands work to coolton's subagents (agent.subagents): no other tools, so
each reply is one fast model call that starts streaming words right away. Delegating
doesn't block it: the subagent runs in the background, coolton says it's on it, and the
result comes back later as a new message to tell people about.

Its replies are spoken sentence by sentence while they're still being written. Anything
inside <chat>...</chat> is posted in the huddle's thread instead (links, code, lists, and
answers to people who typed), <leave/> leaves the huddle once the reply is said, and
<silent/> is a reply of nothing, for speech that wasn't meant for coolton.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any

from pydantic_ai import Agent, RunContext

VOICE_PROMPT = """You are coolton, an AI assistant in the Hack Club Slack (pronouns she/it), \
in a live Slack huddle: a voice call. Everything you write is spoken aloud with text-to-speech \
the moment you write it.

How you talk:
- Like a person on a call: short, natural sentences, usually one to three. Get to the point; \
no greetings or sign-offs unless someone greets you or leaves.
- Spoken words only: no markdown, lists, headings, emoji, code or URLs in speech. Say numbers, \
dates and symbols the way you'd say them out loud.
- Anything people should read rather than hear (links, code, commands, lists, long details) goes \
inside <chat>...</chat>, which is posted in the huddle's thread instead of spoken. Mention it out \
loud, like "I put the link in the chat".

What you get: lines tagged by where they came from.
- [voice] Name: what someone said in the call. Speech-to-text can mishear, so read generously \
(your name often comes through as Colton or Coulton).
- [chat] Name: what someone typed in the huddle's thread. They may not be listening, so answer \
typed messages inside <chat>...</chat>, optionally with a short spoken line too.
- [task N finished] / [task N failed]: a subagent's result.
- Lines you already answered aren't repeated. Earlier lines in a message are context: things \
said that weren't for you.

Not every line is for you. People in a huddle talk to each other. If the newest line isn't \
addressed to you and doesn't need you, reply with exactly <silent/> and nothing else.

Doing things: you can't look anything up or do anything yourself. For anything that needs the \
web, Slack, files, code, the sandbox or a computer, use delegate_to_subagent (one task) or \
delegate_to_subagents (several independent ones at once). They run in the background: first say \
a very short acknowledgement ("on it", "give me a sec, I'll check"), then call the tool in the \
same reply, and stop; never make up an answer while you wait. Give each task everything it needs (names, links, ids, what to return): \
subagents can't hear the call. When a task finishes, tell people the gist out loud in a sentence \
or two, and put links and details in <chat>...</chat>. A subagent using the desktop or a browser \
shows its screen as your video in the huddle while it works.

Leaving: when someone asks you to leave or hang up, say a short goodbye and end your reply with \
<leave/>.

Never use em dashes."""

# What a reply's tags look like, forgiving about spaces, case and self-closing slashes.
_TAG = re.compile(r"<\s*(/?)\s*(chat|leave|silent)\s*/?\s*>", re.IGNORECASE)
_LONGEST_TAG = 12
_SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*(?=\s)|\n+")
# Speak the first clause early when it's this long, so the first audio starts sooner.
_FIRST_CLAUSE_CHARS = 40
_MAX_SENTENCE_CHARS = 220


class ReplyParser:
    """Turns a reply streamed in pieces into actions, as soon as each is complete:
    ("say", sentence), ("chat", text), ("leave", ""), ("silent", "")."""

    def __init__(self):
        self._raw = ""
        self._pos = 0
        self._say = ""
        self._chat: str | None = None  # inside <chat> while not None
        self._said_any = False

    def feed(self, delta: str) -> list[tuple[str, str]]:
        # Models (or gateways) sometimes put zero-width characters inside tags.
        self._raw += _ZERO_WIDTH.sub("", delta)
        actions: list[tuple[str, str]] = []
        while True:
            text = self._raw[self._pos:]
            match = _TAG.search(text)
            if match:
                self._text(text[:match.start()], actions)
                self._pos += match.end()
                self._tag(match.group(1) == "/", match.group(2).lower(), actions)
                continue
            # Hold back a "<" at the end that may still turn into a tag.
            hold = text.rfind("<")
            if hold != -1 and len(text) - hold < _LONGEST_TAG and ">" not in text[hold:]:
                self._text(text[:hold], actions)
                self._pos += hold
            else:
                self._text(text, actions)
                self._pos = len(self._raw)
            return actions

    def end_part(self) -> list[tuple[str, str]]:
        """A text part of the reply ended (the model may now call a tool): say what's
        pending instead of holding it until after the tool call."""
        actions: list[tuple[str, str]] = []
        if self._chat is None:
            self._text(self._raw[self._pos:], actions)
            self._pos = len(self._raw)
            self._flush_say(actions)
        return actions

    def finish(self) -> list[tuple[str, str]]:
        actions: list[tuple[str, str]] = []
        self._text(self._raw[self._pos:], actions)
        self._pos = len(self._raw)
        if self._chat is not None:
            self._tag(True, "chat", actions)
        self._flush_say(actions)
        return actions

    def _text(self, text: str, actions: list) -> None:
        if not text:
            return
        if self._chat is not None:
            self._chat += text
            return
        self._say += text
        while True:
            end = _SENTENCE_END.search(self._say)
            if end:
                cut = end.end()
            elif not self._said_any and len(self._say) >= _FIRST_CLAUSE_CHARS and ", " in self._say[_FIRST_CLAUSE_CHARS // 2:]:
                cut = self._say.index(", ", _FIRST_CLAUSE_CHARS // 2) + 1
            elif len(self._say) > _MAX_SENTENCE_CHARS and " " in self._say:
                cut = self._say.rindex(" ")
            else:
                return
            self._emit_say(self._say[:cut], actions)
            self._say = self._say[cut:]

    def _tag(self, closing: bool, name: str, actions: list) -> None:
        if name == "chat":
            if closing and self._chat is not None:
                if self._chat.strip():
                    actions.append(("chat", self._chat.strip()))
                self._chat = None
            elif not closing and self._chat is None:
                self._flush_say(actions)
                self._chat = ""
        elif not closing or name in ("leave", "silent"):
            self._flush_say(actions)
            actions.append((name, ""))

    def _flush_say(self, actions: list) -> None:
        self._emit_say(self._say, actions)
        self._say = ""

    def _emit_say(self, text: str, actions: list) -> None:
        text = speakable(text)
        if text:
            self._said_any = True
            actions.append(("say", text))


_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff]")
_URL = re.compile(r"https?://\S+|<https?://[^>]+>")
_SLACK_EMOJI = re.compile(r":[a-z0-9_+-]+:")
# What's left of a tag the parser couldn't read, e.g. one cut off at the end.
_TAG_REMNANT = re.compile(r"<\s*/?\s*[a-z]+\s*/?\s*>?", re.IGNORECASE)


def speakable(text: str) -> str:
    """What text-to-speech should read: no markup, tags, links or emoji codes."""
    text = _URL.sub("a link", text)
    text = _TAG_REMNANT.sub("", text)
    text = _SLACK_EMOJI.sub("", text)
    text = re.sub(r"[*_`#>~|]", "", text)
    text = text.replace("—", ", ").replace("–", "-")
    return " ".join(text.split())


@dataclass
class VoiceDeps:
    session: Any
    # Set once the reply is really wanted. A reply started on a turn that may not be over
    # yet (Flux's EagerEndOfTurn, agent.huddle.speech) waits here before doing anything
    # people notice.
    confirmed: asyncio.Event
    # Who the reply answers; tasks it delegates run on their behalf.
    user_id: str | None = None
    # The task this reply started. One per reply (several subagents go in one
    # delegate_to_subagents call): a model unsure the task started would otherwise keep
    # starting it again, reworded.
    started: int | None = None


_END = ("If you already said you're on it, end your reply with exactly <silent/>; otherwise say so "
        "briefly now.")


def _start(ctx: RunContext[VoiceDeps], tasks: list[tuple[str, str]]) -> str:
    if ctx.deps.started is not None:
        return f"Refused: this reply already started task {ctx.deps.started}, which is running. {_END}"
    number = ctx.deps.started = ctx.deps.session.delegate(tasks, ctx.deps.user_id)
    return (f"Started task {number}: it's running in the background, and its result arrives by itself "
            f"later as a [task {number} finished] message. {_END}")


async def delegate_to_subagent(ctx: RunContext[VoiceDeps], target: str, task: str) -> str:
    """Start one self-contained task on a subagent, in the background. Returns right away;
    the result arrives later as a "[task N finished]" message.

    Subagents:
    - "general": does the task with all of coolton's tools (sandbox, web, Slack, files,
      email, computer use...).
    - "research": read-only Slack/web/docs research; returns compact sourced findings.
    - "explore": reads the sandbox workspace for context.
    - "summarizer": summarizes a transcript you include in the task.

    Args:
        target: One of "general", "research", "explore", "summarizer".
        task: A fully self-contained instruction: the subagent can't hear the call, so
            include every name, link, id and detail it needs, and what to return.
    """
    from agent.subagents import SUBAGENT_PROMPTS

    target = (target or "").strip().lower()
    if target not in SUBAGENT_PROMPTS:
        return f"Error: unknown subagent {target!r}. Use one of: {', '.join(SUBAGENT_PROMPTS)}."
    if not (task or "").strip():
        return "Error: the subagent needs a task."
    await ctx.deps.confirmed.wait()
    return _start(ctx, [(target, task)])


async def delegate_to_subagents(ctx: RunContext[VoiceDeps], tasks: str) -> str:
    """Start several independent tasks on subagents at once (up to 6), in the background.
    Returns right away; all their results arrive together later as one message.

    Args:
        tasks: A JSON array, as a plain STRING, of {"target": ..., "task": ...} objects
            (targets as for delegate_to_subagent). Each task must be self-contained.
    """
    from agent.subagents import parse_tasks

    parsed, error = parse_tasks(tasks)
    if error:
        return error
    await ctx.deps.confirmed.wait()
    return _start(ctx, parsed)


voice_agent = Agent(
    deps_type=VoiceDeps,
    system_prompt=VOICE_PROMPT,
    tools=[delegate_to_subagent, delegate_to_subagents],
)


@voice_agent.instructions
def _today() -> str:
    # Without it, tasks get written "as of" the model's training cutoff.
    return f"Today is {datetime.now(timezone.utc):%A, %Y-%m-%d} (UTC)."
