"""Deepgram, for coolton's ears and voice in a huddle.

- Listener: streams the call's audio to Flux, Deepgram's speech-to-text for voice agents.
  Flux detects the end of each turn itself, so there's no separate voice activity detector
  to tune: StartOfTurn (someone started talking: coolton stops), EagerEndOfTurn (they're
  probably done: start thinking of a reply), TurnResumed (they weren't: drop it) and
  EndOfTurn (done: answer).
- Voice: one streaming text-to-speech socket for the whole huddle, so each reply skips the
  connection setup. Text goes in sentence by sentence (Speak + Flush), audio comes back as
  it's made, and Clear drops whatever's left when someone interrupts.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from deepgram import AsyncDeepgramClient
from deepgram.speak.v1.types import SpeakV1Text

from agent.huddle.media import MIC_RATE, SPEAKER_RATE

logger = logging.getLogger(__name__)

STT_MODEL = "flux-general-en"
# Deepgram's low-latency settings for Flux: start a reply at moderate confidence that a
# turn is over, commit at 0.7, and end a turn after 4 s of uncertain silence.
_EAGER_EOT_THRESHOLD = "0.4"
_EOT_THRESHOLD = "0.7"
_EOT_TIMEOUT_MS = "4000"
_CHUNK_BYTES = MIC_RATE * 2 * 80 // 1000  # 80 ms, what Flux works best with
_MAX_BACKLOG = 50  # chunks (4 s) kept while (re)connecting; older audio is dropped
TTS_MODEL = "aura-2-thalia-en"


class Listener:
    """Speech-to-text for the call. feed() it 16 kHz mono 16-bit PCM; it calls `on_turn`
    with each of Flux's TurnInfo messages, reconnecting when the socket drops.

    `epoch` is the time.monotonic() of the current connection's audio second 0, so a
    turn's audio_window_start/end map onto the call's clock (agent.huddle.chime.Attendees)."""

    def __init__(self, api_key: str, on_turn: Callable[[object], Awaitable[None]]):
        self._client = AsyncDeepgramClient(api_key=api_key)
        self._on_turn = on_turn
        self._pending = bytearray()
        self._chunks: asyncio.Queue[bytes] = asyncio.Queue(maxsize=_MAX_BACKLOG)
        self.epoch: float | None = None

    def feed(self, pcm: bytes) -> None:
        self._pending += pcm
        while len(self._pending) >= _CHUNK_BYTES:
            chunk = bytes(self._pending[:_CHUNK_BYTES])
            del self._pending[:_CHUNK_BYTES]
            if self._chunks.full():
                self._chunks.get_nowait()
            self._chunks.put_nowait(chunk)

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with self._client.listen.v2.connect(
                    model=STT_MODEL, encoding="linear16", sample_rate=str(MIC_RATE),
                    eager_eot_threshold=_EAGER_EOT_THRESHOLD, eot_threshold=_EOT_THRESHOLD,
                    eot_timeout_ms=_EOT_TIMEOUT_MS, keyterm="coolton",
                ) as connection:
                    self.epoch = None
                    sender = asyncio.create_task(self._send(connection))
                    try:
                        async for message in connection:
                            kind = getattr(message, "type", "")
                            if kind == "TurnInfo":
                                delay = 1.0
                                await self._on_turn(message)
                            elif kind in ("Error", "Warning"):
                                logger.warning("Deepgram Flux %s: %s", kind, getattr(message, "description", message))
                    finally:
                        sender.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Deepgram Flux connection failed, reconnecting: %s", e)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _send(self, connection) -> None:
        while True:
            chunk = await self._chunks.get()
            if self.epoch is None:
                self.epoch = time.monotonic() - len(chunk) / (MIC_RATE * 2)
            await connection.send_media(chunk)


class Voice:
    """Text-to-speech for coolton's replies: say() a sentence and its audio (48 kHz mono
    16-bit PCM) comes back through `on_audio`, then `on_flushed` once it's all there."""

    def __init__(self, api_key: str, on_audio: Callable[[bytes], None], on_flushed: Callable[[], None]):
        self._client = AsyncDeepgramClient(api_key=api_key)
        self._on_audio = on_audio
        self._on_flushed = on_flushed
        self._connection = None
        self._stack: contextlib.AsyncExitStack | None = None
        self._reader: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # After a Clear, audio still in flight is dropped until Deepgram confirms it.
        self._clearing = False

    async def _connected(self):
        if self._connection is not None and self._reader is not None and not self._reader.done():
            return self._connection
        await self._disconnect()
        self._stack = contextlib.AsyncExitStack()
        self._connection = await self._stack.enter_async_context(self._client.speak.v1.connect(
            model=TTS_MODEL, encoding="linear16", sample_rate=str(SPEAKER_RATE),
        ))
        self._clearing = False
        self._reader = asyncio.create_task(self._read(self._connection))
        return self._connection

    async def _read(self, connection) -> None:
        try:
            async for message in connection:
                if isinstance(message, bytes):
                    if not self._clearing:
                        self._on_audio(message)
                    continue
                kind = getattr(message, "type", "")
                if kind == "Cleared":
                    self._clearing = False
                elif kind == "Flushed" and not self._clearing:
                    self._on_flushed()
                elif kind == "Warning":
                    logger.warning("Deepgram TTS warning: %s", getattr(message, "description", message))
        except Exception as e:
            logger.info("Deepgram TTS socket closed: %s", e)

    async def say(self, text: str) -> None:
        async with self._lock:
            for attempt in range(2):
                try:
                    connection = await self._connected()
                    await connection.send_text(SpeakV1Text(text=text))
                    await connection.send_flush()
                    return
                except Exception as e:
                    # An idle socket Deepgram closed: open a new one and try once more.
                    logger.info("Deepgram TTS send failed (%s), reconnecting", e)
                    await self._disconnect()
                    if attempt:
                        raise

    async def clear(self) -> None:
        async with self._lock:
            if self._connection is None:
                return
            self._clearing = True
            try:
                await self._connection.send_clear()
            except Exception:
                await self._disconnect()

    async def _disconnect(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
        if self._stack is not None:
            with contextlib.suppress(Exception):
                await self._stack.aclose()
        self._connection = self._stack = self._reader = None

    async def close(self) -> None:
        async with self._lock:
            await self._disconnect()
