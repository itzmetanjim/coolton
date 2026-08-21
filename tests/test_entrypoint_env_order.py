"""Regression test for a critical import-order bug: agent.platforms.slack bakes
COOLTON_BOT_ID/COOLTON_USER_ID into a module-level SYSTEM_PROMPT string ONCE at
import time. If an entrypoint imports anything from the `agent` package before
calling load_dotenv(), those ids are permanently empty for the process's whole
lifetime, while build_context_prompt (same file) re-reads them correctly every
turn — the model then sees two contradictory values for its own identity in
every prompt. Both app.py and app_oauth.py must call load_dotenv() before any
`agent`-package import.
"""

import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_import_order_check(env_dir: Path, order: str) -> bool:
    """Run a tiny script that imports in the given order and reports whether the
    env var made it into SYSTEM_PROMPT. `order` is "correct" or "buggy"."""
    if order == "correct":
        script = """
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=".env", override=False)
        from agent.platforms import slack as slack_mod
        print("UTESTBOT123" in slack_mod.SYSTEM_PROMPT)
        """
    else:
        script = """
        from agent.platforms import slack as slack_mod  # noqa: F401 (import before load_dotenv, on purpose)
        from dotenv import load_dotenv
        load_dotenv(dotenv_path=".env", override=False)
        print("UTESTBOT123" in slack_mod.SYSTEM_PROMPT)
        """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(script)],
        cwd=env_dir,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
    )
    assert result.returncode == 0, f"subprocess failed: {result.stderr}"
    return result.stdout.strip() == "True"


def test_import_order_bug_reproduces_when_agent_imported_first(tmp_path):
    """Sanity-check the test harness itself: confirm the bug is real when
    reproduced directly (agent import before load_dotenv)."""
    (tmp_path / ".env").write_text("COOLTON_BOT_ID=UTESTBOT123\n")
    assert _run_import_order_check(tmp_path, "buggy") is False


def test_import_order_correct_when_load_dotenv_first(tmp_path):
    (tmp_path / ".env").write_text("COOLTON_BOT_ID=UTESTBOT123\n")
    assert _run_import_order_check(tmp_path, "correct") is True


def test_app_py_calls_load_dotenv_before_any_agent_import():
    _assert_load_dotenv_precedes_agent_import(REPO_ROOT / "app.py")


def test_app_oauth_py_calls_load_dotenv_before_any_agent_import():
    _assert_load_dotenv_precedes_agent_import(REPO_ROOT / "app_oauth.py")


def _assert_load_dotenv_precedes_agent_import(path: Path) -> None:
    source = path.read_text()
    load_dotenv_pos = source.find("load_dotenv(")
    assert load_dotenv_pos != -1, f"{path.name}: no load_dotenv( call found"

    import ast

    tree = ast.parse(source, filename=str(path))
    for node in ast.walk(tree):
        module = None
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
        elif isinstance(node, ast.Import):
            module = node.names[0].name if node.names else ""
        if module and (module == "agent" or module.startswith("agent.")):
            lineno: int = node.lineno  # type: ignore[attr-defined]
            import_pos = sum(len(line) + 1 for line in source.splitlines(keepends=False)[: lineno - 1])
            assert import_pos > load_dotenv_pos, (
                f"{path.name}: `agent` import at line {lineno} appears before "
                "load_dotenv() — COOLTON_BOT_ID/COOLTON_USER_ID would be baked in "
                "as empty strings for the process's whole lifetime."
            )
