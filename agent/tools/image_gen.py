import os
import requests
from agent.byok_store import get_image_endpoint_id, get_endpoint_decrypted


def generate_image_with_byok(user_id: str, prompt: str, n: int = 1, size: str = "1024x1024") -> str:
    """Generate images using the user's BYOK image endpoint (any OpenAI-compatible API).

    If no BYOK endpoint is set, falls back to global OPENAI_API_KEY.
    """
    if user_id:
        ep_id = get_image_endpoint_id(user_id)
        if ep_id:
            ep = get_endpoint_decrypted(user_id, ep_id)
            if ep:
                return _generate_openai_compatible(ep["api_key"], ep["base_url"], ep["model"], prompt, n, size)

    global_key = os.environ.get("OPENAI_API_KEY")
    if global_key:
        return _generate_openai_compatible(global_key, "https://api.openai.com/v1", "dall-e-3", prompt, n, size)

    return "Error: No image generation API key found. Add an endpoint via BYOK (Home tab) or set OPENAI_API_KEY globally."


def _generate_openai_compatible(api_key: str, base_url: str, model: str, prompt: str, n: int, size: str) -> str:
    url = f"{base_url.rstrip('/')}/images/generations"
    try:
        payload = {"model": model, "prompt": prompt, "n": min(n, 4), "size": size}
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=60,
        )
        res = response.json()
        if "data" in res:
            urls = [img["url"] for img in res["data"]]
            result = f"Generated {len(urls)} image(s):\n"
            for i, u in enumerate(urls, 1):
                result += f"{i}. {u}\n"
            return result.strip()
        return f"Image error: {res.get('error', {}).get('message', 'unknown')}"
    except Exception as e:
        return f"Error generating image: {str(e)}"
