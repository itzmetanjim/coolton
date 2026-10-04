import os
import shlex
import time
import requests
from agent.byok_store import get_image_endpoint_id, get_endpoint_decrypted


# Known aspect ratios -> OpenAI-compatible size strings. Unknown ratios (this
# deliberately excludes 4:3/3:4 — 1536x1024/1024x1536 are 3:2/2:3, not 4:3/3:4,
# and there's no correct fixed pixel size to offer across arbitrary BYOK
# providers) are passed through to providers that accept an `aspect_ratio`
# field instead.
ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1792x1024",
    "9:16": "1024x1792",
    "3:2": "1536x1024",
    "2:3": "1024x1536",
}


def _with_key_fallback(call, api_key: str, provider: str | None) -> str:
    """`call(api_key)`, retried with the provider's next API key while the one it used
    is out of credits or at a spending limit (agent.provider_config.on_outage, the same
    as the chat chain does). A provider with no usable key left is marked dead."""
    from agent.provider_config import on_outage

    result = call(api_key)
    for _ in range(5):
        if "image(s)" in result or not provider:
            return result
        outage, next_key = on_outage(provider, api_key, result)
        if outage != "next_key":
            return result
        api_key = next_key
        result = call(api_key)
    return result


def _resolve_size(size: str, aspect_ratio: str | None) -> str:
    if aspect_ratio:
        normalized = aspect_ratio.strip().lower()
        mapped = ASPECT_TO_SIZE.get(normalized)
        if mapped:
            return mapped
        return size
    return size


def generate_image(
    user_id: str, prompt: str, n: int = 1, size: str = "1024x1024",
    aspect_ratio: str | None = None, quality: str = "low",
) -> str:
    """Generate images, trying in order:

    1. The user's BYOK image endpoint, if one is set — `quality` has no
       effect here; a user's own endpoint carries no quality tiers of ours
       to pick between.
    2. HCAI, model chosen by `quality` (see
       agent.provider_config.build_image_provider_order) — falls back to
       the OTHER quality's HCAI model if the requested one's request fails
       (e.g. HCAI itself is down), before giving up on HCAI entirely.
    3. The global OPENAI_API_KEY, if set, as a last resort.

    Returns the first attempt's success, or the last attempt's error if
    every reachable option failed.
    """
    if user_id:
        ep_id = get_image_endpoint_id(user_id)
        if ep_id:
            ep = get_endpoint_decrypted(user_id, ep_id)
            if ep:
                return _generate_openai_compatible(ep["api_key"], ep["base_url"], ep["model"], prompt, n, size, aspect_ratio)

    from agent.fallback_cache import get_dead_families
    from agent.provider_config import build_image_provider_order

    dead_families = set(get_dead_families())
    attempts = [
        (c["api_key"], c["base_url"], c["model"], c.get("provider"))
        for c in build_image_provider_order(quality)
        if c.get("provider") not in dead_families
    ]
    global_key = os.environ.get("OPENAI_API_KEY")
    if global_key:
        attempts.append((global_key, "https://api.openai.com/v1", "dall-e-3", "openai"))

    if not attempts:
        return (
            "Error: No image generation API key found. Add an endpoint via "
            "BYOK (Home tab), configure HCAI_API_KEY, or set OPENAI_API_KEY globally."
        )

    result = ""
    for api_key, base_url, model, provider in attempts:
        if provider in dead_families:
            continue  # a sibling attempt above just marked this family dead
        result = _with_key_fallback(
            lambda key: _generate_openai_compatible(key, base_url, model, prompt, n, size, aspect_ratio),  # noqa: B023
            api_key, provider,
        )
        if "image(s)" in result:
            return result
        if provider and provider in get_dead_families():
            dead_families.add(provider)
    return result


MAX_REFERENCE_BYTES = 8 * 1024 * 1024


def _image_media_type(data: bytes) -> str:
    """Sniff an image's media type from its magic bytes (PNG default)."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/png"


def edit_images(prompt: str, references: list[bytes], quality: str = "low") -> str:
    """Edit/combine reference images with a prompt (e.g. "make the sky purple",
    "put the cat from image 1 into image 2"), via HCAI's image models.

    Same result format as generate_image. HCAI only: its image models take
    reference images through a multimodal chat completion, which BYOK image
    endpoints and OpenAI's images/generations fallback don't — there's no
    fallback beyond HCAI's own two models.
    """
    for i, data in enumerate(references, 1):
        if len(data) > MAX_REFERENCE_BYTES:
            return f"Error: reference image {i} is {len(data) // (1024 * 1024)}MB; resize it below 8MB first."

    from agent.fallback_cache import get_dead_families
    from agent.provider_config import build_image_provider_order

    dead_families = set(get_dead_families())
    attempts = [c for c in build_image_provider_order(quality) if c.get("provider") not in dead_families]
    if not attempts:
        return "Error: image editing needs HCAI (HCAI_API_KEY), which isn't available right now."

    result = ""
    for config in attempts:
        if config.get("provider") in dead_families:
            continue
        result = _with_key_fallback(
            lambda key: _edit_via_chat_completions(key, config["base_url"], config["model"], prompt, references),  # noqa: B023
            config["api_key"], config.get("provider"),
        )
        if "image(s)" in result:
            return result
        if config.get("provider") and config["provider"] in get_dead_families():
            dead_families.add(config["provider"])
    return result


def _edit_via_chat_completions(api_key: str, base_url: str, model: str, prompt: str, references: list[bytes]) -> str:
    """One multimodal chat completion that returns image(s) in
    choices[0].message.images[].image_url.url (verified live against HCAI)."""
    import base64

    content = [{"type": "text", "text": prompt}]
    for data in references:
        encoded = base64.b64encode(data).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:{_image_media_type(data)};base64,{encoded}"}})
    try:
        response = requests.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json={"model": model, "modalities": ["image", "text"], "messages": [{"role": "user", "content": content}]},
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=120,
        )
        res = response.json()
    except Exception as e:
        return f"Error editing image: {e}"
    if res.get("error"):
        return f"Image error: {(res['error'] or {}).get('message', 'unknown')}"
    message = ((res.get("choices") or [{}])[0].get("message") or {})
    urls = [
        (img.get("image_url") or {}).get("url", "")
        for img in (message.get("images") or [])
    ]
    urls = [u for u in urls if u]
    if not urls:
        reply = (message.get("content") or "").strip()
        return f"Image error: the model returned no image{': ' + reply[:300] if reply else ''}"
    return "Generated {} image(s):\n".format(len(urls)) + "\n".join(f"{i}. {u}" for i, u in enumerate(urls, 1))


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

    An image URL comes from whatever endpoint generated it — including a user's
    own BYOK endpoint — so it is fetched from INSIDE the sandbox (curl), never by
    the host: a host-side fetch of an attacker-chosen URL (localhost services,
    cloud metadata, via redirects too) whose result lands in a sandbox the same
    user can read would be a full-read SSRF against coolton's own server.

    Args:
        sandbox: An E2B sandbox.
        urls: Image URLs to fetch (data: URIs and http(s) only).
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
                name = f"coolton-image-{batch}-{i}.{_image_ext(header)}"
                sandbox.files.write(f"/home/user/downloads/{name}", content)
            elif u.startswith(("https://", "http://")):
                partial = f"/home/user/downloads/.coolton-image-{batch}-{i}.part"
                # curl -f exits non-zero on an HTTP error, which commands.run raises on.
                result = sandbox.commands.run(
                    f"curl -fsSL --proto =http,https --max-time 60 --max-filesize {_MAX_IMAGE_BYTES} "
                    f"-o {shlex.quote(partial)} -w '%{{content_type}}' {shlex.quote(u)}",
                    timeout=90,
                )
                name = f"coolton-image-{batch}-{i}.{_image_ext(result.stdout or '')}"
                sandbox.commands.run(f"mv {shlex.quote(partial)} {shlex.quote('/home/user/downloads/' + name)}")
            else:
                continue
            saved.append(f"~/downloads/{name}")
        except Exception:
            continue
    return saved


_MAX_IMAGE_BYTES = 25 * 1024 * 1024


def _image_ext(content_type: str) -> str:
    content_type = content_type.lower()
    if "jpeg" in content_type or "jpg" in content_type:
        return "jpg"
    if "webp" in content_type:
        return "webp"
    return "png"
