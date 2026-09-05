"""agent/tools/sandbox_files.py — coolton's Read/Write/Edit/Grep/Glob
equivalents for the E2B sandbox. Each is a plain function taking
(channel_id, thread_ts, ...) and returning a string; agent.agent wraps each
in a `@agent.tool` function that just forwards ctx.deps.channel_id/thread_ts.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from e2b.exceptions import FileNotFoundException

from agent.tools import sandbox_files as sf


class _FakeFiles:
    def __init__(self, initial=None):
        self.store = dict(initial or {})

    def read(self, path):
        if path not in self.store:
            raise FileNotFoundException(path)
        return self.store[path]

    def write(self, path, content):
        self.store[path] = content.decode() if isinstance(content, bytes) else content


class _FakeCommands:
    def __init__(self, canned=None):
        self.calls = []
        self._canned = canned or []  # list of (substring, SimpleNamespace(stdout=...))
        self.default = SimpleNamespace(stdout="", stderr="", exit_code=0)

    def run(self, cmd, envs=None, timeout=None):
        self.calls.append(cmd)
        for substring, response in self._canned:
            if substring in cmd:
                return response
        return self.default

    @property
    def last_cmd(self):
        return self.calls[-1] if self.calls else None


class _FakeSandbox:
    def __init__(self, files=None, canned=None):
        self.files = _FakeFiles(files)
        self.commands = _FakeCommands(canned)


@pytest.fixture
def sandbox_env(monkeypatch):
    def _patch(files=None, canned=None):
        fake = _FakeSandbox(files, canned)
        monkeypatch.setattr(sf, "get_or_create_sandbox", lambda c, t: (fake, {}))
        return fake

    return _patch


def test_get_sandbox_reports_connect_errors():
    with patch("agent.tools.sandbox_files.get_or_create_sandbox", side_effect=RuntimeError("sandbox expired")):
        sandbox, error = sf._get_sandbox("C1", "1.1")
    assert sandbox is None
    assert error == "Error: sandbox expired"


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def test_read_returns_line_numbered_content(sandbox_env):
    sandbox_env(files={"/f.txt": "line one\nline two\nline three"})
    result = sf.read_sandbox_file("C1", "1.1", "/f.txt")
    assert result == "     1\tline one\n     2\tline two\n     3\tline three"


def test_read_pages_with_offset_and_limit(sandbox_env):
    sandbox_env(files={"/f.txt": "\n".join(f"l{i}" for i in range(1, 11))})
    result = sf.read_sandbox_file("C1", "1.1", "/f.txt", offset=3, limit=2)
    lines = result.splitlines()
    assert lines[0] == "     3\tl3"
    assert lines[1] == "     4\tl4"
    assert "more line(s) not shown" in result
    assert "offset=5" in result


def test_read_offset_beyond_end_of_file_is_an_error(sandbox_env):
    sandbox_env(files={"/f.txt": "one\ntwo"})
    result = sf.read_sandbox_file("C1", "1.1", "/f.txt", offset=10)
    assert "beyond the end of the file" in result


def test_read_empty_file(sandbox_env):
    sandbox_env(files={"/f.txt": ""})
    assert sf.read_sandbox_file("C1", "1.1", "/f.txt") == "(empty file: /f.txt)"


def test_read_file_not_found(sandbox_env):
    sandbox_env(files={})
    result = sf.read_sandbox_file("C1", "1.1", "/missing.txt")
    assert "File not found" in result


def test_read_truncates_a_very_long_line(sandbox_env):
    sandbox_env(files={"/f.txt": "x" * 3000})
    result = sf.read_sandbox_file("C1", "1.1", "/f.txt")
    assert "line truncated" in result
    assert "3000 chars total" in result


def test_read_propagates_sandbox_connect_errors(monkeypatch):
    def _boom(channel_id, thread_ts):
        raise RuntimeError("expired")

    monkeypatch.setattr(sf, "get_or_create_sandbox", _boom)
    assert sf.read_sandbox_file("C1", "1.1", "/f.txt") == "Error: expired"


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def test_write_creates_parent_dirs_and_writes_content(sandbox_env):
    fake = sandbox_env()
    result = sf.write_sandbox_file("C1", "1.1", "/home/user/sub/out.txt", "hello")
    assert "Written 5 bytes" in result
    assert fake.files.store["/home/user/sub/out.txt"] == "hello"
    assert any("mkdir -p" in c for c in fake.commands.calls)


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


def test_edit_replaces_a_unique_match(sandbox_env):
    fake = sandbox_env(files={"/f.py": "def foo():\n    return 1\n"})
    result = sf.edit_sandbox_file("C1", "1.1", "/f.py", "return 1", "return 2")
    assert "Replaced 1 occurrence" in result
    assert fake.files.store["/f.py"] == "def foo():\n    return 2\n"


def test_edit_rejects_empty_old_string(sandbox_env):
    sandbox_env(files={"/f.py": "x = 1"})
    result = sf.edit_sandbox_file("C1", "1.1", "/f.py", "", "y = 2")
    assert "must not be empty" in result


def test_edit_rejects_identical_old_and_new(sandbox_env):
    sandbox_env(files={"/f.py": "x = 1"})
    result = sf.edit_sandbox_file("C1", "1.1", "/f.py", "x = 1", "x = 1")
    assert "identical" in result


def test_edit_file_not_found(sandbox_env):
    sandbox_env(files={})
    result = sf.edit_sandbox_file("C1", "1.1", "/missing.py", "a", "b")
    assert "File not found" in result


def test_edit_old_string_not_present(sandbox_env):
    sandbox_env(files={"/f.py": "x = 1"})
    result = sf.edit_sandbox_file("C1", "1.1", "/f.py", "y = 2", "y = 3")
    assert "not found" in result


def test_edit_ambiguous_match_without_replace_all_is_rejected(sandbox_env):
    fake = sandbox_env(files={"/f.py": "x = 1\nx = 1\n"})
    result = sf.edit_sandbox_file("C1", "1.1", "/f.py", "x = 1", "x = 2")
    assert "appears 2 times" in result
    assert fake.files.store["/f.py"] == "x = 1\nx = 1\n"  # unchanged


def test_edit_replace_all_replaces_every_occurrence(sandbox_env):
    fake = sandbox_env(files={"/f.py": "x = 1\nx = 1\nx = 1\n"})
    result = sf.edit_sandbox_file("C1", "1.1", "/f.py", "x = 1", "x = 2", replace_all=True)
    assert "Replaced 3 occurrence" in result
    assert fake.files.store["/f.py"] == "x = 2\nx = 2\nx = 2\n"


# ---------------------------------------------------------------------------
# Grep (search_sandbox_files)
# ---------------------------------------------------------------------------


def test_grep_rejects_an_invalid_output_mode(sandbox_env):
    sandbox_env()
    result = sf.search_sandbox_files("C1", "1.1", "foo", output_mode="bogus")
    assert "output_mode must be one of" in result


def test_grep_default_content_mode(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="/f.py:3:foo()\n", stderr="", exit_code=0))])
    result = sf.search_sandbox_files("C1", "1.1", "foo", path="/repo")
    assert result == "/f.py:3:foo()"
    cmd = fake.commands.last_cmd
    assert "-r" in cmd and "-n" in cmd and "-E" in cmd
    assert "-i" not in cmd.split()


def test_grep_case_insensitive_adds_flag(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="match\n", stderr="", exit_code=0))])
    sf.search_sandbox_files("C1", "1.1", "foo", case_insensitive=True)
    assert "-i" in fake.commands.last_cmd.split()


def test_grep_glob_filters_via_include(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="match\n", stderr="", exit_code=0))])
    sf.search_sandbox_files("C1", "1.1", "foo", glob="*.py")
    assert "--include" in fake.commands.last_cmd
    assert "*.py" in fake.commands.last_cmd


def test_grep_files_with_matches_uses_dash_l_not_dash_n(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="/f.py\n", stderr="", exit_code=0))])
    result = sf.search_sandbox_files("C1", "1.1", "foo", output_mode="files_with_matches")
    assert result == "/f.py"
    grep_portion = fake.commands.last_cmd.split("|")[0].split()
    assert "-l" in grep_portion
    assert "-n" not in grep_portion


def test_grep_count_mode_drops_zero_count_files(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="/a.py:3\n/b.py:0\n", stderr="", exit_code=0))])
    result = sf.search_sandbox_files("C1", "1.1", "foo", output_mode="count")
    assert result == "/a.py:3"
    assert "-c" in fake.commands.last_cmd.split()


def test_grep_context_lines_adds_dash_capital_c_in_content_mode(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="match\n", stderr="", exit_code=0))])
    sf.search_sandbox_files("C1", "1.1", "foo", context_lines=3)
    assert "-C" in fake.commands.last_cmd.split()
    assert "3" in fake.commands.last_cmd.split()


def test_grep_head_limit_is_applied(sandbox_env):
    fake = sandbox_env(canned=[("grep", SimpleNamespace(stdout="match\n", stderr="", exit_code=0))])
    sf.search_sandbox_files("C1", "1.1", "foo", head_limit=5)
    assert "head -n 5" in fake.commands.last_cmd


def test_grep_no_matches(sandbox_env):
    sandbox_env(canned=[("grep", SimpleNamespace(stdout="", stderr="", exit_code=1))])
    assert sf.search_sandbox_files("C1", "1.1", "nope") == "No matches found."


# ---------------------------------------------------------------------------
# Glob (list_sandbox_files)
# ---------------------------------------------------------------------------


def test_glob_builds_a_recursive_glob_script(sandbox_env):
    fake = sandbox_env(canned=[("python3", SimpleNamespace(stdout="/repo/a.py\n/repo/b.py\n", stderr="", exit_code=0))])
    result = sf.list_sandbox_files("C1", "1.1", pattern="**/*.py", path="/repo")
    assert result == "/repo/a.py\n/repo/b.py"
    cmd = fake.commands.last_cmd
    assert "glob.glob" in cmd
    assert "recursive=True" in cmd
    assert "/repo/**/*.py" in cmd


def test_glob_absolute_pattern_is_not_joined_with_path(sandbox_env):
    fake = sandbox_env(canned=[("python3", SimpleNamespace(stdout="", stderr="", exit_code=0))])
    sf.list_sandbox_files("C1", "1.1", pattern="/etc/*.conf", path="/home/user")
    assert "/etc/*.conf" in fake.commands.last_cmd
    assert "/home/user" not in fake.commands.last_cmd


def test_glob_no_matches(sandbox_env):
    sandbox_env(canned=[("python3", SimpleNamespace(stdout="", stderr="", exit_code=0))])
    assert sf.list_sandbox_files("C1", "1.1") == "No files found."
