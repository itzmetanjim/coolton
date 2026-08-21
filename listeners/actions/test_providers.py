import logging
import time

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

from agent import provider_config
from agent.redact import redact

logger = logging.getLogger(__name__)


def _build_provider_order(user_id: str) -> list[tuple[str, dict]]:
    return provider_config.build_provider_order(user_id)


def _set_env(provider_name: str, api_key: str):
    provider_config.apply_provider_env(provider_name, api_key)


def _test_single(provider_name: str, config: dict) -> tuple[bool, str, float, str]:
    display = config.get("display", provider_name)
    start = time.time()

    try:
        _set_env(provider_name, config["api_key"])

        from pydantic_ai import Agent

        if config.get("base_url"):
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.openai import OpenAIProvider
            model = OpenAIChatModel(
                config["model"],
                provider=OpenAIProvider(
                    base_url=config["base_url"],
                    api_key=config["api_key"],
                ),
            )
        else:
            model = config["model"]

        a = Agent(model)
        result = a.run_sync("Hello! Respond with the single word 'ok' if you receive this.")
        elapsed = time.time() - start
        return True, display, elapsed, result.output
    except Exception as e:
        elapsed = time.time() - start
        # This is the one place testing provider credentials directly — an SDK/HTTP
        # error can echo the key back (a malformed base_url, an auth-header dump),
        # and this gets posted straight to the user's Slack DM, so redact it like
        # every other secret-adjacent path in the codebase does.
        return False, display, elapsed, redact(str(e), context="test_providers")


def handle_test_providers(ack: Ack, body: dict, client: WebClient, context: BoltContext):
    ack()
    try:
        user_id = context.user_id
        client.chat_postEphemeral(
            channel=user_id, user=user_id,
            text="Testing all AI providers... this may take a minute.",
        )

        order = _build_provider_order(user_id)
        if not order:
            client.chat_postMessage(channel=user_id, text="No AI providers configured.")
            return

        results = []
        for provider_name, config in order:
            ok, display, elapsed, detail = _test_single(provider_name, config)
            status = ":white_check_mark:" if ok else ":x:"
            line = f"{status} *{display}* — {elapsed:.1f}s"
            if ok:
                line += f"\n       {detail}"
            else:
                line += f"\n       ```\n{detail}\n       ```"
            results.append(line)

        lines = "\n".join(results)
        client.chat_postMessage(
            channel=user_id,
            text=f"*AI Provider Test Results*\n{lines}",
            mrkdwn=True,
        )
    except Exception as e:
        logger.exception("Failed to test providers: %s", e)
