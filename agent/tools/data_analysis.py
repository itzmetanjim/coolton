import json
import shlex

from agent.sandbox_helpers import get_or_create_sandbox


def _get_sandbox(channel_id: str, thread_ts: str):
    try:
        return get_or_create_sandbox(channel_id, thread_ts)[0], None
    except Exception as e:
        return None, f"Error: {e}"


def _ensure_py_libs(sandbox, libs: list[str]) -> str | None:
    """Ensure the given Python packages are importable in the sandbox, pip-installing if missing."""
    import_script = shlex.quote("\n".join(f"import {lib}" for lib in libs))
    try:
        sandbox.commands.run(f"python3 -c {import_script}")
        return None
    except Exception:
        pass
    try:
        sandbox.commands.run(
            "python3 -m pip install --break-system-packages --quiet " + " ".join(libs),
            timeout=300,
        )
    except Exception as e:
        stderr = getattr(e, "stderr", None) or ""
        stdout = getattr(e, "stdout", None) or ""
        return f"Failed to install {', '.join(libs)}: {stderr or stdout or str(e)}"
    return None


def extract_tar_gz_in_sandbox(channel_id: str, thread_ts: str, archive_path: str, extract_to: str = "/home/user/data") -> str:
    """Extract a .tar.gz or .tgz file in the sandbox.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        archive_path: Path to the .tar.gz file in sandbox (e.g., ~/attachments/data.tar.gz).
        extract_to: Directory to extract to (default: /home/user/data).
        
    Returns:
        Summary of extraction results.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        # Create extraction directory
        sandbox.commands.run(f"mkdir -p {shlex.quote(extract_to)}")
        
        # Validate tar members before extraction to prevent path traversal
        check_result = sandbox.commands.run(f"tar -tzf {shlex.quote(archive_path)} 2>&1")
        if check_result.exit_code != 0:
            return f"Error reading archive: {check_result.stderr or check_result.stdout}"
        for member in check_result.stdout.splitlines():
            if member.startswith("/") or ".." in member or member.startswith("./.."):
                return f"Error: Archive contains unsafe path: {member}"
        # Extract using tar
        result = sandbox.commands.run(f"tar -xzf {shlex.quote(archive_path)} -C {shlex.quote(extract_to)} 2>&1")
        
        # List extracted files
        list_result = sandbox.commands.run(f"find {shlex.quote(extract_to)} -type f | head -20")
        
        output = []
        if result.stdout:
            output.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        if list_result.stdout:
            output.append(f"Extracted files:\n{list_result.stdout}")
        
        return "\n\n".join(output) if output else "Extraction completed (no output)."
    except Exception as e:
        return f"Error extracting archive: {str(e)}"


def analyze_csv_in_sandbox(channel_id: str, thread_ts: str, csv_path: str, query: str = "") -> str:
    """Analyze a CSV file in the sandbox using Python/pandas.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        csv_path: Path to the CSV file in sandbox.
        query: Optional analysis question or pandas code to run.
        
    Returns:
        Analysis results.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    err = _ensure_py_libs(sandbox, ["pandas"])
    if err:
        return err
    try:
        # csv_path is user-supplied: escape it for the shell command AND for the
        # single-quoted Python string literal it is embedded into below.
        shell_path = shlex.quote(csv_path)
        py_path = csv_path.replace("\\", "\\\\").replace("'", "\\'")

        # First check the CSV structure
        check_result = sandbox.commands.run(f"head -5 {shell_path}")
        if check_result.stdout:
            check_result.stdout
        else:
            return f"Error: Could not read {csv_path}"
        
        # If no query provided, do basic analysis
        if not query:
            script = f"""
import pandas as pd
import sys

df = pd.read_csv('{py_path}')
print("=== SHAPE ===")
print(df.shape)
print("\\n=== COLUMNS ===")
print(list(df.columns))
print("\\n=== DTYPES ===")
print(df.dtypes)
print("\\n=== HEAD ===")
print(df.head())
print("\\n=== DESCRIBE ===")
print(df.describe(include='all'))
print("\\n=== NULL COUNTS ===")
print(df.isnull().sum())
print("\\n=== MEMORY USAGE ===")
print(df.memory_usage(deep=True).sum(), "bytes")
"""
        else:
            # Run custom query
            script = f"""
import pandas as pd
import sys

df = pd.read_csv('{py_path}')
print("=== RESULT ===")
try:
    result = {query}
    if hasattr(result, 'to_string'):
        print(result.to_string())
    else:
        print(result)
except Exception as e:
    print(f"Error: {{e}}")
"""
        
        result = sandbox.commands.run(f"python3 -c {shlex.quote(script)}")
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        return "\n\n".join(output) if output else "Analysis completed (no output)."
        
    except Exception as e:
        return f"Error analyzing CSV: {str(e)}"


def run_sql_on_csv(channel_id: str, thread_ts: str, csv_path: str, sql_query: str) -> str:
    """Run SQL queries on CSV files using DuckDB in the sandbox.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        csv_path: Path to the CSV file in sandbox.
        sql_query: SQL query to run (table name is 'data').
        
    Returns:
        Query results.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    err = _ensure_py_libs(sandbox, ["duckdb"])
    if err:
        return err
    try:
        script = (
            "import duckdb\n"
            "import sys\n\n"
            "conn = duckdb.connect()\n"
            f"conn.execute(\"CREATE TABLE data AS SELECT * FROM read_csv_auto('{csv_path}')\")\n"
            f"result = conn.execute({json.dumps(sql_query)}).fetchall()\n"
            "columns = [desc[0] for desc in conn.description]\n"
            "print(' | '.join(columns))\n"
            "print('-' * 80)\n"
            "for row in result:\n"
            "    print(' | '.join(str(v) for v in row))\n"
            f"print(f'\\nRows returned: {{len(result)}}')"
        )
        result = sandbox.commands.run(f"python3 -c {shlex.quote(script)}")
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        return "\n\n".join(output) if output else "Query completed (no results)."
    except Exception as e:
        return f"Error running SQL: {str(e)}"


def run_opencode_in_sandbox(channel_id: str, thread_ts: str, task: str, model: str = "") -> str:
    """Run opencode in the sandbox to perform complex coding tasks.
    
    Opencode is an open-source AI coding agent (like Claude Code).
    It can read/write files, run commands, and use tools to complete tasks.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        task: The task/question for opencode to complete.
        model: Optional model override (e.g., "anthropic/claude-sonnet-4-6").
        
    Returns:
        Opencode's output/results.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        # Install opencode if not present
        install_check = sandbox.commands.run("which opencode || echo 'not found'")
        if "not found" in install_check.stdout:
            # Install via npm (requires node/npm)
            install_result = sandbox.commands.run("npm install -g opencode-ai 2>&1 || curl -fsSL https://opencode.ai/install | bash 2>&1")
            if install_result.stderr and "error" in install_result.stderr.lower():
                return f"Failed to install opencode: {install_result.stderr}"
        
        # Prepare the task
        model_flag = f"--model {model}" if model else ""
        
        # Run opencode with the task
        # opencode reads from stdin or takes task as argument
        cmd = f"echo '{task}' | opencode run {model_flag} 2>&1"
        result = sandbox.commands.run(cmd, timeout=300)
        
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        return "\n\n".join(output) if output else "Opencode completed (no output)."
        
    except Exception as e:
        return f"Error running opencode: {str(e)}"


def install_opencode_in_sandbox(channel_id: str, thread_ts: str) -> str:
    """Install opencode in the sandbox if not already installed.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        
    Returns:
        Installation status.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    try:
        # Check if already installed
        check = sandbox.commands.run("which opencode")
        if check.stdout.strip():
            return f"Opencode already installed at: {check.stdout.strip()}"
        
        # Install via npm
        result = sandbox.commands.run("npm install -g opencode-ai 2>&1")
        if result.stdout:
            return f"Installed via npm:\n{result.stdout}"
        if result.stderr:
            return f"STDERR:\n{result.stderr}"
        return "Installation attempted (check output)."
    except Exception as e:
        return f"Error installing opencode: {str(e)}"


def run_python_data_analysis(channel_id: str, thread_ts: str, code: str) -> str:
    """Run arbitrary Python data analysis code in the sandbox with pandas/numpy/duckdb pre-loaded.
    
    Args:
        channel_id: Slack channel ID.
        thread_ts: Thread timestamp.
        code: Python code to execute. Has access to: pd, np, duckdb, conn (DuckDB connection).
        
    Returns:
        Code output.
    """
    sandbox, err = _get_sandbox(channel_id, thread_ts)
    if err:
        return err
    err = _ensure_py_libs(sandbox, ["pandas", "numpy", "duckdb"])
    if err:
        return err
    try:
        # Wrap code with common imports
        wrapped_code = f"""
import pandas as pd
import numpy as np
import duckdb
import json
import sys

conn = duckdb.connect()

{code}
"""
        result = sandbox.commands.run(f"python3 -c {shlex.quote(wrapped_code)}", timeout=120)
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        return "\n\n".join(output) if output else "Code executed (no output)."
    except Exception as e:
        return f"Error running Python: {str(e)}"