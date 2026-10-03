import base64

import pytest

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


# ---------------------------------------------------------------------------
# _AdminHandler._admin_ok — timing-safe comparison, fail-closed on empty token
# ---------------------------------------------------------------------------


def _admin_handler(auth_header):
    handler = gp._AdminHandler.__new__(gp._AdminHandler)
    handler.headers = {"Authorization": auth_header} if auth_header is not None else {}
    return handler


def test_admin_ok_accepts_the_real_token(monkeypatch):
    monkeypatch.setattr(gp, "ADMIN_TOKEN", "correct-admin-token")
    assert _admin_handler("Bearer correct-admin-token")._admin_ok() is True


def test_admin_ok_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(gp, "ADMIN_TOKEN", "correct-admin-token")
    assert _admin_handler("Bearer wrong-token")._admin_ok() is False


def test_admin_ok_rejects_missing_header(monkeypatch):
    monkeypatch.setattr(gp, "ADMIN_TOKEN", "correct-admin-token")
    assert _admin_handler(None)._admin_ok() is False


def test_admin_ok_fails_closed_when_admin_token_unconfigured(monkeypatch):
    """An empty ADMIN_TOKEN must never be trivially satisfiable by an empty
    presented value (mirrors coolton_web_helper._authorized's own posture)."""
    monkeypatch.setattr(gp, "ADMIN_TOKEN", "")
    assert _admin_handler("Bearer ")._admin_ok() is False
    assert _admin_handler("Bearer anything")._admin_ok() is False


def test_admin_ok_uses_constant_time_comparison(monkeypatch):
    monkeypatch.setattr(gp, "ADMIN_TOKEN", "correct-admin-token")
    calls = []
    real_compare = gp.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare(a, b)

    monkeypatch.setattr(gp.hmac, "compare_digest", spy)
    _admin_handler("Bearer wrong-token")._admin_ok()
    assert calls == [("wrong-token", "correct-admin-token")]


# ---------------------------------------------------------------------------
# _forward's denial log never contains the presented token verbatim
# ---------------------------------------------------------------------------


def test_forward_denial_log_never_contains_the_raw_token(monkeypatch, caplog):
    import io
    from unittest.mock import MagicMock

    handler = gp._Handler.__new__(gp._Handler)
    handler.command = "GET"
    handler.path = "/o/r"
    handler.headers = {"Authorization": "Bearer a-mistakenly-pasted-real-pat"}
    handler.rfile = io.BytesIO(b"")
    handler.wfile = io.BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.connection = MagicMock()

    with caplog.at_level("WARNING", logger="github_proxy"):
        handler._forward()

    assert "a-mistakenly-pasted-real-pat" not in caplog.text


# ---------------------------------------------------------------------------
# _forbidden_reason — sandboxed code can use GitHub normally through the proxy,
# but not for destructive / access-granting / code-exposing account actions.
# ---------------------------------------------------------------------------


_API = "https://api.github.com"


@pytest.mark.parametrize("method,path,body", [
    ("DELETE", "/repos/coolton-agent/coolton", None),
    ("POST", "/repos/coolton-agent/coolton/transfer", b'{"new_owner": "someone"}'),
    ("PATCH", "/repos/coolton-agent/coolton", b'{"private": false}'),
    ("PATCH", "/repos/coolton-agent/coolton", b'{"visibility": "public"}'),
    ("PATCH", "/repos/coolton-agent/coolton", b'{"archived": true}'),
    ("PUT", "/repos/coolton-agent/coolton/collaborators/someone", b"{}"),
    ("POST", "/repos/coolton-agent/coolton/keys", b'{"key": "ssh-ed25519 AAAA"}'),
    ("POST", "/repos/coolton-agent/coolton/hooks", b"{}"),
    ("PUT", "/repos/coolton-agent/coolton/actions/secrets/TOKEN", b"{}"),
    ("DELETE", "/repos/coolton-agent/coolton/branches/main/protection", None),
    ("POST", "/user/keys", b'{"key": "ssh-ed25519 AAAA"}'),
    ("PATCH", "/user", b'{"name": "x"}'),
    ("PUT", "/orgs/hackclub/memberships/someone", b"{}"),
    ("GET", "/authorizations", None),
    ("POST", "/graphql", b'{"query": "mutation { archiveRepository(input: {repositoryId: \\"R1\\"}) { clientMutationId } }"}'),
])
def test_destructive_github_calls_are_refused(method, path, body):
    assert gp._forbidden_reason(method, _API + path, body)


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/repos/coolton-agent/coolton", None),
    ("POST", "/repos/itzmetanjim/coolton/pulls", b'{"title": "fix", "head": "coolton-agent:fix", "base": "main"}'),
    ("POST", "/repos/itzmetanjim/coolton/issues/1/comments", b'{"body": "hi"}'),
    ("PATCH", "/repos/coolton-agent/coolton", b'{"description": "new description"}'),
    ("DELETE", "/repos/coolton-agent/coolton/git/refs/heads/old-branch", None),
    ("GET", "/repos/coolton-agent/coolton/collaborators", None),
    ("POST", "/user/repos", b'{"name": "scratch"}'),
    ("POST", "/graphql", b'{"query": "query { viewer { login } }"}'),
])
def test_normal_github_work_goes_through(method, path, body):
    assert gp._forbidden_reason(method, _API + path, body) is None


def test_non_api_hosts_are_not_filtered():
    # git smart-HTTP and raw content aren't REST calls.
    assert gp._forbidden_reason("POST", "https://github.com/coolton-agent/coolton.git/git-receive-pack", b"...") is None


# ---------------------------------------------------------------------------
# The allowlist must agree with the HTTP client about which host a URL means:
# urllib's urlparse and urllib3 (what requests uses) disagree on some URLs, and
# a URL that looks like *.githubusercontent.com to one but evil.example to the
# other would forward the real PAT to evil.example.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", [
    "https://evil.example\\.githubusercontent.com/x",  # backslash: urllib3 host is evil.example
    "https://evil.example\\@api.github.com/x",
    "https://user@api.github.com/x",  # userinfo would replace the Authorization header
    "https://user:pw@raw.githubusercontent.com/x",
    "https://evil.example%2f.github.io/x",
    "https://evil_host.githubusercontent.com/x",
])
def test_ambiguous_or_non_dns_upstreams_are_denied(url):
    assert not gp._is_allowed_upstream(url)
    assert not gp._needs_auth(url)


def test_requests_really_would_have_gone_elsewhere_for_the_backslash_host():
    """Pins down the parser disagreement the check above guards against."""
    import requests

    url = "https://evil.example\\.githubusercontent.com/x"
    assert gp.urlparse(url).netloc.endswith(".githubusercontent.com")
    assert requests.Request("GET", url).prepare().url.startswith("https://evil.example/")


def test_pages_owner_segment_cannot_smuggle_a_host():
    upstream = gp._rewrite_url("pages.ghproxy.tanjim.org", "/evil.example\\/index.html")
    assert not gp._is_allowed_upstream(upstream)
    upstream = gp._rewrite_url("ghproxy.tanjim.org", "/pages/evil.example@x/index.html")
    assert not gp._is_allowed_upstream(upstream)


def test_forward_never_contacts_an_ambiguous_host(monkeypatch):
    """End to end through the handler: a spoofed Host header is refused before
    any upstream request is made."""
    gp.allowlist.add("sandbox-token")
    calls = []
    monkeypatch.setattr(gp.requests, "request", lambda *a, **k: calls.append((a, k)))

    handler = gp._Handler.__new__(gp._Handler)
    handler.command = "GET"
    handler.path = "/x"
    handler.headers = {"Host": "evil.example\\.githubusercontent.com", "Authorization": "Bearer sandbox-token"}
    denied = []
    handler._deny = lambda code=403, challenge=False: denied.append(code)
    try:
        handler._forward()
    finally:
        gp.allowlist.remove("sandbox-token")
    assert denied == [502]
    assert calls == []


# ---------------------------------------------------------------------------
# request bodies: gzip (git's larger requests) and chunked (large pushes)
# ---------------------------------------------------------------------------


def _send(monkeypatch, path, headers, raw_body):
    """Push one POST through _forward; returns (what reached GitHub or None, response code)."""
    import io
    from unittest.mock import MagicMock

    captured = {}

    class _FakeResp:
        status_code = 200
        headers = {}

        def iter_content(self, n):
            return iter([b""])

        def close(self):
            pass

    def fake_request(method, url, data=None, headers=None, **kwargs):
        captured.update(url=url, data=data, headers=headers)
        return _FakeResp()

    monkeypatch.setattr(gp.requests, "request", fake_request)
    gp.allowlist.add("sandbox-tok")
    handler = gp._Handler.__new__(gp._Handler)
    handler.command = "POST"
    handler.path = path
    handler.headers = {"Authorization": "Bearer sandbox-tok", "Host": HOST, **headers}
    handler.rfile = io.BytesIO(raw_body)
    handler.wfile = io.BytesIO()
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    handler.connection = MagicMock()
    handler._forward()
    return (captured or None), handler.send_response.call_args.args[0]


def test_a_gzipped_git_request_reaches_github_with_its_encoding(monkeypatch):
    """git gzips its larger fetch negotiation; dropping Content-Encoding made GitHub
    answer 400 ("RPC failed; HTTP 400") for any clone with enough unknown local history."""
    import gzip

    body = gzip.compress(b"0032want 645855beed708ce5b3312c2e99593c32d52b1ad6\n" * 50)
    sent, _ = _send(monkeypatch, "/o/r/git-upload-pack", {
        "Content-Encoding": "gzip", "Content-Length": str(len(body)), "Git-Protocol": "version=2",
        "Content-Type": "application/x-git-upload-pack-request"}, body)

    assert sent["url"] == "https://github.com/o/r/git-upload-pack" and sent["data"] == body
    assert sent["headers"]["Content-Encoding"] == "gzip" and sent["headers"]["Git-Protocol"] == "version=2"


def test_gzip_cant_hide_a_forbidden_call_and_other_encodings_are_refused(monkeypatch):
    import gzip
    import json as _json

    mutation = gzip.compress(_json.dumps({"query": "mutation { deleteRepository(input: {}) { clientMutationId } }"}).encode())
    sent, code = _send(monkeypatch, "/api/graphql", {"Content-Encoding": "gzip", "Content-Length": str(len(mutation))}, mutation)
    assert sent is None and code == 403

    sent, code = _send(monkeypatch, "/api/graphql", {"Content-Encoding": "br", "Content-Length": "4"}, b"abcd")
    assert sent is None and code == 415


def test_a_chunked_request_body_is_read_in_full(monkeypatch):
    raw = b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    sent, _ = _send(monkeypatch, "/o/r/git-receive-pack", {"Transfer-Encoding": "chunked"}, raw)
    assert sent["data"] == b"hello world"


def test_oversized_bodies_are_refused_before_anything_reaches_github(monkeypatch):
    """Bodies are held in memory to check and forward: past MAX_BODY_BYTES (raw, chunked,
    or after un-gzipping) they're refused, so a sandbox can't exhaust the server's memory."""
    import gzip

    monkeypatch.setattr(gp, "MAX_BODY_BYTES", 1000)
    sent, code = _send(monkeypatch, "/o/r/git-receive-pack", {"Content-Length": "1001"}, b"x" * 1001)
    assert sent is None and code == 413

    chunked = b"3e8\r\n" + b"x" * 1000 + b"\r\n1\r\nx\r\n0\r\n\r\n"
    sent, code = _send(monkeypatch, "/o/r/git-receive-pack", {"Transfer-Encoding": "chunked"}, chunked)
    assert sent is None and code == 413

    bomb = gzip.compress(b"\0" * 100_000)  # a few hundred bytes that inflate 100x past the cap
    sent, code = _send(monkeypatch, "/o/r/git-upload-pack",
                       {"Content-Encoding": "gzip", "Content-Length": str(len(bomb))}, bomb)
    assert len(bomb) < 1000 and sent is None and code == 413


def test_a_corrupt_gzip_or_chunk_size_is_a_bad_request(monkeypatch):
    sent, code = _send(monkeypatch, "/o/r/git-upload-pack", {"Content-Encoding": "gzip", "Content-Length": "4"}, b"nope")
    assert sent is None and code == 400

    sent, code = _send(monkeypatch, "/o/r/git-receive-pack", {"Transfer-Encoding": "chunked"}, b"zz\r\nhi\r\n0\r\n\r\n")
    assert sent is None and code == 400


def test_multi_member_gzip_is_checked_in_full(monkeypatch):
    """gzip.decompress read every member; a forbidden call in a later member must not slip past."""
    import gzip
    import json as _json

    body = gzip.compress(b" ") + gzip.compress(
        _json.dumps({"query": "mutation { deleteRepository(input: {}) { clientMutationId } }"}).encode())
    sent, code = _send(monkeypatch, "/api/graphql", {"Content-Encoding": "gzip", "Content-Length": str(len(body))}, body)
    assert sent is None and code == 403
