#!/usr/bin/env python3
"""coolton GitHub proxy.

A tiny standalone HTTP forward proxy that lets the sandbox talk to GitHub as the
coolton-agent user WITHOUT the real PAT ever leaving this host.

How it works (no CA, no MITM, no CONNECT):
  * It listens on localhost:29054. Caddy terminates TLS for ghproxy.tanjim.org and
    reverse-proxies plain HTTP to it, so the sandbox connects directly to
    https://ghproxy.tanjim.org (over normal HTTPS) - no proxy env, no CONNECT.
  * The sandbox authenticates with a short-lived *sandbox token* (never the real PAT).
    gh sends `Authorization: Bearer <sandbox_token>`; git sends
    `Authorization: Basic <anything>:<sandbox_token>`.
  * If the presented token is in the allowlist, the proxy rewrites it to the real
    PAT (`Authorization: Basic <PAT>`) and forwards the request to github.com,
    mapping ghproxy.tanjim.org hostnames back to github.com.
  * Unknown tokens get 403.

Run:
    python3 github_proxy.py
Importable:
    from github_proxy import add_token, remove_token, start_proxy, stop_proxy
"""

import base64
import json
import logging
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, urlunparse

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("github_proxy")

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 29054
# Admin endpoint (localhost only) for the agent to issue/revoke per-sandbox tokens.
ADMIN_PORT = 29055
PROXY_HOST_SUFFIX = "ghproxy.tanjim.org"
GITHUB_TOKEN = os.environ.get("COOLTON_GH_TOKEN", "")
# Token used to authenticate admin calls (set via GH_PROXY_ADMIN_TOKEN; defaults to the
# GitHub token so a single secret protects both). Never exposed to sandboxes.
ADMIN_TOKEN = os.environ.get("GH_PROXY_ADMIN_TOKEN", "") or GITHUB_TOKEN


def _real_auth(upstream: str) -> str:
    # git smart-HTTP endpoints (github.com, uploads.github.com) require HTTP Basic auth
    # with the PAT as the password; the REST/GraphQL API accepts "token <PAT>" (what gh
    # sends). Use Basic for git, token form for the API.
    if "api.github.com" in upstream or "uploads.github.com" in upstream:
        return "token " + GITHUB_TOKEN
    return "Basic " + base64.b64encode(f"{GITHUB_TOKEN}:".encode()).decode()


def translate_ghe_to_github(url: str, ghe_hostname: str = PROXY_HOST_SUFFIX) -> str:
    """Translate a GitHub Enterprise (ghproxy.tanjim.org) URL to its github.com equivalent.

    gh treats a custom GH_HOST as GitHub Enterprise and emits GHE-style URLs:
    REST under /api/v3, GraphQL at /api/graphql, and git/UI on the bare host. This maps
    all of those (plus raw/gist/pages/upload variants) back to the public github.com
    endpoints. See the shared reference implementation for the full matrix.
    """
    if url.startswith(f"git@{ghe_hostname}:"):
        return url.replace(f"git@{ghe_hostname}:", "git@github.com:")

    parsed = urlparse(url)
    if parsed.scheme == "ssh" and parsed.netloc == f"git@{ghe_hostname}":
        return url.replace(f"git@{ghe_hostname}", "git@github.com")

    netloc = parsed.netloc.lower()
    path = parsed.path
    host_only = netloc.split(":")[0]

    # Case A: GitHub Pages subdomain (pages.host -> [owner].github.io)
    if host_only == f"pages.{ghe_hostname}":
        parts = [p for p in path.split("/") if p]
        if parts:
            owner = parts[0]
            new_netloc = f"{owner}.github.io"
            new_path = "/" + "/".join(parts[1:])
        else:
            new_netloc, new_path = "github.io", path

    # Case B: GitHub Pages subpath (host/pages/owner -> [owner].github.io)
    elif host_only == ghe_hostname and path.startswith("/pages/"):
        parts = [p for p in path.split("/") if p][1:]
        if parts:
            owner = parts[0]
            new_netloc = f"{owner}.github.io"
            new_path = "/" + "/".join(parts[1:])
        else:
            new_netloc, new_path = "github.io", "/"

    # Case C: Raw subdomain (raw.host -> raw.githubusercontent.com)
    elif host_only == f"raw.{ghe_hostname}":
        new_netloc, new_path = "raw.githubusercontent.com", path

    # Case D: Gist subdomain (gist.host -> gist.github.com)
    elif host_only == f"gist.{ghe_hostname}":
        new_netloc, new_path = "gist.github.com", path

    # Case E: Standard host endpoints
    elif host_only == ghe_hostname:
        if path.startswith("/api/v3/uploads"):
            new_netloc = "uploads.github.com"
            new_path = path.replace("/api/v3/uploads", "", 1)
        elif path.startswith("/api/v3/"):
            new_netloc = "api.github.com"
            new_path = path.replace("/api/v3", "", 1)
        elif path == "/api/v3":
            new_netloc, new_path = "api.github.com", "/"
        elif path.startswith("/api/graphql"):
            new_netloc = "api.github.com"
            new_path = path.replace("/api/graphql", "/graphql", 1)
        elif re.match(r"^/[^/]+/[^/]+/raw/", path):
            new_netloc = "raw.githubusercontent.com"
            new_path = re.sub(r"^/([^/]+)/([^/]+)/raw/(.+)$", r"/\1/\2/\3", path)
        else:
            new_netloc, new_path = "github.com", path

    else:
        return url

    if new_path.startswith("//"):
        new_path = "/" + new_path.lstrip("/")

    return urlunparse(parsed._replace(netloc=new_netloc, path=new_path))


def _rewrite_url(host: str, path: str) -> str:
    """Build the full upstream github.com URL for an incoming proxy request."""
    return translate_ghe_to_github(f"https://{host}{path}")


class _Allowlist:
    """Per-sandbox scoped allowlist.

    Tokens are keyed by (user, token) where `user` is the sandbox id the client
    sends as the Basic-auth username. A token issued for sandbox A is NOT valid
    for sandbox B, so a leaked token cannot be reused across sandboxes.
    """

    def __init__(self):
        self._set = set()
        self._lock = threading.Lock()

    def add(self, tok: str, user: str = ""):
        if tok:
            with self._lock:
                self._set.add((user, tok))

    def remove(self, tok: str, user: str = ""):
        with self._lock:
            self._set.discard((user, tok))

    def contains(self, tok: str, user: str = "") -> bool:
        with self._lock:
            return (user, tok) in self._set


allowlist = _Allowlist()
# A long-lived test token may be supplied via GH_PROXY_TOKEN (handy for manual testing);
# the agent issues short-lived per-sandbox tokens at runtime via add_token()/remove_token()
# (or the admin HTTP endpoint). If GH_PROXY_TOKEN is set it is always authorized.
_test_token = os.environ.get("GH_PROXY_TOKEN", "")
if _test_token:
    allowlist.add(_test_token)


# Importable helpers (these are what the agent will call later).
def add_token(tok: str, user: str = ""):
    allowlist.add(tok, user)


def remove_token(tok: str, user: str = ""):
    allowlist.remove(tok, user)


class _AdminHandler(BaseHTTPRequestHandler):
    """localhost-only admin API for issuing/revoking per-sandbox tokens.

    POST /tokens   {token: "..."}   -> authorize a sandbox token
    DELETE /tokens  {token: "..."}  -> revoke a sandbox token
    GET  /health                -> {"ok": true}
    Authenticated with `Authorization: Bearer <GH_PROXY_ADMIN_TOKEN>`.
    """

    protocol_version = "HTTP/1.1"

    def _admin_ok(self) -> bool:
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            return h[7:].strip() == ADMIN_TOKEN
        return False

    def _send_json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/health" and self._admin_ok():
            self._send_json(200, {"ok": True})
        else:
            self._send_json(403, {"error": "forbidden"})

    def do_POST(self):
        if self.path != "/tokens" or not self._admin_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        body = self._read_json() or {}
        tok = body.get("token", "")
        user = body.get("user", "")
        if not tok:
            self._send_json(400, {"error": "missing token"})
            return
        allowlist.add(tok, user)
        self._send_json(200, {"ok": True})

    def do_DELETE(self):
        if self.path != "/tokens" or not self._admin_ok():
            self._send_json(403, {"error": "forbidden"})
            return
        body = self._read_json() or {}
        tok = body.get("token", "")
        user = body.get("user", "")
        if tok:
            allowlist.remove(tok, user)
        self._send_json(200, {"ok": True})

    def log_message(self, *a): pass


def issue_token(tok: str, user: str = ""):
    """Authorize a sandbox token (callable from the agent process directly OR via admin HTTP)."""
    allowlist.add(tok, user)


def revoke_token(tok: str, user: str = ""):
    allowlist.remove(tok, user)


def _extract_token(headers: dict) -> str | None:
    """Return the sandbox token from the request, or None if not authorized.

    Handles the ways clients send it:
      * gh (github.com):   Authorization: Bearer <tok>
      * gh (custom host):  Authorization: token <tok>   (GH_ENTERPRISE_TOKEN)
      * git (basic auth):  Authorization: Basic <user>:<tok>
    """
    auth = headers.get("Proxy-Authorization") or headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[len("Bearer "):].strip() or None
    if auth.startswith("token ") or auth.startswith("Token "):
        return auth.split(" ", 1)[1].strip() or None
    user = ""
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode(errors="ignore")
        except Exception:
            return None, None
        # git form is "user:token"; user is the sandbox id, password is the token
        if ":" in decoded:
            user, tok = decoded.rsplit(":", 1)
        else:
            tok = decoded
        return tok.strip() or None, user.strip()
    tok = auth.split(" ", 1)[1].strip() if " " in auth else None
    return tok or None, user


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _deny(self, code=403, challenge=False):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain")
        if challenge:
            # git sends an anonymous probe first; respond 401 (not 403) with a Basic
            # challenge so git retries using its credential helper.
            self.send_header("WWW-Authenticate", 'Basic realm="GitHub"')
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(b"forbidden")
        self.close_connection = True

    def _forward(self):
        tok, user = _extract_token(self.headers)
        if tok is None:
            # No credential presented (e.g. git's first anonymous probe). Challenge so
            # the client retries with auth instead of failing outright.
            self._deny(401, challenge=True)
            return
        if not allowlist.contains(tok, user):
            logger.warning("deny: tok=%r user=%r in_allowlist=False", tok, user)
            self._deny(403)
            return

        target = self.path
        if target.startswith("http://") or target.startswith("https://"):
            parsed = urlparse(target)
            host = parsed.netloc
        else:
            host = self.headers.get("Host", "")
            parsed = None

        # gh treats GH_HOST as GitHub Enterprise, so its URLs are GHE-formatted
        # (/api/v3, /api/graphql, bare host for git/UI). Translate back to github.com.
        upstream = _rewrite_url(host.split(":")[0], self.path)

        body = None
        if self.command in ("POST", "PUT", "PATCH"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length) if length else None

        fwd = {
            "Authorization": _real_auth(upstream),
            "User-Agent": self.headers.get("User-Agent", "coolton-sandbox"),
            "Accept": self.headers.get("Accept", "*/*"),
        }
        if "Content-Type" in self.headers:
            fwd["Content-Type"] = self.headers["Content-Type"]

        try:
            req = requests.request(
                self.command, upstream, data=body, headers=fwd,
                stream=True, timeout=120, allow_redirects=False,
            )
        except Exception as e:
            logger.warning("upstream error: %s", e)
            self.send_response(502)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return

        self.send_response(req.status_code)
        self.send_header("Connection", "close")
        for k, v in req.headers.items():
            if k.lower() in ("transfer-encoding", "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.end_headers()
        try:
            for chunk in req.iter_content(65536):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception as e:
            logger.warning("stream error: %s", e)
        finally:
            req.close()
            try:
                self.wfile.flush()
                self.connection.shutdown(1)
            except Exception:
                pass
            self.close_connection = True

    def do_GET(self): self._forward()
    def do_POST(self): self._forward()
    def do_PUT(self): self._forward()
    def do_PATCH(self): self._forward()
    def do_DELETE(self): self._forward()
    def do_HEAD(self): self._forward()
    def log_message(self, *a): pass


_server = None


def start_proxy(host=LISTEN_HOST, port=LISTEN_PORT):
    global _server
    if _server is not None:
        return _server
    if not GITHUB_TOKEN:
        raise RuntimeError("COOLTON_GH_TOKEN not set")

    class _S(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    _server = _S((host, port), _Handler)
    t = threading.Thread(target=_server.serve_forever, daemon=True)
    t.start()
    logger.info("github_proxy listening on %s:%s", host, port)

    class _A(ThreadingMixIn, HTTPServer):
        daemon_threads = True
    _admin = _A(("127.0.0.1", ADMIN_PORT), _AdminHandler)
    ta = threading.Thread(target=_admin.serve_forever, daemon=True)
    ta.start()
    logger.info("github_proxy admin listening on 127.0.0.1:%s", ADMIN_PORT)
    return _server


def stop_proxy():
    global _server
    if _server:
        _server.shutdown()
        _server = None


if __name__ == "__main__":
    start_proxy()
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop_proxy()
