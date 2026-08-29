---
name: computer-use
description: 'Playbook for driving the XFCE desktop inside your sandbox with computer_use / computer_stream_tool — waiting for windows to paint, dismissing first-run dialogs, staying in the right focus, when a GUI action is worth it over run_linux_command, and when agent-browser should be used instead. USE FOR: using a native GUI app, visually verifying a rendered page. DO NOT USE FOR: websites or Electron apps (use agent-browser), or anything a shell command or API call can do faster.'
---

# Computer Use

## Before you start
Computer use only works on a vision-capable turn — `computer_use` refuses otherwise and tells the
user to re-send with `[!WITH:vision]`. Don't attempt the desktop loop on a turn where you can't
see screenshots; there's no way to act correctly blind.

Ask yourself first: does this actually need a screen? `run_linux_command` (curl, a CLI, a script)
is faster and more reliable for anything it can do. Reach for the desktop only when the task is
inherently visual or GUI-only.

## computer_use vs agent-browser
These overlap on "things with a screen" but are not interchangeable — pick wrong and you'll either
waste turns clicking through screenshots for something a DOM query would've done in one call, or
fight a GUI app that has no accessibility tree to snapshot.

- **A website, or an Electron app** (VS Code, Slack, Discord, Figma, Notion, Spotify) → use
  `agent-browser` (via `run_linux_command`; run `agent-browser skills get core` first). It drives
  Chrome/Chromium over CDP with accessibility-tree snapshots and `@eN` element refs — no pixel
  coordinates, no screenshot-then-guess loop, and it survives page reflows that would invalidate a
  computer_use screenshot's coordinates instantly.
- **A native GUI app with no browser involved** (LibreOffice, GIMP, the file manager, a text
  editor, a calculator) → `computer_use`. agent-browser has nothing to attach to here.
- **You need to confirm what something actually *looks like***, not just what's in the DOM (visual
  layout, rendering, a generated image, whether text is legible) → `computer_use`, even on a
  webpage — agent-browser's accessibility tree doesn't tell you what pixels the user would
  literally see.
- A web flow that genuinely resists agent-browser (something that only reacts to a real synthetic
  mouse/keyboard event, not automation) is the one case worth falling back to computer_use on a
  website. Try agent-browser first; don't default to computer_use for the web out of habit.

agent-browser runs headless by default (fast CDP automation, no rendering needed) — nothing to
watch even if you post a stream link. If the session is nontrivial and worth letting the user
watch live: call `agent_browser_stream_tool()` once first (it's the exact same view-only desktop
stream `computer_stream_tool` posts — agent-browser doesn't get its own separate viewer), then run
agent-browser itself with `--headed` and `DISPLAY=:0` so its Chrome window actually renders into
that desktop instead of staying invisible:
```
DISPLAY=:0 agent-browser open --headed https://example.com
```
Both are required together — `--headed` alone with no DISPLAY set (or a DISPLAY nothing is
listening on) still won't show up anywhere, and `agent_browser_stream_tool` without `--headed`
just shows an empty desktop while agent-browser works invisibly off-screen.

`run_linux_command` defaults to a 60s timeout — plenty for a quick command, not for a cold browser
session opening a page and waiting for it to load. Raise `timeout` up front (a few hundred
seconds, or up to 1800) for anything chained (`open ... && wait ...`) rather than finding out from
a "context deadline exceeded" error. `timeout=0` disables it entirely if you're confident the
command will finish on its own.

## The loop
1. `computer_use(action="screenshot")` first, always — see the actual current state before acting.
2. Decide the single next action from what you see. Coordinates are pixels *in that screenshot*,
   nothing else — if the window moved, resized, or a dialog appeared since your last screenshot,
   your old coordinates are stale.
3. Take another screenshot after any action that could have changed the screen (a click that
   opens something, typing that triggers autocomplete, a page navigation). Don't chain several
   blind actions in a row on the assumption of what happened.
4. **If that screenshot doesn't show the change you expected, don't assume the action failed and
   retry it.** Call `computer_use(action="wait", amount=1000)` (or longer for something heavy —
   an app launch, a page load) and screenshot again. A double click, a duplicate keystroke, or
   re-submitting a form because you acted too early is a much worse failure mode than pausing a
   beat. Only treat it as an actual failure once a wait-and-recheck still shows nothing changed.
5. Call `computer_stream_tool` once near the start of a session so the user has a live view. It's
   safe to call again if you think they lost the link.
6. Every `action="screenshot"` also posts that image to the thread as its own message (throttled
   to a few seconds apart), not just the live stream link — so take one every so often even when
   you don't strictly need it to decide your next move. This matters just as much during a
   `--headed` agent-browser session (same shared desktop, same throttle) — a long stretch of
   agent-browser commands with no screenshot in between means the user sees nothing until you're
   done, so check in with a screenshot periodically rather than only at the end.

## Sandbox keepalive
The sandbox always pauses at the end of a turn — this is intentional (one running sandbox per
active turn, not one that lingers idle for the whole thread), so re-post the stream link with
`computer_stream_tool`/`agent_browser_stream_tool` at the start of every new turn you want one in;
a link from a previous turn is dead.

Within a turn, once a stream is running the sandbox stays up for 120s after your last action
before auto-pausing on its own, and every action (a `run_linux_command` call, a `computer_use`
action) resets that countdown back to 120s. Without this, the sandbox would pause the instant the
command that started it returns — the viewer would see the browser for about 2 seconds and then go
dark, which is the opposite of a *live* view. You normally don't need to think about this at all;
it just works as long as you keep doing things. If you genuinely expect a longer idle stretch with
nothing running (waiting on the user to look at something, a page that takes a while with no
intermediate commands to reset the clock), call `set_sandbox_keepalive_tool(seconds=...)` to extend
it, or `seconds=0` to go back to pausing immediately once you're done with the stream.

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
