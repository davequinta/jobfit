# CLAUDE.md

Read `SPEC.md` first. It is the source of truth for scope. This file is about how
to work in this repo.

## Context

This is a personal tool built under a hard deadline (2026-08-30) that doubles as
a public portfolio piece. Both facts matter: the code will be read by engineers
who might hire me, and it will not be maintained after October.

Optimize for **legible over clever**. Someone should be able to read
`src/score.py` in three minutes and understand the whole scoring approach.

## Working agreements

- **Push back on scope.** If I ask for something in the "Explicitly out of scope"
  list in SPEC.md, say so before building it. If I insist, build it — but say it
  first.
- **Stages stay independent.** Each stage reads from and writes to SQLite. Never
  chain stages in memory; I need to re-run scoring without re-ingesting.
- **No silent fallbacks.** If a feed is down or a parse fails, log it loudly and
  keep going with the other sources. Do not swallow exceptions.
- **Every LLM call is testable offline.** Fixture-based tests with recorded
  responses. I should be able to run the eval suite without spending tokens.
- **One file per stage.** `ingest.py`, `prefilter.py`, `score.py`, `draft.py`,
  all under `src/jobfit/` so the tool is installable (`jobfit init`, `jobfit
  ingest`). `cli.py` is argument plumbing, not a stage, and stays that way.
  Resist splitting a stage across files until it passes 300 lines.

## Prompting rules

The scoring prompt lives in `src/jobfit/templates/score_system.md` as a plain
markdown file, not embedded in Python. `jobfit init` copies it to
`prompts/score_system.md` in the working directory, which is what gets tuned. It gets versioned and diffed like code, because prompt
changes are the changes most likely to break the evals.

Structure is fixed and required for prompt caching:

```
system: [rubric + CV + stack profile]   ← cache_control breakpoint
user:   [posting]
```

Do not move CV content into the user message. Do not add per-posting content
before the breakpoint. Both silently break caching and the cost goes up 10x
without any error.

## When changing the rubric

1. Run `pytest evals/` and record the current precision/recall
2. Make the change
3. Re-run and record
4. Append both numbers plus a one-line rationale to `evals/results.md`

Never change the rubric and the threshold in the same commit. If the numbers
move, I need to know which one caused it.

## Definition of done for a stage

- Runs end to end against real data
- Failure mode is logged, not silent
- Has at least one test that doesn't hit the network
- The README section describing it is written

## Things that are not done

- "It works but I skipped the tests"
- "The evals are stubbed out for now"
- Committed secrets, CV content, or queue files — check `.gitignore` before
  every commit
