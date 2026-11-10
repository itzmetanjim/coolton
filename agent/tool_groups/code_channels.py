"""Code channels (agent.tools.code_channel, agent.tools.code_channel_tools): the tools that
create one and manage its tabs, context bar, commands, name and archiving.

Registered on the agent in agent.agent; nothing here imports agent.agent.
"""

from pydantic_ai import RunContext

from agent.deps import AgentDeps
from agent.sandbox_helpers import get_or_create_sandbox, sandbox_use
from agent.surface import get_surface as _surface

def create_code_channel_tool(ctx: RunContext[AgentDeps], name: str, task: str = "", private: bool = False) -> str:
    """Create a Slack "code channel" and move this whole conversation into it as
    its own single coolton conversation.

    Use it when the user asks for a code channel, or offer one (and create it once they
    agree) when a request grows into long, multi-step work that deserves its own space,
    like a coding project or a big investigation.

    `name` is a DISPLAY name, not a slug, write it like a sentence/title, e.g.
    "Code audit and bug detection in Coolton", never
    "code-audit-and-bug-detection-in-coolton". Spaces, uppercase letters, and
    unicode are all fine, and duplicate names are fine too, don't invent
    uniqueness suffixes.

    Slack adds you and the person who asked to the new channel, and (outside DMs) puts a
    join card on their message that people in this channel can join from. A few seconds after
    this returns, you pick up `task` there on your own, with this conversation's
    context carried over. Every message sent directly in that channel (not in a
    thread inside it) is then addressed to you and answered at channel level, as if
    the whole channel were one ongoing thread with you. A thread started inside the
    channel behaves like a normal Slack thread instead, separate conversation,
    mention required. So just tell the user you're moving the work over; don't keep
    working on `task` in the current thread once you've called this.

    Only usable on Slack, not available on the web UI.

    Args:
        name: The code channel's display name (see above, a real sentence,
            not a slug).
        task: What you'll be doing there, used to seed the handoff. Optional.
        private: Make the channel private (people join from the card) when asked to.
            Otherwise it gets this conversation's privacy: public from a public channel,
            private from anything else. It can't be made public from a private one.
    """
    surface = _surface(ctx.deps)
    if getattr(surface, "name", "slack") != "slack":
        return "Error: code channels are a Slack-only feature, not available here."
    from agent.tools.code_channel import create_code_channel
    return create_code_channel(
        ctx.deps.client, name, task, ctx.deps.user_id,
        ctx.deps.channel_id, ctx.deps.thread_ts, ctx.deps.message_ts, private=private,
    )


def code_channel_create_view_tool(
    ctx: RunContext[AgentDeps], view_type: str, view_key: str = "", name: str = "", content: str = "",
    content_file: str = "", blocks: str = "", markdown: str = "", pr_url: str = "", base_branch: str = "",
    head_branch: str = "", access_level: str = "comment", resource_domains: str = "",
) -> str:
    """CREATE a tab (an artifact) in this code channel, shown next to the chat. Calling it
    again with the same view_key updates that tab in place (send the whole new content).
    Up to 5 tabs per channel, at most one diff. To see what a tab holds, use
    code_channel_read_view_tool; to delete one, code_channel_remove_view_tool.

    view_type:
    - "html": a full, self-contained HTML document (a report, a dashboard, a demo, a
      rendered page). Needs view_key and content (or content_file). Allow outside https
      origins for scripts/styles/images with resource_domains.
    - "diff": the work's changes as a unified diff (`git diff` output). Needs content (or
      content_file); base_branch and head_branch label it. One per channel.
    - "block_kit": Block Kit blocks (a JSON array as a string). Buttons and selects work:
      when someone uses one, you get a message saying what they did. Needs view_key.
    - "canvas": a plan or document as markdown, which channel members can comment on
      (access_level "comment", the default) while only you edit it. Updating an existing
      canvas tab keeps comments on unchanged sections. Needs view_key and markdown. Read
      its comments with code_channel_read_canvas_tool.
    - "pull_request": a pull request, by pr_url.

    Args:
        view_type: One of "html", "diff", "block_kit", "canvas", "pull_request".
        view_key: The tab's stable id (e.g. "plan", "reports/coverage.html"); reuse it to update.
        name: The tab's label.
        content: The HTML document or diff text.
        content_file: A sandbox file holding the tab's content instead (a big page or diff, a
            Block Kit JSON array, or canvas markdown), read for whichever of content, blocks
            or markdown the view_type uses.
        blocks: Block Kit blocks as a JSON array string, for block_kit.
        markdown: The canvas content, for canvas.
        pr_url: The pull request's https URL, for pull_request.
        base_branch: Base branch label, for diff.
        head_branch: Head branch label, for diff.
        access_level: Canvas only: "comment" (default), "read" or "write".
        resource_domains: HTML only: comma-separated https origins the page may load from.
    """
    from agent.tools.code_channel_tools import set_view

    if content_file:
        try:
            with sandbox_use(ctx.deps.channel_id, ctx.deps.thread_ts) as use:
                use.sandbox, _ = get_or_create_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)
                from_file = use.sandbox.files.read(content_file)
        except Exception as e:
            return f"Error: couldn't read {content_file} from the sandbox: {e}"
        # The file holds whatever this view_type takes (seen live: a Block Kit tab made from a
        # file came out empty, because the file only ever filled `content`).
        if view_type.strip().lower() == "block_kit":
            blocks = from_file
        elif view_type.strip().lower() == "canvas":
            markdown = from_file
        else:
            content = from_file
    return set_view(ctx.deps.channel_id, view_type, view_key, name, content, blocks, markdown, pr_url,
                    base_branch, head_branch, access_level, resource_domains)


def block_kit_validate_tool(
    ctx: RunContext[AgentDeps], blocks: str = "", blocks_file: str = "", surface: str = "message",
) -> str:
    """Check Block Kit blocks before posting them or putting them in a code channel tab:
    Slack's own validation (each problem with a JSON pointer like /0/rows/1/1 = block 0,
    row 1, cell 1) plus, for surface "tab", the rules code channel tabs add. Fix what it
    points at and check again before using the blocks.

    Args:
        blocks: The blocks as a JSON array string.
        blocks_file: A sandbox file holding the JSON array instead.
        surface: "message" (chat.postMessage) or "tab" (a code channel Block Kit tab).
    """
    from agent.tools.block_kit import validate

    if blocks_file:
        try:
            with sandbox_use(ctx.deps.channel_id, ctx.deps.thread_ts) as use:
                use.sandbox, _ = get_or_create_sandbox(ctx.deps.channel_id, ctx.deps.thread_ts)
                blocks = use.sandbox.files.read(blocks_file)
        except Exception as e:
            return f"Error: couldn't read {blocks_file} from the sandbox: {e}"
    return validate(ctx.deps.client, blocks, surface)


def code_channel_list_views_tool(ctx: RunContext[AgentDeps]) -> str:
    """List every tab in this code channel (HTML, Block Kit, canvas and the diff, whoever
    made them), with each one's view_key when known and its view_id."""
    from agent.tools.code_channel_tools import list_views

    return list_views(ctx.deps.client, ctx.deps.channel_id)


def code_channel_remove_view_tool(ctx: RunContext[AgentDeps], view_key: str = "", view_id: str = "") -> str:
    """DELETE one of this code channel's tabs, by view_key or view_id (from
    code_channel_list_views_tool). Deleting a canvas tab deletes its canvas too.

    Args:
        view_key: The tab's view_key.
        view_id: The tab's view_id, from code_channel_list_views_tool.
    """
    from agent.tools.code_channel_tools import remove_view

    return remove_view(ctx.deps.client, ctx.deps.channel_id, view_key, view_id)


def code_channel_read_view_tool(ctx: RunContext[AgentDeps], view_key: str = "", view_id: str = "") -> str:
    """READ what one of this code channel's tabs currently holds: an HTML page's source, the
    diff's text, a Block Kit tab's blocks (JSON), or a canvas's markdown and comments. Use
    it before updating a tab you didn't just write, or when someone asks about a tab.

    Args:
        view_key: The tab's view_key (the diff tab's is "diff").
        view_id: The tab's view_id, from code_channel_list_views_tool.
    """
    from agent.tools.code_channel_tools import read_view

    return read_view(ctx.deps.client, ctx.deps.channel_id, view_key, view_id)


def code_channel_read_canvas_tool(ctx: RunContext[AgentDeps], view_key: str, include_resolved: bool = False) -> str:
    """Read a canvas tab in this code channel: its markdown and the comment threads people
    left on it (with the text each is anchored to). Check it before revising a plan.

    Args:
        view_key: The canvas tab's view_key.
        include_resolved: Also include resolved comment threads.
    """
    from agent.tools.code_channel_tools import read_canvas

    return read_canvas(ctx.deps.channel_id, view_key, include_resolved)


def code_channel_context_bar_tool(ctx: RunContext[AgentDeps], items: str) -> str:
    """Set this code channel's context bar: up to 5 items pinned at the top (the repo, the
    branch, the PR, CI status...). Each call replaces all of them, so send the full set,
    and keep it current as things change ("PR #42 merged").

    Args:
        items: JSON array string of {"key", "label", "icon"?, "url"?}. key is a unique id
            (max 64 chars), label the text (max 128); icon is one of branch, folder,
            hierarchy, life-ring, link, globe, terminal, code, search, lock.
    """
    from agent.tools.code_channel_tools import set_context_bar

    return set_context_bar(ctx.deps.channel_id, items)


def code_channel_commands_tool(ctx: RunContext[AgentDeps], commands: str) -> str:
    """Register slash commands for this code channel (like /run-tests or /create-pr), shown
    when someone types / here. When someone runs one, you get a message saying so, with
    its text. Each call replaces your whole set (max 10); send [] to clear them.

    Args:
        commands: JSON array string of {"name", "description", "argument_hint"?}. name has
            no leading slash: 1-31 lowercase letters, digits, - or _, not a built-in Slack
            command.
    """
    from agent.tools.code_channel_tools import set_commands

    return set_commands(ctx.deps.channel_id, commands)


def code_channel_rename_tool(ctx: RunContext[AgentDeps], title: str) -> str:
    """Rename this code channel (its name and its session title), e.g. once the task is clearer.

    Args:
        title: The new name, a readable title (max 200 characters).
    """
    from agent.tools.code_channel_tools import rename

    return rename(ctx.deps.channel_id, title)


def code_channel_archive_tool(ctx: RunContext[AgentDeps], summary: str) -> str:
    """Archive this code channel when the work is done: posts `summary` here, then archives
    the channel with it as the summary (also shared on the message the work started from).
    The history and tabs stay readable. ONLY when the person asks you to archive it or
    agrees to your suggestion; never on your own.

    Args:
        summary: A wrap-up of what was done, with links (Markdown).
    """
    from agent.tools.code_channel_tools import archive

    from agent.attribution import attribution_user_id
    return archive(ctx.deps.client, ctx.deps.channel_id, summary, attribution_user_id(ctx.deps))
