"""agent.debug_timing — the [!DEBUG] directive parser and the timing hooks that
record every model request and tool call in a real pydantic-ai run."""
import time

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from agent.debug_timing import TurnTimer, build_timing_hooks, extract_debug_directive


def test_directive_is_stripped_case_insensitively():
    assert extract_debug_directive("[!debug] why is this slow") == ("why is this slow", True)


def test_escaped_directive_stays_literal_and_does_not_enable_timing():
    assert extract_debug_directive(r"what does \[!DEBUG] do") == ("what does [!DEBUG] do", False)


def test_text_without_directive_is_untouched():
    assert extract_debug_directive("  hello  ") == ("  hello  ", False)


def test_hooks_time_each_model_request_and_tool_call():
    timer = TurnTimer()
    timer.current_provider = "hcai_0"

    def slow_tool() -> str:
        time.sleep(0.05)
        return "done"

    def model(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("slow_tool", {})])
        return ModelResponse(parts=[TextPart("finished")])

    agent = Agent(FunctionModel(model), tools=[slow_tool], capabilities=[build_timing_hooks(timer)])
    run_started = time.perf_counter()
    assert agent.run_sync("go").output == "finished"
    timer.record("phase", "agent run", run_started, time.perf_counter())

    models = [s for s in timer.spans if s.category == "model"]
    tools = [s for s in timer.spans if s.category == "tool"]
    assert len(models) == 2
    assert all(s.label.startswith("hcai_0 / ") for s in models)
    assert [s.label for s in tools] == ["slow_tool"]
    assert tools[0].duration >= 0.05

    report = timer.format_report()
    assert "model requests" in report and "across 2 call(s)" in report
    assert "`slow_tool`" in report
    assert "tool     slow_tool" in report  # in the timeline


def test_report_breaks_the_agent_run_into_model_tool_backoff_and_other():
    timer = TurnTimer(started_at=100.0)
    timer.record("phase", "agent run", 100.0, 110.0)
    timer.record("attempt", "hcai_0 / m (attempt 1)", 100.0, 103.0, "failed: 429")
    timer.record("backoff", "before retrying hcai_0", 103.0, 105.0)
    timer.record("attempt", "hcai_0 / m (attempt 2)", 105.0, 110.0)
    timer.record("model", "hcai_0 / m", 105.0, 108.0)
    timer.record("tool", "run_linux_command", 108.0, 109.5)

    report = timer.format_report()
    assert "retry backoff (sleeping between attempts): 2.00s" in report
    assert "other (prompt/tool setup, provider setup, status updates, hooks): 3.50s" in report
    assert "failed: 429" in report  # provider attempts section
