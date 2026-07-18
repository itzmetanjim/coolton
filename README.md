# coolton

A self-improving Slack agent built on [Bolt for Python](https://docs.slack.dev/tools/bolt-python/) and [Pydantic AI](https://ai.pydantic.dev/).

coolton answers questions in Slack like a normal bot — but it gets better over time without
being re-prompted. After every real conversation turn, a **separate silent agent (kevinton)**
reads the transcript and, if the turn contained reusable knowledge, captures it as a new
**skill**. The next time a similar request comes in, coolton automatically loads that skill.

This is achieved by **structural separation**, not by nagging the main agent:

- **coolton** — stays calm. It only uses, installs, or creates skills when you explicitly ask.
- **kevinton** — runs fire-and-forget after each turn, watches for reusable patterns, and writes
  new skills to `skills/`. It has read-only access to the Slack conversation and the skill
  sandbox; it never edits app code or the repo.

## How it works

```
Slack message
      │
      ▼
coolton (Pydantic AI agent) ── answers, shows a plan card + model used
      │  result.all_messages()
      ▼
kevinton (daemon thread) ── list_skills → find_skills → create_skill/install_skill
      │  writes to skills/
      ▼
skills/  ── scanned by SkillsCapability on every turn (auto_reload=True)
```

kevinton's default posture is **capture**: for any non-trivial turn (tool calls, research,
comparisons, multi-step reasoning) it checks the existing skill catalog and creates or updates a
skill as needed. It only skips bare social replies and one-line factual lookups (e.g. `"hi"`,
`"what is 1+1?"`).

## Features

- **Skills system** — curated skills in `skills/` (committed) plus CLI-installed skills in
  `.agents/skills/` (gitignored but functional). Both are scanned automatically.
- **Self-improvement (kevinton)** — silent post-hoc skill capture after every turn.
- **Multi-provider model fallback** — a unified provider order (`agent/agent.py` →
  `get_runtime_model` / `_build_provider_order`) tries providers in sequence so the agent keeps
  working even when a provider is down or rate-limited.
- **Plan card** — coolton shows a lightweight plan and the model that answered
  (`agent/plan_block.py`).
- **Thread memory** — multi-turn context within a Slack thread (`thread_context/`).
- **Tools** — web search, image generation, vision, reminders, thread summarization, Mermaid
  diagrams, a code/data sandbox, and more (see `agent/tools/`).

## Project layout

| Path | What it is |
| --- | --- |
| `app.py` / `app_oauth.py` | Entry points. `app.py` runs in Socket Mode; `app_oauth.py` runs in HTTP/OAuth mode. |
| `agent/agent.py` | The Pydantic AI agent, system prompt, and the unified model/provider selection logic. |
| `agent/kevinton.py` | The silent skill-capture agent + `spawn_kevinton()` daemon hook. |
| `agent/deps.py` | `AgentDeps` runtime context (Slack client, model used, plan state). |
| `agent/plan_block.py` | Plan card rendering (`Model: <provider> / <model>`). |
| `agent/tools/` | Agent tools (web search, vision, image gen, reminders, sandbox, etc.). |
| `listeners/` | Slack event/action/view handlers (`events/`, `actions/`, `views/`). |
| `thread_context/` | In-memory per-thread conversation history store. |
| `skills/` | Curated, version-controlled skills (kevinton writes here). |
| `.agents/skills/` | CLI-installed skills (gitignored, still scanned). |

## Setup

### 1. Slack app

1. Create an app at [api.slack.com/apps/new](https://api.slack.com/apps/new) using
   [`manifest.json`](./manifest.json).
2. Install it to your workspace.
3. Copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.
4. Create an **App-Level Token** with `connections:write` and copy it into `SLACK_APP_TOKEN`.

### 2. Environment

```sh
cp .env.sample .env
```

Then set the provider keys you have. coolton reads these (only the ones you provide are used):

| Variable | Provider |
| --- | --- |
| `JAMS_API_KEY` | JAMS (Kimi / MiniMax / etc.) |
| `HCAI_API_KEY` | HCAI gateway |
| `OPENROUTER_API_KEY` / `OPENROUTER_API_KEY_FALLBACK` | OpenRouter |
| `ANTHROPIC_API_KEY` | Anthropic |
| `OPENAI_API_KEY` | OpenAI |
| `GROQ_API_KEY` | Groq |
| `GOOGLE_API_KEY` | Gemini |
| `MISTRAL_API_KEY` | Mistral |
| `CEREBRAS_API_KEY` | Cerebras |

The effective provider order is defined in `agent/agent.py`. The first keyed provider in the
order is used; on failure it falls through to the next.

### 3. Python environment

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 4. Run

```sh
python3 app.py          # Socket Mode (local dev)
# or
python3 app_oauth.py    # HTTP / OAuth mode
```

## Using coolton

- **Direct Messages** — message the bot; it replies in-thread and keeps context.
- **Channel @mentions** — `@coolton <message>`; it responds in-thread.
- **App Home** — welcome + instructions.
- **Assistant Panel** — add the agent via Slack's _Add Agent_.

Ask coolton to use or create skills directly, e.g. _"create a skill for summarizing standups"_
or _"use the summarize-channel skill on #general"_.

## Skills

Skills are Markdown files with a YAML frontmatter (`name`, `description`) plus instructions.
They live in:

- `skills/` — curated and committed to git (this is where kevinton writes).
- `.agents/skills/` — installed via the skill CLI; gitignored.

The agent discovers them automatically via `SkillsCapability(
directories=["skills", ".agents/skills"], auto_reload=True)`.

## Deployment

A `coolton.service` systemd unit runs the bot as the `tanjim` user. Use the provided
`deploy.sh` to ship updates. The service reads `.env` via `EnvironmentFile`.

```sh
sudo systemctl restart coolton
```

## Development

```sh
ruff check      # lint
ruff format     # format
```

## Contributing & debugging

The architecture is deliberately split so a bug stays contained. When something breaks,
find which half it's in first:

### Where things live (mental model)

| Concern | File | Notes |
| --- | --- | --- |
| User-facing answer | `agent/agent.py` (`run_agent`) | The main Pydantic AI agent. |
| Model selection | `agent/agent.py` (`get_runtime_model`, `_build_provider_order`) | **Single source of truth** for which provider/model is used. Change the order here only. |
| Silent skill capture | `agent/kevinton.py` (`spawn_kevinton`) | Runs in a **daemon thread** after each turn. |
| Inbound Slack events | `listeners/events/message.py`, `app_mentioned.py` | After coolton answers, these call `spawn_kevinton`. |
| Plan card / model line | `agent/plan_block.py` | Renders `Model: <provider> / <model>`. |
| Skills on disk | `skills/`, `.agents/skills/` | Scanned by `SkillsCapability(auto_reload=True)`. |

### "The bot didn't answer"

1. Check the service is up: `systemctl status coolton`.
2. Read live logs: `journalctl -u coolton -f`.
3. Most failures here are **model/provider** errors. Look for which provider was tried
   (the plan card shows `Model: <provider> / <model>`). If a key is missing or a provider
   is rate-limited, the agent falls through to the next in `_build_provider_order` — if
   *all* fail, the answer fails. Confirm at least one provider key is set in `.env`.

### "The bot answered but didn't learn / kevinton didn't create a skill"

kevinton runs **best-effort in a background thread** — it can never block or break the user's
answer. If it errors, only kevinton fails (logged separately), coolton is unaffected.

- Trivial turns (`"hi"`, `"what is 1+1?"`) are intentionally skipped — that's not a bug.
- Find kevinton logs in `journalctl -u coolton` (look for kevinton trace lines).
- kevinton writes to `skills/` only. If a skill didn't appear, check disk writes there
  and that the turn was non-trivial (had tool calls / research / a comparison).

### "A skill is wrong / stale"

Skills are plain Markdown in `skills/`. Edit the `SKILL.md` directly, commit, and it's picked
up on the next turn (auto-reload). To remove one, delete the directory.

### Important invariants (don't break these)

- **Never** add prompt-based enforcement telling coolton to "self-improve" — that was tried
  and didn't work. Self-improvement lives only in kevinton.
- **Never** let kevinton edit app code or the repo. It is restricted to `skills/` via the
  hardened skill tools.
- **Never** commit `.env`, runtime JSON (`conversations.json`, etc.), or `byok_key.bin`.
  They're gitignored. Scan before committing if unsure:
  `git ls-files | grep -iE '\.env|byok_key|conversations|reminders'`.
- Model selection must go through `get_runtime_model` / `_build_provider_order` — don't
  hardcode a provider elsewhere.

### Running tests

```sh
pip install -e ".[test]"
pytest
```

## Notes

- `.env`, runtime JSON state (`conversations.json`, `reminders.json`, etc.), and `byok_key.bin`
  are gitignored and never committed.
- kevinton writes skills to disk but does **not** auto-commit them; review `skills/` and commit
  when you're happy with the capture.
