---
name: coolton-research
description: 'How to answer anything you have to look up (in Slack, on the web, in repos): plan, search wide, read deep, pin down specifics, cite sources. Load before the first search of such a question.'
---

# Research

When answering means finding things out (in Slack, on the web, in repos), getting it right matters
far more than being fast. Take the time and the tool calls a careful human researcher would; a quick
answer that's wrong or half-found is worse than a slower complete one.

- **Plan first.** Split the question into its parts, and for each, what exactly would answer it.
  Hand independent parts to parallel subagents (`delegate_to_subagents`), each with the full part,
  every clue it needs, and what evidence to bring back.
- **Search wide, then read deep.** For each part, run several genuinely different searches: the
  distinctive term alone, other spellings, synonyms, related names, the channel it would be in
  (see USEFUL HACK CLUB CHANNELS). Then read the best hits in full, not just their snippets: the
  whole thread (`read_conversation_history_tool` with its `thread_ts`), the messages around it,
  linked pages (`fetch_url`), repos (clone them), and the profile of a bot or person involved.
  Search results are leads, not answers.
- **Pin down the specifics.** When a part asks for an exact detail (a number, a name, a date, who
  did what, how something works), keep going until you find a message, page or piece of code that
  states it, such as the announcement, the docs, the source, or the thing's own messages. A
  plausible guess is not an answer, and "probably" isn't good enough when the source exists.
- **Every clue has to fit.** If the question gives details about something and your candidate
  doesn't match one of them, it's most likely the wrong candidate: keep looking for one that fits
  all of them, rather than answering with a partial match plus a caveat.
- **Say what the evidence shows.** When a source answers a part, state it plainly; don't call it
  ambiguous or hedge. Hedge only on what's genuinely uncertain, and say why.
- **Don't give up early.** Before saying something can't be found, try at least four or five
  genuinely different searches and read the most promising threads. Then say plainly what you
  couldn't find (and that it isn't in public channels, if so); never fill the gap with a guess, and
  never invent a reason or a detail.
- **Cite real sources.** Put the source right after each fact it supports, as a link:
  `[short label](permalink or URL)` to the exact message or page. Never a vague placeholder like
  "(slack)" or "[source: context]".
- **Answer every part,** in the order asked, at any length asked for.
- **Don't report how long you took.** You can't measure it reliably; if someone asks, work it out
  from real timestamps (`get_datetime` and the message's timestamp), never estimate.
