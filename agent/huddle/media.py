"""The media coolton sends to and takes from a huddle: its voice (SpeakerTrack), the call's
audio turned into what speech-to-text wants (MicResampler), and its desktop as video
(DesktopTrack, fed by DesktopCapture from the thread's sandbox)."""
from __future__ import annotations

import asyncio
import base64
import fractions
import logging
import threading
import time

import av
from aiortc.mediastreams import MediaStreamTrack

from agent.huddle.chime import VIDEO_SIZE

logger = logging.getLogger(__name__)

SPEAKER_RATE = 48_000
_FRAME_SAMPLES = 960  # 20 ms
_FRAME_BYTES = _FRAME_SAMPLES * 2  # mono s16
MIC_RATE = 16_000


class SpeakerTrack(MediaStreamTrack):
    """coolton's voice in the call: plays queued 48 kHz mono 16-bit PCM in real time, and
    silence when there's nothing to say (Chime expects a steady stream)."""

    kind = "audio"

    def __init__(self):
        super().__init__()
        self._buffer = bytearray()
        self._paused = False
        self._start: float | None = None
        self._pts = 0
        # Bytes ever queued and ever played, to tell which words people actually heard.
        self.enqueued = 0
        self.played = 0

    def play(self, pcm: bytes) -> None:
        self._buffer += pcm
        self.enqueued += len(pcm)

    def clear(self) -> None:
        """Stop talking: drop everything still queued."""
        self._buffer.clear()
        self.played = self.enqueued

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def busy(self) -> bool:
        return bool(self._buffer)

    async def recv(self) -> av.AudioFrame:
        if self._start is None:
            self._start = time.time()
        else:
            self._pts += _FRAME_SAMPLES
            wait = self._start + self._pts / SPEAKER_RATE - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        chunk = b""
        if not self._paused and self._buffer:
            chunk = bytes(self._buffer[:_FRAME_BYTES])
            del self._buffer[:_FRAME_BYTES]
            self.played += len(chunk)
        frame = av.AudioFrame(format="s16", layout="mono", samples=_FRAME_SAMPLES)
        frame.planes[0].update(chunk.ljust(_FRAME_BYTES, b"\0"))
        frame.pts = self._pts
        frame.sample_rate = SPEAKER_RATE
        frame.time_base = fractions.Fraction(1, SPEAKER_RATE)
        return frame


class MicResampler:
    """The call's audio (Opus decoded to 48 kHz stereo) as 16 kHz mono 16-bit PCM."""

    def __init__(self):
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=MIC_RATE)

    def __call__(self, frame: av.AudioFrame) -> bytes:
        return b"".join(bytes(f.planes[0])[: f.samples * 2] for f in self._resampler.resample(frame))


_VIDEO_CLOCK = 90_000
_VIDEO_FPS = 10


class DesktopTrack(MediaStreamTrack):
    """coolton's video: the latest frame of its desktop, sent at a steady rate (repeated
    frames cost almost nothing to encode)."""

    kind = "video"

    def __init__(self):
        super().__init__()
        self._frame = av.VideoFrame(*VIDEO_SIZE, "yuv420p")
        for plane, value in zip(self._frame.planes, (16, 128, 128)):  # black
            plane.update(bytes([value]) * plane.buffer_size)
        self._start: float | None = None
        self._pts = 0

    def show(self, frame: av.VideoFrame) -> None:
        self._frame = frame

    async def recv(self) -> av.VideoFrame:
        if self._start is None:
            self._start = time.time()
        else:
            self._pts += _VIDEO_CLOCK // _VIDEO_FPS
            wait = self._start + self._pts / _VIDEO_CLOCK - time.time()
            if wait > 0:
                await asyncio.sleep(wait)
        frame = self._frame.reformat(format="yuv420p")
        frame.pts = self._pts
        frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK)
        return frame


def decode_jpeg(data: bytes) -> av.VideoFrame | None:
    codec = av.CodecContext.create("mjpeg", "r")
    frames = codec.decode(av.Packet(data))
    if not frames:
        return None
    return frames[0].reformat(width=VIDEO_SIZE[0], height=VIDEO_SIZE[1], format="yuv420p")


class DesktopCapture:
    """Grabs the thread's sandbox desktop a few times a second for a DesktopTrack, on its
    own thread (E2B's SDK is synchronous). Each grab is one short command that prints a
    JPEG as base64, so nothing in the sandbox is exposed and nothing is left running.

    While it runs it holds the sandbox in use (agent.sandbox_helpers.sandbox_use), so the
    tools a subagent runs meanwhile don't pause the sandbox under it."""

    _INTERVAL = 0.25
    _RETRY_DELAY = 3.0

    def __init__(self, channel_id: str, thread_ts: str, track: DesktopTrack):
        self._key = (channel_id, thread_ts)
        self._track = track
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="huddle-desktop")

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        from agent import desktop_helpers as dh
        from agent.sandbox_helpers import get_or_create_sandbox, sandbox_use

        with sandbox_use(*self._key) as use:
            while not self._stop.is_set():
                try:
                    sandbox, proxy_info = get_or_create_sandbox(*self._key)
                    use.sandbox = sandbox
                    dh.ensure_desktop(sandbox, proxy_info)
                    while not self._stop.is_set():
                        started = time.monotonic()
                        frame = decode_jpeg(base64.b64decode(dh.capture_jpeg(sandbox, proxy_info)))
                        if frame is not None:
                            self._track.show(frame)
                        self._stop.wait(max(0.0, self._INTERVAL - (time.monotonic() - started)))
                except Exception as e:
                    logger.warning("Huddle desktop capture failed, retrying: %s", e)
                    self._stop.wait(self._RETRY_DELAY)
