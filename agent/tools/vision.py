import base64
import requests

IMAGE_MIME_PREFIX = "image/"
_UNSUPPORTED_IMAGE_MIMES = {"image/svg+xml", "image/x-icon", "image/vnd.microsoft.icon"}


def download_attached_images(client, files, max_images: int = 4, max_bytes: int = 10 * 1024 * 1024) -> list[dict]:
    """Download image files attached to a Slack message event.

    Args:
        client: Slack WebClient (used for its bot token).
        files: The `files` array from a Slack message event.
        max_images: Maximum number of images to fetch.
        max_bytes: Per-image size cap.

    Returns:
        List of {"data": bytes, "media_type": str, "name": str} dicts.
    """
    token = getattr(client, "token", None)
    if not token:
        return []
    images = []
    for f in files or []:
        if len(images) >= max_images:
            break
        mimetype = (f.get("mimetype") or "").lower()
        name = f.get("name") or f.get("filetype") or "image"
        if not mimetype.startswith(IMAGE_MIME_PREFIX) or mimetype in _UNSUPPORTED_IMAGE_MIMES:
            continue
        url = f.get("url_private_download") or f.get("url_private")
        if not url:
            continue
        try:
            resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if resp.status_code != 200 or len(resp.content) > max_bytes:
                continue
            images.append({"data": resp.content, "media_type": mimetype, "name": name})
        except requests.RequestException:
            continue
    return images


def analyze_image(image_data: bytes, filename: str, prompt: str = "Describe this image in detail.") -> str:
    """Analyze an image using a vision-capable model.

    Tries the vision provider chain in order and returns the first success.
    Each provider is OpenAI-compatible (chat completions with image_url).
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "png"
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
    mime = mime_map.get(ext, "image/png")
    b64 = base64.b64encode(image_data).decode()
    data_uri = f"data:{mime};base64,{b64}"

    errors = []
    for provider, base_url, api_key, model in _vision_provider_chain():
        result = _analyze_openai_compatible(data_uri, prompt, base_url, api_key, model)
        if result and not result.startswith("Error"):
            return result
        errors.append(f"{provider}/{model}: {result or 'empty response'}")
    return "Error: All vision providers failed:\n" + "\n".join(errors)


def _vision_provider_chain() -> list[tuple[str, str, str, str]]:
    """(provider_label, base_url, api_key, model) pairs in fallback order.

    Delegates to provider_config which reads from providers.json.
    """
    from agent.provider_config import build_vision_chain
    return build_vision_chain()


def _analyze_openai_compatible(data_uri: str, prompt: str, base_url: str, api_key: str, model: str) -> str:
    try:
        response = requests.post(
            f"{base_url}/chat/completions",
            json={
                "model": model,
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_uri, "detail": "high"}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=120,
        )
        res = response.json()
        if "choices" in res and res["choices"]:
            content = res["choices"][0].get("message", {}).get("content")
            if content:
                return content
        err = res.get("error", {})
        message = err.get("message") if isinstance(err, dict) else err
        return f"Error: {message or res}"
    except Exception as e:
        return f"Error: {e}"
