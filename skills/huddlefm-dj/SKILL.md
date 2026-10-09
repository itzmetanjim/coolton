---
name: huddlefm-dj
description: 'DJ a HuddleFM listening session in a Slack huddle (playback, queue, volume) with huddlefm_request_control_tool and huddlefm_command_tool. Load before using either tool.'
---

# HuddleFM DJ

HuddleFM runs shared listening sessions in Slack huddles. You can DJ one: control playback, manage
the queue, adjust volume, by DMing JSON commands to the HuddleFM bot user (you're allowlisted for
this) and reading its threaded JSON reply. Full spec:
https://github.com/ingoau/huddlefm/blob/main/docs/bot-api.md

## Getting the tools
`huddlefm_request_control_tool` and `huddlefm_command_tool` aren't in your tool list. If a
`search_tools` result with them isn't already in the conversation, run `search_tools("huddlefm")`,
then call them with `call_tool`.

## Flow
1. `huddlefm_request_control_tool(channel, permissions)`: ONCE per session. `channel` must be the
   huddle's source channel, controls channel, or companion channel (ask the user if you don't know
   which channel a session lives in, don't guess). Only request the permissions you actually need
   for what was asked (see the table below), never request everything by default.
2. There's no immediate success reply: the session **host** gets an approval prompt in their Slack
   client and can take up to 5 minutes to respond, or never respond. Tell the user you're waiting
   on the host; don't poll or block for it. If a command below is tried before they approve, it
   fails with `not_granted`, that means "still waiting" or "declined", not a bug.
3. Once approved, use `huddlefm_command_tool(command_type, channel?, fields?)` for everything else.
   `channel` is only needed if you hold grants on more than one session at once.

## Commands
`command_type` | permission needed | `fields` JSON keys:
- `status` | any grant | (none) | current playback state, queue, and `yourCapabilities`
- `search` | `add` or `add-bulk` | `{"query": "..."}` | returns `results: [{label, reference}]`
- `add` | `add` / `add-bulk` | `{"reference": "..."}` (a `reference` from `search`, or a media URL)
- `remove` | `remove-own` / `manage-queue` | `{"trackId": "..."}`
- `move` | `manage-queue` | `{"trackId": "...", "direction": "up"|"down"}` or `{"trackId": "...", "playNext": true}` or `{"trackId": "...", "position": 1}`
- `clear` | `clear` | (none)
- `skip` / `previous` | `skip` | (none)
- `toggle` / `pause` / `resume` | `pause` | (none)
- `seek` | `skip` | `{"seconds": N}` (relative; negative rewinds)
- `volume` | `volume` | `{"percent": 0-100}`
- `settings` | `configure-settings` | any of `displayMode`, `autoplay`, `transitionMode`, `anchorEnabled`
- `end` | `end-session` | (none)
- `release_control` | your own grant | (none)

## Limits
Never claim/transfer host, change the permission preset, or touch personal scrobbling: HuddleFM
doesn't allow granting those to a bot at all. Grants don't survive a HuddleFM restart: if commands
that used to work start failing with `not_granted`, request control again rather than assuming
you did something wrong.
