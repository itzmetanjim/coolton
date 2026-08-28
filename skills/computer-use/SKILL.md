---
name: computer-use
description: 'Playbook for driving the XFCE desktop inside your sandbox with computer_use / computer_stream_tool — waiting for windows to paint, dismissing first-run dialogs, staying in the right focus, and when a GUI action is worth it over run_linux_command. USE FOR: clicking through a web UI, filling out a form, using a GUI app, visually verifying a rendered page. DO NOT USE FOR: anything a shell command or API call can do faster.'
---

# Computer Use

## Before you start
Computer use only works on a vision-capable turn — `computer_use` refuses otherwise and tells the
user to re-send with `[!WITH:vision]`. Don't attempt the desktop loop on a turn where you can't
see screenshots; there's no way to act correctly blind.

Ask yourself first: does this actually need a screen? `run_linux_command` (curl, a CLI, a script)
is faster and more reliable for anything it can do. Reach for the desktop only when the task is
inherently visual or GUI-only — clicking through a web flow with no API, using an app with no CLI,
or visually confirming something rendered correctly.

## The loop
1. `computer_use(action="screenshot")` first, always — see the actual current state before acting.
2. Decide the single next action from what you see. Coordinates are pixels *in that screenshot*,
   nothing else — if the window moved, resized, or a dialog appeared since your last screenshot,
   your old coordinates are stale.
3. Take another screenshot after any action that could have changed the screen (a click that
   opens something, typing that triggers autocomplete, a page navigation). Don't chain several
   blind actions in a row on the assumption of what happened.
4. Call `computer_stream_tool` once near the start of a session so the user has a live view. It's
   safe to call again if you think they lost the link.

## Timing
The desktop starts cold on first use (Xvfb + xfce4 boot inside `ensure_desktop`) — the very first
screenshot of a session may show a mostly-blank desktop or an in-progress panel; that's normal, not
a failure. After launching an app (`launch_app` or `open_url`), use `computer_use(action="wait",
amount=1500)` (milliseconds) before screenshotting again — GUI apps and web pages take real time to
paint, and screenshotting too early just shows you a half-drawn window.

## Common friction
- **First-run dialogs**: Firefox/Chromium may show a "set as default browser" or profile-picker
  dialog on first launch. Screenshot, find the dismiss/skip button, click it, then proceed.
- **Focus**: `type` sends keystrokes to whatever currently has keyboard focus, not to whatever you
  most recently clicked in a *previous* screenshot's coordinates. Click the actual input field
  first (from the latest screenshot), then type.
- **Losing track of window state**: if actions stop producing the expected result, take a fresh
  screenshot rather than guessing — a dialog, notification, or focus change may have interrupted
  the sequence you had in mind.
- **Apps available**: Firefox (`firefox-esr`), Chromium (`chromium`), LibreOffice, GIMP, the Xfce
  file manager (Thunar), a text editor (Mousepad), a calculator, an image viewer (Ristretto), a PDF
  viewer (Evince). `launch_app` takes the `.desktop` id (e.g. `firefox-esr.desktop` or just
  `firefox-esr` — both work via `gtk-launch`).

## Finishing
When you're done with a computer-use session, just stop calling the tools — the sandbox (and its
desktop) persists across the thread automatically, the same as `run_linux_command`'s sandbox. There
is no explicit teardown step.
