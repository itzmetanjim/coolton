import os
import requests

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
