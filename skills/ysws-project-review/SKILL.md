---
name: ysws-project-review
description: 'Reviews YSWS project submissions against published guidelines using verifiable repository, runtime, security, licensing, and hours-evidence checks. USE FOR: auditing a GitHub project for YSWS approval or override-hours justification. DO NOT USE FOR: generic code review without YSWS evidence requirements.'
---

# Ysws Project Review

## When to use
Use this for a YSWS project review when the reviewer needs an evidence-backed recommendation and an internal override-hours justification. Treat the result as reviewer assistance, not an official eligibility decision.

## Required inputs
- Repository URL and the exact revision/branch to review
- YSWS guidelines URL, especially the override-hours justification page
- Claimed hours, Hackatime/Lapse links, AI-use disclosure, and submission metadata when available

## Workflow
1. Read the guidelines first. Extract required fields and the standard that an uninvolved reviewer must be able to follow links and reach the same conclusion.
2. Inspect the repository at the submitted revision. Record commit SHA/date, tree size, major subsystems, README run instructions, license, CI, tests, and explicit TODO or unimplemented features. Cite stable GitHub file/tree links and line ranges where possible.
3. Reproduce the documented setup when feasible. Prefer a clean checkout; run the smallest relevant build/test/lint or Docker validation. Clearly label observed results versus claims in the README. Do not imply a hosted demo or local run was verified if it was not.
4. Audit implementation depth: identify user-facing features and trace important ones through frontend, API/controller, service, persistence/migrations, and deployment layers. Distinguish implemented behavior from UI-only placeholders and backend stubs such as `NotImplementedException`.
5. Perform a focused security/operations pass: authentication and authorization boundaries, secret/password logging, cookie and HTTPS behavior, development credentials, CORS/proxy headers, dependency lockfiles, and production configuration. Report concrete findings with severity and evidence; do not turn every concern into an automatic rejection.
6. Check license compatibility with the specific YSWS rules. Flag nonstandard or noncommercial licenses for human confirmation rather than assuming they are acceptable.
7. Validate hours separately from technical merit. GitHub commits, LOC, infrastructure files, and project complexity show scope, not active hours. Require Hackatime project ID/name, exact date range, Lapse links where applicable, claimed/approved totals, and a feature-to-hours explanation. Request an AI-use statement and confirmation of personally authored infrastructure.
8. Produce: (a) recommendation, (b) implemented scope, (c) reproducibility result, (d) incomplete features, (e) security/license findings, (f) missing evidence, (g) a concise internal justification containing links, numbers, and concrete observations, and (h) a manual verification checklist.

## Decision rule
Use `substantial; needs submitter evidence` when the implementation is credible but hours or eligibility cannot be independently validated. Approve hours only when the supplied time evidence and required disclosures support the number. Reject only for a guideline-level disqualifier or failure to demonstrate a qualifying project, not merely for missing polish, absent tests, or unfinished non-core features.

## Safety and accuracy
- Never invent hours, Hackatime/Lapse records, runtime success, authorship, or AI percentage.
- Separate repository facts, reproducibility observations, reviewer inferences, and submitter claims.
- Treat passwords or tokens found in source/logging as sensitive; do not reproduce secrets in the report.
- A code review is not an official YSWS decision; state what remains for a human reviewer.

## Acceptance examples
- “Review this GitHub repo for YSWS and write the override justification.”
- “Audit this submission and tell me whether the claimed 80 hours are supportable.”
- “Do a deeper pass than the previous reviewer, including reproducibility and security.”
