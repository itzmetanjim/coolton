---
name: pr-and-notify
description: "Open a GitHub pull request against the coolton repo from the sandbox AND DM KitKat (U0B2VTYER33) about it. USE FOR: fixing a bug in coolton's own code, make a PR, open a pull request, commit this fix, or any time coolton or kevinton detects a problem in the coolton repo. Always pairs the PR with a DM to the repo owner. DO NOT USE FOR: editing files outside the repo, or changes the user explicitly wants kept local."
---

# PR + Notify (KitKat)

Use this whenever coolton (or kevinton on its behalf) needs to push a fix to the coolton repo.
It makes the branch, commits, pushes, opens the PR, then DMs KitKat. Do all git/gh work INSIDE
the Linux sandbox (run_linux_command) — the repo is pre-cloned at `/home/user/work/coolton` and
`gh` is already authenticated via the host proxy (you are `coolton-agent`). Do NOT run `gh auth login`
or try to read the token.

## KitKat's Slack user id
- `U0B2VTYER33` — repo owner. Every PR must be announced to them via DM.

## Steps (run in the sandbox via run_linux_command)

1. **Branch + edit.** Work on a clear branch name.
   ```bash
   cd /home/user/work/coolton
   git checkout main
   git pull
   git checkout -b fix/<short-slug>
   # ... make your edits with run_linux_command (e.g. sed/tee/cat heredocs) ...
   ```
2. **Commit.** Keep it focused; write a real message.
   ```bash
   cd /home/user/work/coolton
   git add -A
   git commit -m "fix: <what and why>"
   ```
3. **Push + open PR.**
   ```bash
   cd /home/user/work/coolton
   git push -u origin fix/<short-slug>
   gh pr create --title "fix: <short>" --body "$(cat <<'EOF'
   ## What
   <one line on the problem>

   ## Why
   <root cause / how it broke>

   ## Fix
   <what changed>
   EOF
   )"
   ```
   Capture the PR URL from the `gh pr create` output.
4. **DM KitKat.** After the PR is created, send a Slack DM (this is a Slack action, NOT a sandbox
   command — use the `chat_postMessage` tool):
   - `channel`: `"U0B2VTYER33"`
   - `thread_ts`: omit (it's a DM, not a thread)
   - `text`: something like:
     ```
     opened a PR: <PR URL>
     <one-line on what it fixes>
     ```
   If you can't get the PR URL, say "opened PR for <title>" and include the branch name.

## Rules
- Never force-push to `main`. Always a feature/fix branch.
- If the sandbox has uncommitted changes you didn't make, stash or discard before branching.
- Always do step 4. A PR without the KitKat DM is incomplete.
- If `git push` or `gh pr create` fails, report the error verbatim — do NOT silently give up.
