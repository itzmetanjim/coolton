import base64
import zlib
import requests


MERMAID_INK_URL = "https://mermaid.ink"
KROKI_URL = "https://kroki.io"


def render_mermaid(diagram_code: str, theme: str = "default") -> str:
    """Render a Mermaid diagram and return a URL to the PNG image.

    Tries mermaid.ink first, falls back to kroki.io for better compatibility.

    Args:
        diagram_code: The Mermaid diagram definition (e.g. "graph TD; A-->B;").
        theme: Mermaid theme ("default", "dark", "forest", "neutral", default "default").

    Returns:
        A URL string pointing to the rendered PNG, or an error message.
    """
    # Validate diagram code isn't empty
    if not diagram_code.strip():
        return "Error: Empty diagram code"

    # Try mermaid.ink first
    url = _render_with_mermaid_ink(diagram_code, theme)
    if url.startswith("http"):
        return url

    # Fallback to kroki.io
    return _render_with_kroki(diagram_code, theme)


def _render_with_mermaid_ink(diagram_code: str, theme: str) -> str:
    try:
        compressed = zlib.compress(diagram_code.encode())
        encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        url = f"{MERMAID_INK_URL}/img/{encoded}?theme={theme}"
        # Quick HEAD request to verify
        resp = requests.head(url, timeout=5)
        if resp.status_code == 200:
            return url
    except Exception:
        pass
    return ""


def _render_with_kroki(diagram_code: str, theme: str) -> str:
    """Fallback renderer using kroki.io which supports more Mermaid features."""
    try:
        compressed = zlib.compress(diagram_code.encode())
        encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        # kroki.io uses a different encoding path
        url = f"{KROKI_URL}/mermaid/svg/{encoded}"
        # kroki accepts theme via query params
        if theme != "default":
            url += f"?theme={theme}"
        resp = requests.head(url, timeout=5)
        if resp.status_code == 200:
            return url
    except Exception:
        pass
    return "Error: Failed to render diagram on both mermaid.ink and kroki.io"


def render_mermaid_svg(diagram_code: str, theme: str = "default") -> str:
    """Render a Mermaid diagram as SVG using kroki.io (more reliable)."""
    try:
        compressed = zlib.compress(diagram_code.encode())
        encoded = base64.urlsafe_b64encode(compressed).decode().rstrip("=")
        url = f"{KROKI_URL}/mermaid/svg/{encoded}"
        if theme != "default":
            url += f"?theme={theme}"
        return url
    except Exception as e:
        return f"Error rendering Mermaid SVG: {str(e)}"