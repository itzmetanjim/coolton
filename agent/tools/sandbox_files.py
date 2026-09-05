"""Structured file tools for the E2B sandbox — coolton's equivalent of a coding
agent's Read/Write/Edit/Grep/Glob, instead of doing everything through
run_linux_command (cat/sed/grep/find). Each function here is wrapped by a
`@agent.tool`-decorated `<name>_tool` in agent/agent.py.
"""

import os
import shlex

from e2b.exceptions import FileNotFoundException

from agent.sandbox_helpers import get_or_create_sandbox

_MAX_LINE_CHARS = 2000
_DEFAULT_READ_LIMIT = 2000
_DEFAULT_GREP_HEAD_LIMIT = 100
_DEFAULT_GLOB_LIMIT = 200


def _get_sandbox(channel_id: str, thread_ts: str):
    try:
        return get_or_create_sandbox(channel_id, thread_ts)[0], None
    except Exception as e:
        return None, f"Error: {e}"


def _decode(content) -> str:
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def read_sandbox_file(
    channel_id: str, thread_ts: str, path: str, offset: int = 1, limit: int = _DEFAULT_READ_LIMIT,
) -> str:
    """Read a file from the sandbox filesystem, cat -n style (1-indexed line
    numbers, tab-separated), with offset/limit so a large file can be paged
    through instead of dumped whole into context.

    Args:
        path: Path to the file in the sandbox.
        offset: 1-indexed line number to start from (default 1, the top).
        limit: Max number of lines to return (default 2000).

    Returns:
        Line-numbered file contents, or an error/status message.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        content = sandbox.files.read(path)
    except FileNotFoundException:
        return f"Error: File not found at {path}"
    except Exception as e:
        return f"Error reading file: {str(e)}"

    text = _decode(content)
    if text == "":
        return f"(empty file: {path})"

    lines = text.splitlines()
    offset = max(1, offset)
    if offset > len(lines):
        return f"Error: offset {offset} is beyond the end of the file ({len(lines)} lines total)."
    limit = max(1, limit)
    end = offset - 1 + limit
    selected = lines[offset - 1 : end]

    out_lines = []
    for i, line in enumerate(selected, start=offset):
        if len(line) > _MAX_LINE_CHARS:
            line = line[:_MAX_LINE_CHARS] + f"... (line truncated, {len(line)} chars total)"
        out_lines.append(f"{i:6d}\t{line}")

    result = "\n".join(out_lines)
    remaining = len(lines) - end
    if remaining > 0:
        result += f"\n\n... {remaining} more line(s) not shown. Re-read with offset={end + 1} to continue."
    return result


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def write_sandbox_file(channel_id: str, thread_ts: str, path: str, content: str) -> str:
    """Write content to a file in the sandbox filesystem, overwriting it
    entirely if it already exists. Creates parent directories if needed.

    Prefer edit_sandbox_file for a targeted change to an existing file —
    it's cheaper and safer than rewriting the whole thing.

    Args:
        path: Path to write to (e.g., /home/user/output.txt).
        content: Text content to write.

    Returns:
        Success/error message.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        parent = os.path.dirname(path)
        if parent:
            sandbox.commands.run(f"mkdir -p {shlex.quote(parent)}")
        sandbox.files.write(path, content.encode())
        return f"Written {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error writing file: {str(e)}"


# ---------------------------------------------------------------------------
# Edit
# ---------------------------------------------------------------------------


def edit_sandbox_file(
    channel_id: str, thread_ts: str, path: str, old_string: str, new_string: str, replace_all: bool = False,
) -> str:
    """Replace an exact string in an existing sandbox file — a targeted diff
    instead of rewriting the whole file with write_sandbox_file.

    old_string must match the file's contents EXACTLY, including whitespace
    and indentation, and (unless replace_all=True) must be unique in the
    file — include enough surrounding context (a full line or more) to pin
    down one specific occurrence rather than guessing at a short fragment.

    Args:
        path: Path to the file to edit. Must already exist (use
            write_sandbox_file to create a new file).
        old_string: The exact text to find and replace. Must not be empty.
        new_string: The text to replace it with.
        replace_all: Replace every occurrence of old_string instead of
            requiring it to be unique (default False).

    Returns:
        Success/error message.
    """
    if not old_string:
        return "Error: old_string must not be empty. Use write_sandbox_file to create a new file."
    if old_string == new_string:
        return "Error: old_string and new_string are identical — nothing to change."

    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        content = sandbox.files.read(path)
    except FileNotFoundException:
        return f"Error: File not found at {path}. Use write_sandbox_file to create a new file."
    except Exception as e:
        return f"Error reading file: {str(e)}"

    text = _decode(content)
    count = text.count(old_string)
    if count == 0:
        return (
            f"Error: old_string not found in {path}. Make sure it matches the file's "
            "contents exactly, including whitespace and indentation — read the file "
            "first if unsure."
        )
    if count > 1 and not replace_all:
        return (
            f"Error: old_string appears {count} times in {path}. Either include more "
            "surrounding context to make it unique, or pass replace_all=True to "
            "replace every occurrence."
        )

    new_text = text.replace(old_string, new_string, -1 if replace_all else 1)
    try:
        sandbox.files.write(path, new_text.encode())
    except Exception as e:
        return f"Error writing file: {str(e)}"

    replaced = count if replace_all else 1
    return f"Replaced {replaced} occurrence(s) in {path}."


# ---------------------------------------------------------------------------
# Grep
# ---------------------------------------------------------------------------

_GREP_OUTPUT_MODES = {"content", "files_with_matches", "count"}


def search_sandbox_files(
    channel_id: str,
    thread_ts: str,
    pattern: str,
    path: str = "/home/user",
    glob: str = "",
    case_insensitive: bool = False,
    output_mode: str = "content",
    context_lines: int = 0,
    head_limit: int = _DEFAULT_GREP_HEAD_LIMIT,
) -> str:
    """Search file contents in the sandbox with a regex — coolton's grep, for
    finding where something is defined/used across a directory. Prefer this
    over running `grep`/`rg` yourself via run_linux_command.

    Args:
        pattern: Extended regex (grep -E syntax) to search for.
        path: File or directory to search (default /home/user).
        glob: Optional filename glob to restrict the search to (e.g. "*.py").
        case_insensitive: Match case-insensitively (default False).
        output_mode: "content" (matching lines, default), "files_with_matches"
            (just the file paths), or "count" (per-file match counts).
        context_lines: Lines of context to show before/after each match
            (content mode only, default 0).
        head_limit: Cap on the number of output lines returned (default 100),
            so a broad search can't flood context.

    Returns:
        Matches per output_mode, or an error/status message.
    """
    if output_mode not in _GREP_OUTPUT_MODES:
        return f"Error: output_mode must be one of {sorted(_GREP_OUTPUT_MODES)}, got {output_mode!r}."

    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err

    flags = ["-r", "-n", "-E"]
    if case_insensitive:
        flags.append("-i")
    if glob:
        flags += ["--include", shlex.quote(glob)]
    if output_mode == "files_with_matches":
        flags = [f for f in flags if f != "-n"] + ["-l"]
    elif output_mode == "count":
        flags = [f for f in flags if f != "-n"] + ["-c"]
    elif context_lines > 0:
        flags += ["-C", str(context_lines)]

    cmd = f"grep {' '.join(flags)} {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null | head -n {max(1, head_limit)}"
    try:
        result = sandbox.commands.run(cmd)
    except Exception as e:
        return f"Error searching files: {str(e)}"

    output = (result.stdout or "").rstrip("\n")
    if output_mode == "count":
        # grep -c prints "path:0" for every file it looked at, even non-matches —
        # keep only files that actually matched.
        output = "\n".join(
            line for line in output.splitlines() if not line.endswith(":0")
        )
    if not output.strip():
        return "No matches found."
    return output


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


def list_sandbox_files(
    channel_id: str, thread_ts: str, pattern: str = "*", path: str = "/home/user", limit: int = _DEFAULT_GLOB_LIMIT,
) -> str:
    """Find files in the sandbox matching a glob pattern — coolton's file
    finder, for "where is this file" instead of `find`/`ls` via
    run_linux_command. Supports "**" for recursive matching (e.g.
    "**/*.py" finds every .py file under `path`, at any depth).

    Results are sorted by modification time, most recently modified first.

    Args:
        pattern: Glob pattern, relative to `path` unless it starts with "/"
            (default "*"). Use "**/*.ext" to search recursively.
        path: Directory to search from (default /home/user).
        limit: Max number of results to return (default 200).

    Returns:
        Newline-separated matching file paths, or a status message.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err

    full_pattern = pattern if pattern.startswith("/") else os.path.join(path, pattern)
    script = (
        "import glob, os\n"
        f"matches = glob.glob({full_pattern!r}, recursive=True)\n"
        "matches = [m for m in matches if os.path.isfile(m)]\n"
        "matches.sort(key=lambda p: os.path.getmtime(p), reverse=True)\n"
        f"for m in matches[:{max(1, limit)}]:\n"
        "    print(m)\n"
    )
    try:
        result = sandbox.commands.run(f"python3 -c {shlex.quote(script)}")
    except Exception as e:
        return f"Error listing files: {str(e)}"

    output = (result.stdout or "").strip()
    if not output:
        return "No files found."
    return output

