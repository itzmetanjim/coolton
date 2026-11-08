"""Rich reply cards from json-render specs (github.com/vercel-labs/json-render).

The model writes a spec with json-render's chat components (`@json-render/chat`'s
standard catalog), and this turns it into Slack Block Kit the way Vercel's Chat SDK
Slack adapter does: a header, KPI fields, a native data table, data_visualization
charts and link buttons. What Slack can't show natively (a second table, a third chart,
a chart over Slack's limits) becomes a plain-text table in a code block, as there.

This is a Python port of that catalog and conversion, not the npm package: coolton
posts with slack_sdk, and the spec format is what carries over.
"""

from __future__ import annotations

import json
import re
from typing import Any

# Slack's limits, as the Chat SDK Slack adapter enforces them.
MAX_BLOCKS = 50
SECTION_TEXT_MAX = 3000
CONTEXT_TEXT_MAX = 3000
FIELD_TEXT_MAX = 2000
FIELDS_PER_SECTION = 10
HEADER_MAX = 150
ALT_TEXT_MAX = 2000
BUTTON_TEXT_MAX = 75
BUTTONS_PER_ACTIONS = 25
TABLE_MAX_ROWS = 100
TABLE_MAX_COLS = 20
TABLE_MAX_CHARS = 10_000
CHART_MAX_PER_MESSAGE = 2
CHART_TITLE_MAX = 50
CHART_LABEL_MAX = 20
CHART_MAX_SERIES = 12
CHART_MAX_POINTS = 20
MAX_ELEMENTS = 200

COMPONENTS = ("Card", "Section", "Text", "Fields", "Field", "Table", "Chart", "Divider", "Image",
              "Actions", "LinkButton")
_CONTAINERS = {"Card", "Section", "Fields", "Actions"}
_ALLOWED_CHILDREN = {
    "Card": set(COMPONENTS) - {"Card", "Field", "LinkButton"},
    "Section": set(COMPONENTS) - {"Card", "Section", "Field", "LinkButton"},
    "Fields": {"Field"},
    "Actions": {"LinkButton"},
}


class CardSpecError(ValueError):
    """The spec can't become a card. `issues` lists every problem found."""

    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


# ---------------------------------------------------------------------------
# Reading the spec
# ---------------------------------------------------------------------------


def parse_spec(spec: Any) -> dict:
    """The spec as a nested tree: {"type", "props", "children": [...]}. Accepts json-render's
    flat form ({"root": key, "elements": {key: {type, props, children: [keys]}}}) or a
    nested one (children as objects)."""
    if isinstance(spec, str):
        try:
            spec = json.loads(spec)
        except json.JSONDecodeError as e:
            raise CardSpecError([f"spec isn't valid JSON: {e}"]) from e
    if not isinstance(spec, dict):
        raise CardSpecError(["spec must be a JSON object"])
    if "root" in spec and "elements" in spec:
        return _from_flat(spec)
    if "type" in spec:
        return _from_nested(spec, "root", [0])
    raise CardSpecError(['spec needs "root" and "elements" (json-render\'s flat form), or a root element with "type"'])


def _from_flat(spec: dict) -> dict:
    elements = spec.get("elements")
    if not isinstance(elements, dict):
        raise CardSpecError(['"elements" must be an object of key -> element'])
    count = [0]

    def build(key: Any, seen: tuple) -> dict:
        if not isinstance(key, str) or key not in elements:
            raise CardSpecError([f"element {key!r} is referenced but not defined in elements"])
        if key in seen:
            raise CardSpecError([f"element {key!r} contains itself"])
        element = elements[key]
        if not isinstance(element, dict):
            raise CardSpecError([f"element {key!r} must be an object"])
        count[0] += 1
        if count[0] > MAX_ELEMENTS:
            raise CardSpecError([f"more than {MAX_ELEMENTS} elements"])
        children = element.get("children") or []
        if not isinstance(children, list):
            raise CardSpecError([f"{key}: children must be a list of element keys"])
        return {"key": key, "type": element.get("type"), "props": element.get("props") or {},
                "children": [build(child, seen + (key,)) for child in children]}

    return build(spec.get("root"), ())


def _from_nested(node: Any, key: str, count: list) -> dict:
    if not isinstance(node, dict):
        raise CardSpecError([f"{key}: each element must be an object"])
    count[0] += 1
    if count[0] > MAX_ELEMENTS:
        raise CardSpecError([f"more than {MAX_ELEMENTS} elements"])
    children = node.get("children") or []
    if not isinstance(children, list):
        raise CardSpecError([f"{key}: children must be a list"])
    return {"key": key, "type": node.get("type"), "props": node.get("props") or {},
            "children": [_from_nested(child, f"{key}.{i}", count) for i, child in enumerate(children)]}


# ---------------------------------------------------------------------------
# Checking it against the catalog
# ---------------------------------------------------------------------------


def _is_text(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def _display(value: Any) -> str:
    # Models often write numbers for display values; show them as text (as json-render does).
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _has_expression(value: Any) -> bool:
    if isinstance(value, dict):
        return any(str(k).startswith("$") for k in value) or any(_has_expression(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_expression(v) for v in value)
    return False


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check(node: dict, issues: list[str], parent: str | None = None) -> None:
    key, kind, props = node["key"], node["type"], node["props"]
    where = f"{key} ({kind})"
    if kind not in COMPONENTS:
        hint = " Buttons that run actions do nothing in a message, so use LinkButton." if kind == "Button" else ""
        issues.append(f"{key}: unknown component {kind!r}; use one of {', '.join(COMPONENTS)}.{hint}")
        return
    if not isinstance(props, dict):
        issues.append(f"{where}: props must be an object")
        return
    if _has_expression(props):
        issues.append(f"{where}: write literal values; $state, $item and other expressions aren't supported")
    if parent is None and kind != "Card":
        issues.append(f"{where}: the root must be a Card")
    if parent is not None and kind not in _ALLOWED_CHILDREN.get(parent, set()):
        issues.append(f"{where}: can't go inside {parent}")
    if node["children"] and kind not in _CONTAINERS:
        issues.append(f"{where}: takes no children")

    if kind == "Text":
        if not _is_text(props.get("content")):
            issues.append(f"{where}: content is required")
        if props.get("style") not in (None, "plain", "bold", "muted"):
            issues.append(f"{where}: style must be plain, bold or muted")
    elif kind == "Field":
        if not _is_text(props.get("label")):
            issues.append(f"{where}: label is required")
        if props.get("value") is None or _display(props.get("value")) == "":
            issues.append(f"{where}: value is required")
    elif kind == "Table":
        headers, rows = props.get("headers"), props.get("rows")
        if not isinstance(headers, list) or not headers or not all(isinstance(h, str) for h in headers):
            issues.append(f"{where}: headers must be a non-empty list of strings")
        if not isinstance(rows, list) or not all(isinstance(r, list) for r in rows):
            issues.append(f"{where}: rows must be a list of lists")
    elif kind == "Chart":
        _check_chart(where, props, issues)
    elif kind == "Image":
        if not str(props.get("url") or "").startswith("https://"):
            issues.append(f"{where}: url must be a public https URL")
        if not _is_text(props.get("alt")):
            issues.append(f"{where}: alt is required")
    elif kind == "LinkButton":
        if not _is_text(props.get("label")):
            issues.append(f"{where}: label is required")
        if not re.match(r"^https?://", str(props.get("url") or "")):
            issues.append(f"{where}: url must be an http(s) URL")
    elif kind == "Card" and props.get("imageUrl") and not str(props["imageUrl"]).startswith("https://"):
        issues.append(f"{where}: imageUrl must be a public https URL")

    for child in node["children"]:
        _check(child, issues, kind)


def _check_chart(where: str, props: dict, issues: list[str]) -> None:
    if not _is_text(props.get("title")):
        issues.append(f"{where}: title is required")
    chart = props.get("chart")
    if not isinstance(chart, dict):
        issues.append(f"{where}: chart is required")
        return
    kind = chart.get("type")
    if kind == "pie":
        segments = chart.get("segments")
        if not isinstance(segments, list) or not segments:
            issues.append(f"{where}: a pie chart needs segments")
            return
        for segment in segments:
            if not isinstance(segment, dict) or not _is_text(segment.get("label")) \
                    or not _number(segment.get("value")) or segment["value"] <= 0:
                issues.append(f"{where}: each segment needs a label and a positive value")
                return
    elif kind in ("bar", "line", "area"):
        categories, series = chart.get("categories"), chart.get("series")
        if not isinstance(categories, list) or not categories or not all(_is_text(c) for c in categories):
            issues.append(f"{where}: categories must be a non-empty list of strings")
            return
        if not isinstance(series, list) or not series:
            issues.append(f"{where}: series must be a non-empty list")
            return
        for s in series:
            values = s.get("values") if isinstance(s, dict) else None
            if not isinstance(s, dict) or not _is_text(s.get("name")) or not isinstance(values, list) \
                    or not all(_number(v) for v in values):
                issues.append(f"{where}: each series needs a name and a list of numbers in values")
                return
            if len(values) != len(categories):
                issues.append(f"{where}: series {s['name']!r} has {len(values)} values for "
                              f"{len(categories)} categories")
    else:
        issues.append(f"{where}: chart type must be pie, bar, line or area")


# ---------------------------------------------------------------------------
# Slack blocks
# ---------------------------------------------------------------------------


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _mrkdwn(text: str) -> str:
    """Standard Markdown's bold and links in Slack's mrkdwn."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"<\2|\1>", text)
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def _ascii_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(r[i])) if i < len(r) else 0 for r in [headers, *rows]) for i in range(len(headers))]
    line = lambda cells: " | ".join(str(c).ljust(w) for c, w in zip(cells, widths)).rstrip()  # noqa: E731
    return "\n".join([line(headers), "-+-".join("-" * w for w in widths), *(line(r) for r in rows)])


def _code_block(content: str) -> dict:
    budget = SECTION_TEXT_MAX - 8
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"```\n{_truncate(content, budget)}\n```"}}


def _table_rows(props: dict) -> tuple[list[str], list[list[str]]]:
    headers = [_display(h) for h in props["headers"]]
    rows = [[_display(c) for c in row][: len(headers)] for row in props["rows"]]
    return headers, [row + [""] * (len(headers) - len(row)) for row in rows]


def _table_blocks(props: dict, state: dict) -> list[dict]:
    headers, rows = _table_rows(props)
    chars = sum(len(c) for c in [*headers, *[c for r in rows for c in r]])
    caption = props.get("caption")
    if state["table"] or len(rows) > TABLE_MAX_ROWS or len(headers) > TABLE_MAX_COLS or chars > TABLE_MAX_CHARS:
        text = _ascii_table(headers, rows)
        return [_code_block(f"{caption}\n\n{text}" if caption else text)]
    state["table"] = True
    cell = lambda text: {"type": "raw_text", "text": text or " "}  # noqa: E731
    if not rows:
        return [{"type": "table", "rows": [[cell(h) for h in headers]]}]
    return [{"type": "data_table", "caption": caption or "Table",
             "rows": [[cell(h) for h in headers], *[[cell(c) for c in r] for r in rows]]}]


def _chart_fallback(title: str, chart: dict) -> str:
    if chart["type"] == "pie":
        return _ascii_table([title, "Value"], [[s["label"], _display(s["value"])] for s in chart["segments"]])
    headers = [title, *[s["name"] for s in chart["series"]]]
    rows = [[c, *[_display(s["values"][i]) for s in chart["series"]]] for i, c in enumerate(chart["categories"])]
    return _ascii_table(headers, rows)


def _chart_block(props: dict, state: dict) -> dict:
    title, chart = props["title"], props["chart"]
    native = _native_chart(title, chart) if state["charts"] < CHART_MAX_PER_MESSAGE else None
    if native is None:
        return _code_block(_chart_fallback(title, chart))
    state["charts"] += 1
    return native


def _native_chart(title: str, chart: dict) -> dict | None:
    """A data_visualization block, or None if Slack would reject this chart."""
    fits = lambda label: 1 <= len(label) <= CHART_LABEL_MAX  # noqa: E731
    if len(title) > CHART_TITLE_MAX:
        return None
    if chart["type"] == "pie":
        segments = chart["segments"]
        if len(segments) > CHART_MAX_SERIES or not all(fits(s["label"]) for s in segments):
            return None
        return {"type": "data_visualization", "title": title,
                "chart": {"type": "pie", "segments": [{"label": s["label"], "value": s["value"]} for s in segments]}}
    categories, series = chart["categories"], chart["series"]
    if (len(categories) > CHART_MAX_POINTS or len(set(categories)) != len(categories)
            or not all(fits(c) for c in categories) or len(series) > CHART_MAX_SERIES
            or len({s["name"] for s in series}) != len(series) or not all(fits(s["name"]) for s in series)):
        return None
    axis: dict[str, Any] = {"categories": categories}
    for name, slack_name in (("xLabel", "x_label"), ("yLabel", "y_label")):
        if chart.get(name):
            if len(chart[name]) > CHART_TITLE_MAX:
                return None
            axis[slack_name] = chart[name]
    return {"type": "data_visualization", "title": title, "chart": {
        "type": chart["type"], "axis_config": axis,
        "series": [{"name": s["name"], "data": [{"label": c, "value": v} for c, v in zip(categories, s["values"])]}
                   for s in series],
    }}


def _blocks_for(node: dict, state: dict) -> list[dict]:
    kind, props = node["type"], node["props"]
    if kind == "Card":
        blocks: list[dict] = []
        if props.get("title"):
            blocks.append({"type": "header", "text": {"type": "plain_text", "text": _truncate(props["title"], HEADER_MAX)}})
        if props.get("subtitle"):
            blocks.append({"type": "context", "elements": [
                {"type": "mrkdwn", "text": _truncate(_mrkdwn(props["subtitle"]), CONTEXT_TEXT_MAX)}]})
        if props.get("imageUrl"):
            blocks.append({"type": "image", "image_url": props["imageUrl"],
                           "alt_text": _truncate(props.get("title") or "Card image", ALT_TEXT_MAX)})
        for child in node["children"]:
            blocks += _blocks_for(child, state)
        return blocks
    if kind == "Section":
        return [b for child in node["children"] for b in _blocks_for(child, state)]
    if kind == "Text":
        text = _mrkdwn(props["content"])
        if props.get("style") == "muted":
            return [{"type": "context", "elements": [{"type": "mrkdwn", "text": _truncate(text, CONTEXT_TEXT_MAX)}]}]
        if props.get("style") == "bold":
            text = f"*{_truncate(text, SECTION_TEXT_MAX - 2)}*"
        return [{"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text, SECTION_TEXT_MAX)}}]
    if kind == "Fields":
        fields = [{"type": "mrkdwn", "text": _truncate(f"*{_mrkdwn(f['props']['label'])}*\n{_mrkdwn(_display(f['props']['value']))}",
                                                       FIELD_TEXT_MAX)} for f in node["children"]]
        return [{"type": "section", "fields": fields[i:i + FIELDS_PER_SECTION]}
                for i in range(0, len(fields), FIELDS_PER_SECTION)]
    if kind == "Table":
        return _table_blocks(props, state)
    if kind == "Chart":
        return [_chart_block(props, state)]
    if kind == "Divider":
        return [{"type": "divider"}]
    if kind == "Image":
        return [{"type": "image", "image_url": props["url"], "alt_text": _truncate(props["alt"], ALT_TEXT_MAX)}]
    if kind == "Actions":
        buttons = [{"type": "button", "text": {"type": "plain_text", "text": _truncate(b["props"]["label"], BUTTON_TEXT_MAX)},
                    "url": b["props"]["url"], "action_id": f"link_{i}"} for i, b in enumerate(node["children"])]
        return [{"type": "actions", "elements": buttons[i:i + BUTTONS_PER_ACTIONS]}
                for i in range(0, len(buttons), BUTTONS_PER_ACTIONS)]
    return []


def _fallback_text(node: dict) -> str:
    """A short plain summary for notifications and screen readers."""
    props = node["props"]
    parts = [props.get("title"), props.get("content")]
    if node["type"] == "Field":
        parts = [f"{props.get('label')}: {_display(props.get('value'))}"]
    if node["type"] == "Chart":
        parts = [props.get("title")]
    text = " · ".join(str(p) for p in parts if p)
    rest = [t for t in (_fallback_text(c) for c in node["children"]) if t]
    return _truncate(" · ".join([text, *rest] if text else rest), 300)


def to_blocks(spec: Any) -> tuple[list[dict], str]:
    """(Slack blocks, fallback text) for a json-render spec. Raises CardSpecError."""
    tree = parse_spec(spec)
    issues: list[str] = []
    _check(tree, issues)
    if issues:
        raise CardSpecError(issues)
    blocks = _blocks_for(tree, {"table": False, "charts": 0})
    if not blocks:
        raise CardSpecError(["the card is empty"])
    if len(blocks) > MAX_BLOCKS:
        raise CardSpecError([f"the card makes {len(blocks)} Slack blocks; a message holds at most {MAX_BLOCKS}"])
    return blocks, _fallback_text(tree) or "card"


def to_markdown(spec: Any) -> str:
    """The card as Markdown, for surfaces without Block Kit (the web UI)."""
    tree = parse_spec(spec)
    issues: list[str] = []
    _check(tree, issues)
    if issues:
        raise CardSpecError(issues)
    return "\n\n".join(p for p in _markdown_for(tree, {"table": False, "charts": CHART_MAX_PER_MESSAGE}) if p)


def _markdown_for(node: dict, state: dict) -> list[str]:
    kind, props = node["type"], node["props"]
    children = [p for c in node["children"] for p in _markdown_for(c, state)]
    if kind == "Card":
        head = [f"## {props['title']}"] if props.get("title") else []
        head += [f"_{props['subtitle']}_"] if props.get("subtitle") else []
        return head + children
    if kind in ("Section", "Fields"):
        return children if kind == "Section" else ["\n".join(children)]
    if kind == "Field":
        return [f"- **{props['label']}:** {_display(props['value'])}"]
    if kind == "Text":
        text = props["content"]
        return [f"**{text}**" if props.get("style") == "bold" else f"_{text}_" if props.get("style") == "muted" else text]
    if kind == "Table":
        headers, rows = _table_rows(props)
        lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers),
                 *("| " + " | ".join(r) + " |" for r in rows)]
        return ([f"**{props['caption']}**"] if props.get("caption") else []) + ["\n".join(lines)]
    if kind == "Chart":
        return [f"**{props['title']}**\n```\n{_chart_fallback(props['title'], props['chart'])}\n```"]
    if kind == "Divider":
        return ["---"]
    if kind == "Image":
        return [f"![{props['alt']}]({props['url']})"]
    if kind == "Actions":
        return [" · ".join(f"[{b['props']['label']}]({b['props']['url']})" for b in node["children"])]
    return []
