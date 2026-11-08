import json

import pytest

from agent.surfaces.slack import SlackSurface
from agent.tools.json_render_card import CardSpecError, to_blocks


def _spec(*children, title="Q3 revenue at a glance", **elements):
    """A flat json-render spec: a Card holding `children` keys defined in `elements`."""
    return {"root": "card", "elements": {
        "card": {"type": "Card", "props": {"title": title}, "children": list(children)}, **elements}}


DEMO = _spec(
    "kpis", "trend", "regions", "links",
    kpis={"type": "Fields", "props": {}, "children": ["revenue", "arr"]},
    revenue={"type": "Field", "props": {"label": "Revenue", "value": "$4.2M +18%"}},
    arr={"type": "Field", "props": {"label": "Win rate", "value": 31}},
    trend={"type": "Chart", "props": {"title": "Monthly revenue", "chart": {
        "type": "area", "categories": ["Jul", "Aug", "Sep"],
        "series": [{"name": "Revenue", "values": [1.2, 1.4, 1.52]}], "yLabel": "USD M"}}},
    regions={"type": "Table", "props": {"headers": ["Region", "Revenue"], "rows": [["EMEA", 1.1], ["NA"]],
                                        "caption": "Revenue by region"}},
    links={"type": "Actions", "props": {}, "children": ["report", "region", "drill"]},
    region={"type": "Select", "props": {"id": "region", "label": "Region", "initial": "emea",
                                        "options": [{"label": "EMEA", "value": "emea"}, {"label": "NA"}]}},
    drill={"type": "Button", "props": {"label": "Break it down", "prompt": "Break Q3 revenue down by the chosen region",
                                       "style": "primary"}},
    report={"type": "LinkButton", "props": {"label": "View report", "url": "https://example.com/r"}},
)


def test_a_dashboard_spec_becomes_native_slack_blocks():
    blocks, text = to_blocks(json.dumps(DEMO))

    assert [b["type"] for b in blocks] == ["header", "section", "data_visualization", "data_table", "actions"]
    assert blocks[0]["text"]["text"] == "Q3 revenue at a glance"
    assert [f["text"] for f in blocks[1]["fields"]] == ["*Revenue*\n$4.2M +18%", "*Win rate*\n31"]
    chart = blocks[2]["chart"]
    assert chart["axis_config"] == {"categories": ["Jul", "Aug", "Sep"], "y_label": "USD M"}
    assert chart["series"][0]["data"][2] == {"label": "Sep", "value": 1.52}
    # Short rows are padded so every row has one cell per header.
    assert blocks[3]["caption"] == "Revenue by region"
    assert [c["text"] for c in blocks[3]["rows"][2]] == ["NA", " "]
    link, select, button = blocks[4]["elements"]
    assert link["url"] == "https://example.com/r"
    assert select["action_id"] == "card_input_region"
    assert select["initial_option"]["value"] == "emea" and select["options"][1]["value"] == "NA"
    assert button == {"type": "button", "text": {"type": "plain_text", "text": "Break it down"},
                      "action_id": "card_prompt_2", "value": "Break Q3 revenue down by the chosen region",
                      "style": "primary"}
    assert "Revenue: $4.2M +18%" in text


def test_what_slack_cant_draw_natively_becomes_a_text_table():
    chart = lambda t: {"type": "Chart", "props": {"title": t, "chart": {  # noqa: E731
        "type": "pie", "segments": [{"label": "a", "value": 1}, {"label": "b", "value": 2}]}}}
    table = {"type": "Table", "props": {"headers": ["x"], "rows": [["1"]]}}
    long_label = {"type": "Chart", "props": {"title": "Long", "chart": {
        "type": "bar", "categories": ["a category name over twenty chars"], "series": [{"name": "s", "values": [1]}]}}}
    blocks, _ = to_blocks(_spec("c1", "c2", "c3", "t1", "t2", "c4", c1=chart("One"), c2=chart("Two"),
                                c3=chart("Three"), t1=table, t2=table, c4=long_label))

    kinds = [b["type"] for b in blocks[1:]]
    assert kinds == ["data_visualization", "data_visualization", "section", "data_table", "section", "section"]
    assert blocks[3]["text"]["text"].startswith("```\nThree")
    assert "a category name over twenty chars" in blocks[6]["text"]["text"]


@pytest.mark.parametrize("element,expected", [
    ({"type": "Button", "props": {"label": "Go"}}, "can't go inside Card"),
    ({"type": "Toggle", "props": {}}, "unknown component"),
    ({"type": "Chart", "props": {"title": "T", "chart": {"type": "bar", "categories": ["a", "b"],
                                                           "series": [{"name": "s", "values": [1]}]}}},
     "has 1 values for 2 categories"),
    ({"type": "Text", "props": {"content": {"$state": "/summary"}}}, "write literal values"),
    ({"type": "Image", "props": {"url": "http://insecure.example/x.png", "alt": "x"}}, "public https URL"),
    ({"type": "Field", "props": {"label": "x", "value": 1}}, "can't go inside Card"),
])
def test_problems_are_reported_for_the_model_to_fix(element, expected):
    with pytest.raises(CardSpecError) as e:
        to_blocks(_spec("bad", bad=element))
    assert any(expected in issue for issue in e.value.issues)


def test_a_missing_element_and_a_non_card_root_are_refused():
    with pytest.raises(CardSpecError, match="not defined"):
        to_blocks(_spec("ghost"))
    with pytest.raises(CardSpecError, match="root must be a Card"):
        to_blocks({"root": "t", "elements": {"t": {"type": "Divider", "props": {}}}})


def test_nested_specs_and_markdown_text_work_too():
    nested = {"type": "Card", "props": {"title": "Hi"}, "children": [
        {"type": "Text", "props": {"content": "Up **12%**, see [the doc](https://example.com)", "style": "muted"}}]}
    blocks, _ = to_blocks(nested)
    assert blocks[1] == {"type": "context", "elements": [
        {"type": "mrkdwn", "text": "Up *12%*, see <https://example.com|the doc>"}]}


def test_slack_posts_the_card_without_pinging_groups():
    class Client:
        def chat_postMessage(self, **kwargs):
            self.sent = kwargs
            return {"ok": True}

    client = Client()
    blocks, text = to_blocks(_spec("t", t={"type": "Text", "props": {"content": "hey <!channel>"}}))
    result = SlackSurface(client, "C1", "1.0", "1.0").post_card(blocks, text)

    assert result == "Posted the card in the thread."
    assert client.sent["thread_ts"] == "1.0"
    assert "<!channel>" not in json.dumps(client.sent)


def _actions(*children):
    elements = {f"e{i}": c for i, c in enumerate(children)}
    return _spec("row", row={"type": "Actions", "props": {}, "children": list(elements)}, **elements)


def test_inputs_need_their_own_ids_and_buttons_need_a_prompt():
    check = {"type": "Checkboxes", "props": {"id": "x", "options": [{"label": "a"}]}}
    with pytest.raises(CardSpecError, match="its own id"):
        to_blocks(_actions(check, check))
    with pytest.raises(CardSpecError, match="prompt is required"):
        to_blocks(_actions({"type": "Button", "props": {"label": "Go"}}))


def test_the_web_ui_gets_the_card_blocks():
    from agent.surfaces.web import WebSurface

    surface = WebSurface.__new__(WebSurface)
    surface.conversation_id = "c1"
    sent = []
    surface._append = sent.append
    blocks, text = to_blocks(DEMO)
    surface.post_card(blocks, text)
    assert sent == [{"type": "agent_message", "variant": "card", "blocks": blocks, "text": text}]


def test_a_card_button_is_answered_as_a_mention_from_whoever_clicked(monkeypatch):
    from listeners.actions import card_prompt_actions

    handed = []
    monkeypatch.setattr("listeners.events.app_mentioned.handle_app_mentioned",
                        lambda **kw: handed.append(kw))
    monkeypatch.setattr("agent.ban_store.is_banned", lambda user: False)

    class Client:
        def chat_postMessage(self, **kwargs):
            self.sent = kwargs
            return {"ts": "9.9"}

    client = Client()
    body = {"user": {"id": "U2"}, "channel": {"id": "C1"}, "message": {"ts": "5.0", "thread_ts": "1.0"},
            "actions": [{"type": "button", "action_id": "card_prompt_0", "text": {"text": "Break it down"},
                         "value": "Break Q3 revenue down by region"}],
            "state": {"values": {"b": {"card_input_region": {
                "type": "static_select", "selected_option": {"text": {"text": "EMEA"}, "value": "emea"}}}}}}

    card_prompt_actions.hand_click_to_coolton(body, client)

    assert client.sent["thread_ts"] == "1.0"
    assert "<@U2> clicked *Break it down*" in client.sent["text"]
    event = handed[0]["event"]
    assert (event["user"], event["ts"], event["thread_ts"]) == ("U2", "9.9", "1.0")
    assert "type" not in event
    assert event["text"] == ('[clicked the "Break it down" button on your card] Break Q3 revenue down by region\n'
                             "The card's inputs at the time:\n- region: EMEA (emea)")
