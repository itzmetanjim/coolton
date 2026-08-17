---
name: slack-bot-deployer
description: 'Deploy a Slack bot to Cloudflare Workers using Wrangler, handling bot creation, manifest generation, and secure token management. USE FOR: "make a Slack bot", "deploy a bot with /command", "create a bot that does X", or any request to build and host a Slack bot autonomously.'
---

# Slack Bot Deployer

# Slack Bot Deployer

Deploy a Slack bot to Cloudflare Workers using Wrangler, handling bot creation, manifest generation, and secure token management. This skill ensures the bot's tokens are never exposed to the AI and are securely injected during deployment.

## Workflow Overview

1. **Generate the Slack App Manifest**: Create a `manifest.yml` based on the user's requirements (slash commands, event subscriptions, permissions).
2. **Create the Slack App**: Use the Slack API to create a new bot/app with the generated manifest. This returns a UUID and bot tokens.
3. **Scaffold the Bot Code**: Write the bot logic (e.g., Bolt.js) in a Cloudflare Worker-compatible format.
4. **Deploy with Wrangler**: Use the `wranglerBotDeploy` tool to deploy the bot to Cloudflare Workers, securely injecting the bot tokens during deployment.
5. **Report Back**: Provide the user with the bot's URL and any relevant details (e.g., slash command usage).

---

## Step 1: Generate the Slack App Manifest

The manifest defines the bot's capabilities, permissions, and settings. Use the following template and customize it based on the user's requirements:

```yaml
_metadata:
  major_version: 1
  minor_version: 1
display_information:
  name: <BOT_NAME>
  description: <BOT_DESCRIPTION>
  background_color: "#4a154b"
features:
  bot_user:
    display_name: <BOT_NAME>
    always_online: false
  slash_commands:
    - command: /<COMMAND>
      description: <COMMAND_DESCRIPTION>
      usage_hint: "[args]"
      should_escape: false
oauth_config:
  scopes:
    bot:
      - app_mentions:read
      - chat:write
      - commands
      - incoming-webhook
      - users:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
      - message.im
  interactivity:
    is_enabled: true
  org_deploy_enabled: false
  socket_mode_enabled: true
  is_hosted: false
```

### Customization Rules
- Replace `<BOT_NAME>`, `<BOT_DESCRIPTION>`, `<COMMAND>`, and `<COMMAND_DESCRIPTION>` with the user's requirements.
- Add or remove `slash_commands` as needed.
- Adjust `bot_events` based on the bot's functionality (e.g., add `reaction_added` for reaction-based interactions).
- Ensure `socket_mode_enabled: true` for WebSocket-based bots (recommended for simplicity).

---

## Step 2: Create the Slack App

Use the Slack API to create the bot/app with the generated manifest. This step requires:

1. A tool to call the Slack API (`slack_api_call`).
2. The generated `manifest.yml` as input.

### API Call
```json
{
  "method": "apps.manifest.create",
  "params": {
    "app_id": "<APP_ID>",  // Leave empty for new apps
    "manifest": "<MANIFEST_YAML>"  // The YAML string from Step 1
  }
}
```

### Output
The API returns a UUID for the bot/app. This UUID is used to associate the bot's tokens with the deployment. Example response:

```json
{
  "app_id": "A0123456789",
  "credentials": {
    "client_id": "1234567890.1234567890",
    "client_secret": "xoxb-...",
    "verification_token": "...",
    "signing_secret": "..."
  },
  "oauth_authorize_url": "https://slack.com/oauth/v2/authorize?..."
}
```

Store the `app_id` (UUID) for use in Step 4.

---

## Step 3: Scaffold the Bot Code

Write the bot logic in a Cloudflare Worker-compatible format. Use Bolt.js for Node.js or a lightweight HTTP server for Python. Below is an example for a Bolt.js bot:

### Example: Bolt.js Bot for `/calculate`

1. **Project Structure**:
   ```
   /bot
   ├── src/
   │   ├── index.js       # Worker entry point
   │   ├── bot.js         # Bolt.js bot logic
   │   └── commands.js    # Slash command handlers
   ├── package.json
   └── wrangler.toml
   ```

2. **`src/bot.js`**:
   ```javascript
   const { App } = require('@slack/bolt');

   const app = new App({
     socketMode: true,
     appToken: process.env.SLACK_APP_TOKEN,
     token: process.env.SLACK_BOT_TOKEN,
     signingSecret: process.env.SLACK_SIGNING_SECRET
   });

   // Slash command handler
   app.command('/calculate', async ({ command, ack, respond }) => {
     await ack();
     try {
       const expression = command.text;
       const result = eval(expression); // Note: Use a safer eval alternative in production
       await respond({
         response_type: 'ephemeral',
         text: `Result: ${result}`
       });
     } catch (error) {
       await respond({
         response_type: 'ephemeral',
         text: `Error: Invalid expression`
       });
     }
   });

   // Event handler for mentions
   app.event('app_mention', async ({ event, say }) => {
     await say(`Hey <@${event.user}>! Use /calculate to compute expressions.`);
   });

   module.exports = { app };
   ```

3. **`src/index.js`** (Worker entry):
   ```javascript
   import { app } from './bot';

   export default {
     async fetch(request, env) {
       if (request.method === 'POST') {
         return await app.start()(request, env);
       }
       return new Response('Slack bot is running!', { status: 200 });
     }
   };
   ```

4. **`package.json`**:
   ```json
   {
     "name": "slack-bot",
     "version": "1.0.0",
     "main": "src/index.js",
     "scripts": {
       "deploy": "wrangler deploy"
     },
     "dependencies": {
       "@slack/bolt": "^3.12.2"
     }
   }
   ```

5. **`wrangler.toml`**:
   ```toml
   name = "slack-bot"
   main = "src/index.js"
   compatibility_date = "2026-07-01"
   ```

---

## Step 4: Deploy with Wrangler

Use the `wranglerBotDeploy` tool to deploy the bot to Cloudflare Workers. This tool:

1. Accepts the **UUID** (from Step 2) and **working directory** (where the bot code is scaffolded).
2. Internally retrieves the bot's tokens (e.g., `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `SLACK_SIGNING_SECRET`).
3. Creates a `.env_slack` file in the working directory with the tokens:
   ```
   SLACK_BOT_TOKEN=xoxb-...
   SLACK_APP_TOKEN=xapp-...
   SLACK_SIGNING_SECRET=...
   ```
4. Runs `npx wrangler deploy --temporary --secrets-file .env_slack`.
5. Deletes `.env_slack` after deployment to ensure tokens are never exposed.

### Tool Call
```json
{
  "tool_name": "wranglerBotDeploy",
  "arguments": {
    "uuid": "A0123456789",  // The app_id from Step 2
    "working_dir": "/home/user/sites/slack-bot",  // Directory with bot code
    "additional_flags": ""  // Optional: e.g., "--name my-bot"
  }
}
```

### Output
The tool returns:
- The **preview URL** (e.g., `https://slack-bot.<subdomain>.workers.dev`).
- The **claim URL** (for the user to claim the deployment).

---

## Step 5: Report Back to the User

After deployment, provide the user with:

1. The bot's **preview URL** (for testing).
2. The **claim URL** (to claim the deployment before it expires).
3. Instructions for using the bot (e.g., `/calculate 1+2+3`).
4. Any additional setup steps (e.g., inviting the bot to channels).

Example response:

```
Your Slack bot is now live! Here's how to use it:

- **Preview URL**: [https://slack-bot.<subdomain>.workers.dev](https://slack-bot.<subdomain>.workers.dev)
- **Claim URL**: [Claim your deployment here](<CLAIM_URL>) (expires in ~60 minutes)

### How to Use
1. Invite the bot to a channel: `/invite @<BOT_NAME>`
2. Try the slash command: `/calculate 1+2+3`
3. The bot will respond with the result in an ephemeral message.

### Notes
- The bot supports threads and ephemeral responses.
- Claim the deployment before it expires to keep it running.
```

---

## Rules and Guardrails

1. **Token Security**:
   - Never expose the bot's tokens to the AI or the user.
   - Always use the `wranglerBotDeploy` tool to inject tokens during deployment.
   - Ensure `.env_slack` is deleted after deployment.

2. **User Tokens**:
   - User tokens (`xoxp-...`) are **not allowed**. Only bot tokens (`xoxb-...`) and app tokens (`xapp-...`) are permitted.

3. **Manifest Validation**:
   - Validate the `manifest.yml` before creating the bot. Ensure required fields (e.g., `display_information.name`, `oauth_config.scopes`) are present.
   - Use the Slack API's `apps.manifest.validate` endpoint to validate the manifest before creation.

4. **Error Handling**:
   - If the Slack API call fails, report the error verbatim and suggest fixes (e.g., invalid scopes, duplicate app name).
   - If `wranglerBotDeploy` fails, report the error and suggest troubleshooting steps (e.g., invalid UUID, missing files).

5. **Deployment Limits**:
   - Temporary deployments expire in ~60 minutes. Always provide the claim URL to the user.
   - If the user wants a permanent deployment, guide them to claim the deployment and set up their own Cloudflare account.

6. **Bot Functionality**:
   - Ensure the bot's code is compatible with Cloudflare Workers (e.g., no Node.js-specific modules unless polyfilled).
   - Use `socketMode: true` for simplicity unless the user explicitly requests HTTP mode.

---

## Example Workflow

**User Request**: "Make a bot implementing `/calculate` so that `/calculate 1+2+3` gives `6` in an ephemeral message, and make it support threads."

### Steps:
1. Generate the manifest with `/calculate` command and required scopes.
2. Create the Slack app using the manifest.
3. Scaffold the bot code (Bolt.js) with `/calculate` handler.
4. Deploy using `wranglerBotDeploy` with the UUID and working directory.
5. Report the preview URL, claim URL, and usage instructions to the user.

### Manifest:
```yaml
_metadata:
  major_version: 1
  minor_version: 1
display_information:
  name: Calculator Bot
  description: A bot for calculating expressions
  background_color: "#4a154b"
features:
  bot_user:
    display_name: Calculator Bot
    always_online: false
  slash_commands:
    - command: /calculate
      description: Calculate an expression (e.g., /calculate 1+2+3)
      usage_hint: "[expression]"
      should_escape: false
oauth_config:
  scopes:
    bot:
      - chat:write
      - commands
      - app_mentions:read
settings:
  event_subscriptions:
    bot_events:
      - app_mention
      - message.channels
  interactivity:
    is_enabled: true
  socket_mode_enabled: true
```

### Bot Code:
See the example above for `/calculate`.

### Deployment:
```json
{
  "uuid": "A0123456789",
  "working_dir": "/home/user/sites/calculator-bot",
  "additional_flags": ""
}
```
