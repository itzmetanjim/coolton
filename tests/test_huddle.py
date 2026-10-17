"""Slack huddles (agent.huddle): finding and joining one, the media connection to Chime,
when coolton talks, and how the huddle plugs into the rest of coolton."""
import asyncio
import importlib
import math
import struct
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import websockets
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from agent import huddle
from agent.huddle import signaling_pb2 as pb
from agent.huddle import slack_api
from agent.huddle.chime import Attendees, ChimeConnection, decode_frame, encode_frame
from agent.huddle.media import DesktopTrack, SpeakerTrack
from agent.huddle.session import HuddleSession
from agent.huddle.slack_api import JoinedHuddle
from agent.huddle.voice_agent import ReplyParser


async def _until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.01)


def test_reply_parser_streams_sentences_chat_posts_and_directives():
    """Tags and sentences arrive split across stream chunks; nothing in <chat> is spoken."""
    parser = ReplyParser()
    actions = []
    for delta in ["Sure. The li", "nk is in the <ch", "at>https://example.com/x and a ", "list</chat> Bye", " now. <lea", "ve/>"]:
        actions += parser.feed(delta)
    actions += parser.finish()
    assert actions == [
        ("say", "Sure."), ("say", "The link is in the"), ("chat", "https://example.com/x and a list"),
        ("say", "Bye now."), ("leave", ""),
    ]
    silent = ReplyParser()
    # Seen live: a zero-width character inside the tag, which must never be read out.
    assert silent.feed("<silen\u200ct") + silent.feed("/>") + silent.finish() == [("silent", "")]
    # A text part ends before a tool call: say it now, not after the tool round trip.
    before_tool = ReplyParser()
    assert before_tool.feed("On it, I'll check.") + before_tool.end_part() == [("say", "On it, I'll check.")]
    literal = ReplyParser()
    assert literal.feed("3 < 5, ") + literal.finish() == [("say", "3 < 5,")]


def test_join_response_and_finding_the_live_huddle(monkeypatch):
    joined = slack_api.parse_join_response({
        "ok": True,
        "call": {"call_id": "R1", "free_willy": {
            "meeting": {"MeetingId": "m", "MeetingFeatures": None, "MediaPlacement": {"SignalingUrl": "wss://s", "AudioHostUrl": "a"}},
            "attendee": {"AttendeeId": "A0", "JoinToken": "tok"},
        }},
        "canvas": {"thread_channel_id": "C1", "root_thread_ts": "9.9"},
        "huddle": {"participants": ["U1", {"user_id": "U2"}]},
    })
    assert (joined.call_id, joined.channel_id, joined.thread_ts) == ("R1", "C1", "9.9")
    assert joined.participant_ids == ["U1", "U2"]
    assert "MeetingFeatures" not in joined.meeting and joined.attendee["JoinToken"] == "tok"

    live = {"ts": "5.5", "subtype": "huddle_thread", "room": {"id": "R1", "date_end": 0}}
    ended = {"ts": "4.4", "subtype": "huddle_thread", "room": {"id": "R0", "has_ended": True}}
    responses = {
        "conversations.replies": {"messages": [{"ts": "1.1", "text": "hi"}]},
        "conversations.history": {"messages": [{"ts": "6.6"}, live, ended]},
    }
    monkeypatch.setattr(slack_api, "_call", lambda method, fields: responses[method])
    # Asked in a thread that isn't a huddle's: the channel's live huddle, not the ended one.
    assert slack_api.find_live_huddle("C1", "1.1") == "5.5"
    responses["conversations.replies"] = {"messages": [ended]}
    responses["conversations.history"] = {"messages": [ended]}
    assert slack_api.find_live_huddle("C1", "4.4") is None


# -- the voice session -----------------------------------------------------------------

class _FakeVoice:
    """Deepgram TTS stand-in: each sentence becomes `frames` 20 ms frames of audio."""

    def __init__(self, session, frames=2):
        self.session = session
        self.frames = frames
        self.said = []
        self.cleared = 0

    async def say(self, text):
        self.said.append(text)
        self.session._on_tts_audio(b"\x01\x00" * 960 * self.frames)
        self.session._on_tts_flushed(1)

    async def flush(self):
        pass

    async def clear(self):
        self.cleared += 1

    async def close(self):
        pass


def _turn(event, transcript=""):
    return SimpleNamespace(event=event, transcript=transcript, audio_window_start=0.0, audio_window_end=1.0)


async def _session(stream, frames=2, others=2):
    joined = JoinedHuddle(call_id="R1", channel_id="C1", thread_ts="9.9", participant_ids=[], meeting={},
                          attendee={"AttendeeId": "A0"})
    client = Mock()
    client.users_info.return_value = {"user": {"profile": {"display_name": "Lily"}}}
    session = HuddleSession(joined, client=client, requester_id="U1", request_text="join us")
    session.loop = asyncio.get_running_loop()
    session.models = [("fake", FunctionModel(stream_function=stream))]
    session.voice = _FakeVoice(session, frames)
    session.listener = SimpleNamespace(epoch=None)
    frame = pb.SdkSignalFrame(timestamp_ms=1, type=pb.SdkSignalFrame.AUDIO_STREAM_ID_INFO)
    for i in range(others + 1):
        frame.audio_stream_id_info.streams.add(audio_stream_id=i + 1, attendee_id=f"A{i}")
    session.attendees.handle(frame)

    async def play():  # what sending coolton's audio to Chime does to the speaker track
        while True:
            await session.speaker.recv()

    session._tasks = [asyncio.create_task(play()), asyncio.create_task(session._work())]
    return session


def _last_prompt(messages) -> str:
    for message in reversed(messages):
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                return part.content
    return ""


def test_a_reply_started_early_is_only_heard_once_the_turn_really_ends(monkeypatch):
    asyncio.run(_a_reply_started_early_is_only_heard_once_the_turn_really_ends(monkeypatch))


async def _a_reply_started_early_is_only_heard_once_the_turn_really_ends(monkeypatch):
    """EagerEndOfTurn starts the reply (one model call, reused); nothing is said, delegated or
    posted until EndOfTurn confirms the same words; TurnResumed drops it without side effects."""
    calls = []

    async def stream(messages, info):
        calls.append(_last_prompt(messages))
        if "weather" in calls[-1]:
            yield {0: DeltaToolCall(name="delegate_to_subagent", json_args='{"target": "research", "task": "weather"}')}
        else:
            yield "It's four."

    session = await _session(stream)
    delegated = []
    monkeypatch.setattr(session, "delegate", lambda tasks, user_id: delegated.append(tasks) or 1)

    await session.on_turn(_turn("EagerEndOfTurn", "coolton what's two plus two"))
    await asyncio.sleep(0.1)
    assert session.voice.said == []
    await session.on_turn(_turn("EndOfTurn", "Coolton, what's two plus two?"))
    await _until(lambda: session._reply is None)
    assert session.voice.said == ["It's four."]
    assert len(calls) == 1

    await session.on_turn(_turn("EagerEndOfTurn", "coolton what's the weather"))
    await asyncio.sleep(0.1)
    await session.on_turn(_turn("TurnResumed"))
    await asyncio.sleep(0.1)
    assert delegated == [] and session._speculative is None
    assert session.voice.said == ["It's four."]


def test_barge_in_pauses_then_resumes_for_a_backchannel_or_stops_for_an_interruption():
    asyncio.run(_barge_in_pauses_then_resumes_for_a_backchannel_or_stops_for_an_interruption())


async def _barge_in_pauses_then_resumes_for_a_backchannel_or_stops_for_an_interruption():
    async def stream(messages, info):
        yield "First sentence here. Second sentence here. Third sentence here."

    session = await _session(stream, frames=25)  # half a second of audio per sentence
    await session.on_turn(_turn("EndOfTurn", "coolton tell me a story"))
    await _until(lambda: session.speaker.busy)

    await session.on_turn(_turn("StartOfTurn"))
    assert session.speaker._paused
    await session.on_turn(_turn("EndOfTurn", "yeah"))
    assert not session.speaker._paused and session._reply is not None  # carries on

    await asyncio.sleep(0.2)
    await session.on_turn(_turn("StartOfTurn"))
    await session.on_turn(_turn("Update", "hold on wait a second"))
    assert not session.speaker.busy and session.voice.cleared == 1
    await _until(lambda: session.history)
    spoken = session.history[-1].parts[0].content
    assert spoken.startswith("First sentence here.") and spoken.endswith("[interrupted]")
    assert "Third" not in spoken  # queued, never heard


def test_delegated_work_runs_in_the_background_and_its_result_is_announced(monkeypatch):
    asyncio.run(_delegated_work_runs_in_the_background_and_its_result_is_announced(monkeypatch))


async def _delegated_work_runs_in_the_background_and_its_result_is_announced(monkeypatch):
    async def stream(messages, info):
        last = messages[-1]
        if any(isinstance(p, ToolReturnPart) for p in last.parts):
            yield "On it."
        elif "[task 1 finished]" in _last_prompt(messages):
            yield "It's forty two."
        else:
            yield {0: DeltaToolCall(name="delegate_to_subagent", json_args='{"target": "research", "task": "find the answer"}')}

    ran = []

    def fake_delegate_many(tasks, deps):
        ran.append((tasks, deps))
        time.sleep(0.2)
        return "The answer is 42."

    monkeypatch.setattr("agent.subagents.delegate_many", fake_delegate_many)
    session = await _session(stream)
    await session.on_turn(_turn("EndOfTurn", "coolton can you find the answer"))
    await _until(lambda: session.voice.said == ["On it."])
    assert session._delegations == 1  # acknowledged while the task still runs
    await _until(lambda: session.voice.said == ["On it.", "It's forty two."])
    (tasks, deps), = ran
    assert tasks[0][0] == "research" and tasks[0][1].startswith("find the answer")
    assert "live Slack huddle" in tasks[0][1]
    assert (deps.channel_id, deps.thread_ts, deps.user_id) == ("C1", "9.9", "U1")


def test_the_call_is_saved_in_the_thread_history_for_later_turns(monkeypatch, tmp_path):
    asyncio.run(_the_call_is_saved_in_the_thread_history_for_later_turns(monkeypatch, tmp_path))


async def _the_call_is_saved_in_the_thread_history_for_later_turns(monkeypatch, tmp_path):
    """What was said and typed in the call (and what coolton said) ends up in the huddle
    thread's stored history next to what was there before, saved once however often it's saved."""
    from thread_context.store import ConversationStore

    store = ConversationStore(directory=str(tmp_path), legacy_file=None)
    monkeypatch.setattr("thread_context.conversation_store", store)
    before = [ModelRequest(parts=[UserPromptPart(content="join the huddle")]), ModelResponse(parts=[TextPart(content="joining!")])]
    store.set_history("C1", "9.9", before)

    async def stream(messages, info):
        yield "The demo is at three."

    session = await _session(stream)
    session._joined_at = time.time()
    await session.on_turn(_turn("EndOfTurn", "coolton when's the demo"))
    await _until(lambda: session.voice.said and session._reply is None)
    await session.on_turn(_turn("EndOfTurn", "yeah I'll be there Sam"))
    session.on_chat("U2", "<@BOT1> can you send the deck?")
    await asyncio.sleep(0.1)
    await session._save_transcript()
    await session.stop("asked to leave")

    history = store.get_history("C1", "9.9")
    assert history[:2] == before and len(history) == 3
    record = history[-1].parts[0].content
    for line in ("[voice] someone: coolton when's the demo", "coolton said: The demo is at three.",
                 "[voice] someone: yeah I'll be there Sam", "[chat] Lily: <@BOT1> can you send the deck?",
                 "coolton left the huddle: asked to leave"):
        assert line in record
    assert store.get_last_seen_ts("C1", "9.9") == "9.9"  # the next turn still reads the thread


# -- the media connection --------------------------------------------------------------

def _tone(seconds=0.5, rate=48000) -> bytes:
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / rate)))
                    for i in range(int(seconds * rate)))


class _ToneTrack(SpeakerTrack):
    def __init__(self):
        super().__init__()
        self.play(_tone(5))


async def _fake_chime(received: dict):
    """A stand-in for Chime's signaling server, answering with its own WebRTC peer."""
    pcs = []

    async def consume(track, key):
        try:
            while True:
                frame = await track.recv()
                if key == "audio" and any(bytes(frame.planes[0])):
                    received["audio"] = True
                if key == "video":
                    received["video"] = True
        except MediaStreamError:
            pass

    async def handler(socket):
        async for data in socket:
            frame = decode_frame(data)
            kind = pb.SdkSignalFrame.Type.Name(frame.type)
            received.setdefault("frames", []).append(kind)
            if kind == "JOIN":
                await socket.send(encode_frame("JOIN_ACK", joinack=pb.SdkJoinAckFrame()))
                info = pb.SdkAudioStreamIdInfoFrame()
                info.streams.add(audio_stream_id=1, attendee_id="A0", external_user_id="coolton")
                info.streams.add(audio_stream_id=2, attendee_id="A-lily", external_user_id="T0266FRGM-U0LILY1234")
                await socket.send(encode_frame("AUDIO_STREAM_ID_INFO", audio_stream_id_info=info))
                await socket.send(encode_frame("INDEX", index=pb.SdkIndexFrame()))
            elif kind == "SUBSCRIBE":
                received.setdefault("subscribes", []).append(frame.sub)
                pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
                pcs.append(pc)
                pc.on("track", lambda track: asyncio.ensure_future(consume(track, track.kind)))
                await pc.setRemoteDescription(RTCSessionDescription(frame.sub.sdp_offer, "offer"))
                pc.addTrack(_ToneTrack())
                await pc.setLocalDescription(await pc.createAnswer())
                ack = pb.SdkSubscribeAckFrame(sdp_answer=pc.localDescription.sdp, duplex=frame.sub.duplex)
                await socket.send(encode_frame("SUBSCRIBE_ACK", suback=ack))
                metadata = pb.SdkAudioMetadataFrame()
                metadata.attendee_states.add(audio_stream_id=2, volume=10)
                await socket.send(encode_frame("AUDIO_METADATA", audio_metadata=metadata))
            elif kind == "LEAVE":
                await socket.send(encode_frame("LEAVE_ACK", leave_ack=pb.SdkLeaveAckFrame()))

    server = await websockets.serve(handler, "127.0.0.1", 0, subprotocols=["_aws_wt_session"])
    return server, pcs


def test_chime_connection_talks_listens_and_turns_video_on():
    asyncio.run(_chime_connection_talks_listens_and_turns_video_on())


async def _chime_connection_talks_listens_and_turns_video_on():
    """The real signaling and WebRTC code against a fake Chime on loopback: audio both ways,
    who's who from the signaling frames, video turned on by rebuilding, and leaving."""
    received: dict = {}
    server, pcs = await _fake_chime(received)
    port = server.sockets[0].getsockname()[1]
    heard = []
    speaker = SpeakerTrack()
    attendees = Attendees("A0")
    connection = ChimeConnection(
        {"MediaPlacement": {"SignalingUrl": f"ws://127.0.0.1:{port}/control", "AudioHostUrl": "audio"}},
        {"AttendeeId": "A0", "JoinToken": "token"},
        speaker=speaker,
        on_mic_frame=lambda frame: heard.append(any(bytes(frame.planes[0]))),
        on_signal_frame=attendees.handle,
        on_ended=lambda reason: None,
        relay_only=False,
    )
    try:
        await connection.start()
        speaker.play(_tone(2))
        await _until(lambda: received.get("audio") and any(heard), timeout=10)
        assert attendees.others == {"A-lily"} and attendees.user_id("A-lily") == "U0LILY1234"
        assert attendees.loudest(time.monotonic() - 10, time.monotonic()) == "A-lily"
        first = received["subscribes"][0]
        assert first.duplex == pb.RX and first.sdp_offer.startswith("v=0") and "o=mozilla-chrome" in first.sdp_offer

        await connection.set_video(DesktopTrack())
        await _until(lambda: len(received["subscribes"]) == 2 and received.get("video"), timeout=10)
        second = received["subscribes"][1]
        assert second.duplex == pb.DUPLEX and [s.media_type for s in second.send_streams] == [pb.AUDIO, pb.VIDEO]
    finally:
        await connection.leave()
        for pc in pcs:
            await pc.close()
        server.close()
    assert "LEAVE" in received["frames"]


# -- how a huddle plugs into the rest of coolton ----------------------------------------

class _FakeSession:
    def __init__(self):
        self.chat = []
        self.desktop = 0
        self.video_on = False
        self.loop = SimpleNamespace(call_soon_threadsafe=lambda fn, *args: fn(*args))

    def on_chat(self, user_id, text):
        self.chat.append((user_id, text))

    def desktop_active(self):
        self.desktop += 1
        self.video_on = True


@pytest.fixture
def in_huddle(monkeypatch):
    session = _FakeSession()
    monkeypatch.setitem(huddle._sessions, ("C123", "50.5"), session)
    return session


def test_the_huddle_thread_goes_to_the_huddle_not_a_normal_turn(monkeypatch, in_huddle):
    from listeners.events.app_mentioned import handle_app_mentioned
    from listeners.events.message import handle_message

    monkeypatch.setenv("COOLTON_BOT_ID", "BOT1")
    monkeypatch.setattr("agent.policy_consent.has_consent", lambda user_id: True)
    turns = []
    monkeypatch.setattr("listeners.events.message.run_agent_turn", lambda **kw: turns.append(kw))
    monkeypatch.setattr("listeners.events.app_mentioned.run_agent_turn", lambda **kw: turns.append(kw))
    context = SimpleNamespace(channel_id="C123", user_id="U1", user_token=None)
    common = dict(client=Mock(), context=context, logger=Mock(), say=Mock(), say_stream=Mock(), set_status=None)

    handle_message(event={"type": "message", "text": "anyone got the link?", "ts": "51.1", "thread_ts": "50.5"}, **common)
    handle_app_mentioned(event={"type": "app_mention", "text": "<@BOT1> leave pls", "ts": "52.2", "thread_ts": "50.5"}, **common)
    assert in_huddle.chat == [("U1", "anyone got the link?"), ("U1", "<@BOT1> leave pls")]
    assert turns == []


def test_stream_tools_share_the_desktop_as_huddle_video_instead_of_a_link(monkeypatch, in_huddle):
    from pydantic_ai import RunContext

    agent_mod = importlib.import_module("agent.agent")

    monkeypatch.setenv("E2B_API_KEY", "e2b-test")
    monkeypatch.setattr(agent_mod, "_computer_stream_start", Mock(side_effect=AssertionError("no VNC link")))
    deps = SimpleNamespace(client=Mock(), channel_id="C123", thread_ts="50.5", user_id="U1",
                           last_screenshot_post_ts=0.0, sandbox_keepalive_seconds=0.0)
    ctx = RunContext(model=None, usage=None, prompt="", deps=deps)
    assert "video" in agent_mod.computer_stream_tool(ctx)
    assert in_huddle.desktop == 1

    posted = []
    monkeypatch.setattr(agent_mod, "_surface", lambda deps: SimpleNamespace(post_image=lambda *a: posted.append(a)))
    agent_mod._maybe_post_screenshot(ctx, b"png")
    assert posted == []  # people are watching it live already


def test_coolton_joins_only_once_someone_in_the_huddle_approves(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setattr(slack_api, "workspace_url", lambda: "https://x.slack.com")
    monkeypatch.setattr(slack_api, "find_live_huddle", lambda channel, thread: "50.5")
    joins = []
    monkeypatch.setattr(huddle, "_join", lambda client, pending, approver: joins.append((pending["huddle_ts"], approver)))
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "60.6"}

    status = huddle.request_join(client, channel_id="C123", thread_ts="1.1", requester_id="U1", request_text="join")
    assert "approve" in status and joins == []
    posted = client.chat_postMessage.call_args.kwargs
    assert posted["thread_ts"] == "50.5" and "!kick-coolton" in posted["text"]
    pending_id = posted["blocks"][1]["elements"][0]["value"]

    huddle.approve(client, pending_id, "U2")
    huddle.approve(client, pending_id, "U3")  # a second click does nothing
    assert joins == [("50.5", "U2")]


def test_kick_coolton_in_the_huddle_thread_leaves_without_a_mention(monkeypatch, in_huddle):
    from listeners.events.message import handle_message

    stops = []
    in_huddle.stop = lambda reason: stops.append(reason)
    monkeypatch.setattr(huddle.asyncio, "run_coroutine_threadsafe", lambda result, loop: None)
    context = SimpleNamespace(channel_id="C123", user_id="U1", user_token=None)
    handle_message(event={"type": "message", "text": "!kick-coolton", "ts": "51.1", "thread_ts": "50.5"},
                   client=Mock(), context=context, logger=Mock(), say=Mock(), say_stream=Mock(), set_status=None)
    assert stops == ["<@U1> kicked it"] and in_huddle.chat == []


def test_with_no_huddle_going_coolton_starts_one_and_can_leave_it(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test")
    monkeypatch.setattr(slack_api, "workspace_url", lambda: "https://x.slack.com")
    monkeypatch.setattr(slack_api, "find_live_huddle", lambda channel, thread: None)
    started = []
    monkeypatch.setattr(huddle, "_join", lambda client, pending, approver, start=False: started.append((pending["channel_id"], start)))
    client = Mock()

    assert "started one" in huddle.request_join(client, channel_id="C123", thread_ts="1.1", requester_id="U1", request_text="start a huddle")
    assert started == [("C123", True)] and not client.chat_postMessage.called  # no approval prompt
    assert "DM" in huddle.request_join(client, channel_id="D123", thread_ts="1.1", requester_id="U1", request_text="call me")

    session = _FakeSession()
    session.channel_id, session.thread_ts = "C123", "50.5"
    stops = []
    session.stop = lambda reason: stops.append(reason)
    monkeypatch.setitem(huddle._sessions, ("C123", "50.5"), session)
    monkeypatch.setattr(huddle.asyncio, "run_coroutine_threadsafe", lambda result, loop: None)
    assert huddle.leave("C123", "7.7", "U1") == "Left the huddle."
    assert stops == ["<@U1> asked it to leave"]


def test_offer_and_answer_are_rewritten_the_way_chime_needs():
    from agent.huddle.chime import _browser_like_offer, _candidates_in_every_section

    offer = ("v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
             "a=fingerprint:sha-256 AA\r\na=fingerprint:sha-384 BB\r\na=fingerprint:sha-512 CC\r\n")
    assert _browser_like_offer(offer) == ("v=0\r\no=mozilla-chrome 1 1 IN IP4 0.0.0.0\r\n"
                                          "m=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=fingerprint:sha-256 AA\r\n")

    # Chime lists its candidates under the first section only; aiortc reads the bundle's
    # candidates from the last one.
    answer = ("v=0\r\na=group:BUNDLE 0 1\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\na=mid:0\r\n"
              "a=candidate:H 1 udp 1 10.3.107.247 17298 typ host\r\nm=video 9 UDP/TLS/RTP/SAVPF 102\r\na=mid:1\r\n")
    sections = _candidates_in_every_section(answer).split("m=")[1:]
    assert all("a=candidate:H 1 udp 1 10.3.107.247 17298 typ host" in s for s in sections) and len(sections) == 2


def test_tts_batches_sentences_and_keeps_under_deepgrams_flush_limit(monkeypatch):
    from agent.huddle import speech

    monkeypatch.setattr(speech, "_FLUSHES_PER_WINDOW", 2)
    monkeypatch.setattr(speech, "_FLUSH_WINDOW", 0.3)
    voice = speech.Voice("dg-test", on_audio=lambda pcm: None, on_flushed=lambda n: None)
    flushes = []

    async def sent(*args):
        return None

    async def flush():
        flushes.append(voice._unflushed)

    connection = SimpleNamespace(send_text=sent, send_flush=flush)

    async def connected():
        voice._connection = connection
        return connection

    voice._connected = connected

    async def deepgram_flushed():  # what Voice._read does when Deepgram says "Flushed"
        voice._groups.popleft()
        await voice._flush_waiting()

    async def main():
        for i in range(3):
            await voice.say(f"sentence {i}.")
        assert flushes == [1]  # the first right away; the rest wait for its audio
        await deepgram_flushed()
        assert flushes == [1, 2]  # both together: Deepgram's limit (2 here) is now used up
        await voice.say("one more.")
        await deepgram_flushed()
        assert flushes == [1, 2]  # over the limit: it waits
        await voice.flush()  # the end of the reply waits for room under the limit
        assert flushes == [1, 2, 1]

    asyncio.run(main())


def test_a_restart_leaves_huddles_properly_then_ends_as_sigterm_always_did(monkeypatch):
    import signal

    from agent import shutdown

    handlers, kills = {}, []
    monkeypatch.setattr(signal, "signal", lambda sig, handler: handlers.__setitem__(sig, handler))
    monkeypatch.setattr(shutdown.os, "kill", lambda pid, sig: kills.append(sig))
    assert huddle._leave_all in shutdown._cleanups
    left = []
    monkeypatch.setattr(shutdown, "_cleanups", [lambda: left.append(True), lambda: 1 / 0, lambda: left.append(2)])
    shutdown.install()
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    # Every cleanup runs, even after one fails, then SIGTERM ends the process as before.
    assert left == [True, 2] and kills == [signal.SIGTERM] and handlers[signal.SIGTERM] == signal.SIG_DFL


def test_the_reply_to_an_interruption_knows_what_it_interrupted():
    asyncio.run(_the_reply_to_an_interruption_knows_what_it_interrupted())


async def _the_reply_to_an_interruption_knows_what_it_interrupted():
    seen = {}

    async def stream(messages, info):
        prompt = _last_prompt(messages)
        seen[prompt] = " ".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)
        if "story" in prompt:
            yield "Pixel was a gray cat. She lived in a bookshop. She liked the window."
        else:
            yield "Okay, stopping."

    session = await _session(stream, frames=25)
    await session.on_turn(_turn("EndOfTurn", "coolton tell me a story"))
    await _until(lambda: session.speaker.busy)
    await asyncio.sleep(0.1)

    # "Coolton, stop." is too short to interrupt mid-turn, so the reply to it starts
    # speculatively while the story is still the reply being said.
    await session.on_turn(_turn("StartOfTurn"))
    await session.on_turn(_turn("Update", "coolton stop"))
    await session.on_turn(_turn("EagerEndOfTurn", "coolton stop"))
    await session.on_turn(_turn("EndOfTurn", "Coolton, stop."))
    await _until(lambda: session._reply is None and session.voice.said[-1:] == ["Okay, stopping."])

    stop_prompt = next(p for p in seen if "stop" in p.lower())
    assert "Pixel was a gray cat. [interrupted]" in seen[stop_prompt]
