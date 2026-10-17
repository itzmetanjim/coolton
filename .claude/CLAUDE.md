# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

See the root `../.claude/CLAUDE.md` for monorepo-wide architecture, commands, and a comparison of all implementations.

## Pydantic AI Specifics

**Agent (`agent/agent.py`)** is a Pydantic AI `Agent` with `deps_type=AgentDeps`. The model is **not** set on the agent (to avoid import-time client creation); instead `get_model()` selects the provider at runtime based on available API keys (`ANTHROPIC_API_KEY` preferred over `OPENAI_API_KEY`) and is passed at each `run_sync()` call site. Tools are passed via the `tools=[]` constructor parameter (not decorators) so each tool lives in its own file under `agent/tools/`.

**Conversation history** stores `list[ModelMessage]` from Pydantic AI and is passed directly as `message_history=` to `run_sync()`.

**Feedback blocks** use the native `FeedbackButtonsElement` from `slack_sdk.models.blocks`. A single `feedback` action ID is registered.

## Slack huddles (`agent/huddle/`)

`join_huddle_tool` starts a huddle when the channel has none (`rooms.join` creates one), and otherwise posts an approval prompt in the huddle's thread (`huddle_join_approve`/`decline` buttons, `listeners/actions/huddle_actions.py`); one approval opts the whole huddle in to the policy, so the session doesn't ask anyone for consent (it only skips banned people), and `!kick-coolton` in that thread leaves at once. coolton joins a huddle as cooltonUser (bots can't), with that account's browser session (`COOLTON_USER_XOXC`/`COOLTON_USER_XOXD`, Slack's client endpoints: `rooms.join` returns the huddle's Amazon Chime meeting). `chime.py` speaks Chime's protobuf signaling (proto vendored from amazon-chime-sdk-js, `signaling_pb2.py` is protoc output) and runs an aiortc peer through Chime's TURN relay; turning video on or off rebuilds the connection. Deepgram does speech both ways (`speech.py`: Flux STT with built-in turn detection, one streaming TTS socket). The voice agent (`voice_agent.py`) is a Pydantic AI agent whose only tools start subagents in the background; its text streams sentence by sentence into TTS, `<chat>` goes to the huddle thread. `session.py` handles turn-taking (speculative replies on EagerEndOfTurn, barge-in) and everything runs on one asyncio loop thread (`agent/huddle/__init__.py`). Messages in the huddle's thread are routed to the session by the message/app_mention handlers; the stream tools and `computer_use` turn on the huddle video instead of posting a noVNC link. The call's transcript is saved (every 30 s, replacing its last save) as one message in the huddle thread's `conversation_store` history, so later turns there remember it.

## Computer Use (XFCE desktop)

`agent/desktop_helpers.py` drives a real XFCE desktop (Xvfb + xfce4 + xdotool/scrot/x11vnc) inside the same per-thread E2B sandbox `run_linux_command` uses — started lazily, not at sandbox boot. It's a hand-ported command layer, not the `e2b-desktop` package: that SDK does all its setup in `Sandbox.create()` and never repairs state on `connect()`, which doesn't fit coolton's reconnect-to-a-stored-sandbox-id model. `ensure_desktop()` is idempotent so any action can call it safely after a pause/resume. The `computer_use` / `computer_stream_tool` tools (`agent/tools/computer_use.py` + wrappers in `agent/agent.py`) are gated on `provider_config.is_vision_model(ctx.model.model_name)` — driven by a `"vision"` tag in `providers.json`, not a hardcoded model list. Unlike `run_linux_command`, the sandbox is NOT paused after every desktop action (a live noVNC stream needs it to stay warm); `AgentDeps.desktop_active` tracks that a session is open so `run_agent` pauses it once at the end of the turn instead.