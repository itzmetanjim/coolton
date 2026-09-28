"""cap_images: only the newest MAX_CONTEXT_IMAGES images are sent per
request; older ones become a note, and stored history is never mutated."""
from pydantic_ai.messages import BinaryContent, ModelRequest, ToolReturnPart, UserPromptPart

from agent import image_cap


def _img(tag: str, size: int = 10) -> BinaryContent:
    return BinaryContent(data=tag.encode().ljust(size, b"\0"), media_type="image/png")


def _screenshot_turn(tag: str) -> ModelRequest:
    """The shape a screenshot-returning tool produces: a tool return plus a
    user part carrying [text, image]."""
    return ModelRequest(parts=[
        ToolReturnPart(tool_name="computer_use", content="took a screenshot", tool_call_id=tag),
        UserPromptPart(content=[f"screenshot {tag}", _img(tag)]),
    ])


def _images(messages):
    return [
        item for m in messages for p in m.parts if isinstance(p.content, list)
        for item in p.content if isinstance(item, BinaryContent)
    ]


def test_keeps_the_newest_eight_and_notes_the_rest():
    messages = [_screenshot_turn(f"s{i}") for i in range(12)]
    capped = image_cap.cap_images(messages)

    kept = _images(capped)
    assert len(kept) == 8
    assert [k.data.rstrip(b"\0").decode() for k in kept] == [f"s{i}" for i in range(4, 12)]
    assert capped[0].parts[1].content == ["screenshot s0", image_cap.OMITTED_NOTE]
    assert capped[0].parts[0].content == "took a screenshot"  # text untouched


def test_does_not_mutate_the_stored_history():
    messages = [_screenshot_turn(f"s{i}") for i in range(12)]
    image_cap.cap_images(messages)
    assert len(_images(messages)) == 12


def test_byte_budget_drops_older_images_even_under_the_count():
    big = image_cap.MAX_CONTEXT_IMAGE_BYTES // 2 + 1
    messages = [ModelRequest(parts=[UserPromptPart(content=["x", _img(f"b{i}", big)])]) for i in range(3)]
    kept = _images(image_cap.cap_images(messages))
    assert [k.data[:2] for k in kept] == [b"b2"]


def test_under_the_limit_returns_the_same_list():
    messages = [_screenshot_turn(f"s{i}") for i in range(3)]
    assert image_cap.cap_images(messages) is messages


def test_a_real_run_sends_at_most_eight_images():
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import ProcessHistory
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    sent = {}

    def model(messages, info):
        sent["images"] = len(_images(messages))
        return ModelResponse(parts=[TextPart("ok")])

    history = []
    for i in range(12):
        history += [ModelRequest(parts=[UserPromptPart(content=[f"look {i}", _img(f"s{i}")])]), ModelResponse(parts=[TextPart("seen")])]
    agent = Agent(FunctionModel(model), capabilities=[ProcessHistory(image_cap.cap_images)])
    agent.run_sync("what changed?", message_history=history)
    assert sent["images"] == 8
