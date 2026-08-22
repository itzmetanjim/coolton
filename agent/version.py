"""Current git commit, for the App Home footer.

coolton runs directly from a git checkout as a systemd service (see deploy.sh)
and is restarted on every deploy, so the process's whole lifetime is exactly
one commit — resolve it once at import time rather than shelling out on every
App Home render.
"""

import logging
import subprocess

logger = logging.getLogger(__name__)

REPO_URL = "https://github.com/itzmetanjim/coolton"


def _resolve_commit_hash() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=True,
        )
        return result.stdout.strip()
    except Exception:
        logger.warning("Could not resolve git commit hash", exc_info=True)
        return ""


COMMIT_HASH = _resolve_commit_hash()
COMMIT_SHORT_HASH = COMMIT_HASH[:10] if COMMIT_HASH else "unknown"
COMMIT_URL = f"{REPO_URL}/commit/{COMMIT_HASH}" if COMMIT_HASH else None
