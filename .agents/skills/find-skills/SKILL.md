---
name: find-skills
description: Helps users discover and install agent skills when they ask questions like "how do I do X", "find a skill for X", "is there a skill that can...", or express interest in extending capabilities. This skill should be used when the user is looking for functionality that might exist as an installable skill.
---

# Find Skills

This skill helps you discover and install skills from the open agent skills ecosystem.

## When to Use This Skill

Use this skill when the user:

- Asks "how do I do X" where X might be a common task with an existing skill
- Says "find a skill for X" or "is there a skill for X"
- Asks "can you do X" where X is a specialized capability
- Expresses interest in extending agent capabilities
- Wants to search for tools, templates, or workflows
- Mentions they wish they had help with a specific domain (design, testing, deployment, etc.)

## What is the Skills CLI?

The Skills CLI (`npx skills`) is the package manager for the open agent skills ecosystem (skills.sh). Skills are modular packages that extend agent capabilities with specialized knowledge, workflows, and tools. Browse the ecosystem at https://skills.sh/.

## CRITICAL — How Installation Actually Works Here

**This agent's own sandbox is isolated.** Any `npx skills ...` command (or any shell/file command) you run inside your own sandbox has **NO effect** on the real agent — it runs in a throwaway environment and is discarded. You CANNOT install, create, or modify skills by running CLI commands yourself.

To actually install a skill, you MUST call the **`install_skill`** tool. That is the only path that touches the real skill files. Do not instruct the user to run `npx skills add` either — just call the tool for them.

```
install_skill(package="vercel-labs/agent-skills", skill="deploy-to-vercel")
```

- `package`: an `owner/repo` (e.g. `vercel-labs/agent-skills`) or a full GitHub URL.
- `skill`: optional specific skill name inside a multi-skill repo. Omit to install all.

After installing, confirm availability with `list_skills` and pull it in with `load_skill` before using it.

## How to Help Users Find Skills

### Step 1: Understand What They Need

Identify the domain (e.g. React, testing, design, deployment), the specific task, and whether it's common enough that a skill likely exists.

### Step 2: Check the Leaderboard First

Before anything else, check the [skills.sh leaderboard](https://skills.sh/) for well-known skills. Top skills for web development include:
- `vercel-labs/agent-skills` — React, Next.js, web design
- `anthropics/skills` — Frontend design, document processing

### Step 3: Search for Skills

If the leaderboard doesn't cover the need, search skills.sh (web) for the domain. You may also call `install_skill` with a guessed `owner/repo` to see if it resolves, but prefer confirming via skills.sh first.

Do NOT run `npx skills find` in your sandbox — it won't help and has no effect.

### Step 4: Verify Quality Before Recommending

1. **Install count** — prefer skills with 1K+ installs; be cautious under 100.
2. **Source reputation** — official sources (`vercel-labs`, `anthropics`, `microsoft`) are more trustworthy.
3. **GitHub stars** — a source repo with <100 stars deserves skepticism.

### Step 5: Present Options, Then Install

When you find a relevant skill, present its name, what it does, install count, and source. Then, if the user wants it, call `install_skill(...)` for them. Example response:

```
I found the "deploy-to-vercel" skill from vercel-labs/agent-skills
(used to deploy apps to Vercel). Want me to install it?
```

If they say yes: `install_skill(package="vercel-labs/agent-skills", skill="deploy-to-vercel")`.

### Step 6: If No Skill Exists

Acknowledge nothing was found, offer to help directly, and offer to **create** a custom skill via the `create_skill(name, description, body)` tool so the workflow is reusable next time.

## Common Skill Categories

| Category        | Example Queries                          |
| --------------- | ---------------------------------------- |
| Web Development | react, nextjs, typescript, css, tailwind |
| Testing         | testing, jest, playwright, e2e           |
| DevOps          | deploy, docker, kubernetes, ci-cd        |
| Documentation   | docs, readme, changelog, api-docs        |
| Code Quality    | review, lint, refactor, best-practices   |
| Design          | ui, ux, design-system, accessibility     |
| Productivity    | workflow, automation, git                 |
