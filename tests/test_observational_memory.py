"""agent.observational_memory: long threads keep a log of observations instead of raw history."""
import datetime
import importlib
from types import SimpleNamespace

from pydantic_ai.messages import (
    BinaryContent, ModelRequest, ModelResponse, SystemPromptPart, TextPart, ToolCallPart, ToolReturnPart,
    UserPromptPart,
)

from agent import observational_memory as om

agent_mod = importlib.import_module("agent.agent")
T0 = datetime.datetime(2026, 10, 2, 14, 0, tzinfo=datetime.timezone.utc)
DEPS = SimpleNamespace(model_context_window=100_000, provider_tag_filter=None)  # observes past 20k tokens


def _turn(i: int, size: int = 4_000) -> list:
    """One user message + reply, ~size/4 tokens each, i minutes after T0."""
    when = T0 + datetime.timedelta(minutes=i)
    return [ModelRequest(parts=[UserPromptPart(f"U1 (Lily): message {i} " + "x" * size, timestamp=when)]),
            ModelResponse(parts=[TextPart(f"reply {i} " + "y" * size)], timestamp=when)]


def _history(turns: int, system: str = "SYSTEM") -> list:
    messages = [m for i in range(turns) for m in _turn(i)]
    first = messages[0]
    return [ModelRequest(parts=[SystemPromptPart(system), *first.parts]), *messages[1:]]


def _observer(monkeypatch, lines="- 2026-10-02 14:00 Lily asked coolton about X", reflected=None):
    tasks = []

    def ask(task, deps):
        tasks.append(task)
        if task.startswith(om.REFLECTOR_INSTRUCTIONS):
            return reflected or ""
        return lines

    monkeypatch.setattr(om, "_ask", ask)
    return tasks


def test_a_short_thread_is_left_alone(monkeypatch):
    tasks = _observer(monkeypatch)
    history = _history(5)
    assert om.maybe_observe(history, DEPS) is history
    assert tasks == []


def test_a_long_thread_becomes_an_observation_log_plus_recent_raw_messages(monkeypatch):
    tasks = _observer(monkeypatch)
    history = _history(20)  # ~40k tokens

    result = om.maybe_observe(history, DEPS)

    log, tail = result[0], result[1:]
    assert isinstance(log.parts[0], SystemPromptPart) and log.parts[0].content == "SYSTEM"
    assert log.parts[1].content == f"{om.LOG_HEADER}\n- 2026-10-02 14:00 Lily asked coolton about X"
    assert tail == history[-len(tail):] and om._estimate_tokens(tail) <= om._budget(DEPS.model_context_window)[1]
    # The Observer sees dated, attributed lines.
    assert "[2026-10-02 14:00] message: U1 (Lily): message 0" in tasks[0]
    assert "[2026-10-02 14:00] coolton: reply 0" in tasks[0]


def test_new_observations_are_appended_so_earlier_ones_never_change(monkeypatch):
    _observer(monkeypatch, lines="- first batch")
    once = om.maybe_observe(_history(20), DEPS)
    _observer(monkeypatch, lines="- second batch")
    twice = om.maybe_observe([*once, *[m for i in range(20, 40) for m in _turn(i)]], DEPS)

    assert twice[0].parts[-1].content == f"{om.LOG_HEADER}\n- first batch\n- second batch"


def test_a_long_log_is_reflected_into_a_shorter_one(monkeypatch):
    long_log = "\n".join(f"- note {i} " + "z" * 100 for i in range(om.REFLECT_AFTER_TOKENS * om._CHARS_PER_TOKEN // 100))
    _observer(monkeypatch, lines="- newest", reflected="- the consolidated log")
    history = [om.log_message(long_log), *[m for i in range(20) for m in _turn(i)]]

    result = om.maybe_observe(history, DEPS)

    assert result[0].parts[-1].content == f"{om.LOG_HEADER}\n- the consolidated log"


def test_an_observer_failure_or_empty_answer_keeps_the_raw_history(monkeypatch):
    history = _history(20)
    _observer(monkeypatch, lines="sure! here are the notes")  # no "- " lines
    assert om.maybe_observe(history, DEPS) is history

    def boom(task, deps):
        raise RuntimeError("all providers down")

    monkeypatch.setattr(om, "_ask", boom)
    assert om.maybe_observe(history, DEPS) is history


def test_a_tool_call_is_never_separated_from_its_result(monkeypatch):
    _observer(monkeypatch)
    history = [*_history(18),
               ModelResponse(parts=[ToolCallPart("run_linux_command", {"cmd": "x" * 30_000}, tool_call_id="c1")]),
               ModelRequest(parts=[ToolReturnPart("run_linux_command", "ok", tool_call_id="c1")])]

    tail = om.maybe_observe(history, DEPS)[1:]

    assert isinstance(tail[0], ModelResponse) and tail[0].parts[0].tool_call_id == "c1"


def test_a_small_model_observes_sooner_and_an_old_summary_seeds_the_log(monkeypatch):
    _observer(monkeypatch, lines="- newer")
    legacy = ModelRequest(parts=[UserPromptPart("[Earlier conversation summary — 40 older messages compressed]\nthey talked about X")])
    history = [legacy, *[m for i in range(8) for m in _turn(i)]]  # ~16k tokens

    assert om.maybe_observe(history, DEPS) is history  # fine for a 1M-token model
    small = om.maybe_observe(history, DEPS, context_window=32_000)
    assert small[0].parts[-1].content == (
        f"{om.LOG_HEADER}\n- (summary of the earliest part of the thread) they talked about X\n- newer")


def test_images_count_as_an_image_not_as_their_bytes():
    shot = ModelRequest(parts=[UserPromptPart(["screenshot", BinaryContent(b"\0" * 200_000, media_type="image/png")])])
    assert om._estimate_tokens([shot]) < 2_000


def test_every_turn_runs_with_the_current_system_prompt():
    """pydantic-ai only adds the system prompt to an empty history: without this a thread
    kept its first turn's prompt forever, and an observed thread had none at all."""
    stale = [ModelRequest(parts=[SystemPromptPart("OLD"), UserPromptPart("hi")]), ModelResponse(parts=[TextPart("hey")])]
    observed = [om.log_message("- notes"), ModelResponse(parts=[TextPart("hey")])]

    for history in (stale, observed):
        fixed = agent_mod._with_system_prompt(history, "CURRENT")
        prompts = [p.content for m in fixed for p in getattr(m, "parts", []) if isinstance(p, SystemPromptPart)]
        assert prompts == ["CURRENT"] and isinstance(fixed[0].parts[0], SystemPromptPart)
        assert fixed[1:] == history[1:]
    assert agent_mod._with_system_prompt([], "CURRENT") == []


def test_a_pause_of_ten_minutes_or_more_is_noted_for_the_model():
    now = datetime.datetime.now(datetime.timezone.utc)

    def history(minutes_ago):
        return [ModelResponse(parts=[TextPart("hey")], timestamp=now - datetime.timedelta(minutes=minutes_ago))]

    assert agent_mod._resumed_after_note(history(3)) == ""
    assert "after a 45 minutes pause" in agent_mod._resumed_after_note(history(45))
    assert "after a 3 days pause" in agent_mod._resumed_after_note(history(3 * 24 * 60 + 5))
    assert agent_mod._resumed_after_note([]) == ""


def _with_turn_request(monkeypatch, request_parts, prompt_size=60_000):
    """A long thread whose history already ends with this turn's own big request (as with
    Jev's preload), fitted to a model the way _run_with_provider_chain does before trying it."""
    _observer(monkeypatch)
    current = ModelRequest(parts=[UserPromptPart("CURRENT REQUEST " + "z" * prompt_size), *request_parts])
    preload = ModelResponse(parts=[ToolCallPart("search_tools", {"queries": ["x"]}, tool_call_id="preload_1")])
    run_kwargs = {"user_prompt": None, "message_history": [*_history(40), current, preload]}
    agent_mod._fit_history_to_model(run_kwargs, SimpleNamespace(provider_tag_filter=None), {"context_window": 131_072})
    return current, run_kwargs["message_history"]


def test_the_turns_own_request_is_never_turned_into_notes(monkeypatch):
    """Folded into the log it'd reach the model as "background, not a new request"."""
    current, fitted = _with_turn_request(monkeypatch, [])
    assert fitted[0].parts[-1].content.startswith(om.LOG_HEADER)  # older turns were observed
    assert current in fitted and fitted[-1].parts[0].tool_name == "search_tools"


def test_a_resumed_turn_keeps_its_request_ahead_of_its_tool_rounds(monkeypatch):
    _observer(monkeypatch)
    current = ModelRequest(parts=[UserPromptPart("CURRENT REQUEST " + "z" * 60_000)])
    rounds = [ModelResponse(parts=[ToolCallPart("run_linux_command", {"command": "ls"}, tool_call_id="c1")]),
              ModelRequest(parts=[ToolReturnPart("run_linux_command", "ok", tool_call_id="c1"),
                                  UserPromptPart(agent_mod.RESTART_NOTE)])]
    run_kwargs = {"user_prompt": None, "message_history": [*_history(40), current, *rounds]}
    agent_mod._fit_history_to_model(run_kwargs, SimpleNamespace(provider_tag_filter=None), {"context_window": 131_072})
    assert run_kwargs["message_history"][-3:] == [current, *rounds]


def test_a_turn_request_passed_separately_doesnt_hold_back_older_history(monkeypatch):
    """Without a preload the request goes in as user_prompt, so the history's last user
    message is an earlier turn's and may be observed as usual."""
    _observer(monkeypatch)
    earlier = ModelRequest(parts=[UserPromptPart("EARLIER REQUEST " + "z" * 60_000)])
    history = [*_history(40), earlier, ModelResponse(parts=[TextPart("done")])]
    run_kwargs = {"user_prompt": "hi", "message_history": history}
    agent_mod._fit_history_to_model(run_kwargs, SimpleNamespace(provider_tag_filter=None), {"context_window": 131_072})
    assert earlier not in run_kwargs["message_history"]
