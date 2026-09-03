import base64

import github_proxy as gp


# ---------------------------------------------------------------------------
# _is_allowed_upstream — the SSRF / token-exfiltration guard
# ---------------------------------------------------------------------------

_ALLOWED = [
    "https://github.com/owner/repo",
    "https://api.github.com/repos/o/r",
    "https://uploads.github.com/uploads/1",
    "https://raw.githubusercontent.com/o/r/main/x.py",
    "https://gist.github.com/user/abc123",
    "https://codeload.github.com/o/r/tar.gz/refs/heads/main",
    "https://objects.githubusercontent.com/some/path",
    "https://media.githubusercontent.com/media/o/r/x.png",
    "https://camo.githubusercontent.com/hash",
    "https://avatars.githubusercontent.com/u/1",
    "https://evil.githubusercontent.com/something",  # wildcard suffix
    "https://someone.github.io/",                    # wildcard pages host
    "https://raw.githubusercontent.com:443/x",       # port is stripped
]

_DENIED = [
    "https://evil.com/steal?token=x",
    "https://github.com.evil.com/repo",          # lookalike domain, not github
    "https://githubusercontent.com.evil.com/x",
    "https://notgithub.io/x",
    "http://127.0.0.1/",
    "http://169.254.169.254/latest/meta-data",   # cloud metadata SSRF
    "https://api.github.com.evil.org/x",
    "https://example.github.com",                 # 'github.com' not a suffix here
]


def test_allowed_upstream_hosts():
    for url in _ALLOWED:
        assert gp._is_allowed_upstream(url), url


def test_denied_upstream_hosts():
    for url in _DENIED:
        assert not gp._is_allowed_upstream(url), url


# ---------------------------------------------------------------------------
# translate_ghe_to_github / _rewrite_url
# ---------------------------------------------------------------------------

HOST = gp.PROXY_HOST_SUFFIX


def test_translate_api_v3():
    url = f"https://{HOST}/api/v3/repos/o/r"
    assert gp.translate_ghe_to_github(url) == "https://api.github.com/repos/o/r"


def test_translate_api_graphql():
    url = f"https://{HOST}/api/graphql"
    assert gp.translate_ghe_to_github(url) == "https://api.github.com/graphql"


def test_translate_bare_host():
    url = f"https://{HOST}/owner/repo"
    assert gp.translate_ghe_to_github(url) == "https://github.com/owner/repo"


def test_translate_raw_subpath():
    url = f"https://{HOST}/owner/repo/raw/main/file.py"
    assert (
        gp.translate_ghe_to_github(url)
        == "https://raw.githubusercontent.com/owner/repo/main/file.py"
    )


def test_translate_raw_subdomain():
    url = f"https://raw.{HOST}/owner/repo/main/file.py"
    assert (
        gp.translate_ghe_to_github(url)
        == "https://raw.githubusercontent.com/owner/repo/main/file.py"
    )


def test_translate_gist_subdomain():
    url = f"https://gist.{HOST}/user/abc123"
    assert gp.translate_ghe_to_github(url) == "https://gist.github.com/user/abc123"


def test_translate_pages_subdomain():
    url = f"https://pages.{HOST}/myowner/some/path"
    assert gp.translate_ghe_to_github(url) == "https://myowner.github.io/some/path"


def test_translate_pages_subpath():
    url = f"https://{HOST}/pages/myowner"
    assert gp.translate_ghe_to_github(url) == "https://myowner.github.io/"


def test_translate_uploads():
    url = f"https://{HOST}/api/v3/uploads/assets/1"
    assert gp.translate_ghe_to_github(url) == "https://uploads.github.com/assets/1"


def test_translate_ssh():
    assert (
        gp.translate_ghe_to_github(f"git@{HOST}:o/r.git") == "git@github.com:o/r.git"
    )


def test_translate_unknown_host_unchanged():
    url = "https://example.com/x"
    assert gp.translate_ghe_to_github(url) == url


def test_rewrite_url():
    assert gp._rewrite_url(HOST, "/owner/repo") == "https://github.com/owner/repo"
    assert gp._rewrite_url("api.github.com", "/repos/o/r") == "https://api.github.com/repos/o/r"


# ---------------------------------------------------------------------------
# _real_auth
# ---------------------------------------------------------------------------


def test_real_auth_api_token_form(monkeypatch):
    monkeypatch.setattr(gp, "GITHUB_TOKEN", "ghp_secret")
    assert gp._real_auth("https://api.github.com/repos") == "token ghp_secret"


def test_real_auth_basic_form(monkeypatch):
    monkeypatch.setattr(gp, "GITHUB_TOKEN", "ghp_secret")
    expected = "Basic " + base64.b64encode(b"ghp_secret:").decode()
    assert gp._real_auth("https://github.com/o/r") == expected


# ---------------------------------------------------------------------------
# _needs_auth — must never attach the real PAT to a GitHub Pages host, since
# *.github.io is literally any GitHub user's own free static site (unlike
# every other allowed upstream, which is fixed GitHub-operated infrastructure)
# ---------------------------------------------------------------------------


def test_needs_auth_false_for_pages_hosts():
    assert not gp._needs_auth("https://someone.github.io/site")
    assert not gp._needs_auth("https://attacker.github.io/steal")
    assert not gp._needs_auth("https://github.io/")


def test_needs_auth_true_for_real_github_infrastructure():
    assert gp._needs_auth("https://github.com/o/r")
    assert gp._needs_auth("https://api.github.com/repos/o/r")
    assert gp._needs_auth("https://uploads.github.com/assets/1")
    assert gp._needs_auth("https://codeload.github.com/o/r/tar.gz/main")
    assert gp._needs_auth("https://gist.github.com/user/abc123")
    assert gp._needs_auth("https://raw.githubusercontent.com/o/r/main/x.py")
    assert gp._needs_auth("https://objects.githubusercontent.com/some/path")


def test_forward_never_sends_real_pat_to_a_pages_host(monkeypatch):
    """End-to-end regression for the credential-exfiltration path: even when the
    proxy is fooled (e.g. a spoofed Host header) into targeting a github.io host —
    which anyone can stand up for free — the real PAT must never leave this
    process in the forwarded request."""
    monkeypatch.setattr(gp, "GITHUB_TOKEN", "ghp_realsecretpat")
    captured = {}

    class _FakeResp:
        status_code = 200
        headers = {}

        def iter_content(self, n):
            return iter([b""])

        def close(self):
            pass

    def fake_request(method, url, data=None, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr(gp.requests, "request", fake_request)
    gp.allowlist.add("sandbox-tok")

    import io
    from unittest.mock import MagicMock

    handler = gp._Handler.__new__(gp._Handler)
    handler.command = "GET"
    handler.path = "/steal"
    handler.headers = {"Authorization": "Bearer sandbox-tok", "Host": "attacker.github.io"}
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.connection = MagicMock()
    handler._forward()

    assert captured["url"] == "https://attacker.github.io/steal"
    assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# translate "ghproxy host" netloc handling in _rewrite_url
# ---------------------------------------------------------------------------


def test_rewrite_url_with_port_in_host():
    assert gp._rewrite_url(f"{HOST}:443", "/o/r") == "https://github.com/o/r"
