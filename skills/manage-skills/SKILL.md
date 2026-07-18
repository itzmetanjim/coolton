---
name: manage-skills
description: The single skill for everything about coolton's agent skills — discover skills in the ecosystem, install them, create/edit/rename/delete them, and write a good SKILL.md. Use when the user says "make a skill", "create a skill", "install a skill", "add a skill", "find a skill", "is there a skill for X", "rename a skill", "delete a skill", or wants to organize/clean up the skill catalog or improve a skill's wording.
---

# Manage Skills

Operate coolton's on-demand **skills** (reusable playbooks with a `SKILL.md`). This one skill
covers the full lifecycle: finding skills in the open ecosystem, installing them, and
creating/editing/deleting coolton's own catalog. Use the dedicated tools — they are the only
things that touch the real skill files.

## CRITICAL — Sandbox Is Isolated

Your own shell/CLI sandbox is throwaway. Any `npx skills ...`, `mkdir`, `rm`, or file write you
run there has **NO effect** on this agent and is discarded. Never claim you installed/created/edited
a skill via sandbox commands, and never ask the user to run such commands. Always use the tools below.

## Tools (use these, nothing else)

- `install_skill(package, skill?)` — install from the skills.sh marketplace. `package` is
  `owner/repo` or a GitHub URL; `skill` is an optional specific skill name. Installed skills land
  in `.agents/skills/` (gitignored, but fully functional). After install, confirm with `list_skills`
  and `load_skill` to use it.
- `create_skill(name, description, body?)` — create a new custom skill in `skills/`. The name is
  slugified automatically; pass a clear `description` (triggers when to use it) and `body` with the
  workflow. Empty body gets a starter template.
- `rename_skill(old_name, new_name)` — rename a skill (moves its folder + updates frontmatter).
  Names are slugified.
- `delete_skill(name)` — permanently remove a skill. This is destructive — confirm with the user
  first unless they clearly asked to delete.
- `list_skills` / `load_skill` (from the skills capability) — discover and load existing skills.

## Safety Rules

- Pass only skill **names**, never absolute paths or `..`. The tools reject anything that tries to
  escape the skill directories.
- `delete_skill` is permanent — double-check the name and get explicit confirmation.
- Curated skills live in `skills/` (committed to git); CLI-installed skills live in `.agents/skills/`
  (gitignored but work fine). Both are picked up automatically via `auto_reload`.

---

## Part 1 — Finding skills (discovery)

Use this when the user asks "how do I do X", "find a skill for X", "is there a skill that can...",
or expresses interest in extending capabilities.

1. **Identify the domain** (web dev, testing, devops, docs, design, productivity…) and whether it's
   common enough that a skill likely exists.
2. **Check skills.sh first.** Browse the [skills.sh leaderboard](https://skills.sh/) for well-known
   skills. Top sources: `vercel-labs/agent-skills` (React, Next.js, web design),
   `anthropics/skills` (frontend design, document processing), `equinor/fusion-skills`,
   `realrossmanngroup/no_ai_slop_writing_rules`.
3. **Search skills.sh (web)** for the domain if the leaderboard doesn't cover it. Do NOT run
   `npx skills find` in your sandbox — it has no effect.
4. **Verify quality before recommending:**
   - Install count — prefer 1K+; be cautious under 100.
   - Source reputation — official orgs (`vercel-labs`, `anthropics`, `microsoft`) are more trustworthy.
   - GitHub stars — <100 stars deserves skepticism.
5. **Present options, then install.** Show name, what it does, install count, source. If the user
   wants it, call `install_skill(package=..., skill=...)` — never ask them to run the CLI.
6. **If nothing exists**, offer to help directly and offer to **create** a custom skill (Part 3) so
   the workflow is reusable next time.

## Part 2 — Action workflows

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

## Part 3 — Writing a good SKILL.md (craft)

Use this when creating or improving a skill's content, not just running the tools.

### Decide whether this should be a skill at all
Reuse or update an existing skill before creating a duplicate. Don't scaffold if the request is
better as plain docs, a template, or a one-off script.

### Minimum viable structure
- `SKILL.md` always.
- `references/` for long guidance, examples, tables.
- `assets/` for templates/checklists.
- `agents/` for helper roles (only if the runtime supports skill-local agents).
- Keep `SKILL.md` under ~300 lines; move overflow to `references/`.

### Frontmatter contract
- `name`: 1–64 chars, lowercase kebab-case, must match the folder name.
- `description`: third-person, <=1024 chars, states both what it does and when to use it. Prefer
  single-quoted YAML with `USE FOR:` and `DO NOT USE FOR:` cues, e.g.
  `description: 'Drafts release notes from validated repo context. USE FOR: release summaries. DO NOT USE FOR: publishing releases.'`

### Body shape
Concise frontmatter → `When to use` / `When not to use` → `Required inputs` → `Instructions` →
`Expected output` → `Safety & constraints`. Prefer specific instructions over background. Include at
least one concrete example. Define 3+ representative requests as acceptance criteria before writing.

### Safety & constraints
- Never expose secrets, run destructive commands without confirmation, or invent validation results.
- Keep helper agents tightly scoped; core workflow must work without them.

---

## Note on merged skills

This skill previously existed as three overlapping ones — `find-skills` (discovery), `manage-skills`
(action), and `fusion-skill-authoring` (craft). They were merged here to remove duplication:
discovery in Part 1, action in Part 2, craft in Part 3. If you reinstall any of those from the
CLI, delete it to avoid two competing skills.
