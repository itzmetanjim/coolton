import json
import shlex

from agent.sandbox_store import get_thread_sandbox_id
try:
    from e2b import Sandbox
except ImportError:
    Sandbox = None


def _get_sandbox(channel_id: str, thread_ts: str):
    if Sandbox is None:
        return None, "E2B sandbox library not available."
    sandbox_id = get_thread_sandbox_id(channel_id, thread_ts)
    if not sandbox_id:
        return None, "No active sandbox for this thread. Run a command first."
    return Sandbox.connect(sandbox_id), None


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
        sandbox.commands.run(f"mkdir -p {extract_to}")
        
        # Extract using tar
        result = sandbox.commands.run(f"tar -xzf {archive_path} -C {extract_to} 2>&1")
        
        # List extracted files
        list_result = sandbox.commands.run(f"find {extract_to} -type f | head -20")
        
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
    try:
        # First check the CSV structure
        check_result = sandbox.commands.run(f"head -5 {csv_path}")
        if check_result.stdout:
            check_result.stdout
        else:
            return f"Error: Could not read {csv_path}"
        
        # If no query provided, do basic analysis
        if not query:
            script = f"""
import pandas as pd
import sys

df = pd.read_csv('{csv_path}')
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

df = pd.read_csv('{csv_path}')
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
        
        result = sandbox.commands.run(f"python3 -c \"{script}\"")
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
        result = sandbox.commands.run(f"python3 -c \"{wrapped_code}\"", timeout=120)
        output = []
        if result.stdout:
            output.append(result.stdout)
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        return "\n\n".join(output) if output else "Code executed (no output)."
    except Exception as e:
        return f"Error running Python: {str(e)}"