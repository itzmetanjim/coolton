# Block Kit block elements

Condensed from Slack's reference (docs.slack.dev/reference/block-kit, fetched 2026-10-10): every field with its type, whether it's required, and its limits, plus one example each. Check a layout with `blocks.validate` before posting it (see SKILL.md).

## Attachment mention element
Renders as a rich app attachment or entity reference.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "attachment_mention".
- `url` (String, required): The URL of the app attachment or entity to reference.
- `text` (String, optional): Fallback text if attachment not found.
- `app_id` (String, optional): The app ID that produced the unfurl. Used to fetch the app's profile for use in rendering.
- `entity_id` (String, optional): The Work Object entity ID when the attachment is a Work Object.
- `icon_url` (String, optional): An optional override of the icon URL. This will typically be used for adding the Work Object product icon, which can be different from the app's icon.
- `channel_id` (String, optional): The encoded channel ID where this attachment lives.
- `ts` (String, optional): The encoded message timestamp where this attachment lives.
- `full_size_preview_enabled` (Boolean, optional): Whether the work object supports full size preview.
- `icon_name` (String, optional): An optional icon name identifier for the attachment (e.g., sf-account, sf-record, sf-list for Salesforce attachments).
- `reference_object_type` (String, optional): An optional type identifier for the referenced object (e.g., list_view, record for Salesforce attachments).
- `product_name` (String, optional): The product name for the Work Object (e.g., Google Docs, Google Sheets). Used to determine per-product click behavior preferences when the attachment is not available.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"attachment_mention","url":"https://example.com/attachment"}]}]}]}`

## Broadcast element
Displays a broadcast mention such as here, channel, or everyone.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "broadcast".
- `range` (String, required): The range of the broadcast; value can be `here`, `channel`, or `everyone`. Using `here` notifies only the active members of a channel; `channel` notifies all members of a channel; `everyone` notifies every person in the #general channel.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"broadcast","range":"everyone"}]}]}]}`

## Button element
Allows users a direct path to performing basic actions.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `button`.
- `text` (Object, required): A text object that defines the button's text. Can only be of `type: plain_text`. `text` may truncate with ~30 characters. Maximum length for the `text` in this field is 75 characters.
- `action_id` (String, optional): An identifier for this action. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `url` (String, optional): A URL to load in the user's browser when the button is clicked. Maximum length is 3000 characters. If you're using `url`, you'll still receive an interaction payload and will need to send an acknowledgement response.
- `value` (String, optional): The value to send along with the interaction payload. Maximum length is 2000 characters.
- `style` (String, optional): Decorates buttons with alternative visual color schemes. Use this option with restraint.`primary` gives buttons a green outline and text, ideal for affirmation or confirmation actions. `primary` should only be used for one button within a set.`danger` gives buttons a red outline and text, and should be used when the action is destructive. Use `danger` even more sparingly than `primary`.If you don't include this field, the default button style will be used.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog after the button is clicked.
- `accessibility_label` (String, optional): A label for longer descriptive text about a button element. This label will be read out by screen readers _instead of_ the button `text` object. Maximum length is 75 characters.
- `agent_prompt` (String, optional): A prompt to hand off to Slackbot, Slack's AI assistant, when the button is clicked. Any app can set this field. When the user who clicks the button has Slackbot AI available to them, clicking opens Slackbot seeded with this prompt instead of sending an interaction payload to your app; otherwise the button sends a standard interaction payload. Maximum length is 4000 characters.
- `agent_prompt_display` (String, optional): A short label shown in place of the button's `text` on the resulting Slackbot message; the full `agent_prompt` stays available when the user expands the message. Only takes effect alongside `agent_prompt`. If you omit it, the button's `text` is used.
- `visible_to_user_ids` (Array, optional): An array of user IDs for which the button appears. If not provided, the button is visible to all users.
Example: `{"type":"button","text":{"type":"plain_text","text":"Click Me"},"value":"click_me_123","action_id":"button"}`

## Canvas element
Renders as a link to a canvas.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "canvas".
- `file_id` (String, required): The ID of the canvas to link to.
- `label` (String, optional): What the canvas is labeled.
- `hide_title` (Boolean, optional): Whether the title should be hidden.
- `section_id` (String, optional): The section ID.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
- `text` (String, optional): Title of the canvas.
- `url` (String, optional): URL of the canvas.
- `is_skill_invocation` (Boolean, optional): True when this canvas element was inserted as a skill invocation.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"canvas","file_id":"F123ABC456"}]}]}]}`

## Canvas message unfurl element
Renders as an inline preview of a message inside a canvas message.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "canvas_message_unfurl".
- `root_message_ts` (String, required): The timestamp of the root message.
- `root_message_channel` (String, required): The channel the root message was posted in.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, and `underline`.
Example: `{ "blocks": [ { "type": "rich_text", "elements": [ { "type": "rich_text_section", "elements": [ { "type": "canvas_message_unfurl", "root_message_ts": "1783613573", "root_message_channel": "CABCDEFGHI", } ] } ] } ]}`

## Canvas user mention element
Renders as a user mention in canvas content.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "canvas_user_mention".
- `user_id` (String, required): The ID of the user to mention within canvas content.
- `thread_id` (String, optional): The ID of the thread.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"canvas_user_mention","user_id":"U123ABC456"}]}]}]}`

## Channel element
Renders as a mention of a channel.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "channel".
- `channel_id` (String, required): The ID of the channel to be mentioned.
- `tab_id` (String, optional): The ID of a specific channel tab.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
- `from_llm` (Boolean, optional): Indicates whether the channel was generated by the LLM itself.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"channel","channel_id":"C123ABC456"}]}]}]}`

## Checkboxes element
Allows users to choose multiple items from a list of options.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `checkboxes`.
- `action_id` (String, optional): An identifier for the action triggered when the checkbox group is changed. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `options` (Object[], required): An array of option objects. A maximum of 10 options are allowed.
- `initial_options` (Object[], optional): An array of option objects that exactly matches one or more of the options within `options`. These options will be selected when the checkbox group initially loads.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after clicking one of the checkboxes in this element.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `visible_to_user_ids` (Array, optional): An array of user IDs for which the checkboxes element appears. If not provided, the checkboxes element is visible to all users.
Example: `{ "type": "modal", "title": { "type": "plain_text", "text": "My App", "emoji": true }, "submit": { "type": "plain_text", "text": "Submit", "emoji": true }, "close": { "type": "plain_text", "text": "Cancel", "emoji": true }, "blocks": [ { "type": "section", "text": { "type": "plain_text", "text": "Check out these charming checkboxes" }, "accessory": { "type": "checkboxes", "action_id": "this_is_an_action_id", "initial_options": [{ "value": "A1", "text": { "type": "plain_text", "text": "Checkbox 1" } }], "options": [ { "value": "A1", "text": { "type": "plain_text", "text": "Checkbox 1" } }, { "value": "A2", "text": { "type": "plain_text", "text": "Checkbox 2" }, "description": { "type": "mrkdwn", "text": "*A description of option two*" }, } ] } } ]}`

## Citation element
Renders as an AI citation (file, external, web, message, or memory).
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "citation".
- `url` (String, required): The URL of the cited source.
- `text` (String, required): The display text for the citation.
- `index` (Number, required): The index of the citation.
- `details` (Object, required): Details about the cited source. This can be a `file`, `external`, `web`, `message`, or `memory` object. See below for the schemas of each.
- `from_llm` (Boolean, optional): Indicates whether the citation was generated by the LLM itself.
- `is_slack_url` (Boolean, optional): Indicates whether the citation is a Slack URL.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"citation","url":"https://example.com/source","text":"Source title","index":1,"details":{"citation_type":"file","descriptor":"Team canvas","file_id":"F0ABCDEFG12"}}]}]}]}`

## Color element
Displays a color swatch from a hex value.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "color".
- `value` (String, required): The hex value for the color.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, and `underline`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"color","value":"#F405B3"}]}]}]}`

## Date element
Displays a formatted, localized date.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case "date".
- `timestamp` (Number, required): A Unix timestamp for the date to be displayed in seconds.
- `format` (String, required): A template string containing curly-brace-enclosed tokens to substitute your provided `timestamp`. See details below.
- `timezone` (String, optional): The timezone in which the date is in.
- `url` (String, optional): URL to link the entire `format` string to.
- `fallback` (String, optional): Text to display in place of the date should parsing, formatting or displaying fail.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, and `underline`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"date","timestamp":1720710212,"format":"{date_num} at {time}","fallback":"timey"}]}]}]}`

## Date picker element
Allows users to select a date from a calendar style UI.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `datepicker`.
- `action_id` (String, optional): An identifier for the action triggered when a menu option is selected. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_date` (String, optional): The initial date that is selected when the element is loaded. This should be in the format `YYYY-MM-DD`.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after a date is selected.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown on the datepicker. Maximum length for the `text` in this field is 150 characters.
Example: `{"blocks":[{"type":"section","block_id":"section1234","text":{"type":"mrkdwn","text":"Pick a date for the deadline."},"accessory":{"type":"datepicker","action_id":"datepicker123","initial_date":"1990-04-28","placeholder":{"type":"plain_text","text":"Select a date"}}}]}`

## Datetime picker element
Allows users to select both a date and a time of day.
Surfaces: Modals, Messages
- `type` (String, required): The type of element. In this case `type` is always `datetimepicker`.
- `action_id` (String, optional): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_date_time` (Integer, optional): The initial date and time that is selected when the element is loaded, represented as a UNIX timestamp in seconds. This should be in the format of 10 digits, for example `1628633820` represents the date and time August 10th, 2021 at 03:17pm PST.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after a time is selected.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
Notes: _Interactive component_ - see our guide to enabling interactivity. On desktop clients, the time picker will take the form of a dropdown list and the date picker will take the form of a dropdown calendar. Both options will have free-text entry for precise choices. On mobile clients, the time picker and date picker will use native UIs.
Example: `{"blocks":[{"type":"input","element":{"type":"datetimepicker","action_id":"datetimepicker-action"},"hint":{"type":"plain_text","text":"This is some hint text","emoji":true},"label":{"type":"plain_text","text":"Start date","emoji":true}}]}`

## Email input element
Allows user to enter an email into a single-line field.
Surfaces: Modals
- `type` (String, required): The type of element. In this case `type` is always `email_text_input`.
- `action_id` (String, optional): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_value` (String, optional): The initial value in the email input when it is loaded.
- `dispatch_action_config` (Object, optional): A dispatch configuration object that determines when during text input the element returns a `block_actions` payload.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown in the email input. Maximum length for the `text` in this field is 150 characters.
Example: `{"blocks":[{"type":"input","block_id":"input123","label":{"type":"plain_text","text":"Email Address"},"element":{"type":"email_text_input","action_id":"email_text_input-action","placeholder":{"type":"plain_text","text":"Enter an email"}}}]}`

## Emoji element
Displays an emoji.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "emoji".
- `name` (String, required): The name of the emoji; i.e. "wave" or "wave::skin-tone-2".
- `unicode` (String, optional): Represents the unicode code point of the emoji, where applicable.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"emoji","name":"basketball"},{"type":"text","text":" "},{"type":"emoji","name":"snowboarder"},{"type":"text","text":" "},{"type":"emoji","name":"checkered_flag"}]}]}]}`

## Feedback buttons element
Surfaces: Messages
- `type` (String, required): The type of element. In this case `type` is always `feedback_buttons`.
- `positive_button` (Object, required): A button to indicate positive feedback. [See button object fields below](#button-object-fields).
- `negative_button` (Object, required): A button to indicate negative feedback. [See button object fields below](#button-object-fields).
- `action_id` (String, optional): An identifier for this action. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id` values in the containing block. Maximum length is 255 characters.
- `text` (Object, required): A text object that defines the button's text. Can only be of `type: plain_text`. Maximum length for the `text` in this field is 75 characters.
- `value` (String, required): The value to send along with the interaction payload. Maximum length is 2000 characters.
- `accessibility_label` (String, optional): A label for longer descriptive text about a button element. This label will be read out by screen readers instead of the button `text` object. Maximum length is 75 characters.
Example: `{"blocks":[{"type":"context_actions","elements":[{"type":"feedback_buttons","action_id":"feedback_buttons_1","positive_button":{"text":{"type":"plain_text","text":"Good"},"value":"positive_feedback","accessibility_label":"Mark this response as good"},"negative_button":{"text":{"type":"plain_text","text":"Bad"},"value":"negative_feedback","accessibility_label":"Mark this response as bad"}}]}]}`

## File element
Renders as a link to a Slack file.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "file".
- `file_id` (String, required): The ID of the file to link to.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, and `underline`.
- `text` (String, optional): Title of the file.
- `url` (String, optional): URL of the file.
- `is_skill_invocation` (Boolean, optional): Indicates whether this file element was inserted as a skill invocation.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"file","file_id":"F123ABC456"}]}]}]}`

## File input element
Allows user to upload files.
Surfaces: Modals
- `type` (String, required): The type of element. In this case `type` is always `file_input`.
- `action_id` (String, optional): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `filetypes` (String[], optional): An array of valid file extensions that will be accepted for this element. All file extensions will be accepted if filetypes is not specified. This validation is provided for convenience only, and you should perform your own file type validation based on what you expect to receive.
- `max_files` (Integer, optional): Maximum number of files that can be uploaded for this `file_input` element. Minimum of 1, maximum of 10. Defaults to 10 if not specified.
Notes: In order to use the `file_input` element within your app, your app must have the `files:read` scope. There is a 100MB file size limit.
Example: `{"title":{"type":"plain_text","text":"My App","emoji":true},"submit":{"type":"plain_text","text":"Submit","emoji":true},"type":"modal","close":{"type":"plain_text","text":"Cancel","emoji":true},"blocks":[{"type":"input","block_id":"input_block_id","label":{"type":"plain_text","text":"Upload Files"},"element":{"type":"file_input","action_id":"file_input_action_id_1","filetypes":["jpg","png"],"max_files":5}}]}`

## Icon button element
Surfaces: Messages
- `type` (String, required): The type of element. In this case `type` is always `icon_button`.
- `icon` (String, required): The icon to show. The `trash` icon is the only icon available at this time.
- `text` (Object, required): A text object that defines the button's text. Can only be of `type: plain_text`.
- `action_id` (String, optional): An identifier for this action. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `value` (String, optional): The value to send along with the interaction payload. Maximum length is 2000 characters.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog after the button is clicked.
- `accessibility_label` (String, optional): A label for longer descriptive text about a button element. This label will be read out by screen readers instead of the button `text` object. Maximum length is 75 characters.
- `visible_to_user_ids` (Array, optional): An array of user IDs for which the icon button appears. If not provided, the button is visible to all users.
Example: `{"blocks":[{"type":"context_actions","elements":[{"type":"icon_button","icon":"trash","text":{"type":"plain_text","text":"Delete"},"action_id":"delete_button","value":"delete_item"}]}]}`

## Image element
Displays an image as part of a larger block of content.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `image`.
- `alt_text` (String, required): A plain-text summary of the image. This should not contain any markup.
- `image_url` (String, optional): The URL for a publicly hosted image. You must provide either an `image_url` or `slack_file`. Maximum length for this field is 3000 characters.
- `slack_file` (Object, optional): A Slack image file object that defines the source of the image.
Notes: Use the `image` block if you want a block with _only_ an image in it.
Example: `{"blocks":[{"type":"section","block_id":"section567","text":{"type":"mrkdwn","text":"This is a section block with an accessory image."},"accessory":{"type":"image","image_url":"https://pbs.twimg.com/profile_images/625633822235693056/lNGUneLX_400x400.jpg","alt_text":"cute cat"}}]}`

## Link element
Displays a hyperlink.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case "link".
- `url` (String, required): The link's url.
- `text` (String, optional): The text shown to the user (instead of the url). If no text is provided, the url is used.
- `unsafe` (Boolean, optional): Indicates whether the link is safe.
- `from_llm` (Boolean, optional): Indicates whether the link was generated by the LLM itself.
- `is_slack_url` (Boolean, optional): Indicates whether the link is a Slack URL.
- `truncated` (Boolean, optional): Indicates whether the link has been truncated.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"link","url":"https://docs.slack.dev"}]}]}]}`

## List record element
Renders as a link to a Slack list record.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "list_record".
- `file_id` (String, required): The ID of the Slack list record to link to.
- `record_id` (String, optional): The record ID.
- `view_id` (String, optional): View ID of the list record.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
- `text` (String, optional): Title of the list record.
- `url` (String, optional): URL of the list record.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"list_record","file_id":"F123ABC456"}]}]}]}`

## Message mention element
Renders as a link to a Slack message.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "message_mention".
- `channel_id` (String, required): The ID of the channel containing the message.
- `message_ts` (String, required): The timestamp of the message to link to.
- `author_id` (String, optional): ID of the author of the message.
- `thread_ts` (String, optional): Timestamp of when the message mention occurred.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
- `text` (String, optional): Text representing the message.
- `url` (String, optional): URL of the message.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"message_mention","channel_id":"C123ABC456","message_ts":"1720710212.123456"}]}]}]}`

## Multi-select menu element
Allows users to select multiple items from a list of options.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `multi_static_select`.
- `action_id` (String, optional): An identifier for the action triggered when a menu option is selected. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `options` (Object[], required): An array of option objects. Maximum number of options is 100. Each option must be less than 76 characters. If `option_groups` is specified, this field should not be.
- `option_groups` (Object[], optional): An array of option group objects. Maximum number of option groups is 100. If `options` is specified, this field should not be.
- `initial_options` (Object[], optional): An array of option objects that exactly match one or more of the options within `options` or `option_groups`. These options will be selected when the menu initially loads.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears before the multi-select choices are submitted.
- `max_selected_items` (Integer, optional): Specifies the maximum number of items that can be selected in the menu. Minimum number is 1.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown on the menu. Maximum length for the `text` in this field is 150 characters.
Notes: _Interactive component_ - see our guide to enabling interactivity. Just like regular select menus, multi-select menus also include type-ahead functionality, where a user can type a part or all of an option string to filter the list. There are different types of multi-select menu that depend on different data sources for their lists of options: * [Menu with static options](#static_multi_select) * [Menu with external data source](#external_multi_select) * [Menu with user list](#users_multi_select) * [Menu with conversations list](#conversation_multi_select) * [Menu with channels list](#channel_multi_select) Example: ![An example of a multi-select element](/assets/images/bk_multiselect_example-7f8fde60bb99b8f50169904d9b0e6e75.png) * * *
Example: `{"blocks":[{"type":"section","block_id":"section678","text":{"type":"mrkdwn","text":"Pick items from the list"},"accessory":{"action_id":"text1234","type":"multi_static_select","placeholder":{"type":"plain_text","text":"Select items"},"options":[{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-0"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-1"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-2"}]}}]}`

## Number input element
Allows user to enter a number into a single-line field.
Surfaces: Modals
- `type` (String, required): The type of element. In this case `type` is always `number_input`.
- `is_decimal_allowed` (Boolean, required): Decimal numbers are allowed if `is_decimal_allowed`\= `true`, set the value to `false` otherwise.
- `action_id` (String, optional): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_value` (String, optional): The initial value in the plain-text input when it is loaded.
- `min_value` (String, optional): The minimum value, cannot be greater than `max_value`.
- `max_value` (String, optional): The maximum value, cannot be less than `min_value`.
- `dispatch_action_config` (Object, optional): A dispatch configuration object that determines when during text input the element returns a `block_actions` payload.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown in the number input. Maximum length for the `text` in this field is 150 characters.
Notes: _Interactive component_ - see our guide to enabling interactivity. The number input element accepts both whole and decimal numbers. For example, 0.25, 5.5, and -10 are all valid input values. Decimal numbers are only allowed when `is_decimal_allowed` is equal to `true`.
Example: `{"blocks":[{"type":"input","element":{"type":"number_input","is_decimal_allowed":false,"action_id":"number_input-action"},"label":{"type":"plain_text","text":"Label","emoji":true}}]}`

## Overflow menu element
Allows users to press a button to view a list of options.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `overflow`.
- `action_id` (String, optional): An identifier for the action triggered when a menu option is selected. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `options` (Object[], required): An array of up to five option objects to display in the menu.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after a menu item is selected.
Notes: _Interactive component_ - see our guide to enabling interactivity. Unlike the select menu, there is no typeahead field, and the button always appears with an ellipsis ("…") rather than customizable text. As such, it is usually used if you want a more compact layout than a select menu, or to supply a list of less visually important actions after a row of buttons. You can also specify URL links as overflow menu options, instead of actions. Example: ![An example of an overflow element](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAABWQAAAF4CAMAAADkCABwAAAAMFBMVEX////4+Pj9/f36+vrt7e319fXy8vIeHR7b29vQ0NC7u7ujoqNOTk5qaWo2NTaGhoZrS3fyAAAACXBIWXMAAAsTAAALEwEAmpwYAAAegUlEQVR42u3da2OiyBaF4VUFRaHp7vP/f+Z0LgJ1Ox9ABaOJSUw60fdx5pxOJg0RZLssNoUEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAMBphk0A4JY01n7gb+c8UGQB4KTWfnABuaPIAsAJK6MSUnnvR//KGZUNRRYAjrGN1RA/tIi6eWOWrdjsAG6Fqz5aY5VLZUx6S2FnswO4lSBrleNHFxLL2+omRRbArchW8eNLCRRZADghffkiKLIAbkj58kVQZAHgE1FkAeAT1Tf1VKNu+ryqHZZt1TqnJfvGNxvwz4rsqlF8/Ozfbu2SVZZRevr4wtrg/37ur9vWSpuv2nN3Ve7eUv2aonZRZFdJ/ow9uO7dA8cJ8PVFtu6kZvjU363xw/Y0XvtNN96yyrlBfvPZF6xM01PUMeruP17AwLWOybaDJPe5v5sf1FQppe96VdqqWn35On8Xx2A6cAtJ1nfy/edOfLAa1P73nceNbT74RnCfPny5W+WwttVfXr/AtRbZqsgZhdVnfjhuOj1+7213WFE7fWGT3xMvXuCKhwt8r9J/csZM8t/7tHam5Q7AZyXZpD7+CsP21FdrhiTrbclDPjI1mMldkaTaaXcuyzYmDZLkalu2E+O0JgbVjckbyR6rYvtlaaWw+1tp93uERWGeL/tgKTFMf0Wd1NTj7GXOmfnP26baPaPapUFqK+XtU3jW/+RqhXjsJ2fxvLLl+TxpdTNf7XytB1+7/tjeMO3sWb64K2S93W6/g/
Example: `{"blocks":[{"type":"section","block_id":"section 890","text":{"type":"mrkdwn","text":"This is a section block with an overflow menu."},"accessory":{"type":"overflow","options":[{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-0"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-1"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-2"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-3"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-4"}],"action_id":"overflow"}}]}`

## Plain-text input element
Allows users to enter freeform text data into a single-line or multi-line field.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `plain_text_input`.
- `action_id` (String, optional): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_value` (String, optional): The initial value in the plain-text input when it is loaded.
- `multiline` (Boolean, optional): Indicates whether the input will be a single line (`false`) or a larger textarea (`true`). Defaults to `false`.
- `min_length` (Integer, optional): The minimum length of input that the user must provide. If the user provides less, they will receive an error. Acceptable values for this field are between 0 and 3000, inclusive.
- `max_length` (Integer, optional): The maximum length of input that the user can provide. If the user provides more, they will receive an error. Acceptable values for this field are between 1 and 3000, inclusive.
- `dispatch_action_config` (Object, optional): A dispatch configuration object that determines when during text input the element returns a `block_actions` payload.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown in the plain-text input. Maximum length for the `text` in this field is 150 characters.
Example: `{"blocks":[{"type":"input","element":{"type":"plain_text_input","action_id":"plain_text_input-action"},"label":{"type":"plain_text","text":"Label","emoji":true}}]}`

## Radio button group element
Allows users to choose one item from a list of possible options.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `radio_buttons`.
- `action_id` (String, optional): An identifier for the action triggered when the radio button group is changed. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `options` (Object[], required): An array of option objects. A maximum of 10 options are allowed.
- `initial_option` (Object, optional): An option object that exactly matches one of the options within `options`. This option will be selected when the radio button group initially loads.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after clicking one of the radio buttons in this element.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
Example: `{"blocks":[{"type":"section","text":{"type":"plain_text","text":"Check out these rad radio buttons"},"accessory":{"type":"radio_buttons","action_id":"this_is_an_action_id","initial_option":{"value":"A1","text":{"type":"plain_text","text":"Radio 1"}},"options":[{"value":"A1","text":{"type":"plain_text","text":"Radio 1"}},{"value":"A2","text":{"type":"plain_text","text":"Radio 2"}}]}}]}`

## Rich text input element
Allows users to enter formatted text in a WYSIWYG composer, offering the same messaging writing experience as in Slack.
Surfaces: Modals, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `rich_text_input`.
- `action_id` (String, required): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique in the containing block. Maximum length is 255 characters.
- `initial_value` (Rich text, optional): The initial value in the rich text input when it is loaded.
- `dispatch_action_config` (Object, optional): A dispatch configuration object that determines when during text input the element returns a `block_actions` payload.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` object that defines the placeholder text shown in the plain-text input. Maximum length for the `text` in this field is 150 characters.
- `min_lines` (Integer, optional): The minimum number of visible text lines the input should display before scrolling. Controls the initial height of the input. Must be between 1 and 100.
- `max_lines` (Integer, optional): The maximum number of visible text lines the input can grow to before scrolling. Defaults to 8 when omitted. Must be between 1 and 100.
Example: `{"blocks":[{"type":"input","element":{"type":"rich_text_input","action_id":"rich_text_input-action","dispatch_action_config":{"trigger_actions_on":["on_character_entered"]},"focus_on_load":true,"placeholder":{"type":"plain_text","text":"Enter text"}},"label":{"type":"plain_text","text":"Label","emoji":true}}],"type":"home"}`

## Rich text list element
Displays a list of rich text items.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of sub-element; in this case, `rich_text_list`.
- `style` (String, required): Either `bullet` or `ordered`, the latter meaning a numbered list.
- `elements` (Object [], required): An array of `rich_text_section` objects containing two properties: `type`, which is "rich_text_section", and `elements`, which is an array of rich text elements. Rich text elements include `attachment_mention`, `broadcast`, `canvas`, `canvas_user_mention`, `canvas_message_unfurl`, `channel`, `citation`, `color`, `date`, `emoji`, `file`, `link`, `list_record`, `message_mention`, `salesforce_data_field`, `tag`, `team`, `text`, `user`, `usergroup`, `work_object_mention`, `workflow_mention`.
- `indent` (Number, optional): Sub-list indent level.
- `offset` (Number, optional): Number to offset the first number in the list. For example, if the `offset = 4`, the first number in the ordered list would be 5.
- `border` (Number, optional): Turn the border on or off.
Example: `{"blocks":[{"type":"rich_text","block_id":"block1","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"My favorite Slack features (in no particular order):"}]},{"type":"rich_text_list","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Huddles"}]},{"type":"rich_text_section","elements":[{"type":"text","text":"Canvas"}]},{"type":"rich_text_section","elements":[{"type":"text","text":"Developing with Block Kit"}]}],"style":"bullet","indent":0,"border":1}]}]}`

## Rich text preformatted element
Displays a preformatted rich text element.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of the sub-element; in this case, `rich_text_preformatted`.
- `elements` (Object [], required): An array of text or link elements.
- `border` (Number, optional): Turn the border on or off.
- `language` (String, optional): The language of the code block, used for syntax highlighting (e.g., `"python"`, `"javascript"`, `"json"`).
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_preformatted","elements":[{"type":"text","text":"{\n  \"object\": {\n    \"description\": \"this is an example of a json object\"\n  }\n}"}],"border":0}]}]}`

## Rich text quote element
Displays a rich text quote block.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of the sub-element; in this case, `rich_text_quote`.
- `elements` (Object [], required): An array of rich text elements. Rich text elements include `attachment_mention`, `broadcast`, `canvas`, `canvas_user_mention`, `canvas_message_unfurl`, `channel`, `citation`, `color`, `date`, `emoji`, `file`, `link`, `list_record`, `message_mention`, `salesforce_data_field`, `tag`, `team`, `text`, `user`, `usergroup`, `work_object_mention`, `workflow_mention`.
- `border` (Number, optional): Turn the border on or off.
Example: `{"blocks":[{"type":"rich_text","block_id":"Vrzsu","elements":[{"type":"rich_text_quote","elements":[{"type":"text","text":"What we need is good examples in our documentation."}]},{"type":"rich_text_section","elements":[{"type":"text","text":"Yes - I completely agree, Luke!"}]}]}]}`

## Rich text section element
A section element that holds rich text elements.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of sub-element; in this case, `rich_text_section`.
- `elements` (Object [], required): An array of rich text elements. Rich text elements include `attachment_mention`, `broadcast`, `canvas`, `canvas_user_mention`, `canvas_message_unfurl`, `channel`, `citation`, `color`, `date`, `emoji`, `file`, `link`, `list_record`, `message_mention`, `salesforce_data_field`, `tag`, `team`, `text`, `user`, `usergroup`, `work_object_mention`, `workflow_mention`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Hello there, I am a basic rich text block!"}]}]},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Hello there, "},{"type":"text","text":"I am a bold rich text block!","style":{"bold":true}}]}]},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Hello there, "},{"type":"text","text":"I am an italic rich text block!","style":{"italic":true}}]}]},{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Hello there, "},{"type":"text","text":"I am a strikethrough rich text block!","style":{"strike":true}}]}]}]}`

## Salesforce data field element
Renders as a Salesforce data field reference.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "salesforce_data_field".
- `salesforce_record_id` (String, required): The ID of the Salesforce record to reference.
- `salesforce_field_label` (String, optional): The Salesforce field label.
- `salesforce_field_api_name` (String, optional): The Salesforce field API name.
- `salesforce_include_field_label` (Boolean, optional): When true, display the field label before the field value.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"salesforce_data_field","salesforce_record_id":"001ABC456DEF789"}]}]}]}`

## Select menu element
Allows users to choose an option from a drop down menu.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `static_select`.
- `action_id` (String, optional): An identifier for the action triggered when a menu option is selected. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `options` (Object[], required): An array of option objects. Maximum number of options is 100. If `option_groups` is specified, this field should not be.
- `option_groups` (Object[], optional): An array of option group objects. Maximum number of option groups is 100. If `options` is specified, this field should not be.
- `initial_option` (Object, optional): A single option that exactly matches one of the options within `options` or `option_groups`. This option will be selected when the menu initially loads.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after a menu item is selected.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown on the menu. Maximum length for the `text` in this field is 150 characters.
Notes: _Interactive component_ - see our guide to enabling interactivity. The select menu also includes type-ahead functionality, where a user can type a part or all of an option string to filter the list. There are different types of select menu elements that depend on different data sources for their lists of options: * [Select menu of static options](#static_select) * [Select menu of external data source](#external_select) * [Select menu of users](#users_select) * [Select menu of conversations](#conversations_select) * [Select menu of public channels](#channels_select) ![An example of a select menu element](/assets/images/bk_select_example-cc82a53fa6adca8ebb0a2cd6937ff2b1.png) * * *
Example: `{"blocks":[{"type":"section","block_id":"section678","text":{"type":"mrkdwn","text":"Pick an item from the dropdown list"},"accessory":{"action_id":"text1234","type":"static_select","placeholder":{"type":"plain_text","text":"Select an item"},"options":[{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-0"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-1"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-2"}]}}]}`

## Tag element
Renders as a colored tag or pill.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "tag".
- `text` (String, required): The text displayed in the tag.
- `color` (String, optional): The color of the tag. The options for this value are: "gray", "brown", "purple", "indigo", "blue", "green", "yellow", "orange", "red".
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"tag","text":"In progress"}]}]}]}`

## Team element
Renders as a mention of a workspace or team.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "team".
- `team_id` (String, required): The ID of the workspace or team to mention.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"team","team_id":"T123ABC456"}]}]}]}`

## Text element
Displays text, optionally with styling.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "text".
- `text` (String, required): The text shown to the user.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `code`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
Example: `{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"text","text":"Hello there, "},{"type":"text","text":"I am a bold rich text block!","style":{"bold":true}}]}]}`

## Time picker element
Allows users to enter numerical data into a single-line field.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of element. In this case `type` is always `timepicker`.
- `action_id` (String, optional): An identifier for the action triggered when a time is selected. You can use this when you receive an interaction payload to identify the source of the action. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_time` (String, optional): The initial time that is selected when the element is loaded. This should be in the format `HH:mm`, where `HH` is the 24-hour format of an hour (00 to 23) and `mm` is minutes with leading zeros (00 to 59), for example `22:25` for 10:25pm.
- `confirm` (Object, optional): A confirm object that defines an optional confirmation dialog that appears after a time is selected.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown on the time picker. Maximum length for the `text` in this field is 150 characters.
- `timezone` (String, optional): A string in the IANA format, e.g. "America/Chicago". The timezone is displayed to end users as hint text underneath the time picker. It is also passed to the app upon certain interactions, such as `view_submission`.
Notes: _Interactive component_ - see our guide to enabling interactivity. On desktop clients, this time picker will take the form of a dropdown list with free-text entry for precise choices. On mobile clients, the time picker will use native time picker UIs.
Example: `{"blocks":[{"type":"section","block_id":"section1234","text":{"type":"mrkdwn","text":"Pick a date for the deadline."},"accessory":{"type":"timepicker","timezone":"America/Los_Angeles","action_id":"timepicker123","initial_time":"11:40","placeholder":{"type":"plain_text","text":"Select a time"}}}]}`

## URL input element
Allows user to enter a URL into a single-line field.
Surfaces: Modals
- `type` (String, required): The type of element. In this case `type` is always `url_text_input`.
- `action_id` (String, optional): An identifier for the input value when the parent modal is submitted. You can use this when you receive a `view_submission` payload to identify the value of the input element. Should be unique among all other `action_id`s in the containing block. Maximum length is 255 characters.
- `initial_value` (String, optional): The initial value in the URL input when it is loaded.
- `dispatch_action_config` (Object, optional): A dispatch configuration object that determines when during text input the element returns a `block_actions` payload.
- `focus_on_load` (Boolean, optional): Indicates whether the element will be set to auto focus within the `view object`. Only one element can be set to `true`. Defaults to `false`.
- `placeholder` (Object, optional): A `plain_text` only text object that defines the placeholder text shown in the URL input. Maximum length for the `text` in this field is 150 characters.
Example: `{"blocks":[{"type":"input","element":{"type":"url_text_input","action_id":"url_text_input-action"},"label":{"type":"plain_text","text":"Label","emoji":true}}]}`

## URL source element
Displays a URL source with optional icon.
Surfaces: Messages
- `type` (String, required): The type of element. In this case `type` is always `url`.
- `url` (String, required): The URL type source.
- `text` (String, required): Display text for the URL.
Notes: The URL source element is used to display clickable URL references within a task card block. It cannot be used within other blocks. Note that whether the URL actually resolves via DNS is not validated.
Example: `{"type":"url","url":"https://docs.slack.dev/","text":"Slack API docs"}`

## User element
Renders as a mention of a user.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "user".
- `user_id` (String, required): The ID of the user to be mentioned.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `underline`, and `unlink`.
- `from_llm` (Boolean, optional): Indicates whether the user was generated by the LLM itself.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"user","user_id":"U123ABC456"}]}]}]}`

## Usergroup element
Renders as a mention of a user group.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case "usergroup".
- `usergroup_id` (String, required): The ID of the user group to be mentioned.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `unlink`, and `underline`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"usergroup","usergroup_id":"G123ABC456"}]}]}]}`

## Work object mention element
Renders as a Work Object reference.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "work_object_mention".
- `entity_id` (String, required): The ID of the Work Object entity.
- `app_id` (String, required): The ID of the app associated with the Work Object.
- `text` (String, required): The display text for the Work Object reference.
- `url` (String, required): The URL of the Work Object.
- `icon_url` (String, optional): Optional product icon URL for the Work Object.
- `full_size_preview_enabled` (Boolean, optional): Whether the Work Object supports full size preview.
- `product_name` (String, optional): The product name for the Work Object. Used to determine per-product click behavior preferences.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `unlink`, and `underline`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"work_object_mention","entity_id":"E123ABC456","app_id":"A123ABC456","text":"Work object","url":"https://example.com/work-object"}]}]}]}`

## Workflow button element
Allows users to run a link trigger with customizable inputs.
Surfaces: Messages
- `type` (String, required): The type of element. In this case `type` is always `workflow_button`.
- `text` (Object, required): A text object that defines the button's text. Can only be of `type: plain_text`. `text` may truncate with ~30 characters. Maximum length for the `text` in this field is 75 characters.
- `workflow` (Object, required): A workflow object that contains details about the workflow that will run when the button is clicked.
- `action_id` (String, required): An identifier for the action. Use this when you receive an interaction payload to identify the source of the action. Every `action_id` in a block should be unique. Maximum length is 255 characters.
- `style` (String, optional): Decorates buttons with alternative visual color schemes. Use this option with restraint.`primary` gives buttons a green outline and text, ideal for affirmation or confirmation actions. `primary` should only be used for one button within a set.`danger` gives buttons a red outline and text, and should be used when the action is destructive. Use `danger` even more sparingly than `primary`.If you don't include this field, the default button style will be used.
- `accessibility_label` (String, optional): A label for longer descriptive text about a button element. This label will be read out by screen readers _instead of_ the button `text` object. Maximum length is 75 characters.
Example: `{"blocks":[{"type":"section","text":{"text":"A message *with some bold text* and _some italicized text_.","type":"mrkdwn"},"accessory":{"type":"workflow_button","text":{"type":"plain_text","text":"Run Workflow"},"action_id":"workflowbutton123","workflow":{"trigger":{"url":"https://slack.com/shortcuts/Ft0123ABC456/xyz...zyx","customizable_input_parameters":[{"name":"input_parameter_a","value":"Value for input param A"},{"name":"input_parameter_b","value":"Value for input param B"}]}}}}]}`

## Workflow mention element
Renders as a link to a workflow.
Surfaces: Modals, Messages, Home tabs
- `type` (String, required): The type of object; in this case, "workflow_mention".
- `workflow_id` (String, required): The ID of the workflow to link to.
- `function_trigger_id` (String, required): The ID of the function trigger for the workflow.
- `text` (String, required): The display text for the workflow link.
- `url` (String, optional): URL of the workflow.
- `channel_id` (String, optional): The encoded channel ID where this workflow mention lives.
- `ts` (String, optional): The encoded message timestamp where this workflow mention lives.
- `style` (Object, optional): An object of optional boolean properties that dictate style: `bold`, `italic`, `strike`, `highlight`, `client_highlight`, `unlink`, and `underline`.
Example: `{"blocks":[{"type":"rich_text","elements":[{"type":"rich_text_section","elements":[{"type":"workflow_mention","workflow_id":"Wf123ABC456","function_trigger_id":"Ft123ABC456","text":"Run workflow"}]}]}]}`

