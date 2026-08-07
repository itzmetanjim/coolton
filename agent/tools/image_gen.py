import os
import re
import time
import requests
from agent.byok_store import get_image_endpoint_id, get_endpoint_decrypted


# Known aspect ratios -> OpenAI-compatible size strings. Unknown ratios are
# passed through to providers that accept an `aspect_ratio` field instead.
ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "4:3": "1536x1024",
    "3:4": "1024x1536",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}


def _resolve_size(size: str, aspect_ratio: str | None) -> str:
    if aspect_ratio:
        normalized = aspect_ratio.strip().lower()
        mapped = ASPECT_TO_SIZE.get(normalized)
        if mapped:
            return mapped
        return size
    return size


def _sanitize(name: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", name)
    return cleaned if cleaned not in ("", ".", "..") else "image"


def generate_image_with_byok(user_id: str, prompt: str, n: int = 1, size: str = "1024x1024", aspect_ratio: str | None = None) -> str:
    """Generate images using the user's BYOK image endpoint (any OpenAI-compatible API).

    If no BYOK endpoint is set, falls back to global OPENAI_API_KEY.
    """
    if user_id:
        ep_id = get_image_endpoint_id(user_id)
        if ep_id:
            ep = get_endpoint_decrypted(user_id, ep_id)
            if ep:
                return _generate_openai_compatible(ep["api_key"], ep["base_url"], ep["model"], prompt, n, size, aspect_ratio)

    global_key = os.environ.get("OPENAI_API_KEY")
    if global_key:
        return _generate_openai_compatible(global_key, "https://api.openai.com/v1", "dall-e-3", prompt, n, size, aspect_ratio)

    return "Error: No image generation API key found. Add an endpoint via BYOK (Home tab) or set OPENAI_API_KEY globally."


def _generate_openai_compatible(api_key: str, base_url: str, model: str, prompt: str, n: int, size: str, aspect_ratio: str | None = None) -> str:
    url = f"{base_url.rstrip('/')}/images/generations"
    try:
        payload = {"model": model, "prompt": prompt, "n": min(n, 4), "size": _resolve_size(size, aspect_ratio)}
        if aspect_ratio:
            normalized = aspect_ratio.strip().lower()
            if normalized not in ASPECT_TO_SIZE:
                payload["aspect_ratio"] = normalized
        response = requests.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=90,
        )
        res = response.json()
        if "data" not in res:
            return f"Image error: {res.get('error', {}).get('message', 'unknown')}"

        urls = []
        for img in res["data"]:
            if img.get("b64_json"):
                urls.append("data:image/png;base64," + img["b64_json"])
            elif img.get("url"):
                urls.append(img["url"])
            else:
                urls.append("")
        result = f"Generated {len(urls)} image(s):\n"
        for i, u in enumerate(urls, 1):
            result += f"{i}. {u}\n"
        return result.strip()
    except Exception as e:
        return f"Error generating image: {str(e)}"


def save_images_to_sandbox(sandbox, urls: list[str], batch: str = "") -> list[str]:
    """Download generated images into the sandbox ~/downloads/ dir (mirrors gorkie).

    Args:
        sandbox: An E2B sandbox.
        urls: Image URLs to fetch (data: URIs supported).
        batch: Optional batch tag used in the filename.

    Returns:
        List of saved sandbox paths.
    """
    if not sandbox:
        return []
    batch = batch or time.strftime("%H%M%S")
    saved = []
    try:
        sandbox.commands.run("mkdir -p ~/downloads")
    except Exception:
        return saved
    for i, u in enumerate(urls, 1):
        if not u:
            continue
        try:
            if u.startswith("data:"):
                import base64 as b64

                header, _, payload = u.partition(",")
                content = b64.b64decode(payload)
                ext = "png"
                if "png" in header:
                    ext = "png"
                elif "jpeg" in header or "jpg" in header:
                    ext = "jpg"
                elif "webp" in header:
                    ext = "webp"
            else:
                resp = requests.get(u, timeout=60)
                if resp.status_code != 200:
                    continue
                content = resp.content
                ct = resp.headers.get("Content-Type", "")
                ext = "png"
                if "jpeg" in ct or "jpg" in ct:
                    ext = "jpg"
                elif "webp" in ct:
                    ext = "webp"
            name = f"coolton-image-{batch}-{i}.{ext}"
            sandbox.files.write(f"/home/user/downloads/{name}", content)
            saved.append(f"~/downloads/{name}")
        except Exception:
            continue
    return saved
