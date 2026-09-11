---
name: slack-bot-deploy
description: "Deploy custom Slack bots on-demand using Cloudflare Workers and wrangler --temporary. Use when the user wants to create a new Slack bot, implement a slash command, build a custom Slack integration, or deploy any Slack app to the cloud. Triggers include 'make a bot', 'create a slash command', 'deploy a slack bot', 'build a slack app', or any request that involves writing a Slack bot and hosting it."
---

# Deploy Slack Bots with Wrangler

Deploy custom Slack bots (Cloudflare Workers) on demand without a Cloudflare account.

## Overview

This skill enables coolton to spin up entirely new Slack bots from scratch. The order below is
NOT arbitrary — it exists because Slack verifies `event_subscriptions.request_url` live (a
challenge/response handshake) before it will accept it, so that URL cannot be set until the
Worker is already deployed and answering requests. Trying to set it upfront with a placeholder
URL will fail. Two manifest passes are required:

1. **Create the app with a MINIMAL manifest** — name, bot user, OAuth scopes only. No
   `slash_commands` or `event_subscriptions` yet (`create_slack_bot_tool`). This returns a UUID
   that tracks the bot internally, plus the signing secret and an OAuth install URL.
2. **A human installs the app** by visiting the OAuth URL. The bot token is captured and
   registered AUTOMATICALLY the moment they finish — `create_slack_bot_tool` bakes coolton's own
   OAuth callback into the app's redirect URLs, so there's no token to copy/paste. Poll
   `check_bot_install_status_tool(uuid)` until it reports `installed`.
3. ~~Register the bot token~~ — not needed anymore, step 2 does it automatically.
   `register_bot_tokens_tool` still exists as a manual fallback (e.g. the user hands you a token
   unprompted, or the automatic capture fails for some reason).
4. **Clone `coolton-agent/slack-bot-template`** and customize it (TypeScript, using
   `slack-cloudflare-workers` — NOT `@slack/bolt`, which has no Cloudflare Workers support; see
   the workflow below for why, and why a verified-working template beats writing this from
   scratch each time).
5. **Deploy with `wrangler_bot_deploy_tool`** — it retrieves the bot's tokens from the UUID,
   writes them to a temporary `.env_slack`, runs `npx wrangler@latest deploy --temporary --secrets-file .env_slack`,
   then deletes the file immediately. Tokens are never exposed. This gives you the real,
   already-live Worker URL.
6. **Update the manifest with the real URL** (`update_slack_bot_manifest_tool`) — NOW that the
   Worker is live and correctly signing-secret-configured, Slack's verification challenge for
   `event_subscriptions.request_url` succeeds.

## Required Environment

- `SLACK_CONFIG_TOKEN` — a Slack App Configuration Token (generate at
  https://api.slack.com/apps under "Your App Configuration Tokens"). Used by `create_slack_bot_tool`
  and `update_slack_bot_manifest_tool`.
- `wrangler` — assumed available via `npx wrangler@latest` in the sandbox.

## Tools (exact names as exposed to the agent)

- `create_slack_bot_tool(manifest: dict)` — Creates the Slack app. Returns UUID, app_id, and an
  OAuth authorize URL that already carries coolton's own callback + a signed state, so the install
  is captured automatically. Stores everything in an internal JSON store on the HOST (not inside
  the sandbox — see Security Notes).
- `check_bot_install_status_tool(uuid)` — Poll this after sharing the OAuth URL. Returns
  `installed` once the human has finished installing and the bot token is registered, or
  `not_installed` while still waiting.
- `register_bot_tokens_tool(uuid, bot_token, app_token="")` — Manual FALLBACK only; installs are
  captured automatically (see above). `bot_token` (xoxb-) is required if you do need it.
  `app_token` (xapp-) is OPTIONAL — it's a Socket Mode-only credential generated manually in the
  app's Basic Information page, not via OAuth install, so omit it entirely for the HTTP-mode
  Workers this skill deploys.
- `update_slack_bot_manifest_tool(uuid, manifest)` — Updates an already-created app's manifest
  (`apps.manifest.update`). The manifest passed REPLACES the app's entire config, so it must
  include every field, not just the one URL you're changing. Use this after deploy to wire in the
  real Worker URL.
- `wrangler_bot_deploy_tool(uuid, working_dir, additional_flags)` — Deploys the Worker.
  Reads tokens from the internal store by UUID, never exposes them.

## Complete Workflow Example: `/calculate` Slash Command Bot

### 1. Build the MINIMAL manifest and create the app

No `slash_commands` or `event_subscriptions` yet — those need a live URL, which doesn't exist
until after deploy (step 5). List every scope the Worker code will actually call now (Slack
rejects calls with `missing_scope` otherwise); scopes are independent of any URL and can be set
upfront:

```json
{
  "display_information": {
    "name": "Calculator Bot",
    "description": "Evaluates expressions via /calculate"
  },
  "features": {
    "bot_user": {
      "display_name": "Calculator Bot",
      "always_online": true
    }
  },
  "oauth_config": {
    "scopes": {
      "bot": ["commands", "chat:write", "app_mentions:read"]
    }
  },
  "settings": {
    "org_deploy_enabled": false,
    "socket_mode_enabled": false,
    "token_rotation_enabled": false
  }
}
```

Call `create_slack_bot_tool(manifest=my_manifest)`. You'll get back JSON with:
- `uuid` — save this; every later step is keyed by it.
- `oauth_authorize_url` — a human (the workspace admin, or whoever asked for this bot) must visit
  this to install the app. Tell them to click it; once they finish, the bot token is captured and
  registered automatically — nothing to copy/paste, no page to dig a token out of.

### 2. Wait for the install

Poll `check_bot_install_status_tool(uuid=<uuid>)` (space it out — every 10-20s is plenty) until it
reports `installed`. If it's taking a while, that just means the human hasn't clicked through yet;
nudge them rather than retrying faster. Only fall back to `register_bot_tokens_tool` if the user
says the automatic capture didn't work (e.g. they hit an error page) or hands you a token
unprompted — and even then, do NOT pass an `app_token`: Socket Mode is off in this manifest, so no
`xapp-` token exists to give it.

### 3. Clone the template and write the bot logic

Don't scaffold a Worker project or write the Slack wiring from scratch — clone
[`coolton-agent/slack-bot-template`](https://github.com/coolton-agent/slack-bot-template), a
verified-working starting point (confirmed: typechecks, actually deploys with
`wrangler deploy --temporary`, and its signature verification / `url_verification` handshake /
slash-command routing were all confirmed against a real deployed Worker before it was published).
It already uses **`slack-cloudflare-workers`** correctly (NOT `@slack/bolt` — Bolt's `App` class
has no Cloudflare Workers support: no `.receive(request, env)` method, and its default receiver
needs a Node/Express server that doesn't run in the Workers V8 isolate at all).

```bash
# In sandbox:
mkdir -p /home/user/bots && cd /home/user/bots
git clone https://github.com/coolton-agent/slack-bot-template.git calculator-bot
cd calculator-bot
rm -rf .git   # this bot gets its own history, not the template's
npm install
```

Edit `wrangler.toml`'s `name` to match the bot (`name = "calculator-bot"`) — everything else in
it can stay as-is.

Edit `src/index.ts` — keep the wiring (the `.command`/`.event` registration, `app.run(request,
ctx)`, the 3-second-ack pattern), replace the handler bodies with the actual bot logic. For a
`/calculate` slash command:

```typescript
import { SlackApp, SlackEdgeAppEnv } from "slack-cloudflare-workers";

export default {
  async fetch(request: Request, env: SlackEdgeAppEnv, ctx: ExecutionContext): Promise<Response> {
    const app = new SlackApp({ env })
      .command(
        "/calculate",
        async () => "Calculating...", // quick ack — must return within 3 seconds
        async ({ context, payload }) => {
          const expr = (payload.text ?? "").trim();
          const sanitized = expr.replace(/[^0-9+\-*/().\s]/g, "");
          let text: string;
          if (!sanitized || sanitized !== expr) {
            text = "Invalid expression. Use numbers and + - * / ( ) only.";
          } else {
            try {
              const result = Function('"use strict"; return (' + sanitized + ")")();
              text = `${expr} = ${result}`;
            } catch {
              text = "Invalid expression. Use numbers and + - * / ( ) only.";
            }
          }
          await context.respond({ response_type: "ephemeral", text });
        },
      )
      .event("app_mention", async ({ context, payload }) => {
        // Reply in-thread if the mention was in a thread.
        await context.client.chat.postMessage({
          channel: context.channelId,
          thread_ts: payload.thread_ts ?? payload.ts,
          text: "Use `/calculate <expression>` to evaluate math.",
        });
      });
    return await app.run(request, ctx);
  },
};
```

The library reads `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` off `env` itself — no manual
signature verification code needed, that's the entire point of using a Workers-native library
instead of Bolt (and of starting from the template instead of writing this wiring by hand each
time). Run `npx tsc --noEmit` in the sandbox before deploying to catch typos early.

### 4. Deploy

Call `wrangler_bot_deploy_tool(uuid=<uuid>, working_dir="/home/user/bots/calculator-bot")`.

This:
1. Reads the bot token (and signing secret, and app token if one was registered) from the
   internal store.
2. Writes `.env_slack` inside `working_dir`.
3. Runs `npx wrangler@latest deploy --temporary --secrets-file .env_slack`.
4. Deletes `.env_slack` immediately.
5. Returns the wrangler output (preview URL, claim URL, errors).

```python
# Example call (agent does this internally):
output = wrangler_bot_deploy_tool(
    uuid="...",
    working_dir="/home/user/bots/calculator-bot",
    additional_flags=""  # e.g. "--minify"
)
```

Parse the **preview URL** (`https://calculator-bot.<subdomain>.workers.dev`) out of the output —
you need it for the next step. Also hang onto the **claim URL**; the user needs it to keep the
deployment past ~60 minutes (see `cf-wrangler`'s pitfalls).

### 5. Update the manifest with the real URL

NOW that the Worker is live and configured with the real signing secret, add
`slash_commands`/`event_subscriptions` pointing at it. This is the FULL manifest from step 1 plus
the URL-bearing blocks — `update_slack_bot_manifest_tool` replaces the entire config, so every
field from step 1 must be included again, not just the new parts:

```json
{
  "display_information": {
    "name": "Calculator Bot",
    "description": "Evaluates expressions via /calculate"
  },
  "features": {
    "bot_user": {
      "display_name": "Calculator Bot",
      "always_online": true
    },
    "slash_commands": [
      {
        "command": "/calculate",
        "url": "https://calculator-bot.<subdomain>.workers.dev/slack/events",
        "description": "Evaluate a math expression",
        "usage_hint": "2 + 2",
        "should_escape": false
      }
    ]
  },
  "oauth_config": {
    "scopes": {
      "bot": ["commands", "chat:write", "app_mentions:read"]
    }
  },
  "settings": {
    "event_subscriptions": {
      "request_url": "https://calculator-bot.<subdomain>.workers.dev/slack/events",
      "bot_events": ["app_mention"]
    },
    "org_deploy_enabled": false,
    "socket_mode_enabled": false,
    "token_rotation_enabled": false
  }
}
```

Call `update_slack_bot_manifest_tool(uuid=<uuid>, manifest=updated_manifest)`. If this errors
mentioning the request URL or a challenge, the Worker isn't actually reachable/responding yet —
verify with `curl` from the sandbox before retrying.

If the deployment is later re-claimed and gets a different URL, or you redeploy to a permanent
Cloudflare account, repeat this step with the new URL.

## Security Notes

- **User tokens (`xoxp-`) are never allowed.** Only a bot token (`xoxb-`), and optionally an
  app-level token (`xapp-`) if this is a Socket Mode app, may be injected into the deployed Worker.
- `.env_slack` is written, consumed by wrangler, and deleted in the same tool call (in a `finally`
  block, so it's cleaned up even if the deploy itself fails). It never appears in chat, logs, or
  tool outputs.
- The token store (`~/.coolton_bots.json` by default, overridable via `COOLTON_BOT_STORE`) lives
  on the HOST running the coolton agent process — NOT inside the E2B sandbox. Don't go looking
  for it with `run_linux_command`; it isn't there.

## Common Pitfalls

- **Don't put `slash_commands`/`event_subscriptions` in the manifest before the Worker is
  deployed.** Slack verifies `event_subscriptions.request_url` live (challenge/response) before
  accepting it — a placeholder URL will cause `apps.manifest.create`/`update` to fail. Follow the
  two-phase order in the workflow above: create with a URL-less manifest, deploy, then
  `update_slack_bot_manifest_tool` with the real URL.
- **Don't require an `app_token`.** It's Socket Mode-only, generated manually (not via OAuth
  install). `register_bot_tokens_tool`'s `app_token` is optional — omit it for HTTP-mode bots
  (which is everything this skill deploys, since Workers are HTTP, not Socket Mode).
- **Don't use `@slack/bolt` in the Worker code.** It has no Cloudflare Workers support — use
  `slack-cloudflare-workers` (see step 3).
- If `wrangler_bot_deploy_tool` errors that no bot token is registered, the human hasn't finished
  the OAuth install yet — check `check_bot_install_status_tool(uuid)` and wait/nudge them rather
  than assuming something is broken.
- `--temporary` creates a throwaway Cloudflare account valid for ~60 minutes. Always give the
  user the **claim URL** printed by wrangler so they can keep the deployment.
- If wrangler isn't installed globally in the sandbox, `npx wrangler@latest` will download it
  on first use (may take a moment).
- For thread support, read `thread_ts` (falling back to `ts`) off the incoming event/payload and
  pass it through explicitly — `slack-cloudflare-workers` doesn't infer it for you the way some
  Bolt helpers do.
- `create_slack_bot_tool` injects coolton's own callback into `oauth_config.redirect_urls` before
  creating the app — that's what makes automatic install capture work. `update_slack_bot_manifest_tool`
  REPLACES the entire config, so if you hand-build the step-6 manifest from scratch instead of
  reusing the step-1 manifest + additions, don't drop `oauth_config.redirect_urls` — it's harmless
  to leave in and only matters again if the app is ever reinstalled.

## Integration with cf-wrangler skill

The `cf-wrangler` skill covers scaffolding and deploying Workers generally — for Slack bots
specifically, cloning `coolton-agent/slack-bot-template` (step 3 above) replaces the scaffolding
part of that flow, since it's already a working `wrangler.toml` + project structure. Still lean on
`cf-wrangler` for:
- Understanding the `--temporary` deployment flow
- Getting the claim URL to the user

This skill (`slack-bot-deploy`) handles the Slack-specific pieces:
- Creating the app via manifest API (two-phase: minimal manifest first, URLs added after deploy)
- Securely managing bot tokens via UUID
- Injecting tokens at deploy time without exposure
- Updating Slack app config after deployment via `update_slack_bot_manifest_tool`