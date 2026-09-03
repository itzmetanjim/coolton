# coolton

coolton is a Slack agent that gets better without being re-prompted. a separate silent agent
(kevinton) reads every conversation after it happens & turns the reusable parts into skills.
the next time a similar request lands, coolton loads that skill on its own.

this isn't a prompt trick. the main agent stays calm & only touches skills when you ask. the
self-improvement lives entirely in kevinton, running in a background thread that can't block or
break the answer.

beyond self-improvement, coolton is also a full agent platform: a persistent per-thread E2B
sandbox for shell commands, code, a browser, and a GUI desktop; a multi-provider LLM fallback
chain that keeps answering when one provider is down; the ability to build & deploy other Slack
bots on request; scheduling, email, vision, image generation, and more.

## how it works

```
slack message
   │
   ▼
coolton (pydantic ai agent) ── answers, shows a plan card + live status + the model it used
   │  result.all_messages()
   ▼
kevinton (daemon thread) ── list_skills → find_skills → create_skill / install_skill
   │  writes to skills/ or .agents/skills/
   ▼
skills/  ── scanned by SkillsCapability on every turn (auto_reload=true)
```

every piece of untrusted or executable code coolton runs — shell commands, `code_mode` scripts,
a skill's own bundled scripts, a fetched skill package's installer — runs inside a disposable,
per-thread E2B sandbox, never on the host. the sandbox only ever gets short-lived, narrowly-scoped
credentials of its own (a per-sandbox GitHub token, an ephemeral tool-proxy token); real secrets
(Slack tokens, the GitHub PAT, provider API keys) never enter it.

kevinton's default is capture. for any non-trivial turn (tool calls, research, a comparison,
multi-step reasoning) it checks the existing catalog & writes a new skill if one fits. it skips
two things only: bare social replies & one-line factual lookups ("hi", "what is 1+1?").

## features

### self-improvement

- **kevinton** — a silent background agent that reads every completed turn and has two jobs:
  decide whether it's worth turning into a reusable skill, and watch for signs coolton just hit a
  bug in its own code. for the latter, it fixes the bug in its own disposable sandbox and opens a
  real pull request against the repo (branch, commit, push to the `coolton-agent` fork, PR, then a
  DM to the owner) — it has the full coolton toolset, not just the skill tools. the code on the
  live server only ever changes via a human merging that PR and pulling it there. runs in a daemon
  thread after coolton answers; if it errors, only kevinton fails — the user-facing answer is
  unaffected.
- **skills system** — curated skills in `skills/` (committed) plus CLI-installed skills in
  `.agents/skills/` (gitignored, still scanned). skills are markdown files with YAML frontmatter
  (name, description) plus instructions, and can bundle their own scripts, reference docs, and
  resources. coolton can create, rename, delete, and install skills on request; installing one
  fetches it from the [skills.sh](https://skills.sh) marketplace inside the sandbox and copies
  the whole validated directory back — never runs the installer on the host.

### reliability

- **multi-provider fallback** — one ordered chain (`providers.json` + `agent/agent.py`) tries
  HCAI, Anthropic, OpenAI, Groq, OpenRouter, Google, Mistral, OpenCode Zen, and Kilocode's free
  tier models in sequence, so the bot keeps answering when one provider is down, rate-limited, or
  out of credits.
- **fallback cache** — remembers which provider last worked and which ones are currently dead, so
  a busy thread doesn't re-discover the same outage on every turn.
- **provider probing** — a background job periodically tests every configured provider so the
  cache stays accurate between real requests.
- **mid-turn checkpoint/resume** — if a provider fails partway through a turn (after real tool
  calls already ran — messages posted, sandbox commands executed), the next provider resumes
  from that checkpoint instead of silently restarting the turn from scratch.
- **prompt caching** — Anthropic prompt caching and an OpenAI-compatible `prompt_cache_key`
  (keyed per thread) are both wired in, so a busy thread's repeat system-prompt/tool-definition
  tokens are cheap.
- **history compaction** — long-running threads get their older history folded into a summary
  once it would eat too much of the model's context window, instead of growing forever.
- **tool-call leakage detection** — some free-tier models leak a broken multi-tool-call attempt
  as raw text instead of a real tool call; that text is detected and dropped instead of being
  posted to the user as if coolton said it.
- **secret redaction** — every tool input/output and final response is scanned for known secret
  values before it can reach Slack, a log line, or a conversation trace.

### sandbox & code execution

- **`run_linux_command`** — a persistent, per-thread E2B Linux sandbox for arbitrary shell
  commands; survives across messages in the same thread.
- **`code_mode`** — write a Python program that calls coolton's own tools programmatically
  (`agent_tools.<tool_name>(...)`) from inside the sandbox, for loops/batch operations that would
  otherwise burn a model call per step.
- **GitHub access without the sandbox ever seeing the real token** — `github_proxy.py` runs on
  the host, issuing each sandbox a short-lived per-sandbox token that it rewrites to the real PAT
  before forwarding to github.com; `gh`/`git`/`curl` in the sandbox are transparently
  authenticated.
- **`computer_use`** — drives a real XFCE desktop (Xvfb + xdotool + scrot) inside the sandbox for
  native GUI apps, with a live view streamed to the thread.
- **agent-browser** — headless browser automation for websites, also with a live view stream.
- **data analysis** — CSV/SQL (via DuckDB) and general Python data analysis tools, plus generic
  sandbox file read/write/search/list and tar.gz extraction.
- **opencode** — install and run the OpenCode CLI inside the sandbox for coding tasks.
- **sandboxed skill scripts** — a skill's bundled scripts (`run_skill_script`) execute inside the
  sandbox too, never as a host subprocess, regardless of where the skill came from.

### Slack-native UX

- **plan / thinking card** — a live-updating block showing what coolton is doing, which tool it
  called, and which model answered (`agent/plan_block.py`).
- **live status pill** — the assistant-thread status (`assistant.threads.setStatus`) tracks the
  current tool call and refreshes automatically.
- **thread memory** — full multi-turn context inside a Slack thread (`thread_context/`).
- **thread engagement** — a top-level `@mention` joins the thread (coolton answers every
  subsequent reply); a mid-thread mention answers once without joining.
- **steering** — a message sent into a thread coolton is already working on gets folded into the
  run in progress instead of racing a second, separate turn.
- **`!stop`** — halts every coolton run in a thread immediately, keeping context of what was
  already done.
- **DMs, channel @mentions, and the Assistant panel** — all three surfaces are supported with the
  same turn pipeline.
- **App Home** — settings, custom instructions, BYOK endpoints, and MCP server management.
- **feedback buttons** — every response carries thumbs up/down feedback.
- **policy consent gate** — first-time users are prompted to opt in before coolton responds.

### vision & media

- **image analysis** — attached Slack images and sandbox screenshots are both understood by
  vision-capable models.
- **image generation** — generate and post images directly to the thread.
- **mermaid diagrams**, **whiteboard embeds** (tldraw), and **HTML embeds** — rendered and hosted
  via `coolton_web_helper.py`, a small file host + base64-HTML-decoder service.

### scheduling & automation

- **one-time reminders** — DM'd back to the requester after a delay.
- **recurring scheduled tasks** — cron-scheduled prompts that fire into their origin thread or
  channel (minimum 30-minute interval).

### build & deploy other Slack bots

coolton can create an entirely new Slack app (via `apps.manifest.create`), register its bot/app
tokens, and deploy it as a Cloudflare Worker with `wrangler deploy --temporary` — no Cloudflare
account or OAuth needed on the user's end. See the `slack-bot-deploy` and `cf-wrangler` skills.

### extensibility

- **BYOK** — bring your own OpenAI-compatible endpoint (App Home), encrypted at rest.
- **custom MCP servers** — register your own MCP server per-user (App Home) in addition to the
  built-in Slack MCP connection.
- **AgentMail** — coolton gets its own email inbox (create/list/read/send) via
  [AgentMail](https://agentmail.to).
- **subagents** — research/explore/summarize delegation for focused sub-tasks.

## project layout

| path | what it is |
| --- | --- |
| `app.py` | main entry point — Socket Mode, single workspace. |
| `app_oauth.py` | alternative entry point — HTTP mode with a multi-workspace OAuth installation store. |
| `oauth_server.py` | small standalone service that handles single-workspace reinstall (token exchange, `.env` update, service restart). |
| `coolton_web_helper.py` | file host + base64-HTML-decoder, used for embeds and sandbox output; runs alongside `app.py`. |
| `github_proxy.py` | host-side GitHub proxy — issues/revokes per-sandbox tokens, rewrites them to the real PAT for outbound requests. |
| `agent/agent.py` | the pydantic-ai agent: system prompt, every `@agent.tool`, and provider/model selection. |
| `agent/kevinton.py` | the silent skill-capture agent + the `spawn_kevinton()` daemon hook. |
| `agent/deps.py` | `AgentDeps` — per-turn runtime context (Slack client, model used, plan state, sandbox flags). |
| `agent/plan_block.py` | plan/thinking card + live status rendering, steering, tool-call-leakage guard. |
| `agent/provider_config.py`, `providers.json` | the provider/model fallback chain definition. |
| `agent/fallback_cache.py`, `agent/provider_probe.py` | dead-provider caching + background provider health checks. |
| `agent/sandbox_helpers.py`, `agent/sandbox_keepalive.py`, `agent/sandbox_store.py` | E2B sandbox lifecycle (create/reuse/recycle) per thread. |
| `agent/tool_proxy.py` | lets sandboxed code (`code_mode`) call coolton's own tools over HTTP, scoped to that thread's credentials. |
| `agent/desktop_helpers.py` | drives the XFCE desktop inside the sandbox for `computer_use`. |
| `agent/scheduler.py` | reminders + recurring cron scheduled tasks. |
| `agent/token_rotation.py` | rotates Slack CLI (xoxe) tokens automatically. |
| `agent/byok_store.py`, `agent/mcp_server_store.py` | encrypted per-user BYOK endpoints / custom MCP servers. |
| `agent/history_compaction.py` | folds old thread history into a summary once it gets long. |
| `agent/redact.py` | strips known secret values out of anything before it can leak. |
| `agent/policy_consent.py` | first-use opt-in gate. |
| `agent/tools/` | individual tool implementations (web search, vision, image gen, reminders, Slack info/search, AgentMail, computer_use, agent-browser stream, mermaid, data analysis, etc.). |
| `listeners/` | Slack event/action/view handlers (`events/`, `actions/`, `views/`). |
| `thread_context/` | conversation history store + training-log trace persistence. |
| `skills/` | curated, version-controlled skills (kevinton writes here). |
| `.agents/skills/` | CLI-installed skills (gitignored, still scanned). |
| `tests/` | pytest suite — one file per module, run before every change. |

## setup

### 1. slack app

create an app at [api.slack.com/apps/new](https://api.slack.com/apps/new) using
[`manifest.json`](./manifest.json). install it to your workspace. copy the **bot user oauth
token** (`xoxb-...`) into `SLACK_BOT_TOKEN`. make an **app-level token** with `connections:write`
& copy it into `SLACK_APP_TOKEN`.

### 2. environment

```sh
cp .env.sample .env
```

at least one AI provider key is required; only the ones you set get used. the effective order is
defined in `providers.json` (`agent/provider_config.py` reads it) — the first keyed provider
wins, falling through on failure:

| variable | provider |
| --- | --- |
| `HCAI_API_KEY` | HCAI gateway |
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `GROQ_API_KEY` | Groq |
| `OPENROUTER_API_KEY` / `OPENROUTER_API_KEY_FALLBACK` | OpenRouter |
| `GOOGLE_API_KEY` | Gemini |
| `MISTRAL_API_KEY` | Mistral |
| `OPENCODE_ZEN_API_KEY` | OpenCode Zen |
| `KILOCODE_API_KEY` | Kilocode (adds several free-tier models to the chain) |

everything else in `.env.sample` is optional and additive:

| variable | enables |
| --- | --- |
| `E2B_API_KEY` | the sandbox — shell commands, `code_mode`, `computer_use`, agent-browser, data analysis, skill scripts. required for all of it. |
| `EXA_API_KEY` | web search / `fetch_url`. |
| `AGENTMAIL_API_KEY` | AgentMail email tools. |
| `COOLTON_GH_USER` / `COOLTON_GH_TOKEN` | GitHub access from the sandbox, via `github_proxy.py` (the real token never enters the sandbox). |
| `SLACK_CONFIG_TOKEN` | creating/deploying other Slack bots (`create_slack_bot_tool`). |
| `BYOK_ENCRYPTION_KEY` | encrypts per-user BYOK endpoints (auto-generated to `byok_key.bin` if unset). |
| `COOLTON_USER_ID` / `SLACK_USER_TOKEN` | the "cooltonUser" helper account, used for user-token Slack API calls. |
| `COOLTON_BOT_ID` | coolton's own bot user id, so it recognizes self-mentions. |
| `KEVINTON_ENABLED` | set to `false` to disable kevinton entirely (default: on). |

### 3. python environment

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. run

```sh
python3 app.py                    # Socket Mode (single workspace, local dev)
python3 coolton_web_helper.py     # optional but needed for embeds/file hosting
# or, for multi-workspace HTTP/OAuth mode:
python3 app_oauth.py
```

in production, `coolton_start.sh` runs `app.py` and `coolton_web_helper.py` together and tears
both down on any exit path (systemd unit `coolton.service`). GitHub access runs as its own
service (`github-proxy.service`, `github_proxy.py`), and single-workspace reinstall runs as a
third (`oauth-server.service`, `oauth_server.py`).

## using coolton

- **direct messages** — message the bot; it replies in-thread & keeps context.
- **channel @mentions** — `@coolton <message>`; it responds in-thread and, on a top-level mention,
  joins the thread for subsequent replies.
- **app home** — settings, custom instructions, BYOK endpoints, MCP servers.
- **assistant panel** — add the agent via Slack's _add agent_.
- **`!stop`** — mention the bot with exactly `!stop` (or just `!stop` in a DM) to halt every
  coolton run in that thread immediately.

ask coolton to use or make skills directly: _"create a skill for summarizing standups"_ or
_"use the summarize-channel skill on #general"_. ask it to build things: _"make a website for
X"_, _"create a Slack bot that does Y"_, _"analyze this CSV"_, _"take a screenshot of this app
running in the sandbox"_.

## skills

skills are markdown files with YAML frontmatter (`name`, `description`) plus instructions, and
can bundle their own scripts, references, and resources. they live in `skills/` (curated,
committed) & `.agents/skills/` (CLI-installed, gitignored). the agent discovers them via
`SkillsCapability(directories=["skills", ".agents/skills"], auto_reload=true)`; a skill's own
scripts run inside the E2B sandbox, never on the host.

the `manage-skills` skill covers the whole lifecycle: find skills in the ecosystem, install them,
& create/edit/rename/delete coolton's own catalog.

## deployment

three systemd units run in production, all as the `tanjim` user:

| unit | runs | purpose |
| --- | --- | --- |
| `coolton.service` | `coolton_start.sh` → `app.py` + `coolton_web_helper.py` | the bot itself + embed/file hosting. |
| `github-proxy.service` | `github_proxy.py` | per-sandbox GitHub token issuance/proxying. |
| `oauth-server.service` | `oauth_server.py` | single-workspace reinstall handler. |

```sh
sudo systemctl restart coolton
sudo systemctl restart github-proxy   # after changing github_proxy.py
sudo systemctl restart oauth-server   # after changing oauth_server.py
```

`deploy.sh` is the Slack CLI deploy hook — updates `.env` with freshly-issued tokens and restarts
`coolton.service`.

## development

```sh
ruff check      # lint
ruff format     # format
pytest          # tests
```

run `ruff check` on changed files and the full `pytest` suite before every change — the CI-shaped
habit this repo is built around.

## contributing & debugging

the architecture splits bugs into contained halves. when something breaks, find which half first.

### where things live

| concern | file | notes |
| --- | --- | --- |
| user-facing answer | `agent/agent.py` (`run_agent`) | the main pydantic-ai agent. |
| model selection | `agent/agent.py` (`_run_with_provider_chain`), `agent/provider_config.py`, `providers.json` | single source of truth for provider/model order. change it here only. |
| silent skill capture | `agent/kevinton.py` (`spawn_kevinton`) | runs in a daemon thread after each turn. |
| inbound Slack events | `listeners/events/message.py`, `app_mentioned.py`, `turn.py` | share one turn pipeline (`run_agent_turn`), then call `spawn_kevinton`. |
| plan card / live status | `agent/plan_block.py`, `agent/thread_status.py` | renders the plan block and the `assistant.threads.setStatus` pill. |
| sandbox lifecycle | `agent/sandbox_helpers.py` | create/reuse/recycle the per-thread E2B sandbox. |
| skills on disk | `skills/`, `.agents/skills/` | scanned by `SkillsCapability(auto_reload=true)`. |

### "the bot didn't answer"

check the service is up (`systemctl status coolton`) & read live logs (`sudo journalctl -u
coolton -f`). most failures here are model/provider errors. the plan card shows `model:
<provider> / <model>` — if a key is missing or a provider is rate-limited, the agent falls
through to the next in the chain. if *all* fail, the answer fails with every provider's error
attached. confirm at least one provider key is set in `.env`.

### "the bot answered but kevinton didn't create a skill"

kevinton runs best-effort in a background thread — it can never block or break your answer. if it
errors, only kevinton fails (logged separately), coolton is unaffected. trivial turns ("hi",
"what is 1+1?") are skipped on purpose. find kevinton's trace in `journalctl -u coolton`. it
writes to `skills/` only; if a skill didn't appear, check that the turn was non-trivial (tool
calls / research / a comparison).

### "a skill is wrong / stale"

skills are plain markdown in `skills/` or `.agents/skills/`. edit the `SKILL.md` directly, commit,
& it's picked up on the next turn (auto-reload). to remove one, delete the directory (or ask
coolton to delete it).

### "the sandbox is stuck / a command hangs"

sandboxes are per-thread and reused across turns; a stale one is detected and recreated
automatically (`agent/sandbox_helpers.py`). if a specific command hangs, raise or disable its
`timeout` — `run_linux_command` clamps to 10–1800s by default, `0` disables it entirely.

### invariants (don't break these)

- **never** add prompt enforcement telling coolton to "self-improve" — that was tried & didn't
  work. self-improvement lives only in kevinton.
- **never** let anything change code files directly on the server. kevinton edits the coolton
  repo freely, but only inside its own disposable sandbox, and only ever ships a change as a PR
  (`pr-and-notify` skill, pushed to the `coolton-agent` fork — that account has no access to push
  to `itzmetanjim/coolton` at all, let alone `main`). the live host's repo only ever changes via a
  human merging a PR and pulling it there.
- **never** run untrusted or model-supplied code on the host. it belongs in the E2B sandbox —
  same rule that applies to `run_linux_command`, `code_mode`, `install_skill`, and skill scripts.
- **never** commit `.env`, runtime JSON (`conversations.json`, etc.), or `byok_key.bin`. they're
  gitignored. scan before committing if unsure:
  `git ls-files | grep -ie '\.env|byok_key|conversations|reminders'`.
- model selection goes through `providers.json` / `agent/provider_config.py` — don't hardcode a
  provider elsewhere.

## note on commit messages

commit messages in this repo are generated by the agent making the change. hand-written ones here
were unreadable — no structure, no context, no sign of what actually changed. if you edit commits
by hand, keep them to a single scannable line plus a short body. don't bury the change under
prose.
