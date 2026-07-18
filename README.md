# coolton

coolton is a Slack agent that gets better without being re-prompted. a separate silent agent
(kevinton) reads every conversation after it happens & turns the reusable parts into skills.
the next time a similar request lands, coolton loads that skill on its own.

this isn't a prompt trick. the main agent stays calm & only touches skills when you ask. the
self-improvement lives entirely in kevinton, running in a background thread that can't block or
break the answer.

## how it works

```
slack message
   │
   ▼
coolton (pydantic ai agent) ── answers, shows a plan card + the model it used
   │  result.all_messages()
   ▼
kevinton (daemon thread) ── list_skills → find_skills → create_skill / install_skill
   │  writes to skills/
   ▼
skills/  ── scanned by skillscapability on every turn (auto_reload=true)
```

kevinton's default is capture. for any non-trivial turn (tool calls, research, a comparison,
multi-step reasoning) it checks the existing catalog & writes a new skill if one fits. it skips
two things only: bare social replies & one-line factual lookups ("hi", "what is 1+1?").

## features

- **skills system** — curated skills in `skills/` (committed) plus CLI-installed skills in
  `.agents/skills/` (gitignored, still scanned). both load automatically.
- **self-improvement (kevinton)** — silent post-hoc skill capture after every turn.
- **multi-provider fallback** — one provider order (`agent/agent.py` → `get_runtime_model` /
  `_build_provider_order`) tries providers in sequence so the bot keeps answering when one is
  down or rate-limited.
- **plan card** — coolton shows a short plan & the model that answered (`agent/plan_block.py`).
- **thread memory** — multi-turn context inside a slack thread (`thread_context/`).
- **tools** — web search, image gen, vision, reminders, thread summarization, mermaid diagrams,
  a code/data sandbox, & more (`agent/tools/`).

## project layout

| path | what it is |
| --- | --- |
| `app.py` / `app_oauth.py` | entry points. `app.py` runs in socket mode; `app_oauth.py` in http/oauth mode. |
| `agent/agent.py` | the pydantic ai agent, system prompt, & the model/provider selection logic. |
| `agent/kevinton.py` | the silent skill-capture agent + the `spawn_kevinton()` daemon hook. |
| `agent/deps.py` | `agentdeps` runtime context (slack client, model used, plan state). |
| `agent/plan_block.py` | plan card rendering (`model: <provider> / <model>`). |
| `agent/tools/` | agent tools (web search, vision, image gen, reminders, sandbox, etc.). |
| `listeners/` | slack event/action/view handlers (`events/`, `actions/`, `views/`). |
| `thread_context/` | in-memory per-thread conversation history store. |
| `skills/` | curated, version-controlled skills (kevinton writes here). |
| `.agents/skills/` | CLI-installed skills (gitignored, still scanned). |

## setup

### 1. slack app

create an app at [api.slack.com/apps/new](https://api.slack.com/apps/new) using
[`manifest.json`](./manifest.json). install it to your workspace. copy the **bot user oauth
token** (`xoxb-...`) into `slack_bot_token`. make an **app-level token** with `connections:write`
& copy it into `slack_app_token`.

### 2. environment

```sh
cp .env.sample .env
```

set the provider keys you have. coolton reads these — only the ones you provide get used:

| variable | provider |
| --- | --- |
| `jams_api_key` | jams (kimi / minimax / etc.) |
| `hcai_api_key` | hcai gateway |
| `openrouter_api_key` / `openrouter_api_key_fallback` | openrouter |
| `anthropic_api_key` | anthropic |
| `openai_api_key` | openai |
| `groq_api_key` | groq |
| `google_api_key` | gemini |
| `mistral_api_key` | mistral |
| `cerebras_api_key` | cerebras |

the effective provider order lives in `agent/agent.py`. the first keyed provider in that order
wins; on failure it falls through to the next.

### 3. python environment

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. run

```sh
python3 app.py          # socket mode (local dev)
# or
python3 app_oauth.py    # http / oauth mode
```

## using coolton

- **direct messages** — message the bot; it replies in-thread & keeps context.
- **channel @mentions** — `@coolton <message>`; it responds in-thread.
- **app home** — welcome + instructions.
- **assistant panel** — add the agent via slack's _add agent_.

ask coolton to use or make skills directly: _"create a skill for summarizing standups"_ or
_"use the summarize-channel skill on #general"_.

## skills

skills are markdown files with yaml frontmatter (`name`, `description`) plus instructions. they
live in `skills/` (curated, committed) & `.agents/skills/` (CLI-installed, gitignored). the
agent discovers them via `skillscapability(directories=["skills", ".agents/skills"],
auto_reload=true)`.

the `manage-skills` skill covers the whole lifecycle: find skills in the ecosystem, install them,
& create/edit/rename/delete coolton's own catalog.

## deployment

a `coolton.service` systemd unit runs the bot as the `tanjim` user. `deploy.sh` ships updates;
the service reads `.env` via `environmentfile`.

```sh
sudo systemctl restart coolton
```

## development

```sh
ruff check      # lint
ruff format     # format
pytest          # tests
```

## contributing & debugging

the architecture splits bugs into contained halves. when something breaks, find which half first.

### where things live

| concern | file | notes |
| --- | --- | --- |
| user-facing answer | `agent/agent.py` (`run_agent`) | the main pydantic ai agent. |
| model selection | `agent/agent.py` (`get_runtime_model`, `_build_provider_order`) | single source of truth for provider/model. change the order here only. |
| silent skill capture | `agent/kevinton.py` (`spawn_kevinton`) | runs in a daemon thread after each turn. |
| inbound slack events | `listeners/events/message.py`, `app_mentioned.py` | call `spawn_kevinton` after coolton answers. |
| plan card / model line | `agent/plan_block.py` | renders `model: <provider> / <model>`. |
| skills on disk | `skills/`, `.agents/skills/` | scanned by `skillscapability(auto_reload=true)`. |

### "the bot didn't answer"

check the service is up (`systemctl status coolton`) & read live logs (`journalctl -u coolton
-f`). most failures here are model/provider errors. the plan card shows `model: <provider> /
<model>` — if a key is missing or a provider is rate-limited, the agent falls through to the next
in `_build_provider_order`. if *all* fail, the answer fails. confirm at least one provider key is
set in `.env`.

### "the bot answered but kevinton didn't create a skill"

kevinton runs best-effort in a background thread — it can never block or break your answer. if it
errors, only kevinton fails (logged separately), coolton is unaffected. trivial turns ("hi",
"what is 1+1?") are skipped on purpose. find kevinton's trace in `journalctl -u coolton`. it
writes to `skills/` only; if a skill didn't appear, check that the turn was non-trivial (tool
calls / research / a comparison).

### "a skill is wrong / stale"

skills are plain markdown in `skills/`. edit the `skill.md` directly, commit, & it's picked up on
the next turn (auto-reload). to remove one, delete the directory.

### invariants (don't break these)

- **never** add prompt enforcement telling coolton to "self-improve" — that was tried & didn't
  work. self-improvement lives only in kevinton.
- **never** let kevinton edit app code or the repo. it's restricted to `skills/` via the hardened
  skill tools.
- **never** commit `.env`, runtime json (`conversations.json`, etc.), or `byok_key.bin`. they're
  gitignored. scan before committing if unsure:
  `git ls-files | grep -ie '\.env|byok_key|conversations|reminders'`.
- model selection goes through `get_runtime_model` / `_build_provider_order` — don't hardcode a
  provider elsewhere.

## note on commit messages

commit messages in this repo are generated by opencode. the bot writes them because hand-written
ones here were unreadable — no structure, no context, no sign of what actually changed. if you
edit commits by hand, keep them to a single scannable line plus a short body. don't bury the
change under prose.
