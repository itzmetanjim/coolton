"""GitHub tunnel for coolton's E2B sandbox.

Problem: we want coolton's sandbox to be able to use `gh` / `git` against GitHub
authenticated as the `coolton-agent` GitHub user, WITHOUT ever placing the real
GitHub token inside the sandbox.

Solution (user's design):
  * A tiny WebSocket server runs INSIDE the sandbox (port 3000, exposed via the
    E2B public URL).
  * A WebSocket CLIENT runs HERE, in the coolton host process. It dials
    wss://<sandbox-public-host>/tunnel and authenticates with a randomly generated
    token that is injected into the sandbox as an env var.
  * Inside the sandbox, a local HTTP forward proxy (port 8899) is configured as the
    HTTPS_PROXY for gh/git/curl. When it sees a CONNECT to a github host, it tunnels
    that TCP stream over the already-open WS to the host.
  * The HOST receives the raw bytes, opens a real TCP connection to github.com:443,
    and shuttles bytes both ways — attaching the REAL GitHub token ONLY on the host
    side (as a Basic auth header injected into the client hello / requests).

The real GitHub token (COOLTON_GH_TOKEN) stays on the host. The only secret inside
the sandbox is the random per-sandbox handshake token (ephemeral, useless to anyone
else because the public WS requires it).

Framing over the single WS connection (JSON lines):
  host->sandbox auth:    {"t":"auth","token":"<rand>"}
  sandbox->host open:    {"t":"open","sid":<n>,"host":"github.com","port":443}
  host->sandbox opened:  {"t":"opened","sid":<n>}
  either->other data:    {"t":"data","sid":<n>,"b":"<base64 bytes>"}
  either->other close:   {"t":"close","sid":<n>}
"""

import asyncio
import base64
import binascii
import json
import logging
import os
import secrets
import threading
import time

logger = logging.getLogger(__name__)

SANDBOX_TUNNEL_PORT = 3000
SANDBOX_PROXY_PORT = 8899
TUNNEL_PATH = "/tunnel"
HANDSHAKE_ENV = "COOLTON_TUNNEL_TOKEN"


def github_scope(host: str) -> bool:
    """True if a host should be routed through the GitHub tunnel."""
    h = (host or "").split(":")[0].lower()
    if h == "github.com" or h.endswith(".github.com") or h == "api.github.com" \
            or h == "codeload.github.com":
        return True
    return False


def build_sandbox_server_script(token: str, proxy_port: int = SANDBOX_PROXY_PORT,
                                tunnel_port: int = SANDBOX_TUNNEL_PORT) -> str:
    """Return the Python source that runs INSIDE the sandbox: a WS server (tunnel_port)
    plus an HTTP CONNECT proxy (proxy_port) that tunnels github hosts over WS."""
    return r'''
import asyncio, base64, json, os, socket, struct, threading, websockets

TOKEN = os.environ.get("__TOKEN__", "")
PROXY_PORT = __PROXY_PORT__
TUNNEL_PORT = __TUNNEL_PORT__
HOST_SCOPE = __HOST_SCOPE__  # list of suffixes considered github

def in_scope(host):
    h = host.split(":")[0].lower()
    return any(h == s or h.endswith("." + s) for s in HOST_SCOPE)

# ---- host-side accept loop (websockets server) ----
connected = threading.Event()
clients = set()

async def ws_handler(ws):
    try:
        msg = await asyncio.wait_for(ws.recv(), timeout=10)
    except Exception:
        return
    try:
        data = json.loads(msg)
    except Exception:
        await ws.close(); return
    if data.get("t") != "auth" or data.get("token") != TOKEN:
        await ws.close(); return
    connected.set()
    clients.add(ws)
    try:
        async for raw in ws:
            # host relays streamed bytes back to the right local tunnel
            try:
                m = json.loads(raw)
            except Exception:
                continue
            sid = m.get("sid")
            tun = OPEN_TUNNELS.get(sid)
            if tun is None:
                continue
            if m.get("t") == "data":
                try:
                    buf = base64.b64decode(m["b"])
                except Exception:
                    buf = b""
                tun["client"].send(buf)
            elif m.get("t") == "close":
                tun["closed_by_host"] = True
    finally:
        clients.discard(ws)

OPEN_TUNNELS = {}

async def main():
    async with websockets.serve(ws_handler, "0.0.0.0", TUNNEL_PORT, ping_interval=20, ping_timeout=60):
        await asyncio.Future()

t = threading.Thread(target=lambda: asyncio.run(main()), daemon=True)
t.start()

# ---- local CONNECT proxy that forwards github streams over WS ----
import http.server, socketserver

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def do_CONNECT(self):
        host, _, port = self.path.partition(":")
        port = int(port or 443)
        if not in_scope(host):
            self.send_response(502); self.end_headers(); return
        ws = next(iter(clients), None) if clients else None
        if ws is None or not connected.is_set():
            self.send_response(502); self.end_headers(); return
        sid = int(time.time() * 1000) % (2**31) + threading.get_ident() % 1000
        # local pipe: reader thread writes to ws, this thread reads from ws via queue
        from queue import Queue
        q = Queue()
        client_sock, remote = socket.socketpair()
        OPEN_TUNNELS[sid] = {"client": client_sock, "queue": q, "closed_by_host": False}
        # tell host to open tcp
        asyncio.run_coroutine_threadsafe(
            ws.send(json.dumps({"t": "open", "sid": sid, "host": host, "port": port})),
            asyncio.get_event_loop() if False else None)
        # send via the running loop: use a simple thread-safe send
        try:
            import asyncio as _a
            loop = _a.new_event_loop()
        except Exception:
            loop = None
        self.send_response(200); self.end_headers()
        # bridge: read from client_sock -> ws ; read from ws -> client_sock
        def reader():
            import select
            while True:
                try:
                    r, _, _ = select.select([client_sock], [], [], 1)
                    if r:
                        d = client_sock.recv(65536)
                        if not d:
                            break
                        try:
                            _send_ws(ws, {"t": "data", "sid": sid, "b": base64.b64encode(d).decode()})
                        except Exception:
                            break
                except Exception:
                    break
            try:
                _send_ws(ws, {"t": "close", "sid": sid})
            except Exception:
                pass
            try:
                del OPEN_TUNNELS[sid]
            except Exception:
                pass
        rt = threading.Thread(target=reader, daemon=True); rt.start()
        try:
            while True:
                try:
                    b = q.get(timeout=1)
                    if b is None:
                        break
                    client_sock.sendall(b)
                except Exception:
                    # feed any host->sandbox data that arrived via ws_handler
                    pass
        finally:
            try:
                client_sock.close()
            except Exception:
                pass

    def log_message(self, *a):
        pass

def _send_ws(ws, obj):
    # websockets send is coroutine; schedule on the server loop
    try:
        loop = _LOOP
        asyncio.run_coroutine_threadsafe(ws.send(json.dumps(obj)), loop)
    except Exception:
        pass

_LOOP = None
def _set_loop():
    global _LOOP
    _LOOP = asyncio.get_event_loop()

# monkeypatch: set loop after server starts
threading.Thread(target=lambda: (time.sleep(0.2), _set_loop()), daemon=True).start()

class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

srv = Server(("127.0.0.1", PROXY_PORT), Handler)
srv.serve_forever()
'''.replace("__TOKEN__", token).replace("__PROXY_PORT__", str(proxy_port)) \
        .replace("__TUNNEL_PORT__", str(tunnel_port)) \
        .replace("__HOST_SCOPE__", json.dumps(["github.com", "api.github.com",
                                                "codeload.github.com"]))


class GHTunnelClient:
    """Host-side WebSocket client. Lives in its own event loop thread. Relays github
    TCP streams tunneled from the sandbox, performing the real fetch with the real
    GitHub token."""

    def __init__(self, public_ws_url: str, handshake_token: str, github_token: str):
        self.url = public_ws_url
        self.handshake_token = handshake_token
        self.github_token = github_token
        self.loop = None
        self.ws = None
        self.thread = None
        self.stop = False
        self.ready = threading.Event()
        self.streams = {}  # sid -> {"sock": real socket, "task": ...}

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        return self

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect())

    async def _connect(self):
        import websockets
        while not self.stop:
            try:
                async with websockets.connect(self.url, max_size=None, ping_interval=20,
                                              ping_timeout=60) as ws:
                    self.ws = ws
                    await ws.send(json.dumps({"t": "auth", "token": self.handshake_token}))
                    self.ready.set()
                    logger.info("gh_tunnel: connected to sandbox %s", self.url)
                    async for raw in ws:
                        try:
                            m = json.loads(raw)
                        except Exception:
                            continue
                        t = m.get("t")
                        if t == "open":
                            await self._open_stream(m)
                        elif t == "data":
                            await self._relay_data(m)
                        elif t == "close":
                            self._close_stream(m.get("sid"))
                    self.ready.clear()
            except Exception as e:
                self.ready.clear()
                logger.warning("gh_tunnel: ws error %s; retrying in 3s", e)
                await asyncio.sleep(3)

    async def _open_stream(self, m):
        sid = m.get("sid")
        host = m.get("host")
        port = int(m.get("port", 443))
        try:
            reader, writer = await asyncio.open_connection(host, port, ssl=True)
        except Exception as e:
            await self._send({"t": "close", "sid": sid})
            logger.warning("gh_tunnel: open %s:%s failed: %s", host, port, e)
            return
        self.streams[sid] = {"reader": reader, "writer": writer}

        # Inject Basic auth into the TLS client hello? No — TLS is already established.
        # Instead, rewrite the first HTTP request's Authorization header as it arrives.
        # We read the first bytes from the sandbox, patch Authorization, then stream.
        async def pump_in():
            try:
                # Read initial request bytes (HTTP/1.1 request line + headers)
                data = await reader.read(65536)
                if data:
                    data = self._inject_auth(data)
                    await self._send({"t": "data", "sid": sid, "b": base64.b64encode(data).decode()})
                while True:
                    data = await reader.read(65536)
                    if not data:
                        break
                    await self._send({"t": "data", "sid": sid, "b": base64.b64encode(data).decode()})
                await self._send({"t": "close", "sid": sid})
            except Exception:
                await self._send({"t": "close", "sid": sid})
            finally:
                self._close_stream(sid)

        async def pump_out():
            # host->sandbox bytes are pushed via _relay_data into writer
            pass

        asyncio.ensure_future(pump_in())

    def _inject_auth(self, data: bytes) -> bytes:
        """Inject the real GitHub Basic token into an outgoing HTTP request's headers."""
        if b"\r\n" not in data or not data.lstrip().lower().startswith(b"get") and \
                not data.lstrip().lower().startswith(b"post") and \
                not data.lstrip().lower().startswith(b"put") and \
                not data.lstrip().lower().startswith(b"patch") and \
                not data.lstrip().lower().startswith(b"head") and \
                not data.lstrip().lower().startswith(b"delete"):
            return data
        try:
            head, sep, body = data.partition(b"\r\n\r\n")
            lines = head.split(b"\r\n")
            auth_line = b"Authorization: Basic " + base64.b64encode(
                (self.github_token + ":").encode()) + b"\r\n"
            # replace existing Authorization if present, else add after request line
            new_lines = []
            replaced = False
            for i, ln in enumerate(lines):
                if i == 0:
                    new_lines.append(ln)
                elif ln.lower().startswith(b"authorization:"):
                    new_lines.append(auth_line)
                    replaced = True
                else:
                    new_lines.append(ln)
            if not replaced:
                new_lines.insert(1, auth_line)
            return b"\r\n".join(new_lines) + b"\r\n\r\n" + body
        except Exception:
            return data

    async def _relay_data(self, m):
        sid = m.get("sid")
        st = self.streams.get(sid)
        if not st:
            return
        try:
            buf = base64.b64decode(m.get("b", ""))
        except (binascii.Error, ValueError):
            buf = b""
        try:
            st["writer"].write(buf)
            await st["writer"].drain()
        except Exception:
            self._close_stream(sid)

    def _close_stream(self, sid):
        st = self.streams.pop(sid, None)
        if st:
            try:
                st["writer"].close()
            except Exception:
                pass

    async def _send(self, obj):
        try:
            if self.ws is not None:
                await self.ws.send(json.dumps(obj))
        except Exception:
            pass

    def send(self, obj):
        if self.loop is not None:
            asyncio.run_coroutine_threadsafe(self._send(obj), self.loop)


# ---- orchestration ----

_tunnels = {}  # sandbox_id -> GHTunnelClient
_tunnels_lock = threading.Lock()


def start_tunnel(sandbox, sandbox_id: str, github_token: str) -> GHTunnelClient | None:
    """Start the in-sandbox tunnel server + host WS client for a sandbox. Returns the
    client, or None if a token isn't configured."""
    token = os.environ.get("COOLTON_GH_TOKEN")
    if not token:
        return None
    with _tunnels_lock:
        if sandbox_id in _tunnels:
            return _tunnels[sandbox_id]
        handshake = secrets.token_hex(24)
        # launch in-sandbox server
        script = build_sandbox_server_script(handshake)
        sandbox.files.write("/home/user/gh_tunnel.py", script)
        proc = sandbox.commands.run(
            "pip install websockets -q && python3 /home/user/gh_tunnel.py",
            background=True,
        )
        # wait for public host + server to come up
        host_url = None
        for _ in range(20):
            try:
                host_url = f"wss://{sandbox.get_host(SANDBOX_TUNNEL_PORT)}{TUNNEL_PATH}"
                break
            except Exception:
                time.sleep(0.5)
        if not host_url:
            return None
        # give the in-sandbox server a moment to bind
        time.sleep(2)
        client = GHTunnelClient(host_url, handshake, token).start()
        _tunnels[sandbox_id] = client
        return client


def get_tunnel(sandbox_id: str) -> GHTunnelClient | None:
    with _tunnels_lock:
        return _tunnels.get(sandbox_id)


def stop_tunnel(sandbox_id: str):
    with _tunnels_lock:
        c = _tunnels.pop(sandbox_id, None)
        if c:
            c.stop = True
