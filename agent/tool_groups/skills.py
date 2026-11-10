"""coolton's agent skills: the tools that install, create, edit, rename and delete them,
how a skill change is applied or sent for review (agent.skill_review), and the skills
capability the main agent, its subagents and kevinton load skills with.

Registered on the agent in agent.agent; nothing here imports agent.agent.
"""

import logging
import os
import re
import shlex
import shutil

from pydantic_ai import RunContext

from agent.deps import AgentDeps
from agent.sandbox_helpers import _proxy_env, get_or_create_sandbox, run_command, sandbox_use

logger = logging.getLogger(__name__)


_SKILL_INSTALL_MAX_DEPTH = 6
_SKILL_INSTALL_MAX_FILE_BYTES = 2 * 1024 * 1024  # 2MB per file
_SKILL_INSTALL_MAX_TOTAL_BYTES = 20 * 1024 * 1024  # 20MB per skill


def install_skill(ctx: RunContext[AgentDeps], package: str, skill: str = "") -> str:
    """Install a new agent skill from the skills.sh marketplace (Vercel's Agent Skills CLI).

    Run this when the user asks to "install a skill", "add a skill", or names a
    skill package/repo they want (e.g. `vercel-labs/agent-skills`, or a GitHub URL).
    After install, the skill is available immediately via load_skill / list_skills.

    Args:
        package: The skill package to install. Either `owner/repo` (e.g.
            `vercel-labs/agent-skills`) or a full GitHub URL
            (e.g. `https://github.com/vercel-labs/agent-skills`).
        skill: Optional specific skill name inside a multi-skill repo. Leave empty
            to install all skills in the package.
    """
    # `package` names an arbitrary npm-fetched CLI + package payload; running it
    # directly on the host (the old behavior) would execute untrusted code as the
    # coolton service user, right next to .env's Slack/GitHub/E2B secrets. It runs
    # in the disposable sandbox instead, same as any other untrusted code coolton
    # touches. Nothing that ran there is trusted merely for having run: SKILL.md
    # must pass the exact same frontmatter validation create_skill enforces (see
    # _validate_skill_md), and every other file the skill shipped (references,
    # scripts, resources — most real skills have more than one file) is copied
    # over too, bounded and guarded against path traversal, but never executed
    # here — a script only ever runs later, inside a sandbox again, via
    # run_skill_script (see _run_skill_script_in_sandbox), and a resource is only
    # ever read as text into model context (read_skill_resource), never executed.
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    channel_id = ctx.deps.channel_id
    thread_ts = ctx.deps.thread_ts
    cmd = f"cd /home/user && rm -rf .agents_skills_install && mkdir -p .agents_skills_install && cd .agents_skills_install && npx -y skills@latest add {shlex.quote(package)} -y"
    if skill:
        cmd += f" -s {shlex.quote(skill)}"
    try:
        with sandbox_use(channel_id, thread_ts) as use:
            sandbox, proxy_info = get_or_create_sandbox(channel_id, thread_ts)
            use.sandbox = sandbox
            result = run_command(sandbox, cmd, timeout=180, envs=_proxy_env(proxy_info))
    except Exception as e:
        return f"Error: {str(e)}"

    if result.exit_code != 0:
        out = (result.stdout or "") + (result.stderr or "")
        return f"Failed to install skill (exit {result.exit_code}):\n{out[-1500:]}"

    from e2b import FileType

    from agent import skill_review

    # Everything is staged first and only lands in .agents/skills/ once the
    # change is approved (immediately, for the maintainer — see agent.skill_review).
    staging = skill_review.new_staging_dir(_repo_root())
    imported, rejected = [], []
    try:
        entries = sandbox.files.list("/home/user/.agents_skills_install/.agents/skills")
    except Exception:
        entries = []
    for entry in entries:
        if entry.type != FileType.DIR:
            continue
        slug = _safe_name(entry.name)
        if not slug or slug != entry.name:
            rejected.append(entry.name)
            continue
        try:
            content = sandbox.files.read(f"/home/user/.agents_skills_install/.agents/skills/{entry.name}/SKILL.md")
        except Exception:
            continue
        ok, err = _validate_skill_md(content)
        if not ok:
            rejected.append(f"{entry.name} ({err})")
            continue
        target_dir = os.path.join(staging, slug)
        if not _is_within(target_dir, staging):
            rejected.append(entry.name)
            continue
        os.makedirs(target_dir, exist_ok=True)
        with open(os.path.join(target_dir, "SKILL.md"), "w") as f:
            f.write(content)

        skill_root = f"/home/user/.agents_skills_install/.agents/skills/{entry.name}"
        try:
            file_entries = sandbox.files.list(skill_root, depth=_SKILL_INSTALL_MAX_DEPTH)
        except Exception:
            file_entries = []
        copied_bytes = 0
        for fe in file_entries:
            if fe.type != FileType.FILE:
                continue
            rel = fe.path[len(skill_root):].lstrip("/")
            if not rel or rel == "SKILL.md":
                continue
            dest = os.path.join(target_dir, *rel.split("/"))
            if not _is_within(dest, target_dir):
                rejected.append(f"{entry.name}/{rel} (path escapes skill directory)")
                continue
            if copied_bytes >= _SKILL_INSTALL_MAX_TOTAL_BYTES:
                rejected.append(f"{entry.name}/{rel} (skill exceeds {_SKILL_INSTALL_MAX_TOTAL_BYTES}-byte total)")
                continue
            try:
                data = bytes(sandbox.files.read(fe.path, format="bytes"))
            except Exception:
                continue
            if len(data) > _SKILL_INSTALL_MAX_FILE_BYTES:
                rejected.append(f"{entry.name}/{rel} (file exceeds {_SKILL_INSTALL_MAX_FILE_BYTES}-byte limit)")
                continue
            copied_bytes += len(data)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)

        imported.append(slug)

    if not imported:
        shutil.rmtree(staging, ignore_errors=True)
        detail = f" Rejected entries: {', '.join(rejected)}." if rejected else ""
        return f"No valid skill found in '{package}' (the installer ran but produced nothing usable).{detail}"
    spec = {"op": "install", "staged_dir": staging, "slugs": imported}
    if skill_review.needs_review(ctx.deps):
        preview = "\n\n".join(_staged_skill_preview(staging, slug) for slug in imported)
        msg = skill_review.submit(spec, ctx.deps, f"install {', '.join(imported)} from `{package}`", preview)
    else:
        msg = _applied_directly(apply_skill_change(spec), ctx.deps, f"install {', '.join(imported)} from `{package}`")
    if rejected:
        msg += f" Skipped invalid entries: {', '.join(rejected)}."
    return msg


def _repo_root() -> str:
    return os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _skill_dirs() -> list[str]:
    root = _repo_root()
    return [os.path.join(root, "skills"), os.path.join(root, ".agents", "skills")]


def _run_skill_script_in_sandbox(script, args=None, ctx=None) -> str:
    """Execute a skill's bundled script (the `run_skill_script` tool) inside the
    thread's E2B sandbox instead of as a host subprocess.

    pydantic_ai_skills' default executor (LocalSkillScriptExecutor) runs every
    skill script as a real subprocess ON THIS HOST, with the full host
    environment merged in — every secret in os.environ (SLACK_BOT_TOKEN,
    COOLTON_GH_TOKEN, E2B_API_KEY, BYOK/MCP encryption keys, all of it).
    Scripts ship with skills from anywhere — install_skill fetches them from
    arbitrary npm packages, and several skills already checked into this repo
    (deploy-to-vercel, marimo-pair, ...) carry their own — so `run_skill_script`
    was a direct, always-available host-RCE-with-secrets primitive regardless
    of how a given skill's script got there. Route it through the same
    disposable sandbox every other untrusted-code path already uses
    (run_linux_command, code_mode, install_skill) instead.
    """
    if ctx is None or getattr(ctx, "deps", None) is None:
        return "Error: no run context available to route this into a sandbox."
    deps = ctx.deps
    if not os.environ.get("E2B_API_KEY"):
        return "Error: E2B_API_KEY not configured."
    if not script.uri:
        return f"Error: script '{script.name}' has no file to execute."
    try:
        with open(script.uri, "rb") as f:
            content = f.read()
    except OSError as e:
        return f"Error reading script '{script.name}': {e}"

    remote_name = os.path.basename(script.uri)
    remote_path = f"/home/user/skill_script_{remote_name}"

    # Mirrors pydantic_ai_skills' own LocalSkillScriptExecutor._build_args: every
    # script takes named (--flag value) arguments, never positional ones.
    #
    # Values are shlex-quoted below, but a flag NAME was interpolated as
    # f"--{key}" completely unquoted — args comes from the model's own
    # run_skill_script tool call, so a key like "foo; rm -rf ~" would inject
    # extra shell syntax into run_cmd. This only runs inside the disposable
    # sandbox (no host exposure, and the model already has unrestricted
    # sandbox code execution via other tools), but there's no reason for a
    # flag name to be anything other than an identifier — reject anything
    # else rather than let it become part of the shell command.
    _FLAG_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
    cmd_args: list[str] = []
    for key, value in (args or {}).items():
        if not _FLAG_NAME_RE.match(key):
            logger.warning("Skipping skill script arg with an unsafe flag name: %r", key)
            continue
        if isinstance(value, bool):
            if value:
                cmd_args.append(f"--{key}")
        elif isinstance(value, list):
            for item in value:
                cmd_args += [f"--{key}", shlex.quote(str(item))]
        elif value is not None:
            cmd_args += [f"--{key}", shlex.quote(str(value))]

    suffix = os.path.splitext(remote_name)[1].lower()
    interpreter = {".py": "python3", ".sh": "sh", ".bash": "bash"}.get(suffix)
    quoted_path = shlex.quote(remote_path)
    run_cmd = f"chmod +x {quoted_path} && "
    run_cmd += f"{interpreter} {quoted_path}" if interpreter else quoted_path
    if cmd_args:
        run_cmd += " " + " ".join(cmd_args)

    try:
        with sandbox_use(deps.channel_id, deps.thread_ts) as use:
            sandbox, proxy_info = get_or_create_sandbox(deps.channel_id, deps.thread_ts)
            use.sandbox = sandbox
            sandbox.files.write(remote_path, content)
            result = run_command(sandbox, run_cmd, envs=_proxy_env(proxy_info), timeout=120)
    except Exception as e:
        return f"Error running skill script in sandbox: {e}"

    output = []
    if result.stdout:
        output.append(f"STDOUT:\n{result.stdout}")
    if result.stderr:
        output.append(f"STDERR:\n{result.stderr}")
    output.append(f"Exit Code: {result.exit_code}")
    return "\n\n".join(output)


def build_skills_capability():
    """The skills capability every agent that loads skills uses (the main
    agent, kevinton). Always wires run_skill_script to the sandbox executor —
    pydantic_ai_skills' default (LocalSkillScriptExecutor) would run skill
    scripts as host subprocesses with every secret in the environment."""
    from pydantic_ai_skills import CallableSkillScriptExecutor, SkillsCapability, SkillsDirectory

    skill_script_executor = CallableSkillScriptExecutor(func=_run_skill_script_in_sandbox)
    return SkillsCapability(
        directories=[SkillsDirectory(path=d, script_executor=skill_script_executor) for d in _skill_dirs()],
        auto_reload=True,
    )


def _is_within(path: str, parent: str) -> bool:
    """True only if `path` is the same as or nested under `parent` (no traversal)."""
    path = os.path.abspath(path)
    parent = os.path.abspath(parent)
    return path == parent or path.startswith(parent + os.sep)


def _build_skill_md(slug: str, description: str, body: str) -> str:
    """Build a SKILL.md string with valid YAML frontmatter.

    The description is single-quoted so embedded colons (the exact thing that
    broke the catalog before) can't terminate the YAML mapping early.
    """
    desc = description.replace("'", "''")
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: '{desc}'\n"
        "---\n\n"
        f"# {slug.replace('-', ' ').title()}\n\n"
        f"{body}\n"
    )


def _validate_skill_md(content: str) -> tuple[bool, str]:
    """Return (ok, error) for a SKILL.md's frontmatter.

    Parses the leading YAML block so a malformed skill is caught before it can
    enter the catalog and break every model's skill scan.
    """
    try:
        import yaml
    except ImportError:
        return True, ""  # yaml unavailable — skip validation rather than block
    if not content.startswith("---"):
        return False, "missing frontmatter delimiters"
    end = content.find("\n---", 3)
    if end == -1:
        return False, "unterminated frontmatter"
    block = content[3:end].strip()
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as e:
        return False, f"invalid YAML: {e}"
    if not isinstance(data, dict):
        return False, "frontmatter is not a mapping"
    if not data.get("name") or not data.get("description"):
        return False, "name and description are required"
    return True, ""


def _resolve_skill(name: str) -> str | None:
    """Find the on-disk folder for a skill by name across known skill dirs.

    Only direct children of a known skill dir are matched; names containing path
    separators or traversal sequences are rejected (returns None).
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        return None
    for base in _skill_dirs():
        cand = os.path.join(base, name)
        # cand must be a direct child of a known skill dir
        if os.path.dirname(os.path.abspath(cand)) != os.path.abspath(base):
            continue
        if os.path.isdir(cand) and os.path.exists(os.path.join(cand, "SKILL.md")):
            return cand
    return None


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", name.strip().lower())


def _staged_skill_preview(staging: str, slug: str) -> str:
    """What the reviewer sees for one staged skill: its SKILL.md and file list."""
    skill_dir = os.path.join(staging, slug)
    try:
        with open(os.path.join(skill_dir, "SKILL.md")) as f:
            skill_md = f.read()
    except OSError:
        skill_md = "(no SKILL.md)"
    files = sorted(
        os.path.relpath(os.path.join(root, name), skill_dir)
        for root, _dirs, names in os.walk(skill_dir) for name in names
    )
    return f"--- {slug} ---\nfiles: {', '.join(files)}\n\n{skill_md}"


def apply_skill_change(spec: dict) -> str:
    """Carry out a skill change — straight away for the maintainer, or once a
    proposal is approved (agent.skill_review). Re-checks everything, since a
    proposal can sit for a while and the skill dirs may have changed since."""
    op = spec.get("op")
    if op == "create":
        slug = spec["slug"]
        target = os.path.join(_repo_root(), "skills", slug)
        if not _is_within(target, os.path.join(_repo_root(), "skills")):
            return "Error: invalid skill name (must not escape the skills directory)."
        if os.path.exists(target):
            return f"Error: a skill named '{slug}' already exists at {target}."
        try:
            os.makedirs(target)
            with open(os.path.join(target, "SKILL.md"), "w") as f:
                f.write(spec["content"])
        except OSError as e:
            return f"Error creating skill: {e}"
        return f"Created skill '{slug}' at skills/{slug}/SKILL.md. It is now available via list_skills / load_skill."

    if op == "install":
        from agent import skill_review

        staging = spec["staged_dir"]
        skills_dir = os.path.join(_repo_root(), ".agents", "skills")
        installed = []
        try:
            for slug in spec["slugs"]:
                src, dst = os.path.join(staging, slug), os.path.join(skills_dir, slug)
                if not _is_within(dst, skills_dir) or not os.path.isdir(src):
                    continue
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                os.makedirs(skills_dir, exist_ok=True)
                shutil.move(src, dst)
                installed.append(slug)
        except OSError as e:
            return f"Error installing skill(s): {e}"
        finally:
            skill_review.discard({"spec": spec})
        if not installed:
            return "Error: the staged skill files are gone; nothing was installed."
        return f"Installed skill(s): {', '.join(installed)}. Available now via list_skills / load_skill."

    if op == "rename":
        src = _resolve_skill(spec["old_name"])
        if not src:
            return f"Error: skill '{spec['old_name']}' not found in any skill directory."
        new_slug = spec["new_slug"]
        dst = os.path.join(os.path.dirname(src), new_slug)
        if os.path.exists(dst):
            return f"Error: a skill named '{new_slug}' already exists."
        try:
            os.rename(src, dst)
            sk_md = os.path.join(dst, "SKILL.md")
            if os.path.exists(sk_md):
                with open(sk_md, "r") as f:
                    txt = f.read()
                txt = re.sub(r"(?m)^name:\s*.*$", f"name: {new_slug}", txt, count=1)
                with open(sk_md, "w") as f:
                    f.write(txt)
        except OSError as e:
            return f"Error renaming skill: {e}"
        return f"Renamed skill '{spec['old_name']}' -> '{new_slug}'."

    if op == "update":
        src = _resolve_skill(spec["name"])
        if not src:
            return f"Error: skill '{spec['name']}' not found in any skill directory."
        try:
            with open(os.path.join(src, "SKILL.md"), "w") as f:
                f.write(spec["content"])
        except OSError as e:
            return f"Error updating skill: {e}"
        return f"Updated skill '{spec['name']}'. The new version is what load_skill returns from now on."

    if op == "delete":
        src = _resolve_skill(spec["name"])
        if not src:
            return f"Error: skill '{spec['name']}' not found in any skill directory."
        try:
            shutil.rmtree(src)
        except OSError as e:
            return f"Error deleting skill: {e}"
        return f"Deleted skill '{spec['name']}' from {src}."

    return f"Error: unknown skill change {op!r}."


def _apply_or_submit_skill_change(ctx: RunContext[AgentDeps], spec: dict, description: str, preview: str = "") -> str:
    from agent import skill_review

    if skill_review.needs_review(ctx.deps):
        return skill_review.submit(spec, ctx.deps, description, preview)
    return _applied_directly(apply_skill_change(spec), ctx.deps, description)


def _applied_directly(result: str, deps, description: str) -> str:
    """Say outright that a change skipped review — without this the model,
    primed by every "goes to review unless the maintainer asked" note, told the
    maintainer their already-live skill was pending review. Also sends the
    maintainer an FYI when AUTO_ACCEPT_SKILLS is what let it through."""
    from agent import skill_review

    if result.startswith("Error"):
        return result
    skill_review.note_auto_accepted(deps, description)
    return f"{result} This is LIVE now — it did NOT go to review ({skill_review.skip_reason(deps)})."


def create_skill(ctx: RunContext[AgentDeps], name: str, description: str, body: str = "") -> str:
    """Create a new custom agent skill in the repo's `skills/` directory.

    Use this when the user wants to "make a skill", "create a skill for X",
    "turn this workflow into a skill", or save a reusable playbook. This writes
    a proper SKILL.md (frontmatter + instructions) so the skill is discoverable
    via list_skills / load_skill. Unless the coolton maintainer asked for it, the
    new skill goes to the maintainer for review first and only goes live once
    approved. The result says which happened, relay that, don't guess. Do NOT use shell/CLI commands
    in the sandbox to create skills, they have no effect on the agent.

    Args:
        name: Skill name (will be slugified, e.g. "My Cool Skill" -> "my-cool-skill").
        description: One-line description; used for skill discovery. It's all a future turn
            sees before loading the skill, so name the situations and requests it's for, in
            terms that make sense without knowing the skill (say what a named service is; no
            "and other X tasks" catch-alls, no how-to).
        body: The skill's instructions/body (Markdown). If empty, a minimal
            template is created for you to fill in later.
    """
    slug = _safe_name(name)
    if not slug:
        return "Error: invalid skill name."
    target = os.path.join(_repo_root(), "skills", slug)
    if not _is_within(target, os.path.join(_repo_root(), "skills")):
        return "Error: invalid skill name (must not escape the skills directory)."
    if os.path.exists(target):
        return f"Error: a skill named '{slug}' already exists at {target}."
    if not body.strip():
        body = (
            "# " + slug.replace("-", " ").title() + "\n\n"
            "Describe the workflow, steps, and guidance for this skill here.\n"
        )
    content = _build_skill_md(slug, description.strip(), body.strip())
    # Validate before writing: a malformed SKILL.md would break the whole skill
    # catalog load (every model that scans skills chokes on bad frontmatter).
    # If invalid, reject and do NOT create the skill.
    ok, err = _validate_skill_md(content)
    if not ok:
        return (
            f"Error: generated SKILL.md failed validation ({err}). The skill was "
            "NOT created. Fix the description/body (avoid unquoted colons in the "
            "description) and try again."
        )
    return _apply_or_submit_skill_change(
        ctx, {"op": "create", "slug": slug, "content": content}, f"create skill `{slug}`", content,
    )


def rename_skill(ctx: RunContext[AgentDeps], old_name: str, new_name: str) -> str:
    """Rename an existing agent skill (moves its folder and updates frontmatter name).

    Use this when the user wants to rename a skill. Operates on skills found in
    the repo's `skills/` or `.agents/skills/` directories. Unless the coolton
    maintainer asked for it, the rename waits for maintainer review (the result says
    which happened, relay that, don't guess). Do NOT use
    sandbox shell commands, they have no effect on the agent.

    Args:
        old_name: Current skill name/folder.
        new_name: Desired new skill name (will be slugified).
    """
    src = _resolve_skill(old_name)
    if not src:
        return f"Error: skill '{old_name}' not found in any skill directory."
    new_slug = _safe_name(new_name)
    if not new_slug:
        return "Error: invalid new skill name."
    if os.path.exists(os.path.join(os.path.dirname(src), new_slug)):
        return f"Error: a skill named '{new_slug}' already exists."
    return _apply_or_submit_skill_change(
        ctx, {"op": "rename", "old_name": old_name, "new_slug": new_slug},
        f"rename skill `{old_name}` -> `{new_slug}`",
    )


def _updated_skill_md(current: str, description: str, body: str) -> str:
    """`current` SKILL.md with a new description and/or body (empty keeps the old one);
    the rest of the frontmatter stays as it was."""
    import yaml

    end = current.find("\n---", 3)
    data = yaml.safe_load(current[3:end]) if current.startswith("---") and end != -1 else None
    if not isinstance(data, dict):
        raise ValueError("its SKILL.md has no readable frontmatter")
    old_body = current[end + 4:].lstrip("\n")
    if description:
        data["description"] = description
    frontmatter = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1_000_000)
    return f"---\n{frontmatter}---\n\n{body or old_body.rstrip()}\n"


def update_skill(ctx: RunContext[AgentDeps], name: str, description: str = "", body: str = "") -> str:
    """Edit an existing agent skill: its description (what it's for and when to use it), its
    instructions, or both. Its name and any other files stay as they are.

    Use this when the user wants to change, reword, fix or extend a skill. `body` replaces the
    whole instructions, so to change part of them, load_skill first and pass the full new text.
    Unless the coolton maintainer asked for it, the edit waits for maintainer review (the result
    says which happened, relay that, don't guess). Do NOT use sandbox shell commands, they have
    no effect on the agent.

    Args:
        name: The skill's name/folder.
        description: New description; leave empty to keep the current one. Same rules as
            create_skill's: when to use it, understandable before loading the skill.
        body: New instructions (Markdown, without frontmatter); leave empty to keep the current ones.
    """
    src = _resolve_skill(name)
    if not src:
        return f"Error: skill '{name}' not found in any skill directory."
    description, body = description.strip(), body.strip()
    if not description and not body:
        return "Error: pass a new description, a new body, or both."
    try:
        with open(os.path.join(src, "SKILL.md")) as f:
            content = _updated_skill_md(f.read(), description, body)
    except (OSError, ValueError, ImportError) as e:
        return f"Error reading skill '{name}': {e}"
    ok, err = _validate_skill_md(content)
    if not ok:
        return f"Error: the updated SKILL.md failed validation ({err}). The skill was NOT changed."
    return _apply_or_submit_skill_change(
        ctx, {"op": "update", "name": name, "content": content}, f"update skill `{name}`", content,
    )


def delete_skill(ctx: RunContext[AgentDeps], name: str) -> str:
    """Delete an agent skill folder entirely from disk.

    Use this when the user wants to remove/uninstall a skill. This is permanent.
    Operates on skills in the repo's `skills/` or `.agents/skills/` directories.
    Unless the coolton maintainer asked for it (the result says which happened, relay
    that, don't guess), the deletion waits for maintainer
    review. Do NOT use sandbox shell commands, they have no effect on the agent.

    Args:
        name: Skill name/folder to delete.
    """
    src = _resolve_skill(name)
    if not src:
        return f"Error: skill '{name}' not found in any skill directory."
    return _apply_or_submit_skill_change(
        ctx, {"op": "delete", "name": name}, f"delete skill `{name}` ({os.path.relpath(src, _repo_root())})",
    )
