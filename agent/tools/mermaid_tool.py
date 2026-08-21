import base64
import zlib
from urllib.parse import quote

import requests


KROKI_URL = "https://kroki.io"
MERMAID_INK_URL = "https://mermaid.ink"


def render_mermaid(diagram_code: str, theme: str = "default") -> str:
    """Render a Mermaid diagram and return a URL to the PNG image.

    Tries kroki.io first (reliable), falls back to mermaid.ink.

    Args:
        diagram_code: The Mermaid diagram definition (e.g. "graph TD; A-->B;").
        theme: Mermaid theme ("default", "dark", "forest", "neutral", default "default").

    Returns:
        A URL string pointing to the rendered PNG, or an error message.
    """
    if not diagram_code.strip():
        return "Error: Empty diagram code"

    url = _render_with_kroki(diagram_code, theme)
    if url.startswith("http"):
        return url

    return _render_with_mermaid_ink(diagram_code, theme)


def _verify_get(url: str) -> bool:
    """GET the render URL and check it actually renders (2xx).

    Renders are verified with GET, not HEAD: kroki.io returns 404 on HEAD but 200
    on GET, and mermaid.ink returns 400 on HEAD. Streaming keeps only the status.
    """
    try:
        resp = requests.get(url, stream=True, timeout=30)
        ok = resp.status_code == 200
        resp.close()
        return ok
    except Exception:
        return False


def _render_with_kroki(diagram_code: str, theme: str) -> str:
    """Primary renderer using kroki.io."""
    try:
        compressed = zlib.compress(diagram_code.encode())
        encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        url = f"{KROKI_URL}/mermaid/png/{encoded}"
        if theme != "default":
            url += f"?theme={quote(theme, safe='')}"
        if _verify_get(url):
            return url
    except Exception:
        pass
    return ""


def _render_with_mermaid_ink(diagram_code: str, theme: str) -> str:
    """Fallback renderer using mermaid.ink."""
    try:
        compressed = zlib.compress(diagram_code.encode())
        encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        url = f"{MERMAID_INK_URL}/img/{encoded}?theme={quote(theme, safe='')}"
        if _verify_get(url):
            return url
    except Exception:
        pass
    return "Error: Failed to render diagram on both kroki.io and mermaid.ink"
