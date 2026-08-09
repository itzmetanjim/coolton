---
name: pr-and-notify
description: "Open a GitHub pull request against the coolton repo from the sandbox AND DM KitKat (U0B2VTYER33) about it. USE FOR: fixing a bug in coolton's own code, make a PR, open a pull request, commit this fix, or any time coolton or kevinton detects a problem in the coolton repo. Always pairs the PR with a DM to the repo owner. DO NOT USE FOR: editing files outside the repo, or changes the user explicitly wants kept local. IMPORTANT: pushing to main does NOT deploy anything — only a merged+accepted PR into itzmetanjim/coolton ever reaches the live bot."
---

# PR + Notify (KitKat)

Use this whenever coolton (or kevinton on its behalf) needs to fix the coolton repo.
It makes a branch, commits, pushes to the coolton-agent fork, opens a PR into
`itzmetanjim/coolton`, then DMs KitKat. Do all git/gh work INSIDE the Linux sandbox
(run_linux_command) — the repo is pre-cloned at `/home/user/work/coolton` and `gh` is already
authenticated via the host proxy (you are `coolton-agent`). Do NOT run `gh auth login` or try to
read the token.

## Reality check: what a push does and does NOT do
- The LIVE bot runs from `https://github.com/itzmetanjim/coolton`, pulled onto the host server and
  restarted by KitKat. That repo is the ONLY code that matters.
- You are `coolton-agent` and have NO write access to `itzmetanjim/coolton`. `git push` to `origin`
  (which points at itzmetanjim/coolton) will be REJECTED — that is expected.
- Pushing to `main` — on the fork (`coolton-agent/coolton`) or anywhere else — does NOTHING. It is
  not a deploy, the feature does not go live, and the running bot is unaffected. NEVER push to main.
- Your fix only reaches the live bot when KitKat merges your PR into `itzmetanjim/coolton` AND pulls
  it on the host. Until then it is a proposal. Say "PR opened" — never "done", "deployed", or "live".

## KitKat's Slack user id
- `U0B2VTYER33` — repo owner. Every PR must be announced to them via DM.

## Steps (run in the sandbox via run_linux_command)

1. **Branch off the latest origin/main.** Don't trust your local `main` — it can be stale.
   ```bash
   cd /home/user/work/coolton
   git fetch origin
   git checkout -b fix/<short-slug> origin/main
   # ... make your edits with run_linux_command (e.g. sed/tee/cat heredocs) ...
   ```
2. **Commit.** Keep it focused; write a real message.
   ```bash
   cd /home/user/work/coolton
   git add -A
   git commit -m "fix: <what and why>"
   ```
3. **Push the branch to your fork, then open the PR into itzmetanjim/coolton.**
   ```bash
   cd /home/user/work/coolton
   git remote add fork https://github.com/coolton-agent/coolton.git   # once; "already exists" is fine
   git push -u fork fix/<short-slug>
   gh pr create --repo itzmetanjim/coolton --title "fix: <short>" --body "$(cat <<'EOF'
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
5. **Report accurately.** Tell the user the PR is OPEN and awaiting KitKat's review, merge, and pull
   on the host. The change is NOT live yet — do not say it's deployed, shipped, or live.

## Rules
- NEVER push to `main`, on any remote. Only push fix branches to the `coolton-agent/coolton` fork.
- A pushed branch or a push to `main` is NOT a deploy and does NOT go live. Only an accepted PR
  (merged into itzmetanjim/coolton + pulled by KitKat on the host) ships code.
- If the sandbox has uncommitted changes you didn't make, stash or discard before branching.
- Always do step 4. A PR without the KitKat DM is incomplete.
- If `git push` or `gh pr create` fails, report the error verbatim — do NOT silently give up.
