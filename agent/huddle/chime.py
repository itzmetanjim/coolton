"""coolton's media connection to a huddle: Slack huddles are Amazon Chime meetings.

One huddle attendee needs two connections to Chime, rebuilt together whenever either drops:
- signaling: a WebSocket carrying Chime's protobuf protocol (SignalingProtocol.proto,
  vendored from amazon-chime-sdk-js v3.31.0, Apache-2.0; signaling_pb2.py is protoc's
  output for it). JOIN gets TURN credentials, SUBSCRIBE trades the WebRTC offer for an
  answer, and the server keeps sending who's in the call and how loud each one is.
- media: a WebRTC peer (aiortc) through Chime's TURN relay, the only way to reach Chime's
  media servers. Audio goes both ways: Chime mixes everyone else into one Opus stream for
  coolton, and plays coolton's own stream to everyone. Video is sent only while coolton is
  sharing its desktop; Chime agrees to video per SUBSCRIBE, so turning it on or off
  rebuilds the connection with the same attendee.
"""
from __future__ import annotations

import asyncio
import collections
import logging
import re
import secrets
import time
from collections.abc import Callable

import websockets
from aioice import TransportPolicy
from aiortc import RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCRtpSender, RTCSessionDescription
from aiortc.contrib.media import MediaBlackhole
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack
from aiortc.rtcconfiguration import RTCBundlePolicy

from agent.huddle import signaling_pb2 as pb

logger = logging.getLogger(__name__)

Frame = pb.SdkSignalFrame

# Every signaling message is this byte and then a protobuf SdkSignalFrame.
_FRAME_PREFIX = b"\x05"
_JOIN_FLAG_HAS_STREAM_UPDATE = 2
# AUDIO_STATUS codes after which the attendee can't come back by reconnecting: joined
# elsewhere, audio disconnected, refused, meeting full, meeting over, removed.
_TERMINAL_AUDIO_STATUS = {301, 302, 403, 409, 410, 411}
_MEETING_ENDED_CLOSE_CODE = 4410
_SILENCE_LIMIT = 15.0  # the server sends BITRATES every few seconds
_RECONNECT_WINDOW = 120.0
VIDEO_SIZE = (1024, 768)  # the sandbox desktop's (agent.desktop_helpers.ensure_desktop)
_VIDEO_FPS = 10
_VIDEO_MAX_KBPS = 1500


# One try per kind of TURN relay Chime offers (UDP, TCP, TLS).
_START_ATTEMPTS = 3

class ChimeEnded(Exception):
    """The meeting is over for coolton (ended, or it was removed); don't reconnect."""


def encode_frame(type_name: str, **body) -> bytes:
    frame = Frame(timestamp_ms=int(time.time() * 1000), type=Frame.Type.Value(type_name))
    for field, message in body.items():
        getattr(frame, field).CopyFrom(message)
    return _FRAME_PREFIX + frame.SerializeToString()


def decode_frame(data: bytes) -> Frame | None:
    """The frame, or None for one this proto can't read (newer servers send types it
    doesn't know, which leaves the required `type` unset)."""
    try:
        return Frame.FromString(bytes(data[1:]))
    except Exception:
        return None


def frame_type(frame: Frame) -> str:
    return Frame.Type.Name(frame.type)


_SLACK_USER_ID = re.compile(r"\b([UW][A-Z0-9]{6,})\b")


class Attendees:
    """Who's in the call, which Slack user each attendee is, and who's speaking, from
    Chime's AUDIO_STREAM_ID_INFO and AUDIO_METADATA frames."""

    _HISTORY_SECONDS = 120.0
    # Chime reports an attendee's volume as attenuation in dB; the Chime JS SDK maps
    # -42..-14 dB onto 0..1, so the same here.
    _QUIET_DB, _LOUD_DB = 42, 14

    def __init__(self, self_attendee_id: str):
        self.self_id = self_attendee_id
        self._streams: dict[int, str] = {}
        self._users: dict[str, str] = {}
        self._levels: collections.deque[tuple[float, dict[str, float]]] = collections.deque()

    def reset(self) -> None:
        """A rebuilt connection: Chime lists the attendees afresh."""
        self._streams.clear()

    def handle(self, frame: Frame, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        if frame.HasField("audio_stream_id_info"):
            for stream in frame.audio_stream_id_info.streams:
                if stream.attendee_id:
                    self._streams[stream.audio_stream_id] = stream.attendee_id
                    match = _SLACK_USER_ID.search(stream.external_user_id or "")
                    if match:
                        self._users[stream.attendee_id] = match.group(1)
                elif not stream.HasField("muted"):
                    # Neither an attendee nor a mute state: that stream left.
                    self._streams.pop(stream.audio_stream_id, None)
        if frame.HasField("audio_metadata"):
            levels = {}
            for state in frame.audio_metadata.attendee_states:
                attendee = self._streams.get(state.audio_stream_id)
                if attendee and attendee != self.self_id and state.HasField("volume"):
                    level = (self._QUIET_DB - state.volume) / (self._QUIET_DB - self._LOUD_DB)
                    levels[attendee] = min(1.0, max(0.0, level))
            if levels:
                self._levels.append((now, levels))
            while self._levels and self._levels[0][0] < now - self._HISTORY_SECONDS:
                self._levels.popleft()

    @property
    def others(self) -> set[str]:
        return {a for a in self._streams.values() if a != self.self_id}

    def user_id(self, attendee_id: str | None) -> str | None:
        return self._users.get(attendee_id or "")

    def loudest(self, start: float, end: float) -> str | None:
        """The attendee who was loudest between two time.monotonic() times."""
        totals: dict[str, float] = collections.defaultdict(float)
        for at, levels in self._levels:
            if start <= at <= end:
                for attendee, level in levels.items():
                    totals[attendee] += level
        return max(totals, key=lambda attendee: totals[attendee]) if totals else None


def _ordered_turn_uris(uris) -> list[str]:
    """UDP first (lowest latency), then TCP, then TLS on 443 for networks that block the rest."""
    def rank(uri: str) -> int:
        if uri.startswith("turns:"):
            return 2
        return 1 if "transport=tcp" in uri else 0
    return sorted((u for u in uris if u.startswith(("turn:", "turns:"))), key=rank)


def _codecs(kind: str, mime: str, **params) -> list:
    return [
        c for c in RTCRtpSender.getCapabilities(kind).codecs
        if c.mimeType.lower() == mime and all(c.parameters.get(k) == v for k, v in params.items())
    ]


def _fail(future: asyncio.Future, error: Exception) -> None:
    if not future.done():
        future.set_exception(error)
        future.exception()  # nobody may be waiting on it any more; don't log it as lost


class _Link:
    """One signaling WebSocket and the peer built on it."""

    def __init__(self, socket):
        self.socket = socket
        self.pc: RTCPeerConnection | None = None
        self.last_inbound = time.monotonic()
        self.tasks: list[asyncio.Task] = []
        self._waiters: list[tuple[str, asyncio.Future]] = []
        self._ping_id = 0

    def send(self, type_name: str, **body) -> None:
        asyncio.ensure_future(self._send(encode_frame(type_name, **body)))

    async def _send(self, data: bytes) -> None:
        try:
            await self.socket.send(data)
        except websockets.ConnectionClosed:
            pass  # the reader notices and reconnects

    def fail_waiters(self, reason: str) -> None:
        for _, future in self._waiters:
            _fail(future, RuntimeError(reason))
        self._waiters.clear()

    def expect(self, type_name: str) -> asyncio.Future:
        """A future for the next frame of a type. Ask before sending what it answers: the
        reader may deliver the reply before the sender gets to wait for it."""
        future = asyncio.get_running_loop().create_future()
        self._waiters.append((type_name, future))
        return future

    def deliver(self, frame: Frame) -> None:
        name = frame_type(frame)
        for wanted, future in list(self._waiters):
            if future.done():
                self._waiters.remove((wanted, future))
            elif name == wanted:
                future.set_result(frame)
                self._waiters.remove((wanted, future))
            elif frame.HasField("error") and name != "PING_PONG":
                # SUBSCRIBE_ACK reports refused video as an error next to a usable answer,
                # so only frames of another type fail a wait.
                _fail(future, RuntimeError(f"Chime {name} error {frame.error.status}: {frame.error.description}"))
                self._waiters.remove((wanted, future))

    def ping(self) -> None:
        self._ping_id += 1
        self.send("PING_PONG", ping_pong=pb.SdkPingPongFrame(type=pb.PING, ping_id=self._ping_id))

    async def close(self) -> None:
        for task in self.tasks:
            task.cancel()
        self.fail_waiters("closed")
        if self.pc is not None:
            await self.pc.close()
        await self.socket.close()


def _ice_pairs(pc: RTCPeerConnection) -> list[str]:
    """What ICE tried, for the log when the media doesn't connect."""
    try:
        ice = pc.getTransceivers()[0].sender.transport.transport.iceGatherer._connection
        return [f"{pair.local_candidate.type} {pair.local_addr} -> {pair.remote_candidate.type} "
                f"{pair.remote_addr}: {pair.state.name}" for pair in ice._check_list]
    except Exception as e:
        return [f"unavailable ({e})"]


def _gather_relay_only(pc: RTCPeerConnection) -> None:
    """Offer only TURN relay candidates, like a browser's iceTransportPolicy "relay". Chime's
    media servers only work through its relay: with host candidates in the offer, ICE can
    pair directly with Chime and pass its checks, but DTLS never completes over that path.
    aiortc has no setting for this, so it's set on aioice's connection (one per BUNDLE
    transport) before gathering starts."""
    for transceiver in pc.getTransceivers():
        ice = transceiver.sender.transport.transport.iceGatherer._connection
        ice._transport_policy = TransportPolicy.RELAY


class ChimeConnection:
    """Keeps coolton connected to a huddle's Chime meeting as one attendee: audio both
    ways, its desktop as video when there is one, and reconnecting for up to two minutes
    when the connection drops. Calls `on_mic_frame` with every frame of the call's audio,
    `on_signal_frame` with every signaling frame, and `on_ended` once, when it's over."""

    def __init__(
        self, meeting: dict, attendee: dict, *,
        speaker: MediaStreamTrack,
        on_mic_frame: Callable,
        on_signal_frame: Callable[[Frame], None],
        on_ended: Callable[[str], None],
        relay_only: bool = True,
    ):
        placement = meeting["MediaPlacement"]
        self._signaling_url = placement["SignalingUrl"]
        self._audio_host = placement.get("AudioHostUrl", "")
        self.attendee_id = attendee["AttendeeId"]
        self._join_token = attendee["JoinToken"]
        self._speaker = speaker
        self._on_mic_frame = on_mic_frame
        self._on_signal_frame = on_signal_frame
        self._on_ended = on_ended
        # Chime's media servers are only reachable through its TURN relay; a test's fake
        # Chime on loopback isn't.
        self._relay_only = relay_only
        # One audio session for the attendee's lifetime, so a reconnect continues it.
        self._audio_session_id = secrets.randbits(32)
        self._video: MediaStreamTrack | None = None
        self._link: _Link | None = None
        self._turn_attempt = 0
        self._stopping = False
        self._reconnecting: asyncio.Task | None = None
        self._ended = False

    @property
    def sending_video(self) -> bool:
        return self._video is not None and self._link is not None

    async def start(self) -> None:
        """Connect, trying each kind of TURN relay Chime offers (UDP, TCP, TLS) in turn."""
        for attempt in range(_START_ATTEMPTS):
            try:
                self._link = await self._connect()
                return
            except Exception as e:
                if attempt == _START_ATTEMPTS - 1:
                    raise
                logger.warning("Chime media attempt %d failed, trying the next relay: %s", attempt + 1, e)

    async def set_video(self, track: MediaStreamTrack | None) -> None:
        """Start (a track) or stop (None) coolton's video, by rebuilding the connection."""
        self._video = track
        link = self._link
        if link is not None:
            self._lost(link, "video changed")

    async def leave(self) -> None:
        self._stopping = True
        if self._reconnecting:
            self._reconnecting.cancel()
        link, self._link = self._link, None
        if link is None:
            return
        try:
            left = link.expect("LEAVE_ACK")
            link.send("LEAVE", leave=pb.SdkLeaveFrame())
            await asyncio.wait_for(left, 3)
        except Exception:
            pass
        await link.close()

    # -- connecting --------------------------------------------------------------------

    async def _connect(self) -> _Link:
        url = f"{self._signaling_url}?X-Chime-Control-Protocol-Version=3&X-Amzn-Chime-Send-Close-On-Error=1"
        socket = await websockets.connect(
            url, subprotocols=["_aws_wt_session", self._join_token],
            open_timeout=10, ping_interval=None, max_size=None,
        )
        link = _Link(socket)
        link.tasks.append(asyncio.create_task(self._read(link)))
        try:
            joined = link.expect("JOIN_ACK")
            # Chime lists the meeting (INDEX) right after JOIN_ACK.
            indexed = link.expect("INDEX")
            link.send("JOIN", join=pb.SdkJoinFrame(
                protocol_version=2,
                flags=_JOIN_FLAG_HAS_STREAM_UPDATE,
                client_details=pb.SdkClientDetails(
                    app_name="coolton", app_version="1", platform_name="python",
                    client_source="amazon-chime-sdk-js", chime_sdk_version="3.31.0",
                ),
                audio_session_id=self._audio_session_id,
                wants_compressed_sdp=False,
            ))
            ack = await asyncio.wait_for(joined, 15)
            turn = ack.joinack.turn_credentials
            uris = _ordered_turn_uris(turn.uris)
            if not uris and self._relay_only:
                raise RuntimeError("Chime's JOIN_ACK carried no TURN servers")
            try:
                await asyncio.wait_for(indexed, 5)
            except asyncio.TimeoutError:
                pass
            # aiortc uses one TURN server per peer; a failed attempt moves to the next kind.
            servers = []
            if uris:
                uri = uris[self._turn_attempt % len(uris)]
                servers = [RTCIceServer(urls=uri, username=turn.username, credential=turn.password)]
                logger.info("Chime media: relaying through %s (%d TURN servers offered)", uri, len(uris))
            self._turn_attempt += 1
            await self._subscribe(link, servers)
            self._turn_attempt -= 1  # this one works, keep it for reconnects
        except BaseException:
            await link.close()
            raise
        link.tasks.append(asyncio.create_task(self._keep_alive(link)))
        return link

    async def _subscribe(self, link: _Link, ice_servers: list[RTCIceServer]) -> None:
        pc = RTCPeerConnection(RTCConfiguration(iceServers=ice_servers, bundlePolicy=RTCBundlePolicy.MAX_BUNDLE))
        link.pc = pc
        connected = asyncio.Event()

        @pc.on("connectionstatechange")
        async def _state_changed():
            if pc.connectionState == "connected":
                connected.set()
            elif pc.connectionState in ("failed", "closed"):
                self._lost(link, f"peer {pc.connectionState}")

        @pc.on("track")
        def _track(track):
            if track.kind == "audio":
                link.tasks.append(asyncio.create_task(self._listen(track)))
            else:
                blackhole = MediaBlackhole()
                blackhole.addTrack(track)
                link.tasks.append(asyncio.create_task(blackhole.start()))

        audio = pc.addTransceiver(self._speaker, direction="sendrecv")
        audio.setCodecPreferences(_codecs("audio", "audio/opus"))
        video_track = self._video
        video = pc.addTransceiver(video_track or "video", direction="sendrecv" if video_track else "inactive")
        video.setCodecPreferences(_codecs("video", "video/h264", **{"packetization-mode": "1", "profile-level-id": "42e01f"}))
        if self._relay_only:
            _gather_relay_only(pc)
        await pc.setLocalDescription(await pc.createOffer())
        offer = pc.localDescription.sdp
        if self._relay_only and "typ relay" not in offer:
            raise RuntimeError("no TURN relay candidate, Chime's media is unreachable")

        streams = [pb.SdkStreamDescriptor(
            media_type=pb.AUDIO, track_label="AmazonChimeExpressAudio", stream_id=1, group_id=1,
            framerate=15, max_bitrate_kbps=600, avg_bitrate_bps=400_000, attendee_id=self.attendee_id,
        )]
        if video_track:
            streams.append(pb.SdkStreamDescriptor(
                media_type=pb.VIDEO, track_label="AmazonChimeExpressVideo", stream_id=2, group_id=2,
                framerate=_VIDEO_FPS, max_bitrate_kbps=_VIDEO_MAX_KBPS, attendee_id=self.attendee_id,
                width=VIDEO_SIZE[0], height=VIDEO_SIZE[1],
            ))
        subscribed = link.expect("SUBSCRIBE_ACK")
        link.send("SUBSCRIBE", sub=pb.SdkSubscribeFrame(
            duplex=pb.DUPLEX if video_track else pb.RX,
            send_streams=streams,
            receive_stream_ids=[0],
            # What the Chime JS SDK's unified-plan offers carry.
            sdp_offer=re.sub(r"^o=\S+", "o=mozilla-chrome", offer, count=1, flags=re.M),
            audio_host=self._audio_host,
            audio_checkin=False,
            audio_muted=False,
        ))
        ack = await asyncio.wait_for(subscribed, 15)
        if not ack.suback.sdp_answer:
            raise RuntimeError(f"Chime refused SUBSCRIBE: {ack.error.status} {ack.error.description}")
        if video_track and ack.suback.duplex == pb.RX:
            logger.warning("Chime refused coolton's video (status %s); audio carries on", ack.error.status)
        answer = "\r\n".join(line for line in ack.suback.sdp_answer.split("\r\n") if "typ srflx" not in line)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
        try:
            await asyncio.wait_for(connected.wait(), 15)
        except asyncio.TimeoutError:
            logger.warning("Chime media didn't connect. Answer candidates: %s. ICE pairs: %s",
                           [line for line in answer.split("\r\n") if line.startswith("a=candidate")],
                           _ice_pairs(pc))
            raise RuntimeError(
                f"Chime's media didn't connect within 15 s (ICE {pc.iceConnectionState}, "
                f"connection {pc.connectionState})"
            ) from None

    # -- running -----------------------------------------------------------------------

    async def _read(self, link: _Link) -> None:
        try:
            async for data in link.socket:
                if not isinstance(data, bytes):
                    continue
                link.last_inbound = time.monotonic()
                frame = decode_frame(data)
                if frame is None:
                    continue
                name = frame_type(frame)
                if name == "PING_PONG" and frame.ping_pong.type == pb.PING:
                    link.send("PING_PONG", ping_pong=pb.SdkPingPongFrame(type=pb.PONG, ping_id=frame.ping_pong.ping_id))
                if name == "AUDIO_STATUS" and frame.audio_status.audio_status != 200:
                    status = frame.audio_status.audio_status
                    if status in _TERMINAL_AUDIO_STATUS:
                        self._end(f"Chime audio status {status}")
                    else:
                        self._lost(link, f"Chime audio status {status}")
                link.deliver(frame)
                self._on_signal_frame(frame)
        except websockets.ConnectionClosed:
            pass
        code = link.socket.close_code
        link.fail_waiters(f"Chime signaling closed ({code})")
        if code == _MEETING_ENDED_CLOSE_CODE:
            self._end("the huddle ended")
        else:
            self._lost(link, f"signaling closed ({code})")

    async def _listen(self, track) -> None:
        try:
            while True:
                self._on_mic_frame(await track.recv())
        except MediaStreamError:
            pass

    async def _keep_alive(self, link: _Link) -> None:
        while True:
            await asyncio.sleep(5)
            link.ping()
            silent = time.monotonic() - link.last_inbound
            if silent > _SILENCE_LIMIT:
                self._lost(link, f"no signaling for {silent:.0f}s")
                return

    def _lost(self, link: _Link, reason: str) -> None:
        if link is not self._link or self._stopping or self._ended:
            return
        logger.info("Huddle media connection lost (%s), reconnecting", reason)
        self._link = None
        asyncio.ensure_future(link.close())
        if self._reconnecting is None or self._reconnecting.done():
            self._reconnecting = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        started = time.monotonic()
        attempt = 0
        while not self._stopping:
            try:
                self._link = await self._connect()
                return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                attempt += 1
                if time.monotonic() - started > _RECONNECT_WINDOW:
                    self._end(f"couldn't reconnect: {e}")
                    return
                logger.warning("Huddle reconnect attempt %d failed: %s", attempt, e)
                await asyncio.sleep(min(8, 2 ** (attempt - 1)))

    def _end(self, reason: str) -> None:
        if self._ended or self._stopping:
            return
        self._ended = True
        link, self._link = self._link, None
        if link is not None:
            asyncio.ensure_future(link.close())
        self._on_ended(reason)
