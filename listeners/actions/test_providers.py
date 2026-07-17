import logging
import os
import time

from slack_bolt import Ack, BoltContext
from slack_sdk import WebClient

logger = logging.getLogger(__name__)

PROVIDER_DISPLAY = {
    "byok": "BYOK",
    "anthropic": "Anthropic",
    "openai": "OpenAI",
    "jams_hy3_free": "Jam's HY3 Free",
    "hcai_hy3_free": "HCAI HY3 Free",
    "openrouter_hy3_free": "OpenRouter HY3 Free",
    "jams_hy3": "Jam's HY3",
    "hcai_hy3": "HCAI HY3",
    "jams": "Jam's Kimi K2.6",
    "jams_minimax": "Jam's MiniMax M2.7",
    "hcai": "HCAI Kimi K2.6",
    "hcai_minimax": "HCAI MiniMax M2.7",
    "openrouter_fb": "OpenRouter Fallback",
    "mistral": "Mistral Large 2512",
    "gemini": "Gemini 3.1 Flash-Lite",
    "groq_oss120b": "Groq GPT-OSS-120B",
    "groq_oss20b": "Groq GPT-OSS-20B",
    "gemini_gemma": "Gemma 4 31B",
    "groq_qwen27b": "Groq Qwen 3.6 27B",
    "groq_qwen32b": "Groq Qwen 3 32B",
    "cerebras": "Cerebras",
}


def _build_provider_order(user_id: str) -> list[tuple[str, dict]]:
    from agent.agent import get_user_text_endpoint

    order = []

    user_endpoint = get_user_text_endpoint(user_id)
    if user_endpoint:
        order.append(("byok", {"model": user_endpoint["model"], "base_url": user_endpoint["base_url"], "api_key": user_endpoint["api_key"]}))

    if os.environ.get("ANTHROPIC_API_KEY"):
        order.append(("anthropic", {"model": "anthropic:claude-sonnet-4-6", "base_url": None, "api_key": os.environ["ANTHROPIC_API_KEY"]}))
    if os.environ.get("OPENAI_API_KEY"):
        order.append(("openai", {"model": "openai:gpt-4.1-mini", "base_url": None, "api_key": os.environ["OPENAI_API_KEY"]}))
    GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
    JAMS_API_KEY = os.environ.get("JAMS_API_KEY")
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if JAMS_API_KEY:
        order.append(("jams_hy3_free", {"model": "openrouter:tencent/hy3:free", "base_url": None, "api_key": JAMS_API_KEY}))
    if HCAI_API_KEY:
        order.append(("hcai_hy3_free", {"model": "tencent/hy3:free", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if os.environ.get("OPENROUTER_API_KEY_FALLBACK"):
        order.append(("openrouter_hy3_free", {"model": "openrouter:tencent/hy3:free", "base_url": None, "api_key": os.environ["OPENROUTER_API_KEY_FALLBACK"]}))
    if JAMS_API_KEY:
        order.append(("jams_hy3", {"model": "openrouter:tencent/hy3", "base_url": None, "api_key": JAMS_API_KEY}))
    if HCAI_API_KEY:
        order.append(("hcai_hy3", {"model": "tencent/hy3", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if JAMS_API_KEY:
        order.append(("jams", {"model": "openrouter:moonshotai/kimi-k2.6", "base_url": None, "api_key": JAMS_API_KEY}))
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if HCAI_API_KEY:
        order.append(("hcai", {"model": "moonshotai/kimi-k2.6", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if GROQ_API_KEY:
        order.append(("groq_qwen27b", {"model": "groq:qwen/qwen3.6-27b", "base_url": None, "api_key": GROQ_API_KEY}))
    if os.environ.get("JAMS_API_KEY"):
        order.append(("jams_minimax", {"model": "openrouter:minimax/minimax-m2.7", "base_url": None, "api_key": os.environ["JAMS_API_KEY"]}))
    HCAI_API_KEY = os.environ.get("HCAI_API_KEY")
    if HCAI_API_KEY:
        order.append(("hcai_minimax", {"model": "minimax/minimax-m2.7", "base_url": "https://ai.hackclub.com/proxy/v1", "api_key": HCAI_API_KEY}))
    if os.environ.get("OPENROUTER_API_KEY_FALLBACK"):
        order.append(("openrouter_fb", {"model": "openrouter:nvidia/nemotron-3-ultra-550b-a55b:free", "base_url": None, "api_key": os.environ["OPENROUTER_API_KEY_FALLBACK"]}))
    if os.environ.get("GOOGLE_API_KEY"):
        order.append(("gemini_gemma", {"model": "google:gemma-4-31b-it", "base_url": None, "api_key": os.environ["GOOGLE_API_KEY"]}))
    if GROQ_API_KEY:
        order.append(("groq_oss120b", {"model": "groq:openai/gpt-oss-120b", "base_url": None, "api_key": GROQ_API_KEY}))
    if os.environ.get("GOOGLE_API_KEY"):
        order.append(("gemini", {"model": "google:gemini-3.1-flash-lite", "base_url": None, "api_key": os.environ["GOOGLE_API_KEY"]}))
    if GROQ_API_KEY:
        order.append(("groq_qwen32b", {"model": "groq:qwen/qwen3-32b", "base_url": None, "api_key": GROQ_API_KEY}))
        order.append(("groq_oss20b", {"model": "groq:openai/gpt-oss-20b", "base_url": None, "api_key": GROQ_API_KEY}))
    if os.environ.get("MISTRAL_API_KEY"):
        order.append(("mistral", {"model": "mistral:mistral-large-2512", "base_url": None, "api_key": os.environ["MISTRAL_API_KEY"]}))
    if os.environ.get("CEREBRAS_API_KEY"):
        order.append(("cerebras", {"model": "cerebras:zai-glm-4.7", "base_url": None, "api_key": os.environ["CEREBRAS_API_KEY"]}))

    return order


def _set_env(provider_name: str, api_key: str):
    mapping = {
        "anthropic": ("ANTHROPIC_API_KEY",),
        "openai": ("OPENAI_API_KEY",),
        "jams": ("OPENROUTER_API_KEY",),
        "jams_hy3_free": ("OPENROUTER_API_KEY",),
        "jams_hy3": ("OPENROUTER_API_KEY",),
        "openrouter_fb": ("OPENROUTER_API_KEY",),
        "openrouter_hy3_free": ("OPENROUTER_API_KEY",),
        "cerebras": ("CEREBRAS_API_KEY",),
        "mistral": ("MISTRAL_API_KEY",),
        "gemini": ("GOOGLE_API_KEY",),
        "gemini_gemma": ("GOOGLE_API_KEY",),
    }
    if provider_name in mapping:
        for var in mapping[provider_name]:
            os.environ[var] = api_key
    elif provider_name == "byok":
        pass
    elif provider_name == "hcai":
        pass
    elif provider_name in ("hcai_hy3_free", "hcai_hy3"):
        pass
    elif provider_name.startswith("groq_"):
        os.environ["GROQ_API_KEY"] = api_key


def _test_single(provider_name: str, config: dict) -> tuple[bool, str, float, str]:
    display = PROVIDER_DISPLAY.get(provider_name, provider_name)
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
        return False, display, elapsed, str(e)


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
