import os
import shlex

from e2b.exceptions import FileNotFoundException

from agent.sandbox_helpers import get_or_create_sandbox


def _get_sandbox(channel_id: str, thread_ts: str):
    try:
        return get_or_create_sandbox(channel_id, thread_ts)[0], None
    except Exception as e:
        return None, f"Error: {e}"


def read_sandbox_file(channel_id: str, thread_ts: str, path: str) -> str:
    """Read a file from the sandbox filesystem.

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        path: Path to the file in the sandbox (e.g., /home/user/file.txt).

    Returns:
        File contents as text, or an error message.
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
    if isinstance(content, bytes):
        return content.decode("utf-8", errors="replace")
    return content


def write_sandbox_file(channel_id: str, thread_ts: str, path: str, content: str) -> str:
    """Write content to a file in the sandbox filesystem.

    Creates parent directories if they don't exist.

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
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


def search_sandbox_files(channel_id: str, thread_ts: str, pattern: str, path: str = "/home/user") -> str:
    """Search for text patterns in sandbox files (grep).

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        pattern: Regex or text pattern to search for.
        path: Directory to search in (default: /home/user).

    Returns:
        Matching lines, or an error message.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        result = sandbox.commands.run(f"grep -rn {shlex.quote(pattern)} {shlex.quote(path)} 2>/dev/null || echo 'No matches found'")
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(f"STDERR: {result.stderr}")
        return "\n".join(output) if output else "No matches found."
    except Exception as e:
        return f"Error searching files: {str(e)}"


def list_sandbox_files(channel_id: str, thread_ts: str, pattern: str = "*", path: str = "/home/user") -> str:
    """List files in the sandbox matching a glob pattern.

    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        pattern: Glob pattern (default: "*").
        path: Directory to search in (default: /home/user).

    Returns:
        File list, or an error message.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        result = sandbox.commands.run(f"find {shlex.quote(path)} -name {shlex.quote(pattern)} -type f 2>/dev/null | head -50")
        if result.stdout:
            return result.stdout
        return "No files found."
    except Exception as e:
        return f"Error listing files: {str(e)}"
