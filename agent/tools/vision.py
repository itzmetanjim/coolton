import base64
import os
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
    """Analyze an image using the AI model with vision capabilities.

    Uses the globally configured model (Claude or GPT-4o). The image is
    passed as a base64-encoded data URI.

    Args:
        image_data: Raw bytes of the image file.
        filename: Original filename (used to infer mime type).
        prompt: The analysis prompt (default: "Describe this image in detail.").

    Returns:
        Analysis text from the vision model.
    """
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "png"
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}
    mime = mime_map.get(ext, "image/png")
    b64 = base64.b64encode(image_data).decode()
    data_uri = f"data:{mime};base64,{b64}"

    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    openai_key = os.environ.get("OPENAI_API_KEY")

    if anthropic_key:
        return _analyze_with_anthropic(data_uri, prompt, anthropic_key)
    elif openai_key:
        return _analyze_with_openai(data_uri, prompt, openai_key)
    else:
        return "Error: No AI provider configured with vision capabilities (Anthropic or OpenAI)."


def _analyze_with_anthropic(data_uri: str, prompt: str, api_key: str) -> str:
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": "claude-sonnet-4-20250514",
                "max_tokens": 4096,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": data_uri.split(";")[0].split(":")[1], "data": data_uri.split(",")[1]}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        res = response.json()
        if "content" in res:
            return "".join(b["text"] for b in res["content"] if b.get("type") == "text")
        return f"Anthropic API error: {res.get('error', {}).get('message', 'unknown')}"
    except Exception as e:
        return f"Error analyzing image with Anthropic: {str(e)}"


def _analyze_with_openai(data_uri: str, prompt: str, api_key: str) -> str:
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            json={
                "model": "gpt-4o",
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
            timeout=30,
        )
        res = response.json()
        if "choices" in res:
            return res["choices"][0]["message"]["content"]
        return f"OpenAI API error: {res.get('error', {}).get('message', 'unknown')}"
    except Exception as e:
        return f"Error analyzing image with OpenAI: {str(e)}"
