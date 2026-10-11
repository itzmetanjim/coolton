# Block Kit composition objects

Condensed from Slack's reference (docs.slack.dev/reference/block-kit, fetched 2026-10-10): every field with its type, whether it's required, and its limits, plus one example each. Check a layout with `blocks.validate` before posting it (see SKILL.md).

## Confirmation dialog object
Defines a dialog that adds a confirmation step to interactive elements.
- `title` (Object, required): A `plain_text` text object that defines the dialog's title. Maximum length for this field is 100 characters.
- `text` (Object, required): A `plain_text` text object that defines the explanatory text that appears in the confirm dialog. Maximum length for the `text` in this field is 300 characters.
- `confirm` (Object, required): A `plain_text` text object to define the text of the button that confirms the action. Maximum length for the `text` in this field is 30 characters.
- `deny` (Object, required): A `plain_text` text object to define the text of the button that cancels the action. Maximum length for the `text` in this field is 30 characters.
- `style` (String, optional): Defines the color scheme applied to the `confirm` button. A value of `danger` will display the button with a red background on desktop, or red text on mobile. A value of `primary` will display the button with a green background on desktop, or blue text on mobile. If this field is not provided, the default value will be `primary`.
Example: `{"blocks":[{"type":"actions","elements":[{"type":"button","text":{"type":"plain_text","emoji":true,"text":"Approve"},"confirm":{"title":{"type":"plain_text","text":"Are you sure?"},"text":{"type":"mrkdwn","text":"Would you not prefer a good game of _chess_?"},"confirm":{"type":"plain_text","text":"Do it"},"deny":{"type":"plain_text","text":"Stop, I changed my mind!"}},"style":"primary","value":"click_me_123"},{"type":"button","text":{"type":"plain_text","emoji":true,"text":"Deny"},"style":"danger","value":"click_me_123"}]}]}`

## Conversation filter object
Defines a filter for the list of options in a conversation selector menu.
- `include` (String[], optional): Indicates which type of conversations should be _included_ in the list. When this field is provided, any conversations that do not match will be excludedYou should provide an array of strings from the following options: `im`, `mpim`, `private`, and `public`. The array cannot be empty.
- `exclude_external_shared_channels` (Boolean, optional): Indicates whether to exclude external shared channels from conversation lists. This field will not exclude users from shared channels. Defaults to `false`.
- `exclude_bot_users` (Boolean, optional): Indicates whether to exclude bot users from conversation lists. Defaults to `false`.
Example: `{"title":{"type":"plain_text","text":"My App","emoji":true},"submit":{"type":"plain_text","text":"Submit","emoji":true},"type":"modal","close":{"type":"plain_text","text":"Cancel","emoji":true},"blocks":[{"type":"input","element":{"type":"conversations_select","placeholder":{"type":"plain_text","text":"Select a conversation","emoji":true},"filter":{"include":["public","mpim"],"exclude_bot_users":true}},"label":{"type":"plain_text","text":"Choose the conversation to publish your result to:","emoji":true}}]}`

## Dispatch action configuration object
Defines when a plain-text input element will return a block_actions interaction payload.
- `trigger_actions_on` (String[], optional): An array of interaction types that you would like to receive a `block_actions` payload for. Should be one or both of:`on_enter_pressed`, payload is dispatched when user presses the enter key while the input is in focus. Hint text will appear underneath the input explaining to the user to press enter to submit.`on_character_entered`, payload is dispatched when a character is entered (or removed) in the input.
Example: `{"blocks":[{"type":"input","dispatch_action":true,"element":{"type":"plain_text_input","multiline":true,"dispatch_action_config":{"trigger_actions_on":["on_character_entered"]}},"label":{"type":"plain_text","text":"This is a multiline plain-text input","emoji":true}}]}`

## Option group object
Defines a way to group options in a menu.
- `label` (Object, required): A `plain_text` text object that defines the label shown above this group of options. Maximum length for the `text` in this field is 75 characters.
- `options` (Object[], required): An array of option objects that belong to this specific group. Maximum of 100 items.
Example: `{"blocks":[{"type":"section","text":{"type":"mrkdwn","text":":mag: Search results for *Cata*"}},{"type":"divider"},{"type":"section","text":{"type":"mrkdwn","text":"*<fakeLink.toYourApp.com|Use Case Catalogue>*\nUse Case Catalogue for the following departments/roles..."},"accessory":{"type":"static_select","placeholder":{"type":"plain_text","emoji":true,"text":"Manage"},"option_groups":[{"label":{"type":"plain_text","text":"Group 1"},"options":[{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-0"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-1"},{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-2"}]},{"label":{"type":"plain_text","text":"Group 2"},"options":[{"text":{"type":"plain_text","text":"*this is plain_text text*"},"value":"value-3"}]}]}}]}`

## Option object
Defines a single item in a number of item selection elements.
- `text` (Object, required): A text object that defines the text shown in the option on the menu. Overflow, select, and multi-select menus can only use `plain_text` objects, while radio buttons and checkboxes can use `mrkdwn` text objects. Maximum length for the `text` in this field is 75 characters.
- `value` (String, required): A unique string value that will be passed to your app when this option is chosen. Maximum length for this field is 150 characters.
- `description` (Object, optional): A `plain_text` text object that defines a line of descriptive text shown below the `text` field beside a single selectable item in a select menu, multi-select menu, checkbox group, radio button group, or overflow menu. Checkbox group and radio button group items can also use `mrkdwn` formatting. Maximum length for the `text` within this field is 75 characters.
- `url` (String, optional): A URL to load in the user's browser when the option is clicked. **The `url` attribute is only available in overflow menus**. Maximum length for this field is 3000 characters. If you're using `url`, you'll still receive an interaction payload and will need to send an acknowledgement response.
Example: `{"text":{"type":"plain_text","emoji":true,"text":"Save it"},"value":"value-2"}`

## Slack file object
Defines an object containing Slack file information to be used in an image block or image element.
- `url` (string, optional): This URL can be the `url_private` or the `permalink` of the Slack file.
- `id` (string, optional): Slack ID of the file.
Example: `{"blocks":[{"type":"image","title":{"type":"plain_text","text":"Please enjoy this photo of a kitten"},"block_id":"image4","slack_file":{"url":"https://files.slack.com/files-pri/T0123456-F0123456/xyz.png"},"alt_text":"An incredibly cute kitten."}]}`

## Slack icon object
Defines an object containing Slack icon information to be used in a card block.
- `type` (string, required): Always `icon`.
- `name` (string, required): One of the following: `archive`, `book`, `bookmark`, `bot`, `bug`, `calendar`, `call`, `caret-left`, `caret-right`, `check`, `clipboard`, `code`, `comment`, `compass`, `copy`, `cube`, `download`, `edit`, `email`, `eye-closed`, `eye-open`, `file`, `flag`, `folder`, `gear`, `globe`, `heart`, `help`, `image`, `info`, `key`, `lightbulb`, `link`, `map`, `mobile`, `new-window`, `pin`, `plus`, `refine`, `refresh`, `rocket`, `save`, `screen`, `share`, `sparkle`, `star`, `star-filled`, `tag`, `thumbs-down`, `thumbs-up`, `trash`, `upload`, `user`, `warning`

## Text object
Defines an object containing some text.
- `type` (String, required): The formatting to use for this text object. Can be one of `plain_text`or `mrkdwn`.
- `text` (String, required): The text for the block. This field accepts any of the standard text formatting markup when `type` is `mrkdwn`. The minimum length is 1 and maximum length is 3000 characters.
- `emoji` (Boolean, optional): Indicates whether emojis in a text field should be escaped into the colon emoji format. This field is only usable when `type` is `plain_text`.
- `verbatim` (Boolean, #### example {#example}): When set to `false` (as is default) URLs will be auto-converted into links, conversation names will be link-ified, and certain mentions will be automatically parsed. When set to `true`, Slack will continue to process all markdown formatting and manual parsing strings, but it won’t modify any plain-text content. For example, channel names will not be hyperlinked. This field is only usable when `type` is `mrkdwn`.
Example: `{"blocks":[{"type":"section","text":{"type":"mrkdwn","text":"A message *with some bold text* and _some italicized text_."}}]}`

## Trigger object
Defines an object containing trigger information.
- `url` (String, required): A link trigger URL. Must be associated with a valid trigger.
- `customizable_input_parameters` (Object[], optional): An array of input parameter objects. Each specified name must match an input parameter defined on the workflow of the provided trigger (url), and the input parameter mapping on the trigger must be set as `customizable: true`. Each specified value must match the type defined by the workflow input parameter of the matching name.
Example: `{"blocks":[{"type":"section","text":{"text":"A message *with some bold text* and _some italicized text_.","type":"mrkdwn"},"accessory":{"type":"workflow_button","text":{"type":"plain_text","text":"Run Workflow"},"action_id":"workflowbutton123","workflow":{"trigger":{"url":"https://slack.com/shortcuts/Ft0123ABC456/xyz...zyx","customizable_input_parameters":[{"name":"input_parameter_a","value":"Value for input param A"},{"name":"input_parameter_b","value":"Value for input param B"}]}}}}]}`

## Workflow object
Defines an object containing workflow information.
- `trigger` (Object, required): A `trigger` object that contains information about a workflow's trigger.
Example: `{"blocks":[{"type":"section","text":{"text":"A message *with some bold text* and _some italicized text_.","type":"mrkdwn"},"accessory":{"type":"workflow_button","text":{"type":"plain_text","text":"Run Workflow"},"action_id":"workflowbutton123","workflow":{"trigger":{"url":"https://slack.com/shortcuts/Ft0123ABC456/xyz...zyx","customizable_input_parameters":[{"name":"input_parameter_a","value":"Value for input param A"},{"name":"input_parameter_b","value":"Value for input param B"}]}}}}]}`

