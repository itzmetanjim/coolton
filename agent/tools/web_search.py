import os
import re

import requests

# A URL, or a site: filter narrowed to one page ("site:example.com/some/page"), in a query.
_URL_RE = re.compile(r"https?://\S+")
_SITE_PAGE_RE = re.compile(r"\bsite:([\w.-]+\.[a-z]{2,}/\S+)", re.I)
_TRAILING = ").,?!;:'\">"  # sentence punctuation after a URL in prose


def specific_page_url(query: str) -> str | None:
    """The URL of the one page a search query is really after (a URL in it, or a
    site: filter with a path), or None for an actual search. A site: filter on a
    whole domain ("site:docs.python.org") is a search, not a page."""
    match = _URL_RE.search(query or "")
    if match:
        return match.group(0).rstrip(_TRAILING)
    match = _SITE_PAGE_RE.search(query or "")
    return f"https://{match.group(1).rstrip(_TRAILING)}" if match else None


EXA_API_URL = "https://api.exa.ai/search"
EXA_API_KEY_ENV = "EXA_API_KEY"


def search_web(query: str, num_results: int = 8) -> str:
    """Search the web using the Exa API.

    Args:
        query: The search query string.
        num_results: Number of results to return (1-20, default 8).
    """
    api_key = os.environ.get(EXA_API_KEY_ENV)
    if not api_key:
        return "Error: EXA_API_KEY is not configured in the server's .env file."
    num_results = max(1, min(int(num_results), 20))

    try:
        response = requests.post(
            EXA_API_URL,
            json={"query": query, "numResults": num_results},
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        res_json = response.json()
        if response.status_code != 200:
            err = res_json.get("error")
            if isinstance(err, dict):
                err = err.get("message", str(err))
            return f"Exa API error (status {response.status_code}): {err or 'unknown'}"

        results = res_json.get("results", [])
        if not results:
            return "No results found."

        lines = []
        for i, r in enumerate(results[:num_results], 1):
            title = r.get("title", "No title")
            url = r.get("url", "")
            snippet = r.get("text", r.get("snippet", ""))
            published = r.get("publishedDate", "")
            date_str = f" ({published[:10]})" if published else ""
            lines.append(f"{i}. [{title}]({url}){date_str}")
            if snippet:
                lines.append(f"   {snippet[:300]}")
        return "\n".join(lines)

    except requests.Timeout:
        return "Error: Exa API request timed out."
    except Exception as e:
        return f"Error searching the web: {str(e)}"


EXA_CONTENTS_URL = "https://api.exa.ai/contents"


def fetch_url(url: str, max_characters: int = 8000) -> str:
    """Fetch the readable text content of a specific URL using Exa.

    Args:
        url: The full URL to fetch (e.g. https://example.com/article).
        max_characters: Max characters of text to return (default 8000).
    """
    api_key = os.environ.get(EXA_API_KEY_ENV)
    if not api_key:
        return "Error: EXA_API_KEY is not configured in the server's .env file."
    if not url:
        return "Error: url is required"

    try:
        response = requests.post(
            EXA_CONTENTS_URL,
            json={
                "urls": [url],
                "text": {"includeHtml": False, "maxCharacters": max_characters},
                "summary": False,
            },
            headers={
                "x-api-key": api_key,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        res_json = response.json()
        if response.status_code != 200:
            err = res_json.get("error")
            if isinstance(err, dict):
                err = err.get("message", str(err))
            return f"Exa API error (status {response.status_code}): {err or 'unknown'}"

        results = res_json.get("results", [])
        if not results:
            return f"No content found at {url}"

        result = results[0]
        title = result.get("title", "No title")
        text = result.get("text", "")
        if not text:
            return f"No readable text found at {url}"
        published = result.get("publishedDate", "")
        date_str = f" ({published[:10]})" if published else ""
        return f"# {title}{date_str}\nURL: {url}\n\n{text[:max_characters]}"

    except requests.Timeout:
        return "Error: Exa API request timed out."
    except Exception as e:
        return f"Error fetching URL: {str(e)}"
