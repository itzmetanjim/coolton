# Skill catalog

Curated, version-controlled skills live in `skills/`. CLI-installed skills live in
`.agents/skills/` (gitignored, but still scanned by the agent). Both directories are loaded
automatically via `SkillsCapability(directories=["skills", ".agents/skills"], auto_reload=True)`.

kevinton writes new skills into `skills/` after real conversation turns.

## Curated skills (`skills/`)

| Skill | Purpose |
| --- | --- |
| `manage-skills` | The single skill for the full skill lifecycle: **find** skills in the ecosystem, **install** them, and **create/edit/rename/delete** coolton's own catalog. **Merged** from the old `find-skills` (discovery) and `fusion-skill-authoring` (authoring craft) skills. |
| `summarize-channel` | Summarize a Slack channel or thread. |
| `compare-ai-models` | Compare two+ AI models with a use-case verdict (auto-captured by kevinton). |

## Merged overlaps (history)

To keep the catalog coherent, overlapping skills were consolidated:

- `no-ai-slop` (CLI) → folded into `talk-like-a-human-not-a-bot` (curated), then folded again
  into the `## WRITING STYLE (anti-slop)` section of `agent/platforms/system_prompt.md` itself.
  The skill's description ("use before composing any reply") meant the model loaded it on
  essentially every turn anyway, so making it part of the always-present system prompt drops the
  `list_skills` + `load_skill` round trip with no loss of coverage. `talk-like-a-human-not-a-bot`
  no longer exists as a skill; if it gets reinstalled via the skills CLI, delete it again.
- `find-skills` (CLI, discovery) + `fusion-skill-authoring` (CLI, authoring craft) → folded into
  `manage-skills` (curated), which now owns discovery (Part 1), action (Part 2), and authoring
  craft (Part 3).

If any of the CLI originals above are reinstalled via the skills CLI, delete them again to avoid
competing skills with the curated versions or the system prompt.
