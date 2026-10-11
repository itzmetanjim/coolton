# Block Kit blocks

Condensed from Slack's reference (docs.slack.dev/reference/block-kit, fetched 2026-10-10): every field with its type, whether it's required, and its limits, plus one example each. Check a layout with `blocks.validate` before posting it (see SKILL.md).

## Actions block
Holds multiple interactive elements.
Surfaces: Modals, Messages, Home tabs
Compatible elements: Button, Checkboxes, Date picker, Datetime picker, Multi-select menu, Overflow menu, Radio button, Rich text input, Select menu, Time picker, Workflow buttons
- `type` (String, required): The type of block. For an actions block, `type` is always `actions`.
- `elements` (Object[], required): An array of interactive element objects - buttons, select menus, overflow menus, or date pickers. There is a maximum of 25 elements in each action block.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated. You can use this `block_id` when you receive an interaction payload to identify the source of the action. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Example: `{"blocks":[{"type":"actions","block_id":"actions1","elements":[{"type":"static_select","placeholder":{"type":"plain_text","text":"Which witch is the witchiest witch?"},"action_id":"select_2","options":[{"text":{"type":"plain_text","text":"Matilda"},"value":"matilda"},{"text":{"type":"plain_text","text":"Glinda"},"value":"glinda"},{"text":{"type":"plain_text","text":"Granny Weatherwax"},"value":"grannyWeatherwax"},{"text":{"type":"plain_text","text":"Hermione"},"value":"hermione"}]},{"type":"button","text":{"type":"plain_text","text":"Cancel"},"value":"cancel","action_id":"button_1"}]}]}`

## Alert block
Displays alerts, warnings, and informational messages.
Surfaces: Modals
- `type` (String, required): The type of block. For an alert block, `type` is always `alert`.
- `text` (String, required): The alert message, using `plain_text` or `mrkdwn` formatting. Maximum 200 characters.
- `level` (Array, optional): One of `default`, `info`, `warning`, `error`, or `success`. Will be `default` if omitted.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated.
Example: `{"blocks":[{"type":"alert","text":{"type":"mrkdwn","text":"The work is mysterious and important.","verbatim":false},"level":"info"}]}`

## Card block
Displays content in a card.
Surfaces: Modals, Messages, Home tabs
Compatible elements: Button
- `type` (String, required): The type of block. For a card block, `type` is always `card`.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated.
- `hero_image` (Image element, optional): Link to the top image used on the card. Max length 3000 characters. The `alt_text` property has a max length of 2000 characters.
- `icon` (Image element, optional): Link to the small image used next to the card's title and subtitle. Max length 3000 characters. The `alt_text` property has a max length of 2000 characters.
- `title` (String, optional): Title of the card, using `plain_text` or `mrkdwn` formatting. 150 characters max.
- `subtitle` (String, optional): Subtitle of the card, using `plain_text` or `mrkdwn` formatting. 150 characters max.
- `body` (String, optional): Content of the card, using `plain_text` or `mrkdwn` formatting. 200 characters max.
- `actions` (Actions block, optional): Action buttons shown at the bottom of the card, maximum of 3 buttons. Buttons with `danger` style will be left-aligned, while buttons with `primary` or no style will be right-aligned (buttons with `primary` style will be furthest to the right).
- `slack_icon` (Slack icon composition object, optional): A Slack icon to be rendered next to the card's title and subtitle. Mutually exclusive with `icon`, that is, only one of `icon` & `slack_icon` can be present as they render in the same location on the card.
- `subtext` (String, optional): Subtext to be rendered below body of the card, using `plain_text` or `mrkdwn` formatting. Maximum 200 characters.
Example: `{"blocks":[{"type":"card","icon":{"type":"image","image_url":"https://picsum.photos/36/36","alt_text":"Icon"},"title":{"type":"mrkdwn","text":"Lumon Industries","verbatim":false},"subtitle":{"type":"mrkdwn","text":"Committed to work-life balance","verbatim":false},"hero_image":{"type":"image","image_url":"https://picsum.photos/400/300","alt_text":"Sample hero image"},"body":{"type":"mrkdwn","text":"Please enjoy each card equally.","verbatim":false},"actions":[{"type":"button","text":{"type":"plain_text","text":"Action Button","emoji":false},"action_id":"button_action"}]}]}`

## Carousel block
Displays related card blocks in a horizontally-scrolling container.
Surfaces: Messages, Home tabs
- `type` (String, required): The type of block. For a carousel block, `type` is always `carousel`.
- `elements` (Array, required): A list of cards. The carousel must contain at least one card, with a maximum of 10 cards.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated.

## Container block
A general-purpose wrapper for grouping child blocks together, with a configurable size.
Surfaces: Messages, Home tabs
- `type` (String, required): The type of block. For a container block, `type` is always `container`.
- `title` (Plain text object, optional (one of `title` or `rich_text_title` is required)): Title of the container, using `plain_text` formatting. Maximum of 150 characters.
- `rich_text_title` (Rich text block, optional (one of `title` or `rich_text_title` is required)): The title for the container as a `rich_text` block. If both `title` and `rich_text_title` are provided, `rich_text_title` takes precedence.
- `subtitle` (Text object, optional): Subtitle of the container, using `plain_text` or `mrkdwn` formatting. Maximum of 150 characters.
- `child_blocks` (Array, required): List of included blocks. Maximum of 10 blocks.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated.
- `width` (String, optional): Sets the width of the container block. The `narrow`, `standard`, and `wide` options use a platform-determined constrained width. The `full` option expands to fill the available space. Default is `standard`.
- `icon` (Image element, optional): Link to the small image used next to the card's title and subtitle. Maximum length of 3000 characters. The `alt_text` property has a maximum length of 2000 characters.
- `is_collapsible` (Boolean, optional): When `true`, the block can be collapsed to show only the title. Defaults to `false`.
- `default_collapsed` (Boolean, optional): When `true` and `is_collapsible` are both `true`, the block initially renders in a collapsed state. Defaults to `false`.
- `has_header_divider` (Boolean, optional): When true, a visible border is rendered below the header to visually separate it from the content. Only applies when the block is not collapsible. Defaults to `false`.
Example: `{"blocks":[{"type":"container","block_id":"bkb_container_bulk_update","title":{"type":"plain_text","text":"Bulk update: 2 records selected"},"subtitle":{"type":"plain_text","text":"Review changes before confirming"},"is_collapsible":true,"child_blocks":[{"type":"section","block_id":"record-row-1","text":{"type":"mrkdwn","text":"*DCW-1024*\nStatus: Open \u2192 Closed\nAssignee: @princessdonut \u2192 @carl"}},{"type":"divider","block_id":"bulk-div-1"},{"type":"section","block_id":"record-row-2","text":{"type":"mrkdwn","text":"*DCW-1025*\nStatus: In Progress \u2192 Closed\nAssignee: @mordecai \u2192 @carl"}},{"type":"divider","block_id":"bulk-div-2"},{"type":"context","block_id":"bulk-status-bar","elements":[{"type":"mrkdwn","text":":white_check_mark: 2 records will be updated \u2022 Status \u2192 Closed \u2022 Assignee \u2192 @carl"}]},{"type":"actions","block_id":"bulk-actions","elements":[{"type":"button","text":{"type":"plain_text","text":"Confirm All","emoji":true},"style":"primary","action_id":"bulk_confirm"},{"type":"button","text":{"type":"plain_text","text":"Cancel","emoji":true},"action_id":"bulk_cancel"}]}]}]}`

## Context actions block
Surfaces: Messages
Compatible elements: Feedback buttons, Icon button
- `type` (String, required): The type of block. For a context actions block, `type` is always `context_actions`.
- `elements` (Object[], required): An array of feedback buttons elements and icon button elements. Maximum number of items is 5.
- `block_id` (String, optional): A unique identifier for a block. You can use this `block_id` when you receive an interaction payload to identify the source of the action. If not specified, `block_id` will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Example: `{"blocks":[{"type":"context_actions","elements":[{"type":"feedback_buttons","action_id":"feedback_buttons_1","positive_button":{"text":{"type":"plain_text","text":"\ud83d\udc4d"},"value":"positive_feedback"},"negative_button":{"text":{"type":"plain_text","text":"\ud83d\udc4e"},"value":"negative_feedback"}}]}]}`

## Context block
Provides contextual info, which can include both images and text.
Surfaces: Modals, Messages, Home tabs
Compatible elements: Image
- `type` (String, required): The type of block. For a context block, `type` is always `context`.
- `elements` (Object[], required): An array of image elements and text objects. Maximum number of items is 10.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Example: `{"blocks":[{"type":"context","elements":[{"type":"image","image_url":"https://image.freepik.com/free-photo/red-drawing-pin_1156-445.jpg","alt_text":"images"},{"type":"mrkdwn","text":"Location: **Dogpatch**"}]}]}`

## Data table block
Displays rich tables that support pagination, sorting, filtering, and interactivity.
Surfaces: Messages, Home tabs
Compatible elements: Button
- `type` (String, required): The type of block. For a data table block, `type` is always `data_table`.
- `rows` (Array, required): An array consisting of table rows.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated.
- `page_size` (Integer, optional): Number of rows per page. Min `1`, Max `100`. Defaults to `5` if omitted.
- `caption` (String, required): A caption for the table; used as the value for the HTML caption element.
- `row_header_column_index` (Integer, optional): The 0-based index of the column that uniquely identifies each row (the row header). This column is treated as the row's primary identifier for screen readers. Defaults to 0 if omitted.
Notes: You can use `rich_text`, `raw_text` (simple text), `raw_number` (numeric values), or `action_cell` (a button) for cell content. The first row of the table is a header, and `rich_text` cannot be used for header cells. You can have a minimum of 2 rows (1 regular row plus the header) and a maximum of 201 rows (200 regular rows plus the header). All rows must have the same number of values. A single table's character count across all cells cannot exceed 20,000 characters. For `action_cell` cells, the button's `text` counts toward this limit. Additionally, the aggregate character count across all table cells for a single message cannot exceed 20,000 characters. Large tables should be broken up into separate messages. Sorting rows by column is done alphabetically by default. If a column contains cells all of type `raw_number`, a numeric sort will be performed instead. You can have a minimum of 1 column and a maximum of 20 columns. ### Schema for raw_text ### Schema for raw_number ### Schema for action_cell The `element` field is required and must be a button element. When a user clicks the button, your app receives a `block_actions` payload, the same as for buttons in other blocks. Set the button's `value` to identify the row so your app can tell which row was clicked. The optional `fallback` field is a `raw_text` or `raw_number` cell that displays in place of the button on clients that don't support action cells. Action cells can only be used in the data table block, not the table
Example: `{"blocks":[{"type":"data_table","caption":"A Fabulous Table","rows":[[{"type":"raw_text","text":"Name"},{"type":"raw_text","text":"Department"},{"type":"raw_text","text":"Badge"}],[{"type":"raw_text","text":"Data Refinement Department"},{"type":"raw_text","text":"MDR"},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Blue","style":{"bold":true}}]}]}],[{"type":"raw_text","text":"Art Sourcing Department"},{"type":"raw_text","text":"O&D"},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Green"},{"type":"text","text":"review","style":{"italic":true}}]}]}],[{"type":"raw_text","text":"Wellness Department"},{"type":"raw_text","text":"Wellness Center"},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Limited","style":{"bold":true}}]}]}]]}]}`

## Data visualization block
Displays data visually in pie, bar, area, or line chart formats.
Surfaces: Messages, Home tabs
- `type` (String, required): The type of block. For a data visualization block, `type` is always `data_visualization`.
- `title` (String, required): A short label displayed above the chart. Maximum 50 characters.
- `chart` (Object, required): The chart-specific payload. Must be one of the following: `pie`, `bar`, `area`, or `line`.
- `block_id` (String, optional): A unique identifier for a block. If not specified, a `block_id` will be generated.
Notes: ### Validation rules (enforced at runtime) Rule Description Label matching Every `data_point.label` in every series must match a value in `axis_config.categories.` Series may not omit data points. Unique series names Each series within a chart must have a distinct name. Category ordering The order of `axis_config.categories` defines the x-axis display order.
Example: `{"type":"data_visualization","title":"My Favorite Candy Bars","chart":{"type":"pie","segments":[{"label":"Kit Kat","value":45},{"label":"Twix","value":28},{"label":"Crunch","value":18},{"label":"Milky Way","value":9}]}}`

## Divider block
Visually separates pieces of info inside of a message.
Surfaces: Modals, Messages, Home tabs
Compatible elements: 
- `type` (String, required): The type of block. For a divider block, `type` is always `divider`.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Notes: A content divider, like an `<hr>`, to split up different blocks inside of a message. The divider block is nice and neat, requiring only a `type`.
Example: `{"blocks":[{"type":"divider"}]}`

## File block
Displays info about remote files.
Surfaces: Messages
Compatible elements:  
- `type` (String, required): The type of block. For a file block, `type` is always `file`.
- `external_id` (String, required): The external unique ID for this file.
- `source` (String, required): At the moment, `source` will always be `remote` for a remote file.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Notes: You can't add this block to app surfaces directly, but it will show up when retrieving messages that contain remote files. If you want to add remote files to messages, follow our guide.
Example: `{"blocks":[{"type":"file","external_id":"ABCD1","source":"remote"}]}`

## Header block
Displays a larger-sized text.
Surfaces: Modals, Messages, Home tabs
Compatible elements:  
- `type` (String, required): The type of block. For this block, type will always be `header`.
- `text` (Object, required): The text for the block, in the form of a `plain_text` text object. Maximum length for the `text` in this field is 150 characters.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
- `level` (Integer, optional): Set the level of the heading. Values `1`\-`4` correspond to H1-H4 heading levels, respectively.
Notes: A `header` is a plain-text block that displays in a larger, bold font. Use it to delineate between different groups of content in your app's surfaces.
Example: `{"blocks":[{"type":"header","text":{"type":"plain_text","text":"A Heartfelt Header"}}]}`

## Image block
Displays an image.
Surfaces: Modals, Messages, Home tabs
Compatible elements:  
- `type` (String, required): The type of block. For an image block, `type` is always `image`.
- `alt_text` (String, required): A plain-text summary of the image. This should not contain any markup. Maximum length for this field is 2000 characters.
- `image_url` (String, optional): The URL for a publicly hosted image. You must provide either an `image_url` or `slack_file`. Maximum length for this field is 3000 characters.
- `slack_file` (Object, optional): A Slack image file object that defines the source of the image.
- `title` (Object, optional): An optional title for the image in the form of a text object that can only be of `type: plain_text`. Maximum length for the `text` in this field is 2000 characters.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Notes: An image block, designed to make those cat photos really pop. Supported file types include `png`, `jpg`, `jpeg`, and `gif`.
Example: `{"blocks":[{"type":"image","title":{"type":"plain_text","text":"Please enjoy this photo of a kitten"},"block_id":"image4","image_url":"http://placekitten.com/500/500","alt_text":"An incredibly cute kitten."}]}`

## Input block
Collects information from users via elements.
Surfaces: Modals, Messages, Home tabs
Compatible elements: Checkboxes, Date picker, Datetime picker, Email input, File input, Multi-select menu, Number input, Plain-text input, Radio button, Rich text input, Select menu, Time picker, URL input
- `type` (String, required): The type of block. For an input block, `type` is always `input`.
- `label` (Object, required): A label that appears above an input element in the form of a text object that must have `type` of `plain_text`. Maximum length for the `text` in this field is 2000 characters.
- `element` (Object, required): A block element. See above for full list.
- `dispatch_action` (Boolean, optional): A boolean that indicates whether or not the use of elements in this block should dispatch a `block_actions` payload. Defaults to `false`. This field is incompatible with the `file_input` block element. If `dispatch_action` is set to `true` and a `file_input` block element is provided, an unsupported type error will be raised.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message or view and each iteration of a message or view. If a message or view is updated, use a new `block_id`.
- `hint` (Object, optional): An optional hint that appears below an input element in a lighter grey. It must be a text object with a `type` of `plain_text`. Maximum length for the `text` in this field is 2000 characters.
- `optional` (Boolean, optional): A boolean that indicates whether the input element may be empty when a user submits the modal. Defaults to `false`.
Notes: Read our guides to collecting input in modals or in Home tabs to learn how input blocks pass information to your app.
Example: `{"blocks":[{"type":"input","element":{"type":"plain_text_input"},"label":{"type":"plain_text","text":"Label","emoji":true}}]}`

## Markdown block
Displays formatted markdown.
Surfaces: Messages
Compatible elements:  
- `type` (String, required): The type of block. For a markdown block, `type` is always `markdown`.
- `text` (String, required): The standard markdown-formatted text. The cumulative limit for all `markdown` blocks in a single payload is 12,000 characters.
- `block_id` (String, optional): The `block_id` is ignored in markdown blocks and will not be retained.
- `**this is bold**` or (`__this is bold__`, italic): **this is bold**
- `*this text is italicized*` or (`_this text is italicized_`, bold and nested italic): _this text is italicized_
- `**this text is _extremely_ important**` (**this text is _extremely_ important**, `***all of this is important***`): All bold and italic
- `[my text](https://www.google.com)` (my text, \- first item in list): Lists (unordered)
- `~~this is strikethrough text~~` (this is strikethrough text, `# header 1`): Headers (level 1)
- `` `this is my code` `` (`this is my code`, `> this is a block quote`): Block quote
- `---` (Renders as a horizontal divider line., | col 1 | col 2 |): Tables
- `![Logo](https://example.com/logo.png)` (This is translated as hyperlink text, i.e. Logo,    \\ backslash): Escaping special characters:
Notes: This block can be used with apps that use platform AI features when you expect a markdown response from an LLM that can get lost in translation rendering in Slack. Providing it in a markdown block leaves the translating to Slack to ensure your message appears as intended. Note that passing a single block may result in multiple blocks after translation.
Example: `{"blocks":[{"type":"markdown","text":"**Lots of information here!!**"}]}`

## Plan block
Displays a collection of related tasks.
Surfaces: Messages
Compatible elements:  
- `type` (String, required): The type of block. For this block, type will always be `plan`.
- `title` (String, required): Title of the plan in plain text.
- `tasks` (Array, required): A sequence of task card blocks, maximum of 50 tasks. Each one contains task-like objects without a type, and each `task_id` in a plan must be unique.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
Notes: A `plan` block groups related task cards under a single title, so users can follow a multi-step process as one list. Each task shows its own title, status, and optional details and output. Use a plan when your agent is working through several steps that belong together. For a single action, use a task card block on its own. When streaming a response, set `task_display_mode` to `plan` in `chat.startStream` to show task updates as a plan, and send `plan_update` chunks to change the plan's title as work progresses.
Example: `{"blocks":[{"type":"plan","title":"Thinking completed","tasks":[{"task_id":"call_001","title":"Fetched user profile information","status":"in_progress","details":{"type":"rich_text","block_id":"viMWO","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Searched database..."}]}]},"output":{"type":"rich_text","block_id":"viMWO","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Profile data loaded"}]}]}},{"task_id":"call_002","title":"Checked user permissions","status":"pending"},{"task_id":"call_003","title":"Generated comprehensive user report","status":"complete","output":{"type":"rich_text","block_id":"crsk","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"15 data points compiled"}]}]}}]}]}`

## Rich text block
Displays formatted, structured representation of text.
Surfaces: Modals, Messages, Home tabs
Compatible elements:  
- `type` (String, required): The type of block. For a rich text block, `type` is always `rich_text`.
- `elements` (Object[], required): An array of rich text objects - `rich_text_list`, `rich_text_preformatted`, `rich_text_quote`, and `rich_text_section`. See linked elements for more details.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message or view and each iteration of a message or view. If a message or view is updated, use a new `block_id`.
Notes: The rich text block is also the output of the Slack client's WYSIWYG message composer, so all messages sent by end-users will have this format. Use this block to include user-defined formatted text in your Block Kit payload. While it is possible to format text with `mrkdwn`, `rich_text` is strongly preferred and allows greater flexibility. You might encounter a `rich_text` block in a message payload, as a built-in type in apps created with the Deno Slack SDK, or as output of the `rich_text_input` block element. Rich text blocks can be deeply nested. For instance: a `rich_text_list` can contain a `rich_text_section` which can contain bold style text. More details on how that works is shown in the examples on the pages for the `rich_text_list`, `rich_text_preformatted`, `rich_text_quote`, and `rich_text_section` block elements.

## Section block
Displays text, possibly alongside elements.
Surfaces: Modals, Messages, Home tabs
Compatible elements: Button, Checkboxes, Date picker, Image, Multi-select menu, Overflow menu, Radio button, Select menu, Time picker, Workflow buttons
- `type` (String, required): The type of block. For a section block, type will always be `section`.
- `text` (Object, preferred): The text for the block, in the form of a text object. Minimum length for the `text` in this field is 1 and maximum length is 3000 characters. This field is not _required_ if a valid array of `fields` objects is provided instead.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. You can use this `block_id` when you receive an interaction payload to identify the source of the action. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
- `fields` (Object[], maybe): Required if no `text` is provided. An array of text objects. Any text objects included with `fields` will be rendered in a compact format that allows for 2 columns of side-by-side text. Maximum number of items is 10. Maximum length for the `text` in each item is 2000 characters. Click here for an example.
- `accessory` (Object, optional): One of the compatible element objects noted above. Be sure to confirm the desired element works with `section`.
- `expand` (Boolean, optional): Whether or not this section block's text should always expand when rendered. If false or not provided, it may be rendered with a 'see more' option to expand and show the full text. For AI Assistant apps, this allows the app to post long messages without users needing to click 'see more' to expand the message.
Notes: A `section` can be used as a text block, in combination with text fields, or side-by-side with certain block elements.
Example: `{"blocks":[{"type":"section","text":{"type":"mrkdwn","text":"A message *with some bold text* and _some italicized text_."}}]}`

## Table block
Displays structured information in a table.
Surfaces: Messages, Home tabs
Compatible elements: Rich text input
- `type` (string, required): Always "table".
- `block_id` (string, optional): A unique identifier for a block. If not specified, a `block_id` will be generated. You can use this `block_id` when you receive an interaction payload to identify the source of the action. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
- `rows` (array, required): An array consisting of table rows. Maximum 100 rows. Each row object is an array with a max of 20 table cells. Table cells can have a type of `rich_text`, `raw_text`, or `raw_number`.
- `column_settings` (array, optional): An array describing column behavior. If there are fewer items in the `column_settings` array than there are columns in the table, then the items in the the `column_settings` array will describe the same number of columns in the table as there are in the array itself. Any additional columns will have the default behavior. Maximum 20 items. See below for column settings schema.
- `align` (string, optional): The alignment for items in this column. Can be `left`, `center`, or `right`. Defaults to `left` if not defined.
- `is_wrapped` (boolean, optional): Whether the contents of this column should be wrapped or not. Defaults to `false` if not defined.
Notes: Apps can programmatically publish messages that include a table by providing a table block in the `attachments` or `blocks` fields of a `chat.postMessage` request. These fields support a top-level table block with `rich_text`, `raw_text`, or `raw_number` options. Tables may include formatted text (bold text, emoji, mentions, hyperlinks, etc.) with a `rich_text` table cell block type, while a `raw_text` cell supports more basic characters and `raw_number` support numeric values. You must include a value for one of either the top-level blocks or text arguments in the message payload. A single table's character count across all cells cannot exceed 10,000 characters. Additionally, the aggregate character count across all table cells for a single message cannot exceed 10,000 characters. Large tables should be broken up into separate messages. The `column_settings` property lets you change text alignment and text wrapping behavior for table columns. In the `JSON` example below, the first column has text wrapping enabled and the second column right aligned. Use null to skip a column. Below is an example attachments value that you should send as a URL-encoded string in your request inside the `blocks` array.
Example: `{"blocks":[{"type":"table","column_settings":[{"is_wrapped":true},{"align":"right"}],"rows":[[{"type":"raw_text","text":"Header A"},{"type":"raw_text","text":"Header B"}],[{"type":"raw_text","text":"Data 1A"},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"text":"Data 1B","type":"link","url":"https://slack.com"}]}]}],[{"type":"raw_text","text":"Data 2A"},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"text":"Data 2B","type":"link","url":"https://slack.com"}]}]}]]}]}`

## Task card block
Displays a single task, representing a single action.
Surfaces: Messages
Compatible elements: URL source
- `type` (String, required): The type of block. For this block, type will always be `task_card`.
- `task_id` (String, required): ID for the task.
- `title` (String, required): Title of the task in plain text.
- `status` (String, required): The state of a task. Can be `"in_progress"`, `"complete"`, or `"error"`.
- `details` (Object, optional): Details of the task in the form of a single `rich_text` entity.
- `output` (Object, optional): Output of the task in the form of a single `rich_text` entity.
- `sources` (Array, optional): Array of URL source elements used to generate a response.
- `block_id` (String, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
- `icon` (Icon, optional): Icon associated with the task.
- `hide_title` (Boolean, optional): When true, `title` is not displayed and `details` becomes the top element. (The `title` field is still required.)
Notes: A `task_card` block displays a single task which represents a single action. It includes a title, optional details and output (both as rich text), sources, and a status indicator.
Example: `{"blocks":[{"type":"task_card","task_id":"task_1","title":"Fetching weather data","status":"in_progress","output":{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Found weather data for Chicago from 2 sources"}]}]},"sources":[{"type":"url","url":"https://weather.com/","text":"weather.com"},{"type":"url","url":"https://www.accuweather.com/","text":"accuweather.com"}]}]}`

## Video block
Displays an embedded video player.
Surfaces: Modals, Messages, Home tabs
Compatible elements:  
- `type` (string, required): The type of block. For a video block, type will always be `video`.
- `alt_text` (string, required): A tooltip for the video. Required for accessibility
- `author_name` (string, optional): Author name to be displayed. Must be less than 50 characters.
- `block_id` (string, optional): A unique identifier for a block. If not specified, one will be generated. Maximum length for this field is 255 characters. `block_id` should be unique for each message and each iteration of a message. If a message is updated, use a new `block_id`.
- `description` (Object, preferred): Description for video in the form of a text object that must have `type` of `plain_text`. `text` within must be less than 200 characters.
- `provider_icon_url` (string, optional): Icon for the video provider, e.g. YouTube icon.
- `provider_name` (string, optional): The originating application or domain of the video, e.g. YouTube.
- `title` (Object, required): Video title in the form of a text object that must have `type` of `plain_text`. `text` within must be less than 200 characters.
- `title_url` (string, preferred): Hyperlink for the title text. Must correspond to the non-embeddable URL for the video. Must go to an HTTPS URL.
- `thumbnail_url` (string, required): The thumbnail image URL
- `video_url` (string, required): The URL to be embedded. Must match any existing unfurl domains within the app and point to a HTTPS URL.
Notes: A `video` block is designed to embed videos in all app surfaces (e.g. link unfurls, messages, modals, App Home), anywhere you can put blocks! To use the video block within your app, you must have the `links.embed:write` scope. The metadata received in the block payload will be used to construct the description, provider, and title of the video in all clients. Developers have the flexibility to leave non-mandatory fields null and use other blocks to format this content. ### Requirements * Video blocks can only be posted by apps; users are not allowed to post embedded videos directly from Block Kit Builder. * Your app must have the the `links.embed:write` scope for both user and bot tokens. * `video_url` has to be included in the unfurl domains specified in your app. * `video_url` should be publicly accessible, unless the app relies on information received from the Events API payloads to make a decision on whether the viewer(s) of the content should have access. If so, the service could create a unique URL accessible only via Slack. * `video_url` must be compatible with an embeddable iFrame. * `video_url` must return a 2xx code OR 3xx with less than 5 redirects and an eventual 2xx. * `video_url` must not point to any Slack-related domain. ### Constraints * Embeddable video players only (audio-only permitted) * Navigation, scrolling and overlays are not allowed within the iFrame. * Interactivity (e.g. likes, comments, and reactions) are allowed within your player but shouldn't 
Example: `{"blocks":[{"type":"video","title":{"type":"plain_text","text":"Use the Events API to create a dynamic App Home","emoji":true},"title_url":"https://www.youtube.com/watch?v=8876OZV_Yy0","description":{"type":"plain_text","text":"Slack sure is nifty!","emoji":true},"video_url":"https://www.youtube.com/embed/8876OZV_Yy0?feature=oembed&autoplay=1","alt_text":"Use the Events API to create a dynamic App Home","thumbnail_url":"https://i.ytimg.com/vi/8876OZV_Yy0/hqdefault.jpg"}]}`

