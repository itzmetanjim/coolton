import base64
import os
import requests


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
