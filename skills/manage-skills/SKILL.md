---
name: manage-skills
description: Manage coolton's agent skills — create, install, rename, and delete skills, and inspect what's available. Use when the user says "make a skill", "create a skill", "install a skill", "add a skill", "rename a skill", "delete a skill", "remove a skill", "list skills", or wants to organize/clean up the skill catalog.
---

# Manage Skills

Operate coolton's on-demand **skills** (reusable playbooks with a `SKILL.md`). Use the dedicated tools — they are the only things that touch the real skill files.

## CRITICAL — Sandbox Is Isolated

Your own shell/CLI sandbox is throwaway. Any `npx skills ...`, `mkdir`, `rm`, or file write you run there has **NO effect** on this agent and is discarded. Never claim you installed/created/edited a skill via sandbox commands, and never ask the user to run such commands. Always use the tools below.

## Tools (use these, nothing else)

- `install_skill(package, skill?)` — install from the skills.sh marketplace. `package` is `owner/repo` or a GitHub URL; `skill` is an optional specific skill name. After install, confirm with `list_skills` and `load_skill` to use it. Installed skills land in `.agents/skills/` (gitignored, but fully functional).
- `create_skill(name, description, body?)` — create a new custom skill in `skills/`. The name is slugified automatically; pass a clear `description` (triggers when to use it) and `body` with the workflow. Empty body gets a starter template.
- `rename_skill(old_name, new_name)` — rename a skill (moves its folder + updates frontmatter). Names are slugified.
- `delete_skill(name)` — permanently remove a skill. This is destructive — confirm with the user first unless they clearly asked to delete.
- `list_skills` / `load_skill` (from the skills capability) — discover and load existing skills.

## Safety Rules

- Pass only skill **names**, never absolute paths or `..`. The tools reject anything that tries to escape the skill directories.
- `delete_skill` is permanent — double-check the name and get explicit confirmation.
- Curated skills live in `skills/` (committed to git); CLI-installed skills live in `.agents/skills/` (gitignored but work fine). Both are picked up automatically via `auto_reload`.

## Workflow Examples

**User: "make a skill that drafts release notes from git commits"**
1. `create_skill(name="release-notes", description="Draft release notes from recent git commits...", body="<steps>")`
2. Confirm with `list_skills`.

**User: "install the deploy-to-vercel skill"**
1. `install_skill(package="vercel-labs/agent-skills", skill="deploy-to-vercel")`
2. `list_skills` to confirm, then `load_skill` if about to use it.

**User: "rename my-cool-skill to cool-skill"**
1. `rename_skill(old_name="my-cool-skill", new_name="cool-skill")`

**User: "delete that test-skill"**
1. Confirm the exact name.
2. `delete_skill(name="test-skill")`
