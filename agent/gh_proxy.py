"""GitHub forward proxy for coolton's E2B sandbox.

Goal: let coolton's sandbox use `gh` / `git` as the `coolton-agent` GitHub user
WITHOUT the real GitHub token ever entering the sandbox.

Design (host-side HTTP forward proxy, exposed via Caddy as https://matrix.tanjim.org:1500):
  * A plain HTTP forward proxy runs HERE on localhost:1357. Caddy terminates the trusted
    Let's Encrypt TLS (matrix.tanjim.org) and reverse-proxies to it, so the sandbox<->host
    link is encrypted and trusted with no custom CA for that hop.
  * It is protected by a per-sandbox bearer token (Proxy-Authorization).
  * The sandbox is told the proxy address + token via env vars and configures gh/git/curl
    to use it as a proxy.
  * Two request shapes are handled:
      - git uses http://github.com remotes, so it sends the request in CLEARTEXT
        absolute-form (`GET http://github.com/...`) to the proxy. The proxy rewrites it to
        https, injects `Authorization: Basic <real token>`, fetches GitHub, streams back.
      - gh targets https://api.github.com, so it CONNECTs to the proxy. The proxy terminates
        the client's TLS with an on-the-fly leaf certificate for github (signed by a CA that
        the sandbox was taught to trust during provisioning), reads the cleartext request,
        injects the token, opens a real TLS to GitHub, and shuttles bytes.
  * The real GitHub token is added ONLY on the host side and never reaches the sandbox.
  * Only github hosts are proxied; everything else is rejected.
"""

import base64
import binascii
import datetime
import logging
import sys
import os
import secrets
import socket
import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

PROXY_LISTEN_HOST = "127.0.0.1"
PROXY_LISTEN_PORT = 1357
PUBLIC_PROXY_URL = "https://matrix.tanjim.org:1500"
GITHUB_SCOPE = ("github.com", "api.github.com", "codeload.github.com")


def github_scope(host: str) -> bool:
    h = (host or "").split(":")[0].lower()
    if h == "github.com" or h.endswith(".github.com"):
        return True
    return h in GITHUB_SCOPE


# ---- CA used to mint github leaf certs for CONNECT (MITM) termination ----
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


class _CA:
    def __init__(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, "coolton-sandbox-proxy"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "coolton"),
        ])
        now = int(__import__("time").time())
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.fromtimestamp(now - 60, tz=datetime.timezone.utc))
            .not_valid_after(datetime.datetime.fromtimestamp(now + 365 * 24 * 3600, tz=datetime.timezone.utc))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=True,
                                         crl_sign=True, encipher_only=False, decipher_only=False),
                           critical=True)
            .sign(key, hashes.SHA256())
        )
        self.key = key
        self.cert = cert
        self.pem = cert.public_bytes(serialization.Encoding.PEM)
        self._leaf_cache = {}
        self._lock = threading.Lock()

    def leaf(self, hostname: str) -> tuple:
        with self._lock:
            if hostname in self._leaf_cache:
                return self._leaf_cache[hostname]
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = int(__import__("time").time())
            cert = (
                x509.CertificateBuilder()
                .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
                .issuer_name(self.cert.subject)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.datetime.fromtimestamp(now - 60, tz=datetime.timezone.utc))
                .not_valid_after(datetime.datetime.fromtimestamp(now + 365 * 24 * 3600, tz=datetime.timezone.utc))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
                .sign(self.key, hashes.SHA256())
            )
            pair = (cert.public_bytes(serialization.Encoding.PEM),
                    key.private_bytes(serialization.Encoding.PEM,
                                      serialization.PrivateFormat.TraditionalOpenSSL,
                                      serialization.NoEncryption()))
            self._leaf_cache[hostname] = pair
            return pair


def _leaf_tempfiles(host: str, ca: "_CA"):
    """Write a github leaf cert+key to temp files (ssl.load_cert_chain needs paths)."""
    import tempfile
    leaf_pem, key_pem = ca.leaf(host)
    cert_f = tempfile.NamedTemporaryFile(suffix=".crt", delete=False)
    key_f = tempfile.NamedTemporaryFile(suffix=".key", delete=False)
    cert_f.write(leaf_pem); cert_f.close()
    key_f.write(key_pem); key_f.close()
    return cert_f.name, key_f.name


def _inject_auth(data: bytes, basic_token: bytes) -> bytes:
    head, sep, rest = data.partition(b"\r\n\r\n")
    if not sep:
        return data
    lines = head.split(b"\r\n")
    auth = b"Authorization: Basic " + base64.b64encode(basic_token + b":") + b"\r\n"
    out, replaced = [], False
    for i, ln in enumerate(lines):
        if i == 0:
            out.append(ln)
        elif ln.lower().startswith(b"authorization:"):
            out.append(auth); replaced = True
        elif ln.lower().startswith(b"proxy-authorization:"):
            continue
        else:
            out.append(ln)
    if not replaced:
        out.insert(1, auth)
    return b"\r\n".join(out) + b"\r\n\r\n" + rest


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    github_token = ""
    tokens = set()
    ca = None

    def _auth_ok(self) -> bool:
        h = self.headers.get("Proxy-Authorization", "")
        if h.startswith("Basic "):
            try:
                decoded = base64.b64decode(h[6:]).decode(errors="ignore")
            except Exception:
                decoded = ""
            return decoded.split(":", 1)[0] in self.tokens
        if h.startswith("Bearer "):
            return h[7:].strip() in self.tokens
        return False

    # --- git-style cleartext absolute-form requests (http://github.com remotes) ---
    def _proxy_request(self):
        if not self._auth_ok():
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="coolton"')
            self.end_headers()
            return
        target = self.path
        if target.startswith("http://") or target.startswith("https://"):
            parsed = urlparse(target)
            host = parsed.netloc
        else:
            host = self.headers.get("Host", "")
            parsed = None
        if not github_scope(host):
            self.send_response(403)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"only github hosts are proxied")
            return
        if parsed is not None:
            upstream = parsed._replace(scheme="https").geturl()
        else:
            upstream = "https://" + host + self.path
        try:
            body = None
            if self.command in ("POST", "PUT", "PATCH"):
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else None
            headers = {
                "Authorization": "Basic " + base64.b64encode(
                    (self.server.github_token + ":").encode()).decode(),
                "User-Agent": self.headers.get("User-Agent", "coolton-sandbox"),
                "Accept": self.headers.get("Accept", "*/*"),
            }
            if "Content-Type" in self.headers:
                headers["Content-Type"] = self.headers["Content-Type"]
            req = requests.request(self.command, upstream, data=body, headers=headers,
                                   stream=True, timeout=120, allow_redirects=False)
        except Exception as e:
            logger.warning("gh_proxy upstream error: %s", e)
            self.send_response(502); self.end_headers(); return
        self.send_response(req.status_code)
        for k, v in req.headers.items():
            if k.lower() in ("transfer-encoding", "connection", "content-encoding"):
                continue
            self.send_header(k, v)
        self.end_headers()
        try:
            for chunk in req.iter_content(65536):
                if chunk:
                    self.wfile.write(chunk); self.wfile.flush()
        except Exception as e:
            logger.warning("gh_proxy stream error: %s", e)
        finally:
            req.close()

    # --- gh-style CONNECT (https targets) -> MITM termination + token inject ---
    def do_CONNECT(self):
        host = self.path.split(":")[0]
        if not github_scope(host):
            self.send_response(403); self.end_headers(); return
        if not self._auth_ok():
            self.send_response(407)
            self.send_header("Proxy-Authenticate", 'Basic realm="coolton"')
            self.end_headers(); return
        try:
            cert_path, key_path = _leaf_tempfiles(host, self.ca)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
            # Force HTTP/1.1 so our header injection (HTTP/1.1 parsing) works.
            ctx.set_alpn_protocols(["http/1.1"])
            # Flush any buffered HTTP response writer, then write the CONNECT 200 directly
            # on the raw socket (before the TLS handshake begins).
            try:
                self.wfile.flush()
            except Exception:
                pass
            self.connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.close_connection = True
            client_tls = ctx.wrap_socket(self.connection, server_side=True)
        except Exception as e:
            sys.stderr.write(f"[dc] tls term failed: {e}\n"); sys.stderr.flush()
            logger.warning("gh_proxy CONNECT tls term failed: %s", e)
            try:
                self.connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
            return
        self._bridge(client_tls, host)

    def _bridge(self, client_tls, host):
        try:
            remote = socket.create_connection((host, 443), timeout=30)
            remote_tls = ssl.create_default_context().wrap_socket(remote, server_hostname=host)
        except Exception as e:
            logger.warning("gh_proxy upstream connect failed: %s", e)
            try: client_tls.close()
            except Exception: pass
            return

        def to_remote():
            first = True
            try:
                while True:
                    data = client_tls.recv(65536)
                    if not data: break
                    if first:
                        data = _inject_auth(data, self.server.github_token.encode())
                        first = False
                    remote_tls.sendall(data)
            except Exception: pass
            finally:
                try: remote_tls.shutdown(socket.SHUT_WR)
                except Exception: pass

        def to_client():
            try:
                while True:
                    data = remote_tls.recv(65536)
                    if not data: break
                    client_tls.sendall(data)
            except Exception: pass
            finally:
                try: client_tls.shutdown(socket.SHUT_WR)
                except Exception: pass

        t1 = threading.Thread(target=to_remote, daemon=True)
        t2 = threading.Thread(target=to_client, daemon=True)
        t1.start(); t2.start(); t1.join(); t2.join()
        try: client_tls.close()
        except Exception: pass
        try: remote_tls.close()
        except Exception: pass

    def do_GET(self): self._proxy_request()
    def do_POST(self): self._proxy_request()
    def do_PUT(self): self._proxy_request()
    def do_PATCH(self): self._proxy_request()
    def do_DELETE(self): self._proxy_request()
    def do_HEAD(self): self._proxy_request()
    def log_message(self, *a): pass


class GHProxy:
    def __init__(self, github_token: str, port: int = PROXY_LISTEN_PORT,
                 listen_host: str = PROXY_LISTEN_HOST):
        self.github_token = github_token
        self.port = port
        self.listen_host = listen_host
        self.ca = _CA()
        self.tokens = set()
        self._server = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        _Handler.github_token = self.github_token
        _Handler.tokens = self.tokens
        _Handler.ca = self.ca

        class _S(ThreadingMixIn, HTTPServer):
            daemon_threads = True
            github_token = self.github_token
            tokens = self.tokens
            ca = self.ca
        self._server = _S((self.listen_host, self.port), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("gh_proxy listening on %s:%s (public: %s)",
                    self.listen_host, self.port, PUBLIC_PROXY_URL)
        return self

    @property
    def ca_pem(self) -> bytes:
        return self.ca.pem

    def issue_token(self, sandbox_id: str) -> str:  # noqa: ARG002
        tok = secrets.token_hex(24)
        with self._lock:
            self.tokens.add(tok)
        return tok

    def revoke_token(self, token: str):
        with self._lock:
            self.tokens.discard(token)

    def stop(self):
        if self._server:
            self._server.shutdown()


_proxies = {}
_proxies_lock = threading.Lock()
_global_proxy = None
_global_proxy_lock = threading.Lock()


def _ensure_global_proxy() -> GHProxy:
    global _global_proxy
    with _global_proxy_lock:
        if _global_proxy is None:
            token = os.environ.get("COOLTON_GH_TOKEN")
            if not token:
                raise RuntimeError("COOLTON_GH_TOKEN not set")
            _global_proxy = GHProxy(token).start()
        return _global_proxy


def start_sandbox_proxy(sandbox, sandbox_id: str) -> dict | None:
    github_token = os.environ.get("COOLTON_GH_TOKEN")
    if not github_token:
        return None
    try:
        proxy = _ensure_global_proxy()
    except RuntimeError:
        return None
    with _proxies_lock:
        if sandbox_id in _proxies:
            proxy, token = _proxies[sandbox_id]
        else:
            token = proxy.issue_token(sandbox_id)
            _proxies[sandbox_id] = (proxy, token)
    # Install our CA so the sandbox trusts the proxy's github leaf certs (CONNECT/MITM path,
    # used by gh). The cleartext git path needs no CA. The real GitHub token stays on the host.
    # The proxy URL/token/env are passed per-command via E2B's `envs=` (see agent.py), NOT
    # written to bashrc, so they're guaranteed present for every command.
    sandbox.files.write("/usr/local/share/ca-certificates/coolton-proxy.crt", proxy.ca_pem)
    sandbox.commands.run("update-ca-certificates >/dev/null 2>&1 || true")
    return {"proxy_url": PUBLIC_PROXY_URL, "token": token}


def stop_sandbox_proxy(sandbox_id: str):
    with _proxies_lock:
        entry = _proxies.pop(sandbox_id, None)
    if entry:
        proxy, token = entry
        proxy.revoke_token(token)
