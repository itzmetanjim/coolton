---
name: compare-ai-models
description: Compare two (or more) AI models — benchmark them, pull intelligence-index/pricing/tokens-per-sec numbers, and give a verdict. Triggers on "is X or Y better", "compare model A vs B", "which model should I use", or any request to benchmark/source current AI model capabilities. Uses web search + benchmark aggregators (artificialanalysis.ai, benchlm.ai, codingfleet, swebench).
---

# Compare Ai Models

# Compare AI Models

## When to use
- "is kimi k2.6 or minimax m2.7 better?"
- "compare model A vs B", "which model should I use for X"
- requests to benchmark or source current AI model capabilities/scores

## Steps
1. Confirm both models are real with parallel web searches:
   - `"<Model A> AI model release"`
   - `"<Model B> AI model release"`
   (some "new" models are rumors — verify before comparing)
2. Search for head-to-head + benchmark data in parallel:
   - `"<A> vs <B> benchmark comparison"`
   - `"<A> benchmark <specific metric e.g. SWE-bench>"`
   - `"<A> intelligence index score artificialanalysis"`
   - `"<B> intelligence index score artificialanalysis"`
3. Best sources for numbers:
   - artificialanalysis.ai (intelligence index, agentic index, $/M tokens, tokens/sec) — has direct model-comparison URLs like `/models/comparisons/<a>-vs-<b>`
   - benchlm.ai/compare, codingfleet.com/blog, swebench.com
4. Structure the reply:
   - Per-model bullet: who/date, open-weights?, key strengths, standout benchmark
   - Bottom line: which wins on raw capability vs price-to-performance; note if neither beats top closed US frontier models
5. Offer to pull exact head-to-head numbers from a comparison page if user wants more depth.

## Notes
- These are fast-moving; always cite that data is from the search date's aggregators.
- Open-weights models (kimi, minimax, qwen, glm, deepseek) can be self-hosted — call that out as an advantage.
- Don't invent scores. If a source doesn't have a number, say so.
