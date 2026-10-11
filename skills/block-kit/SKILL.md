---
name: block-kit
description: 'Build Slack Block Kit layouts (blocks, elements, mrkdwn, limits) for code channel Block Kit tabs and messages, and check them with blocks.validate. Load before writing any blocks.'
---

# Block Kit

Block Kit is Slack's JSON layout format: a message or a tab is an array of **blocks** (sections,
headers, tables, charts...), blocks hold **elements** (buttons, menus, images, rich text), and
both use **composition objects** (text objects, options, confirm dialogs). This file covers where
you can use it, the rules that trip people up, and recipes. For any block's or element's exact
fields and limits, read the reference files (`read_skill_resource`):
- `references/blocks.md`: all 21 blocks.
- `references/elements.md`: all 46 elements (interactive ones, rich text, mentions).
- `references/composition.md`: text, option, option group, confirm dialog and other objects.

## Where you can use it

**1. A Block Kit tab in a code channel** (load the `code-channels` skill for the tab tools):
`code_channel_create_view_tool(view_type="block_kit", view_key=..., name=..., blocks='[...]')`.
- Interactive elements WORK here: when someone presses a button, picks an option, ticks a box or
  picks a date, you get a message from them like `[used button "Approve" (value: yes)
  [action_id: approve] in one of your Block Kit tabs]`. Give every interactive element a clear
  `action_id` and `value` so you can tell what was used.
- To change a tab, call it again with the same `view_key` and the whole new array. See what a tab
  holds with `code_channel_read_view_tool`.
- Tabs are stricter than messages. They refuse:
  - `alert` blocks (modals only) and `file` blocks;
  - a multi-select menu (`multi_static_select`, `multi_users_select`, `multi_conversations_select`,
    `multi_channels_select`, `multi_external_select`) inside an `actions` block: put each one in
    its own `section` as the `accessory` instead;
  - a `datetimepicker` as a section's `accessory`: put it in an `actions` block instead;
  - `rich_text_input` and `file_input` in an `input` block.
- Pass a big array in a sandbox file with `content_file` (it's used as the blocks).

**2. A message**, posted with `slack_api_call` (as cooltonUser) or `slack_api_call_as_bot_tool`
(as the bot): `chat.postMessage` with `channel`, `blocks` (a JSON array) and `text`. `text` is
the fallback shown in notifications and to screen readers: always set it to a short summary.
Edit with `chat.update` (same `channel`, the message's `ts`, new `blocks` and `text`).
- A "(sent from <@user>)" context block is added to the end automatically, so use at most **49**
  blocks of your own (the limit is 50).
- Interactive elements do NOT reach you from a message: a button or menu there does nothing when
  used. Only use them in a code channel tab. Link buttons (a button with `url`) are fine in
  messages: they just open the link.
- Your normal replies are Markdown, not blocks. Use blocks in a message only when someone asks for
  a formatted message, or when the layout really needs it (a table, a chart, cards); otherwise just
  reply.

## Always validate first

Before posting or creating a tab, check the array with `block_kit_validate_tool` (through
`call_tool`; `search_tools("block kit validate")` if it isn't loaded), with `surface` "message" or
"tab", and the blocks inline or in a sandbox file (`blocks_file`). It runs Slack's own validation
plus the tab rules above, and reports each problem with a JSON pointer (`/0/rows/1/1` = block 0,
row 1, cell 1). Fix exactly what it points at and check again. Don't trust Slack's
`blocks.validate` alone for a tab: it passes things tabs refuse.

## Rules that trip people up

- **Text objects** are `{"type": "plain_text", "text": "..."}` or `{"type": "mrkdwn", "text":
  "..."}`. Headers, button labels, placeholders, input labels and option text must be
  `plain_text`. `mrkdwn` is Slack's own syntax, not standard Markdown (see below).
- **IDs:** `block_id` (max 255 chars) must be unique in the message or tab, and should change when
  you update it. `action_id` (max 255) must be unique within its block.
- **Interactive elements** go in an `actions` block (up to 25), as a `section`'s `accessory`, or in
  an `input` block. A `context` block only holds text and images (up to 10).
- **Number cells:** in `table` and `data_table`, a `raw_number` cell needs BOTH `value` (a number)
  and `text` (how to show it): `{"type": "raw_number", "value": 42, "text": "42"}`. `value` alone
  is rejected.
- **`data_table` needs a `caption`.** `table` doesn't take one.
- **Don't ping groups.** Never put `<!here>`, `<!channel>`, `<!everyone>` or `<!subteam^ID>` in
  blocks (they're defused automatically anyway).
- **Escape** `&`, `<` and `>` in text as `&amp;`, `&lt;` and `&gt;` unless you mean a Slack token
  like a link or mention.

## Key limits

| What | Limit |
|---|---|
| Blocks per message / modal or Home tab | 50 / 100 |
| `section` text / `fields` | 3000 chars / 10 fields of 2000 chars (shown in 2 columns) |
| `header` text | 150 chars, `plain_text` only |
| `markdown` block | 12,000 chars across all `markdown` blocks in the payload |
| `context` block | 10 elements |
| `actions` block | 25 elements |
| Button `text` / `value` / `url` | 75 (truncates around 30) / 2000 / 3000 chars |
| Select and multi-select menus | 100 options, each under 76 chars |
| Checkboxes, radio buttons | 10 options |
| `table` | 100 rows of up to 20 cells |
| `data_table` | 20 columns, 100 rows per page, 20,000 chars across all cells |
| `data_visualization` | 2 per message; 12 series or slices; 20 points per series; labels 20 chars |
| `card` | title/subtitle 150, body/subtext 200 chars, up to 3 buttons |
| `carousel` | 1 to 10 cards |
| `container` | up to 10 child blocks |
| `image` | `image_url` (public) or `slack_file`, `alt_text` required (2000 chars) |

## mrkdwn (text objects of type `mrkdwn`)

`*bold*`, `_italic_`, `~strike~`, `` `code` ``, ```` ```code block``` ````, `> quote`, a newline
with `\n`, lists by writing `• item` lines yourself. Links: `<https://example.com|label>`.
Mentions: `<@U123>` (a person), `<#C123>` (a channel). Dates in the reader's own timezone:
`<!date^1792000000^{date_short} at {time}|Oct 13, 2026>` (tokens include `{date_num}`,
`{date}`, `{date_short}`, `{date_long}`, `{date_pretty}`, `{time}`, `{time_secs}`; the part after
`|` is the fallback). Emoji: `:tada:`.

The **`markdown` block** is different: it takes standard Markdown (`**bold**`, `# headings`,
lists, links, tables) in one `text` string. It's the simplest way to put long formatted text in a
message or tab.

## Picking blocks

- Text: `section` (with `fields` for label/value pairs, or an `accessory`), `markdown` for long
  formatted text, `rich_text` for exact structured text (styled runs, lists, quotes, code).
- Structure: `header`, `divider`, `context` (small grey supporting text or images), `container`
  (a titled, optionally collapsible group of up to 10 blocks).
- Data: `table` (a simple grid), `data_table` (paginated, sortable, filterable), `data_visualization`
  (pie, bar, area or line chart).
- Rich items: `card` (title, body, image, up to 3 buttons), `carousel` (cards side by side),
  `image`, `video`.
- Agent progress: `plan` (a list of task cards) and `task_card` (one task with a status:
  `in_progress`, `complete` or `error`).
- Actions: `actions` (buttons, menus, pickers), `context_actions` (feedback and icon buttons).
- `input` blocks collect values in forms (they matter most in modals).

## Recipes

A status report:
```json
[
  {"type": "header", "text": {"type": "plain_text", "text": "Release 4.2 status"}},
  {"type": "section", "fields": [
    {"type": "mrkdwn", "text": "*Tests*\n312 passed, 3 failed"},
    {"type": "mrkdwn", "text": "*Build*\n:white_check_mark: green"}]},
  {"type": "context", "elements": [{"type": "mrkdwn", "text": "Updated <!date^1792000000^{date_short} at {time}|Oct 13>"}]}
]
```

A table with a number column:
```json
[{"type": "table", "rows": [
  [{"type": "raw_text", "text": "Service"}, {"type": "raw_text", "text": "p95 ms"}],
  [{"type": "raw_text", "text": "api"}, {"type": "raw_number", "value": 182, "text": "182"}]]}]
```

A pie chart:
```json
[{"type": "data_visualization", "title": "Failures by suite",
  "chart": {"type": "pie", "segments": [{"label": "auth", "value": 2}, {"label": "billing", "value": 1}]}}]
```

Buttons for a decision (in a code channel tab, where clicks reach you):
```json
[
  {"type": "section", "text": {"type": "mrkdwn", "text": "Ship the migration to production?"}},
  {"type": "actions", "elements": [
    {"type": "button", "text": {"type": "plain_text", "text": "Ship it"}, "style": "primary", "action_id": "ship", "value": "yes",
     "confirm": {"title": {"type": "plain_text", "text": "Sure?"}, "text": {"type": "plain_text", "text": "This deploys to prod."},
                 "confirm": {"type": "plain_text", "text": "Ship"}, "deny": {"type": "plain_text", "text": "Cancel"}}},
    {"type": "button", "text": {"type": "plain_text", "text": "Not yet"}, "action_id": "hold", "value": "no"}]}
]
```

A plan with task statuses:
```json
[{"type": "plan", "title": "Migration", "tasks": [
  {"task_id": "t1", "title": "Back up the database", "status": "complete"},
  {"task_id": "t2", "title": "Run the migration", "status": "in_progress"}]}]
```

For anything else (cards, carousels, containers, menus, pickers, rich text, charts other than
pie), read the block's or element's entry in the reference files, then validate.
