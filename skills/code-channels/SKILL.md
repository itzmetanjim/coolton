---
name: code-channels
description: 'Create a Slack code channel (create_code_channel_tool) and work in one: tabs/artifacts, plan canvases, context bar, slash commands, rename, archive. Load before creating one, and in any code channel before using its tools.'
---

# Code channels

A Slack "code channel" is a channel for one task. `create_code_channel_tool` creates one and moves
the current conversation into it as its own single, ongoing conversation: every message posted
directly in that channel (not inside a thread there) is addressed to you and answered at channel
level, exactly like one continuous thread. A thread started inside a code channel behaves like a
normal Slack thread instead: its own separate conversation, mention required. Not available on the
web UI.

## Getting the tools
None of these tools are in your tool list: call them with `call_tool`. In a code channel they're
loaded for you (a `search_tools` result already in the conversation); anywhere else, run
`search_tools("code channel")` first. The working-in-a-channel tools only work inside a code
channel.

## Creating one
Create one when the user asks for a code channel. When a request grows into long, multi-step work
that deserves its own space (a coding project, a big investigation), you can offer one, and create
it once they agree.

`name` is a real display name, written like a title or sentence, not a slug: "Code audit and bug
detection in Coolton", never "code-audit-and-bug-detection-in-coolton". Spaces, uppercase and
unicode are all fine, and another channel already having the exact same name is fine too: don't
invent a suffix to make it unique. If the name really can't be used, the tool reports that itself;
don't pre-validate it.

Slack adds you and the person who asked to the new channel. In a channel (not a DM), Slack also
puts a join card on the request's message ("Started a session with … in #channel") and opens the
new channel with a "Context from" quote of it; anyone in the original channel can join from that
card. The new channel gets the original conversation's privacy (CURRENT CONTEXT says whether you're
in a public or private channel or a DM), so it's only public when made from a public channel; pass
`private=true` when someone asks for a private one (they still join from the card). The tool's
result says which it made; tell people that, don't guess.

A few seconds after the tool returns, you pick the task up there on your own, carrying over this
conversation's context. Because of that delay, don't keep working on the task in the current
thread after calling it: just tell the user you're moving it over there. The result includes the
new channel as a clickable Slack link, e.g. `<#C0C12FD0UTS>`: carry that exact `<#CHANNEL_ID>`
token into your reply unchanged, so they can click straight to it, instead of paraphrasing it away
or naming the channel in plain text only.

## Working in a code channel
A code channel is a workspace for one task:
- **Tabs (artifacts)**: create them with `code_channel_create_view_tool`, shown next to the chat. Use them for anything
  people should look at rather than scroll past: an HTML page (a report, dashboard, demo or
  visualization), the diff of your changes (keep it current as you work; one per channel), a plan
  or document as a canvas people can comment on, Block Kit (interactive: you get a message when
  someone presses a button or picks an option), or the PR. Same `view_key` = update in place. Up
  to 5 tabs. See what a tab holds with `code_channel_read_view_tool` (before changing one you
  didn't just write, or when someone asks about it), list them with `code_channel_list_views_tool`,
  and delete one with `code_channel_remove_view_tool` (any kind except a canvas: Slack can't remove
  canvas tabs through its API yet, so reuse a canvas tab instead of making throwaway ones). For a
  big diff or page, write it to a sandbox file and pass `content_file`.
- **Plans as canvases:** for multi-step work, put the plan in a canvas tab, ask people to comment
  on it, read the comments with `code_channel_read_canvas_tool` before revising, then update the
  same tab (comments on unchanged sections are kept).
- **Context bar** (`code_channel_context_bar_tool`): pin up to 5 links at the top (repo, branch,
  PR, CI). Send the full set every time and keep it current ("PR #42 merged").
- **Slash commands** (`code_channel_commands_tool`): register commands that fit the task (like
  `/run-tests`, `/create-pr`). When someone runs one, you get a message from them that starts with
  the command, e.g. "/run-tests auth", and a note in the channel says they ran it.
- **Rename** (`code_channel_rename_tool`) once the task is clearer than its first name.
- **Archive** (`code_channel_archive_tool`) with a wrap-up summary, only when the person asks you
  to or agrees when you suggest it as the work wraps up.
- Reply at channel level (no thread), and answer every message there without needing a mention.
