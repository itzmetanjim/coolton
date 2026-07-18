"""Build the coolton E2B sandbox template on Debian 13 (trixie/stable).

Matches the host OS (Debian 13) and pre-installs the toolchain + the gh/git proxy
wrappers so the sandbox can PR to github.com as coolton-agent via the host-side proxy
(https://ghproxy.tanjim.org). The real GitHub PAT never enters the sandbox.

Builds server-side via the E2B SDK. Usage:
    .venv/bin/python build_sandbox_template.py
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

from e2b import Template

TEMPLATE_NAME = "coolton-debian13"

# Provisioning script baked into the template image. Sets up a full dev environment so the
# agent can edit coolton's own code and open PRs. GitHub auth is injected at runtime via the
# host-side proxy (https://ghproxy.tanjim.org) + a per-sandbox token (see agent/agent.py);
# the repo is cloned publicly here so the working tree is ready immediately.
COOLTON_REPO = os.environ.get("COOLTON_REPO", "https://github.com/itzmetanjim/coolton.git")

PROVISION = r"""
set -x
export DEBIAN_FRONTEND=noninteractive
LOG=/home/user/provision.log
exec > "$LOG" 2>&1
echo "=== provision start ==="
date

# --- base toolchain (build commands run as 'user'; use sudo for apt) ---
sudo apt-get update; echo "UPDATE_RC=$?"
sudo apt-get install -y --no-install-recommends \
    git curl ca-certificates build-essential python3 python3-pip python3-venv \
    python3-dev nodejs npm jq unzip gnupg lsb-release \
    neovim less ripgrep tmux zip; echo "APT_BASE_RC=$?"

# --- editor / search niceties (tolerant; don't fail the build if one is unavailable) ---
sudo apt-get install -y --no-install-recommends ripgrep neovim 2>&1 | tail -3 || true

# --- GitHub CLI (latest official binary tarball; avoids apt-repo suite issues) ---
GH_VERSION=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | grep -oP '"tag_name": "\K[^"]+')
GH_VERSION=${GH_VERSION#v}
ARCH=$(dpkg --print-architecture)
echo "GH_VERSION=$GH_VERSION ARCH=$ARCH"
if [ -n "$GH_VERSION" ]; then
  curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${ARCH}.tar.gz" -o /tmp/gh.tgz
  tar -C /tmp -xzf /tmp/gh.tgz
  sudo cp "/tmp/gh_${GH_VERSION}_linux_${ARCH}/bin/gh" /usr/local/bin/gh
  sudo chmod +x /usr/local/bin/gh
  rm -rf /tmp/gh.tgz "/tmp/gh_${GH_VERSION}_linux_${ARCH}"
fi
gh --version | head -1 || echo "GH_INSTALL_FAILED"

# --- git identity (no real token stored in the image) ---
git config --global user.name "coolton-agent"
git config --global user.email "coolton-agent@users.noreply.github.com"
git config --global init.defaultBranch main
git config --global pull.rebase false
git config --global alias.pr '!gh pr create'

# --- pre-clone the coolton repo (public) so the agent can edit + PR immediately ---
mkdir -p /home/user/work
git clone __COOLTON_REPO__ /home/user/work/coolton; echo "CLONE_RC=$?"
sudo chown -R user:user /home/user/work 2>/dev/null || true
cd /home/user/work/coolton 2>/dev/null || true
git remote set-url origin __COOLTON_REPO__ 2>/dev/null || true
python3 -m venv .venv 2>/dev/null || true
if [ -f requirements.txt ] && [ -x ./.venv/bin/pip ]; then ./.venv/bin/pip install -q -r requirements.txt 2>/dev/null || true; fi

echo "provision done: $(git --version) | $(gh --version 2>/dev/null | head -1) | $(node --version 2>&1) | $(python3 --version 2>&1)"
true
""".replace("__COOLTON_REPO__", COOLTON_REPO)

template = (
    Template()
    .from_debian_image("stable")  # debian:stable == trixie (Debian 13)
    .set_envs({"COOLTON_GH_USER": "coolton-agent"})
    .run_cmd(PROVISION)
)


def _log(entry):
    try:
        msg = entry.message
    except Exception:
        msg = str(entry)
    print(msg, flush=True)


if __name__ == "__main__":
    if not os.environ.get("E2B_API_KEY"):
        print("E2B_API_KEY not set", file=sys.stderr)
        sys.exit(1)
    print(f"Building template '{TEMPLATE_NAME}' on Debian 13 (trixie/stable)...")
    import traceback
    try:
        info = Template.build(template, TEMPLATE_NAME, skip_cache=True, on_build_logs=_log)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
    print("\n=== BUILD COMPLETE ===")
    print("template_id:", info.template_id)
    # Persist the ID for agent.py to pick up.
    with open(os.path.join(os.path.dirname(__file__), "SANDBOX_TEMPLATE_ID"), "w") as f:
        f.write(info.template_id)
    print("written to SANDBOX_TEMPLATE_ID")
