---
name: slack-bot-deploy
description: "Deploy custom Slack bots on-demand using Cloudflare Workers and wrangler --temporary. Use when the user wants to create a new Slack bot, implement a slash command, build a custom Slack integration, or deploy any Slack app to the cloud. Triggers include 'make a bot', 'create a slash command', 'deploy a slack bot', 'build a slack app', or any request that involves writing a Slack bot and hosting it."
---

# Deploy Slack Bots with Wrangler

Deploy custom Slack bots (Cloudflare Workers) on demand without a Cloudflare account.

## Overview

This skill enables coolton to spin up entirely new Slack bots from scratch:

1. **Write a manifest** describing the bot's features (e.g. slash commands).
2. **Create the Slack app** via Slack's manifest API using a config token (`xoxe-...`).
   This returns a UUID that tracks the bot internally.
3. **Write the Worker code** (TypeScript) handling the bot's logic.
4. **Register tokens** with the UUID (internally or via OAuth callback).
5. **Deploy with `wrangler_bot_deploy_tool`** — it retrieves the bot's tokens from the UUID,
   writes them to a temporary `.env_slack`, runs `npx wrangler@latest deploy --temporary --secrets-file .env_slack`,
   then deletes the file immediately. Tokens are never exposed.

## Required Environment

- `SLACK_CONFIG_TOKEN` — a Slack App Configuration Token (generate at
  https://api.slack.com/apps under "Your App Configuration Tokens"). Used by `create_slack_bot_tool`.
- `wrangler` — assumed available via `npx wrangler@latest` in the sandbox.

## Tools (exact names as exposed to the agent)

- `create_slack_bot_tool(manifest: dict)` — Creates the Slack app. Returns UUID, app_id,
  credentials, and an OAuth authorize URL. Stores everything in an internal JSON store.
- `register_bot_tokens_tool(uuid, bot_token, app_token)` — Associates tokens with the UUID.
  Call this after the app is installed or via an internal OAuth callback handler.
- `wrangler_bot_deploy_tool(uuid, working_dir, additional_flags)` — Deploys the Worker.
  Reads tokens from the internal store by UUID, never exposes them.

## Complete Workflow Example: `/calculate` Slash Command Bot

### 1. Build the manifest

A manifest for a simple slash-command bot looks like this:

```json
{
  "display_information": {
    "name": "Calculator Bot",
    "description": "Evaluates expressions via /calculate"
  },
  "features": {
    "slash_commands": [
      {
        "command": "/calculate",
        "url": "https://<worker-url>/slack/events",
        "description": "Evaluate a math expression",
        "should_escape": false
      }
    ]
  },
  "oauth_config": {
    "scopes": {
      "bot": ["commands"]
    }
  },
  "settings": {
    "org_deploy_enabled": false,
    "socket_mode_enabled": false,
    "token_rotation_enabled": false
  }
}
```

The `url` should point to the deployed Worker endpoint (`/slack/events` is a Bolt convention).
**Important:** You won't know the final Worker URL until after deployment. Use a placeholder
and update it after deploy, or use `apps.manifest.update` later.

### 2. Create the app

Call `create_slack_bot_tool` with the manifest. You will receive:
- `uuid` — save this for the deploy step.
- `oauth_authorize_url` — the workspace admin or an internal callback must visit this to
  install the app and generate the bot token (xoxb-).

```python
# Example call (agent does this internally):
result = create_slack_bot_tool(manifest=my_manifest)
# Returns JSON with uuid, app_id, oauth_authorize_url, etc.
```

### 3. Scaffold and write the Worker code

Use the `cf-wrangler` skill to scaffold a Worker project, then write the bot logic.

```bash
# In sandbox:
mkdir -p /home/user/bots/calculator-bot && cd /home/user/bots/calculator-bot
npx wrangler@latest init --yes
```

Write `wrangler.toml`:

```toml
name = "calculator-bot"
main = "src/index.ts"
compatibility_date = "2026-07-01"
```

Write `src/index.ts` (Bolt on Cloudflare Workers):

```typescript
import { App } from '@slack/bolt';

export interface Env {
  SLACK_BOT_TOKEN: string;
  SLACK_SIGNING_SECRET: string;
  SLACK_APP_TOKEN?: string;
}

const app = new App({
  signingSecret: (env: Env) => env.SLACK_SIGNING_SECRET,
  token: (env: Env) => env.SLACK_BOT_TOKEN,
  // For HTTP mode (not Socket Mode), we handle requests via fetch
});

app.command('/calculate', async ({ command, ack, respond, client }) => {
  await ack();
  const expr = command.text.trim();
  try {
    // Safe evaluation - only allow basic math
    const sanitized = expr.replace(/[^0-9+\-*/().\s]/g, '');
    if (sanitized !== expr) throw new Error('Invalid characters');
    const result = Function('"use strict"; return (' + sanitized + ')')();
    await respond({
      response_type: 'ephemeral',
      text: `${expr} = ${result}`,
    });
  } catch {
    await respond({
      response_type: 'ephemeral',
      text: 'Invalid expression. Use numbers and + - * / ( ) only.',
    });
  }
});

// Support threads: respond in the same thread if command was in a thread
app.event('app_mention', async ({ event, say }) => {
  await say({
    text: 'Use `/calculate <expression>` to evaluate math.',
    thread_ts: event.thread_ts || event.ts,
  });
});

// Cloudflare Workers fetch handler
export default {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // Handle Slack HTTP requests
    if (request.method === 'POST') {
      const url = new URL(request.url);
      if (url.pathname === '/slack/events') {
        return await app.receive(request, env);
      }
    }
    return new Response('OK');
  },
};
```

Also create `package.json` with dependencies:

```json
{
  "name": "calculator-bot",
  "main": "src/index.ts",
  "scripts": {
    "deploy": "wrangler deploy --temporary"
  },
  "dependencies": {
    "@slack/bolt": "^4.0.0",
    "hono": "^4.0.0"
  },
  "devDependencies": {
    "@cloudflare/workers-types": "^4.20240000.0",
    "typescript": "^5.0.0",
    "wrangler": "^3.0.0"
  }
}
```

Run `npm install` in the sandbox to install dependencies.

### 4. Register tokens

After the app is installed (either via the OAuth URL or an internal process), the bot
and app tokens must be associated with the UUID.

Call `register_bot_tokens_tool(uuid=<uuid>, bot_token="xoxb-...", app_token="xapp-...")`.

If your workspace has an internal OAuth callback handler that automatically stores tokens,
this step may already be complete when `create_slack_bot_tool` returns.

**Note:** Only bot tokens (`xoxb-`) and app-level tokens (`xapp-`) are allowed. User tokens
(`xoxp-`) are never injected into the deployed Worker.

### 5. Deploy

Call `wrangler_bot_deploy_tool(uuid=<uuid>, working_dir="/home/user/bots/calculator-bot")`.

This:
1. Reads tokens from the internal store.
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

### 6. Update the manifest URL (if needed)

If the Worker gets a new preview URL on each deploy, update the slash-command URL in
Slack's app settings (or via `apps.manifest.update`) so Slack knows where to POST.

## Security Notes

- **User tokens (`xoxp-`) are never allowed.** Only bot tokens (`xoxb-`) and app-level tokens
  (`xapp-`) may be injected into the deployed Worker.
- `.env_slack` is written, consumed by wrangler, and deleted in the same subprocess call.
  It never appears in chat, logs, or tool outputs.
- The token store lives in the sandbox at `~/.coolton_bots.json`. It persists across turns
  but is not synced externally.

## Common Pitfalls

- If `create_slack_bot_tool` returns an OAuth URL but no tokens, you must install the app before
  `wrangler_bot_deploy_tool` will succeed. It will error if tokens are missing.
- `--temporary` creates a throwaway Cloudflare account valid for ~60 minutes. Always give the
  user the **claim URL** printed by wrangler so they can keep the deployment.
- The manifest `url` in slash commands must be HTTPS and reachable from Slack's servers.
  Cloudflare Workers `.workers.dev` URLs satisfy this.
- If wrangler isn't installed globally in the sandbox, `npx wrangler@latest` will download it
  on first use (may take a moment).
- For thread support, the Worker must read `thread_ts` from the incoming event and include it
  in responses. The Bolt `respond()` helper does this automatically when given an ephemeral
  response in a thread context.

## Integration with cf-wrangler skill

The `cf-wrangler` skill covers scaffolding and deploying Workers generally. Use it for:
- Initial `wrangler init`
- Writing `wrangler.toml` and project structure
- Understanding the `--temporary` deployment flow
- Getting the claim URL to the user

This skill (`slack-bot-deploy`) handles the Slack-specific pieces:
- Creating the app via manifest API
- Securely managing bot tokens via UUID
- Injecting tokens at deploy time without exposure
- Updating Slack app config after deployment