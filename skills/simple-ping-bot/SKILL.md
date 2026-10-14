---
name: simple-ping-bot
description: 'Create a Slack bot with a /ping slash command that replies ''pong''. Deploy it to Cloudflare using wrangler.'
---

# Simple Ping Bot

This skill creates a minimal Slack bot with a `/ping` slash command that replies `pong` when used. It deploys the bot to Cloudflare using `wrangler --temporary` and guides the user through installing it in their Slack workspace.

## Workflow

1. **Create the Slack app** with a minimal manifest (name, bot user, OAuth scopes).
2. **Generate the OAuth install URL** and ask the user to visit it and install the app.
3. **Register the bot token** after the user provides it.
4. **Scaffold the Worker project** in the sandbox.
5. **Write the Worker code** using `slack-cloudflare-workers`.
6. **Deploy the Worker** with `wrangler_bot_deploy_tool`.
7. **Update the Slack app manifest** with the real Worker URL for the `/ping` command.
8. **Provide the user with the claim URL** to keep the deployment past ~60 minutes.

## Tools Used
- `create_slack_bot_tool`
- `register_bot_tokens_tool`
- `wrangler_bot_deploy_tool`
- `update_slack_bot_manifest_tool`

## Example Worker Code

```typescript
import { SlackApp, SlackEdgeAppEnv } from "slack-cloudflare-workers";

export default {
  async fetch(request: Request, env: SlackEdgeAppEnv, ctx: ExecutionContext): Promise<Response> {
    const app = new SlackApp({ env }).command(
      "/ping",
      async () => "Pong!", // Acknowledge the command within 3 seconds
      async ({ context }) => {
        await context.respond({ text: "pong" });
      },
    );
    return await app.run(request, ctx);
  },
};
```
