# agent/sandbox_helpers.py
"""Sandbox lifecycle helpers shared across all coolton tools.

Centralizes E2B sandbox creation / connection / reuse so every tool behaves the
same way:

- If the thread has no sandbox yet, one is created and provisioned on demand, so
  tools never have to tell the user to "run a command first".
- E2B recycles sandboxes (idle timeout / max lifetime), leaving stale ids in the
  store. connect() to a dead id does not fail eagerly, so a probe ("echo active")
  surfaces the lifecycle error at a predictable point; the stale id is then dropped
  and a fresh provisioned sandbox takes its place.
"""

import logging
import os
import threading

from e2b import Sandbox
from e2b.exceptions import SandboxNotFoundException

from agent.github_proxy_client import PUBLIC_PROXY_HOST, issue_sandbox_token
from agent.sandbox_store import (
    delete_thread_sandbox_id,
    get_thread_sandbox_id,
    save_thread_sandbox_id,
)

logger = logging.getLogger(__name__)

_proxy_cache: dict[str, dict | None] = {}
_proxy_cache_lock = threading.Lock()
_PROXY_CACHE_MAX = 256


def _proxy_cache_set(sandbox_id: str, proxy_info: dict | None) -> None:
    with _proxy_cache_lock:
        _proxy_cache[sandbox_id] = proxy_info
        if len(_proxy_cache) > _PROXY_CACHE_MAX:
            for key in list(_proxy_cache)[: len(_proxy_cache) - _PROXY_CACHE_MAX]:
                _proxy_cache.pop(key, None)


def _proxy_cache_get(sandbox_id: str) -> dict | None:
    with _proxy_cache_lock:
        return _proxy_cache.get(sandbox_id)


def _sandbox_template_id() -> str | None:
    """Return the E2B template ID to build sandboxes from, or None for the default image.

    Set via the SANDBOX_TEMPLATE_ID env var or the SANDBOX_TEMPLATE_ID file written by
    build_sandbox_template.py. Falls back to the default E2B image when unset."""
    env_id = os.environ.get("SANDBOX_TEMPLATE_ID")
    if env_id:
        return env_id
    try:
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "SANDBOX_TEMPLATE_ID")
        with open(p) as f:
            return f.read().strip() or None
    except FileNotFoundError:
        return None


def _proxy_env(proxy_info: dict | None) -> dict:
    """Build the env dict (for E2B `commands.run(envs=...)`) that authenticates gh/git as the
    coolton-agent GitHub user via the host-side proxy, WITHOUT the real PAT ever entering the
    sandbox.

    The sandbox talks to https://ghproxy.tanjim.org (TLS terminated by Caddy) using a short-lived
    per-sandbox token. The proxy rewrites that token to the real PAT on the host and forwards to
    github.com. We alias `gh`/`git`/`curl` so plain `github.com` usage is transparently routed.
    """
    if not proxy_info:
        return {}
    host = proxy_info["proxy_host"]      # e.g. ghproxy.tanjim.org
    tok = proxy_info["token"]            # ephemeral per-sandbox token
    return {
        # gh: custom GH_HOST is treated as GitHub Enterprise, so it sends REST to
        # /api/v3 and GraphQL to /api/graphql; the proxy maps those back to github.com.
        "GH_HOST": host,
        "GH_ENTERPRISE_TOKEN": tok,
        # git: rewrite github.com -> ghproxy.tanjim.org and supply the token via a
        # credential helper so git's anonymous probe gets a 401 and retries with auth.
        "COOLTON_GIT_INSTEADOF": f"https://{host}/",
        "COOLTON_GIT_TOKEN": tok,
        "COOLTON_GIT_USER": "x",
        # Convenience for scripts/curl that hit github.com directly.
        "COOLTON_GH_PROXY_HOST": host,
        "COOLTON_GH_PROXY_TOKEN": tok,
        # User-writable bin holds the `gh` wrapper; keep it ahead of system bins.
        "PATH": "/home/user/bin:/usr/local/bin:/usr/bin:/bin",
    }


_STALE_SANDBOX_MARKERS = (
    "StreamReset",
    "killed",
    "end of life",
    "Sandbox is not running",
    "not found",
    "NotFound",
    "NOT_FOUND",
)


def _is_stale_sandbox_error(e: Exception) -> bool:
    """True if the exception means the sandbox itself is gone (E2B recycled it), as
    opposed to a plain command/file error inside a healthy sandbox."""
    if isinstance(e, SandboxNotFoundException):
        return True
    msg = str(e)
    return any(marker in msg for marker in _STALE_SANDBOX_MARKERS)


def _recycle_dead_sandbox(channel_id: str, thread_ts: str, sandbox_id: str, error: Exception) -> None:
    logger.warning(f"stored sandbox {sandbox_id} is dead ({error}); recreating")
    try:
        Sandbox.connect(sandbox_id).kill()
    except Exception:
        pass
    delete_thread_sandbox_id(channel_id, thread_ts)


def get_or_create_sandbox(channel_id: str, thread_ts: str):
    """Return (sandbox, proxy_info) for this thread, guaranteed to be alive.

    If the thread has no stored sandbox, one is created and provisioned. If the stored
    id points at a sandbox E2B already recycled, that id is dropped and a fresh
    provisioned sandbox is created instead.
    """
    sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
    if sandbox_id:
        try:
            sandbox = Sandbox.connect(sandbox_id)
            proxy_info = _proxy_cache_get(sandbox_id)
            if proxy_info is None:
                tok = issue_sandbox_token(sandbox.sandbox_id)
                proxy_info = {"proxy_host": PUBLIC_PROXY_HOST, "token": tok}
                _proxy_cache_set(sandbox.sandbox_id, proxy_info)
            try:
                # connect() to a recycled sandbox is lazy; a trivial command forces the
                # lifecycle error out immediately instead of mid-request.
                sandbox.commands.run("echo active", envs=_proxy_env(proxy_info))
            except Exception as e:
                if not _is_stale_sandbox_error(e):
                    raise
                _recycle_dead_sandbox(channel_id, thread_ts, sandbox_id, e)
                sandbox_id = None
            else:
                return sandbox, proxy_info
        except Exception as e:
            if not _is_stale_sandbox_error(e):
                raise
            _recycle_dead_sandbox(channel_id, thread_ts, sandbox_id, e)
            sandbox_id = None
    sandbox = Sandbox.create(_sandbox_template_id())
    tok = issue_sandbox_token(sandbox.sandbox_id)
    proxy_info = {"proxy_host": PUBLIC_PROXY_HOST, "token": tok}
    _proxy_cache_set(sandbox.sandbox_id, proxy_info)
    provision = _provision_sandbox(sandbox, proxy_info)
    logger.info(f"coolton sandbox provisioned:\n{provision}")
    save_thread_sandbox_id(channel_id, thread_ts, sandbox.sandbox_id)
    return sandbox, proxy_info


def _provision_sandbox(sandbox, proxy_info: dict | None = None) -> str:
    """One-time setup for a brand-new coolton sandbox.

    The E2B sandbox base image already ships python3, pip, node, npm, git, curl and the
    gh CLI, so we only configure identities, wire up GitHub access, and install the
    agent-browser CLI here.

    Authentication: coolton's real GitHub token is NEVER written into the sandbox. A host-side
    forward proxy (github_proxy.py, exposed via Caddy as https://ghproxy.tanjim.org) rewrites
    the sandbox's ephemeral per-sandbox token to the real PAT on the host and forwards to
    github.com. The sandbox only ever sees its own short-lived token for ghproxy.tanjim.org."""
    gh_user = os.environ.get("COOLTON_GH_USER", "coolton-agent")
    script = r"""
set -e
echo "==> provisioning coolton sandbox =="
git config --global user.name "__GH_USER__"
git config --global user.email "__GH_USER__@users.noreply.github.com"
git config --global init.defaultBranch main
if [ -n "$COOLTON_GIT_INSTEADOF" ]; then
  # Route all github.com git traffic through the host proxy (TLS, real token injected host-side).
  git config --global url."$COOLTON_GIT_INSTEADOF".insteadOf "https://github.com/"
  # Credential helper supplies the ephemeral sandbox token for ghproxy.tanjim.org.
  git config --global "credential.$COOLTON_GIT_INSTEADOF.helper" ""
  git config --global "credential.$COOLTON_GIT_INSTEADOF.helper" '!f() { echo "username=$COOLTON_GIT_USER"; echo "password=$COOLTON_GIT_TOKEN"; }; f'
  # gh wrapper so the sandbox can just run `gh` against github.com transparently. The sandbox
  # runs as the unprivileged 'user', so the wrapper goes in a user-writable bin on PATH.
  # gh 2.96+ removed the --hostname flag; we set GH_HOST instead (also set by _proxy_env).
  # The token comes from GH_ENTERPRISE_TOKEN (set by _proxy_env) / GH_TOKEN.
  mkdir -p /home/user/bin
  cat > /home/user/bin/gh <<'EOF'
#!/bin/sh
export GH_HOST="${COOLTON_GH_PROXY_HOST:-ghproxy.tanjim.org}"
exec /usr/local/bin/gh "$@"
EOF
  chmod +x /home/user/bin/gh
  # ensure /home/user/bin is ahead of /usr/local/bin on PATH for this session
  export PATH="/home/user/bin:$PATH"
fi
echo "==> common tools (wget, unzip, jq, ...):"
command -v wget >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1 && command -v jq >/dev/null 2>&1 || \
  (sudo apt-get update -qq 2>&1 | tail -1 || true) && \
  (sudo apt-get install -y -qq wget curl unzip zip jq ca-certificates less nano vim-tiny htop file sqlite3 2>&1 | tail -1 || echo "APT_INSTALL_FAILED")
echo "==> versions:"
echo "git:  $(git --version 2>&1)"
echo "node: $(node --version 2>&1)"
echo "npm:  $(npm --version 2>&1)"
echo "gh:   $(gh --version 2>&1 | head -1)"
echo "py:   $(python3 --version 2>&1)"
echo "wget: $(wget --version 2>&1 | head -1)"
echo "jq:   $(jq --version 2>&1)"
echo "==> python data libs (pandas, numpy, duckdb):"
python3 -c "import pandas, numpy, duckdb" 2>/dev/null || sudo pip3 install -q --break-system-packages numpy pandas duckdb 2>&1 | tail -2 || echo "PYLIBS_INSTALL_FAILED"
echo "==> agent-browser CLI (for web browsing):"
command -v agent-browser >/dev/null 2>&1 || sudo npm install -g agent-browser 2>&1 | tail -1 || echo "AGENT_BROWSER_NPM_FAILED"
if command -v agent-browser >/dev/null 2>&1; then
  # Download Chrome for Testing + required browser libs. Non-fatal: doctor can repair later.
  agent-browser install --with-deps >/dev/null 2>&1 || agent-browser install >/dev/null 2>&1 || echo "AGENT_BROWSER_SETUP_FAILED"
  echo "agent-browser: $(agent-browser --version 2>&1 | head -1)"
else
  echo "AGENT_BROWSER_NPM_FAILED"
fi
echo "==> gh api self (authenticated via host proxy):"
gh api user --jq .login 2>&1 || true
""".replace("__GH_USER__", gh_user)
    try:
        result = sandbox.commands.run(script, timeout=900, envs=_proxy_env(proxy_info))
        out = []
        if result.stdout:
            out.append(result.stdout)
        if result.stderr:
            out.append("STDERR:\n" + result.stderr)
        out.append(f"provision exit code: {result.exit_code}")
        return "\n".join(out)
    except Exception as e:
        return f"provision error: {e}"
