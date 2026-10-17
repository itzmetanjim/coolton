"""One huddle coolton is in: hearing it, deciding when to talk, talking, and showing its
desktop.

The pieces (agent.huddle.chime, .speech, .media, .voice_agent) are wired together here,
for low latency the way voice agents usually are:
- Streaming all the way: the call's audio streams into Flux, the reply streams out of the
  model, and each sentence goes to text-to-speech as soon as it's written, so coolton
  starts talking while the rest of the reply is still being made.
- Turn detection is Flux's: on EagerEndOfTurn a reply starts speculatively, and is only
  heard (or allowed to delegate or post) once EndOfTurn confirms the turn really ended
  with the same words; TurnResumed drops it.
- Barge-in: when someone starts talking over coolton it pauses at once. A real
  interruption (a few words, or the end of a turn that isn't just "mm-hmm") stops the
  reply for good; a backchannel lets it carry on. The history keeps only what was heard.
- Delegated work runs in the background (voice_agent's tools), so coolton acknowledges at
  once and announces the result when it lands, without making anyone wait in silence.

A huddle is a group call, so not everything said is for coolton. It answers a turn that
names it, any turn when it's alone with one person, and follow-ups right after it spoke
(where the model may still pick <silent/>); everything else is kept as context.

What happened in the call (what people said and typed, what coolton got to say before
anyone cut in, its tasks) is kept as a transcript, saved as one message in the huddle
thread's stored history (thread_context), so coolton remembers the call when it's
mentioned there later. It's saved every half minute while the call goes on, so a restart
mid-call loses little, and each save replaces the last.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor

from pydantic_ai import AgentRunResultEvent
from pydantic_ai.messages import (
    ModelRequest, ModelResponse, PartDeltaEvent, PartEndEvent, PartStartEvent, TextPart, TextPartDelta,
    UserPromptPart,
)

from agent.huddle.chime import Attendees, ChimeConnection
from agent.huddle.media import DesktopCapture, DesktopTrack, MicResampler, SpeakerTrack
from agent.huddle.slack_api import JoinedHuddle
from agent.huddle.voice_agent import ReplyParser, VoiceDeps, voice_agent

logger = logging.getLogger(__name__)

FOLLOW_UP_SECONDS = 20.0
ALONE_SECONDS = 60.0
VIDEO_LINGER_SECONDS = 30.0
_BACKCHANNEL_WORDS = 2
_INTERRUPT_WORDS = 3
_HEARD_LINES = 30
_HISTORY_MESSAGES = 80
_RESULT_CHARS = 12_000
_TRANSCRIPT_SAVE_SECONDS = 30.0
_TRANSCRIPT_CHARS = 60_000
_NAME = re.compile(r"\b(?:cool\s?ton|coulton|colton|kolton|culton|coltan|coolten)\b", re.IGNORECASE)
_MODEL_SETTINGS = {
    "openai_reasoning_effort": "low",
    "max_tokens": 1000,
    "openai_prompt_cache_key": "coolton-huddle",
    "anthropic_cache_instructions": True,
}
_TASK_NOTE = (
    "\n\n(This task comes from coolton in a live Slack huddle. Reply with what coolton should "
    "tell people: the answer first, then links and details. If you use the desktop "
    "(computer_use, or agent-browser with --headed), call computer_stream_tool or "
    "agent_browser_stream_tool first: in a huddle that shows your screen as coolton's video.)"
)
_GREETING = "Hi, coolton here."


def _words(text: str) -> int:
    return len(text.split())


def _same_words(a: str, b: str) -> bool:
    def norm(text: str) -> list[str]:
        return re.sub(r"[^\w\s']", "", text.lower()).split()
    return norm(a) == norm(b)


def _text_delta(event) -> str:
    if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
        return event.part.content
    if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
        return event.delta.content_delta
    return ""


def voice_models(user_id: str | None) -> list:
    """The models a huddle reply tries, in coolton's own fallback order: its default chat
    model first (agent.agent._resolve_provider_order)."""
    from agent.agent import _resolve_provider_order, chain_model

    return [(name, chain_model(config)) for name, config in _resolve_provider_order(user_id)[:3]]


class _Reply:
    def __init__(self, prompt: str, user_id: str | None, confirmed: bool, fixed_text: str | None = None):
        self.prompt = prompt
        self.user_id = user_id
        self.confirmed = asyncio.Event()
        if confirmed:
            self.confirmed.set()
        self.fixed_text = fixed_text  # say this instead of asking the model
        self.source = ""  # the transcript a speculative reply was started on
        self.consumed = 0  # context lines this reply's prompt took from session._heard
        self.task: asyncio.Task | None = None
        # [sentence, where its audio starts in SpeakerTrack.enqueued once it arrives]
        self.sentences: list[list] = []
        self.audio_index = 0
        self.acted = False
        self.leave = False
        self.heard: str | None = None

    @property
    def unflushed(self) -> int:
        return len(self.sentences) - self.audio_index

    def heard_text(self, played: int) -> str:
        return " ".join(text for text, start in self.sentences if start is not None and start < played)


class HuddleSession:
    def __init__(self, joined: JoinedHuddle, *, client, requester_id: str, request_text: str, on_closed=None):
        self.joined = joined
        self.channel_id = joined.channel_id
        self.thread_ts = joined.thread_ts
        self.client = client
        self.requester_id = requester_id
        self.request_text = request_text
        self._on_closed = on_closed
        self.speaker = SpeakerTrack()
        self.attendees = Attendees(joined.attendee.get("AttendeeId", ""))
        self.chime = None
        self.listener = None
        self.voice = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.models: list = []
        self.history: list = []
        self._intro = ""
        self._heard: list[str] = []
        self._reply: _Reply | None = None
        self._speculative: _Reply | None = None
        self._barge = False
        self._user_talking = False
        self._last_spoke = 0.0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="huddle-task")
        self._task_seq = 0
        self._delegations = 0
        self._capture: DesktopCapture | None = None
        self._desktop_seen = 0.0
        self._alone_since: float | None = None
        self._seen_others = False
        self._names: dict[str, str] = {}
        self._consented: set[str] = set()
        self._prompted: set[str] = set()
        self._stopping = False
        self._transcript: list[str] = []
        self._transcript_saved = 0
        self._last_save = 0.0
        self._joined_at: float | None = None

    # -- lifecycle ---------------------------------------------------------------------

    async def start(self) -> None:
        from agent.huddle.speech import Listener, Voice

        self.loop = asyncio.get_running_loop()
        api_key = os.environ["DEEPGRAM_API_KEY"]
        if not self.models:
            self.models = await self.loop.run_in_executor(None, voice_models, self.requester_id)
        self.voice = Voice(api_key, self._on_tts_audio, self._on_tts_flushed)
        self.listener = Listener(api_key, self.on_turn)
        mic = MicResampler()
        self.chime = ChimeConnection(
            self.joined.meeting, self.joined.attendee, speaker=self.speaker,
            on_mic_frame=lambda frame: self.listener.feed(mic(frame)),
            on_signal_frame=self._on_signal,
            on_ended=lambda reason: asyncio.ensure_future(self.stop(reason)),
        )
        await self.chime.start()
        self._tasks = [asyncio.create_task(c) for c in (self.listener.run(), self._work(), self._housekeeping())]
        for user_id in [self.requester_id, *self.joined.participant_ids]:
            self._learn_name(user_id)
        requester = await self._name(self.requester_id)
        self._intro = (f"[huddle] You just joined this huddle because {requester} asked you to "
                       f"(their message: {self.request_text!r}).")
        await self._post(
            "joined the huddle :wave: while I'm here, what's said in the call is transcribed "
            "(by Deepgram) so I can answer. Say \"coolton\" to talk to me, or type in this thread."
        )
        self._joined_at = time.time()
        self._last_save = time.monotonic()
        self._log("coolton joined the huddle")
        self._speak(_GREETING)

    async def stop(self, reason: str) -> None:
        if self._stopping:
            return
        self._stopping = True
        logger.info("Leaving huddle %s/%s: %s", self.channel_id, self.thread_ts, reason)
        for task in self._tasks:
            task.cancel()
        for reply in (self._reply, self._speculative):
            if reply is not None and reply.task is not None:
                reply.task.cancel()
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        for closing in (self.chime and self.chime.leave(), self.voice and self.voice.close()):
            if closing is not None:
                try:
                    await closing
                except Exception as e:
                    logger.warning("Error while leaving the huddle: %s", e)
        await self._post(f"left the huddle: {reason}.")
        self._log(f"coolton left the huddle: {reason}")
        await self._save_transcript()
        self._executor.shutdown(wait=False)
        if self._on_closed:
            self._on_closed(self)

    @property
    def video_on(self) -> bool:
        return self._capture is not None

    # -- hearing -----------------------------------------------------------------------

    async def on_turn(self, info) -> None:
        """One of Flux's TurnInfo events (agent.huddle.speech.Listener)."""
        event = str(getattr(info, "event", ""))
        text = (getattr(info, "transcript", "") or "").strip()
        if event == "StartOfTurn":
            self._user_talking = True
            if self.speaker.busy or (self._reply is not None and self._reply.unflushed):
                self.speaker.pause()
                self._barge = True
        elif event == "Update":
            if self._barge and _words(text) >= _INTERRUPT_WORDS:
                await self._interrupt()
        elif event == "EagerEndOfTurn":
            if text and self._speculative is None and self._addressed(text):
                line, user_id = self._voice_line(text, info)
                reply = self._start_reply([line], user_id, confirmed=False)
                reply.source = text
        elif event == "TurnResumed":
            self._drop_speculative()
        elif event == "EndOfTurn":
            self._user_talking = False
            await self._end_turn(text, info)

    async def _end_turn(self, text: str, info) -> None:
        if self._barge:
            if not text or (_words(text) <= _BACKCHANNEL_WORDS and not _NAME.search(text)):
                # "mm-hmm": carry on talking.
                self._barge = False
                self.speaker.resume()
                self._drop_speculative()
                if text:
                    line = self._voice_line(text, info)[0]
                    self._log(line)
                    self._remember(line)
                return
            await self._interrupt()
        speculative, self._speculative = self._speculative, None
        if not text:
            if speculative is not None:
                speculative.task.cancel()
            return
        line, user_id = self._voice_line(text, info)
        self._log(line)
        if not self._addressed(text) or not await self._may_answer(user_id):
            if speculative is not None:
                speculative.task.cancel()
            self._remember(line)
            return
        if self._reply is not None:
            await self._interrupt()
        if speculative is not None and _same_words(speculative.source, text):
            self._confirm(speculative)
            return
        if speculative is not None:
            speculative.task.cancel()
        self._start_reply([line], user_id, confirmed=True)

    def on_chat(self, user_id: str, text: str) -> None:
        """A message in the huddle's thread (agent.huddle.route_chat)."""
        asyncio.ensure_future(self._chat(user_id, text))

    async def _chat(self, user_id: str, text: str) -> None:
        bot_id = os.environ.get("COOLTON_BOT_ID", "")
        named = bool(bot_id and f"<@{bot_id}>" in text) or bool(_NAME.search(text))
        name = await self._name(user_id)
        # As posted, so a later turn in the thread knows it already saw this message
        # (thread_context.thread_history.build_unseen_messages).
        self._log(f"[chat] {name}: {text}")
        if bot_id:
            text = text.replace(f"<@{bot_id}>", "@coolton")
        line = f"[chat] {name}: {text}"
        if (named or self._addressed(text)) and await self._may_answer(user_id):
            self._queue.put_nowait((line, user_id))
        else:
            self._remember(line)

    def _addressed(self, text: str) -> bool:
        return (
            bool(_NAME.search(text))
            or len(self.attendees.others) <= 1
            or time.monotonic() - self._last_spoke < FOLLOW_UP_SECONDS
        )

    def _voice_line(self, text: str, info) -> tuple[str, str | None]:
        """The transcript line for a turn, with whoever was loudest while it was said."""
        user_id = None
        epoch = self.listener.epoch if self.listener is not None else None
        if epoch is not None:
            start = epoch + float(getattr(info, "audio_window_start", 0) or 0)
            end = epoch + float(getattr(info, "audio_window_end", 0) or 0)
            user_id = self.attendees.user_id(self.attendees.loudest(start, end))
        name = self._names.get(user_id or "", "someone")
        return f"[voice] {name}: {text}", user_id

    def _remember(self, line: str) -> None:
        self._heard.append(line)
        dropped = len(self._heard) - _HEARD_LINES
        if dropped > 0:
            del self._heard[:dropped]
            if self._speculative is not None:
                self._speculative.consumed = max(0, self._speculative.consumed - dropped)

    async def _may_answer(self, user_id: str | None) -> bool:
        """Only people who opted in to coolton's policy get answers; the first time someone
        who hasn't asks, the opt-in prompt goes to the thread (agent.policy_consent)."""
        from agent.policy_consent import has_consent

        if not user_id or user_id in self._consented:
            return True
        if user_id in self._prompted:
            ok = has_consent(user_id)
        else:
            self._prompted.add(user_id)
            ok = await self.loop.run_in_executor(None, self._ensure_consent, user_id)
        if ok:
            self._consented.add(user_id)
        return ok

    def _ensure_consent(self, user_id: str) -> bool:
        from agent.policy_consent import ensure_consent

        def say(**kwargs):
            self.client.chat_postMessage(channel=self.channel_id, **kwargs)

        try:
            return ensure_consent(self.client, say, user_id=user_id, channel_id=self.channel_id,
                                  thread_ts=self.thread_ts, message_ts=self.thread_ts)
        except Exception as e:
            logger.warning("Huddle consent check failed for %s: %s", user_id, e)
            return False

    # -- replying ----------------------------------------------------------------------

    def _start_reply(self, lines: list[str], user_id: str | None, *, confirmed: bool) -> _Reply:
        context = ([self._intro] if self._intro else []) + self._heard
        reply = _Reply("\n".join(context + lines), user_id, confirmed)
        reply.consumed = len(self._heard)
        if confirmed:
            self._take_context(reply)
            self._reply = reply
        else:
            self._speculative = reply
        reply.task = asyncio.create_task(self._run_reply(reply))
        return reply

    def _speak(self, text: str) -> None:
        """Say a fixed line, not part of the conversation."""
        reply = _Reply("", None, confirmed=True, fixed_text=text)
        self._reply = reply
        reply.task = asyncio.create_task(self._run_reply(reply))

    def _take_context(self, reply: _Reply) -> None:
        self._intro = ""
        del self._heard[:reply.consumed]

    def _confirm(self, reply: _Reply) -> None:
        self._take_context(reply)
        self._reply = reply
        reply.confirmed.set()

    def _drop_speculative(self) -> None:
        speculative, self._speculative = self._speculative, None
        if speculative is not None:
            speculative.task.cancel()

    async def _interrupt(self) -> None:
        self._barge = False
        reply, self._reply = self._reply, None
        if reply is not None:
            reply.heard = reply.heard_text(self.speaker.played)
            reply.task.cancel()
        self.speaker.clear()
        self.speaker.resume()
        await self.voice.clear()

    async def _run_reply(self, reply: _Reply) -> None:
        try:
            if reply.fixed_text is not None:
                parser = ReplyParser()
                for action in parser.feed(reply.fixed_text) + parser.finish():
                    await self._act(reply, action)
                new_messages = None
            else:
                new_messages = await self._generate(reply)
            await reply.confirmed.wait()
            while reply.unflushed or self.speaker.busy:
                await asyncio.sleep(0.05)
            if reply.sentences:
                self._last_spoke = time.monotonic()
                self._log("coolton said: " + " ".join(text for text, _ in reply.sentences))
            if new_messages:
                self._add_history(new_messages)
            if reply.leave:
                asyncio.ensure_future(self.stop("asked to leave"))
        except asyncio.CancelledError:
            if reply.confirmed.is_set() and reply.fixed_text is None:
                if reply.acted:
                    heard = reply.heard if reply.heard is not None else reply.heard_text(self.speaker.played)
                    self._log(f"coolton said: {heard} [interrupted]" if heard else "coolton said: [interrupted]")
                    self._add_history([
                        ModelRequest(parts=[UserPromptPart(content=reply.prompt)]),
                        ModelResponse(parts=[TextPart(content=f"{heard} [interrupted]".strip())]),
                    ])
                else:
                    # Never got to say anything: what it was answering is still unanswered.
                    self._heard.insert(0, reply.prompt)
            raise
        except Exception:
            logger.exception("Huddle reply failed")
            if reply.confirmed.is_set() and not reply.acted:
                self._speak("Sorry, something went wrong on my end.")
        finally:
            if self._reply is reply:
                self._reply = None

    async def _generate(self, reply: _Reply):
        """Stream the model's reply into actions, trying the next model if one fails before
        the reply did anything. Returns the run's new messages."""
        deps = VoiceDeps(session=self, confirmed=reply.confirmed, user_id=reply.user_id)
        error: Exception | None = None
        for name, model in self.models:
            parser = ReplyParser()
            try:
                result = None
                async with voice_agent.run_stream_events(
                    reply.prompt, message_history=self.history, model=model, deps=deps,
                    model_settings=_MODEL_SETTINGS,
                ) as events:
                    async for event in events:
                        delta = _text_delta(event)
                        if delta:
                            for action in parser.feed(delta):
                                await self._act(reply, action)
                        elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                            for action in parser.end_part():
                                await self._act(reply, action)
                        elif isinstance(event, AgentRunResultEvent):
                            result = event.result
                for action in parser.finish():
                    await self._act(reply, action)
                return result.new_messages() if result is not None else None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if reply.acted:
                    raise
                logger.warning("Huddle reply on %s failed, trying the next model: %s", name, e)
                error = e
        raise error or RuntimeError("no model available for the huddle")

    async def _act(self, reply: _Reply, action: tuple[str, str]) -> None:
        kind, text = action
        if kind == "silent":
            return
        await reply.confirmed.wait()
        reply.acted = True
        if kind == "say":
            reply.sentences.append([text, None])
            await self.voice.say(text)
        elif kind == "chat":
            self._log(f"coolton posted in the thread: {text}")
            await self._post(text)
        elif kind == "leave":
            reply.leave = True

    def _on_tts_audio(self, pcm: bytes) -> None:
        reply = self._reply
        if reply is None or not reply.unflushed:
            return
        sentence = reply.sentences[reply.audio_index]
        if sentence[1] is None:
            sentence[1] = self.speaker.enqueued
        self.speaker.play(pcm)

    def _on_tts_flushed(self) -> None:
        reply = self._reply
        if reply is not None and reply.unflushed:
            reply.audio_index += 1

    def _add_history(self, messages: list) -> None:
        # A new list, not extended in place: a speculative reply may be reading the old one.
        self.history = self.history + messages
        if len(self.history) > _HISTORY_MESSAGES:
            # Cut where a new user prompt starts, so no tool call loses its result.
            for i in range(len(self.history) - _HISTORY_MESSAGES * 3 // 4, len(self.history)):
                message = self.history[i]
                if isinstance(message, ModelRequest) and any(isinstance(p, UserPromptPart) for p in message.parts):
                    self.history = self.history[i:]
                    break

    async def _work(self) -> None:
        """Answers typed messages and finished tasks, one reply at a time, when nobody's
        talking and coolton isn't already."""
        while True:
            line, user_id = await self._queue.get()
            while self._reply is not None or self._speculative is not None or self._barge or self._user_talking:
                await asyncio.sleep(0.1)
            lines = [line]
            while not self._queue.empty():
                more, more_user = self._queue.get_nowait()
                lines.append(more)
                user_id = more_user or user_id
            reply = self._start_reply(lines, user_id, confirmed=True)
            await asyncio.wait({reply.task})

    # -- delegating --------------------------------------------------------------------

    def delegate(self, tasks: list[tuple[str, str]], user_id: str | None) -> int:
        """Start subagents in the background (voice_agent's tools) and return the task's
        number; the result comes back through the queue as a new message."""
        from agent.deps import AgentDeps
        from agent.subagents import delegate_many

        self._task_seq += 1
        number = self._task_seq
        deps = AgentDeps(
            client=self.client, user_id=user_id or self.requester_id, channel_id=self.channel_id,
            thread_ts=self.thread_ts, message_ts=self.thread_ts, run_started_at=time.time(),
            request_text=tasks[0][1],
        )
        self._delegations += 1
        self._log(f"[task {number} started] " + "; ".join(f"{target}: {task[:300]}" for target, task in tasks))
        future = self.loop.run_in_executor(
            self._executor, delegate_many, [(target, task + _TASK_NOTE) for target, task in tasks], deps,
        )
        future.add_done_callback(functools.partial(self._delegation_done, number))
        return number

    def _delegation_done(self, number: int, future) -> None:
        self._delegations -= 1
        try:
            result, status = future.result(), "finished"
        except Exception as e:
            result, status = f"Error: {e}", "failed"
        if len(result) > _RESULT_CHARS:
            result = result[:_RESULT_CHARS] + "\n[cut]"
        self._log(f"[task {number} {status}] {result[:1500]}")
        if self._stopping:
            asyncio.ensure_future(self._post(f"task {number} {status} after I left the huddle:\n{result[:3000]}"))
            return
        self._queue.put_nowait((f"[task {number} {status}]\n{result}", None))

    # -- video ---------------------------------------------------------------------------

    def desktop_active(self) -> None:
        """A tool used the thread's desktop: show it as coolton's video until it's idle."""
        self._desktop_seen = time.monotonic()
        if self._capture is None and not self._stopping:
            track = DesktopTrack()
            self._capture = DesktopCapture(self.channel_id, self.thread_ts, track)
            self._capture.start()
            asyncio.ensure_future(self.chime.set_video(track))

    async def _stop_video(self) -> None:
        capture, self._capture = self._capture, None
        if capture is not None:
            capture.stop()
            await self.chime.set_video(None)

    async def _housekeeping(self) -> None:
        while True:
            await asyncio.sleep(5)
            now = time.monotonic()
            if self.attendees.others:
                self._seen_others = True
                self._alone_since = None
            elif not self._seen_others:
                pass  # Chime hasn't said who's here (yet): never take that for being alone
            elif self._alone_since is None:
                self._alone_since = now
            elif now - self._alone_since > ALONE_SECONDS:
                asyncio.ensure_future(self.stop("everyone else left"))
                return
            if self._capture is not None and not self._delegations and now - self._desktop_seen > VIDEO_LINGER_SECONDS:
                await self._stop_video()
            if now - self._last_save > _TRANSCRIPT_SAVE_SECONDS:
                await self._save_transcript()

    # -- remembering the call ------------------------------------------------------------

    def _log(self, line: str) -> None:
        self._transcript.append(f"{time.strftime('%H:%M', time.gmtime())} {line}")

    @property
    def _record_header(self) -> str:
        joined = time.strftime("%Y-%m-%d at %H:%M:%S", time.gmtime(self._joined_at or 0))
        return f"[Transcript of the Slack huddle (a voice call) you joined from this thread on {joined} UTC."

    def _transcript_record(self, lines: list[str]) -> ModelRequest:
        text = "\n".join(lines)
        if len(text) > _TRANSCRIPT_CHARS:
            text = "[earlier part cut]\n" + text[-_TRANSCRIPT_CHARS:]
        return ModelRequest(parts=[UserPromptPart(content=(
            f"{self._record_header} It's your memory of the call. [voice] lines were said out loud "
            "(speech-to-text, so a word may be misheard), [chat] lines were typed in this thread during "
            "the call, \"coolton said\" is what you said, and [task] lines are work your subagents "
            "did.]\n" + text
        ))])

    async def _save_transcript(self) -> None:
        if self._joined_at is None or len(self._transcript) == self._transcript_saved:
            return
        lines = list(self._transcript)
        self._last_save = time.monotonic()
        try:
            await self.loop.run_in_executor(None, self._write_transcript, lines)
            self._transcript_saved = len(lines)
        except Exception as e:
            logger.warning("Couldn't save the huddle transcript: %s", e)

    def _write_transcript(self, lines: list[str]) -> None:
        """The thread's stored history, with this call's transcript (replacing an earlier
        save of it) at the end. last_seen_ts starts at the thread's root when the thread had
        no history, so the next turn there still reads the thread's messages."""
        from thread_context import conversation_store

        header = self._record_header
        stored = conversation_store.get_history(self.channel_id, self.thread_ts) or []
        kept = [
            message for message in stored
            if not (isinstance(message, ModelRequest) and any(
                isinstance(part, UserPromptPart) and isinstance(part.content, str)
                and part.content.startswith(header) for part in message.parts))
        ]
        conversation_store.set_history(
            self.channel_id, self.thread_ts, [*kept, self._transcript_record(lines)], last_seen_ts=self.thread_ts,
        )

    # -- Slack ---------------------------------------------------------------------------

    def _on_signal(self, frame) -> None:
        self.attendees.handle(frame)
        for attendee in self.attendees.others:
            self._learn_name(self.attendees.user_id(attendee))

    def _learn_name(self, user_id: str | None) -> None:
        if user_id and user_id not in self._names:
            self._names[user_id] = user_id
            asyncio.ensure_future(self._name(user_id, refresh=True))

    async def _name(self, user_id: str, refresh: bool = False) -> str:
        if user_id in self._names and not refresh:
            return self._names[user_id]

        def lookup() -> str:
            try:
                user = self.client.users_info(user=user_id).get("user") or {}
                profile = user.get("profile") or {}
                return profile.get("display_name") or profile.get("real_name") or user.get("name") or user_id
            except Exception:
                return user_id

        self._names[user_id] = await self.loop.run_in_executor(None, lookup)
        return self._names[user_id]

    async def _post(self, text: str) -> None:
        from agent.mentions import defuse_mass_mentions
        from agent.redact import redact
        from agent.writing_style import strip_em_dashes

        text = defuse_mass_mentions(strip_em_dashes(redact(text, context="huddle chat")))

        def post():
            try:
                self.client.chat_postMessage(channel=self.channel_id, thread_ts=self.thread_ts, text=text)
            except Exception as e:
                logger.warning("Couldn't post in the huddle thread: %s", e)

        await self.loop.run_in_executor(None, post)
