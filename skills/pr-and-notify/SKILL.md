---
name: pr-and-notify
description: 'Open a GitHub pull request against the coolton repo from the sandbox AND DM KitKat (U0B2VTYER33) about it. USE FOR: fixing a bug in coolton's own code, make a PR, open a pull request, commit this fix, or any time coolton or kevinton detects a problem in the coolton repo. Always pairs the PR with a DM to the repo owner. DO NOT USE FOR: editing files outside the repo, or changes the user explicitly wants kept local.'
---

# PR + Notify (KitKat)

Use this whenever coolton (or kevinton on its behalf) needs to push a fix to the coolton repo.
It makes the branch, commits, pushes to the **fork**, opens the PR against the main repo, then DMs
KitKat. Do all git/gh work INSIDE the Linux sandbox (run_linux_command) — the repo is pre-cloned at
`/home/user/work/coolton` and `gh` is already authenticated via the host proxy (you are
`coolton-agent`). Do NOT run `gh auth login` or try to read the token.

## IMPORTANT — mental model (do NOT get confused)
- coolton commits to its OWN repo and opens PRs as the **normal, intended workflow**. A commit
  authored by `coolton@agentmail.to` is NOT a rogue/bug — it is the expected PR flow. Do not treat
  self-commits as something to "guardrail" or "fix" unless the user explicitly asks.
- coolton-agent has **NO push access to `itzmetanjim/coolton`** (the main repo). Pushing there
  fails with 403. This is correct and expected. Always push to the **fork** instead.

## KitKat's Slack user id
- `U0B2VTYER33` — repo owner. Every PR must be announced to them via DM.

## The `gh` wrapper gotcha (read this before any `gh` call)
- In the sandbox, `gh` is a wrapper at `/home/user/bin/gh` that injects
  `--hostname "$COOLTON_GH_PROXY_HOST"` on EVERY call. Most subcommands tolerate this, but
  `gh pr create` / `gh pr list` etc. have NO `--hostname` flag and will die with
  `unknown flag: --hostname`.
- **Fix:** call the real binary directly: `/usr/local/bin/gh ...`. The proxy still routes HTTP via
  the `HTTPS_PROXY` env, so auth works fine. (Do NOT `git push` through the wrapper either — use
  plain `git` with the `fork` remote, which already points at the proxied fork URL.)

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
   This commit being authored by `coolton@agentmail.to` is normal — see "mental model" above.
3. **Push to the FORK (not origin/main).** The fork `coolton-agent/coolton` already exists.
   ```bash
   cd /home/user/work/coolton
   # add the fork remote once if missing
   git remote add fork https://github.com/coolton-agent/coolton.git 2>/dev/null || true
   git push -u fork fix/<short-slug>
   ```
   If `git push` fails with a 403 on `itzmetanjim/coolton`, you pushed to `origin` by mistake —
   push to `fork` instead.
4. **Open the PR from the fork against the main repo.** Use the real gh binary (see wrapper note).
   ```bash
   cd /home/user/work/coolton
   /usr/local/bin/gh pr create \
     -R itzmetanjim/coolton \
     -H "coolton-agent:fix/<short-slug>" \
     -B main \
     -t "fix: <short>" \
     -F /tmp/prbody.md
   ```
   Write the body to a file first (heredocs sometimes confuse the wrapper). Example body file:
   ```
   ## What
   <one line on the problem>

   ## Why
   <root cause / how it broke>

   ## Fix
   <what changed>
   ```
   Capture the PR URL from the `gh pr create` output (it prints the github.com URL).
5. **DM KitKat.** After the PR is created, send a Slack DM (this is a Slack action, NOT a sandbox
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
- Never force-push to `main` (either repo). Always a feature/fix branch.
- Push to the **fork** (`coolton-agent/coolton`), never directly to `itzmetanjim/coolton` — that
  push is denied by design.
- Always call `/usr/local/bin/gh` (not the wrapped `gh`) for `pr create` and similar subcommands.
- Always do step 5. A PR without the KitKat DM is incomplete.
- If `git push` or `gh pr create` fails, report the error verbatim — do NOT silently give up, and
  do NOT treat a normal self-commit as a bug.
