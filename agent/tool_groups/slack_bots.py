"""Building and deploying Slack bots for people (agent.tools.slack_bot_deploy): the tools
that create one, check its install, register its tokens, deploy it and change its manifest.

Registered on the agent in agent.agent; nothing here imports agent.agent.
"""

from pydantic_ai import RunContext

from agent.deps import AgentDeps
from agent.tool_args import parse_json_object_param

def create_slack_bot_tool(ctx: RunContext[AgentDeps], manifest: str) -> str:
    """Create a Slack app from a manifest. Returns app_id and an OAuth install URL.

    Uses the xoxe config token. The manifest must include display_information.name.
    After creating, tell the user to visit the oauth_authorize_url to install the
    app, the bot token is captured and registered AUTOMATICALLY once they do (no
    one needs to dig it out of the Slack UI and hand it back). Poll
    check_bot_install_status_tool with the returned app_id until it reports
    "installed", then move on to wrangler_bot_deploy_tool. The app belongs to
    the person who asked for it: only they (or the coolton maintainer) can check,
    register tokens for, update, or deploy it afterwards.

    Args:
        manifest: JSON-encoded Slack app manifest object, as a plain STRING (with
            display_information, features, etc.), not a nested object.
    """
    parsed_manifest, parse_error = parse_json_object_param("manifest", manifest)
    if parse_error:
        return parse_error
    from agent.tools.slack_bot_deploy import create_slack_bot
    from agent.attribution import attribution_user_id
    return create_slack_bot(parsed_manifest, owner_id=attribution_user_id(ctx.deps))


def check_bot_install_status_tool(ctx: RunContext[AgentDeps], uuid: str) -> str:
    """Check whether a human has finished installing a bot created with create_slack_bot_tool.

    The install is captured automatically (see create_slack_bot_tool), call this
    periodically after sharing the oauth_authorize_url instead of asking the user
    to paste a token back. Once it reports "installed", the bot token is already
    registered and you can go straight to wrangler_bot_deploy_tool.

    Args:
        uuid: The app_id returned by create_slack_bot.
    """
    from agent.tools.slack_bot_deploy import check_bot_install_status
    from agent.attribution import attribution_user_id
    return check_bot_install_status(uuid, requester_id=attribution_user_id(ctx.deps))


def register_bot_tokens_tool(ctx: RunContext[AgentDeps], uuid: str, bot_token: str, app_token: str = "", signing_secret: str = "") -> str:
    """Manually store bot tokens for a created Slack app. Only xoxb- bot tokens (and, if
    given, xapp- app tokens) are accepted.

    This is a FALLBACK, installs normally complete automatically (see
    create_slack_bot_tool / check_bot_install_status_tool). Only use this if the
    user says the automatic capture didn't work, or hands you a token unprompted.

    Args:
        uuid: The app_id returned by create_slack_bot.
        bot_token: The xoxb- bot token from the installed app. Required.
        app_token: The xapp- app-level token. Only needed for Socket Mode apps, it's
            generated manually on the app's Basic Information page, not via OAuth
            install, so most HTTP-mode Workers (deployed via wrangler_bot_deploy_tool)
            never have one. Omit it entirely for those.
        signing_secret: The signing secret from the app credentials (optional).
    """
    from agent.tools.slack_bot_deploy import register_bot_tokens
    from agent.attribution import attribution_user_id
    return register_bot_tokens(uuid, bot_token, app_token, signing_secret, requester_id=attribution_user_id(ctx.deps))


def wrangler_bot_deploy_tool(ctx: RunContext[AgentDeps], uuid: str, working_dir: str, additional_flags: str = "") -> str:
    """Deploy a Slack bot Worker using wrangler inside the sandbox. Injects stored tokens, runs deploy, then deletes the secrets file.

    Args:
        uuid: The app_id from create_slack_bot.
        working_dir: Directory containing the bot code inside the sandbox.
        additional_flags: Extra flags for wrangler deploy (e.g. "--minify").
    """
    from agent.tools.slack_bot_deploy import wrangler_bot_deploy
    from agent.attribution import attribution_user_id
    return wrangler_bot_deploy(
        uuid, working_dir, ctx.deps.channel_id, ctx.deps.thread_ts, additional_flags,
        requester_id=attribution_user_id(ctx.deps),
    )


def update_slack_bot_manifest_tool(ctx: RunContext[AgentDeps], uuid: str, manifest: str) -> str:
    """Update an already-created Slack app's manifest (apps.manifest.update).

    Use this once the Worker is deployed and its real URL is known, to point
    slash_commands[].url / settings.event_subscriptions.request_url at it, Slack only
    accepts an event-subscription request URL once it's live and answers the
    verification challenge, so it can't be set correctly until after deploy. The
    manifest passed here REPLACES the app's entire configuration: include every field
    (scopes, bot_user, display_information, etc.), not just the URL you're changing.

    Args:
        uuid: The app_id from create_slack_bot.
        manifest: JSON-encoded, FULL updated Slack app manifest object, as a plain
            STRING, not a nested object.
    """
    parsed_manifest, parse_error = parse_json_object_param("manifest", manifest)
    if parse_error:
        return parse_error
    from agent.tools.slack_bot_deploy import update_slack_bot_manifest
    from agent.attribution import attribution_user_id
    return update_slack_bot_manifest(uuid, parsed_manifest, requester_id=attribution_user_id(ctx.deps))
