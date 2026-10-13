"""The small JSON files coolton keeps its state in (bans, schedules, endpoints,
feedback, ...), read and written one way.

- load_json: the file's data, or `default()` if there's no file. A file that isn't
  valid JSON, or isn't a `kind` (a dict unless said otherwise), is moved aside to
  `<name>.corrupt-<time>` and logged, then treated as missing: the next save starts a
  fresh file instead of silently writing over whatever was in it. A file that can't be
  read (wrong owner, say) isn't corrupt: it's logged and left exactly where it is.
- save_json: written to a temporary file in the same directory and renamed into
  place, so a crash mid-write never leaves half a file; `private` files (anything
  holding a credential) are readable by this user only.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


def load_json(path, default: Callable[[], Any] = dict, kind: type = dict) -> Any:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return default()
    except OSError:
        logger.error("Can't read %s; leaving it as it is", path, exc_info=True)
        return default()
    except ValueError:
        data = _NOT_JSON
    if isinstance(data, kind):
        return data
    aside = f"{path}.corrupt-{time.time_ns()}"
    try:
        os.replace(path, aside)
        logger.error("%s isn't valid %s JSON; moved it to %s and started fresh", path, kind.__name__, aside)
    except OSError:
        logger.exception("%s isn't valid %s JSON, and couldn't be moved aside", path, kind.__name__)
    return default()


_NOT_JSON = object()


def save_json(path, data: Any, *, private: bool = False, indent: int | None = 2) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        if private:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def write_private_bytes(path, data: bytes) -> None:
    """Write `data` to `path` atomically, readable by this user only."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f".{os.path.basename(path)}.", suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
