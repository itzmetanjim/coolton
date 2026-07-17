---
name: summarize-channel
description: Summarize what happened in a Slack channel or thread into a tight recap with decisions, action items, and open questions. Use when the user asks for a recap, summary, or "what happened in #channel".
---

# Summarize a channel

Produce a scannable recap of a Slack channel or thread.

## When to use
- "recap #general", "what happened in this thread", "summary of #design"
- Any request to compress a conversation into key points

## Steps
1. Use `list_channel_threads_tool` to see recent threads if the user names a channel.
2. Use `summarize_thread_tool` with the `channel_id` and `thread_ts` for the specific thread (or omit both to summarize the current thread).
3. Format the output as:

```
## Recap of <channel/thread>
**Decisions:** <bullet list>
**Action items:** <bullet list with owners if known>
**Open questions:** <bullet list>
```

## Notes
- Keep it under 15 lines. No fluff.
- If the conversation is short, say so plainly instead of padding.
