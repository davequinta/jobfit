# Eval results

## 2026-09-07 — the first labelled measurement

39 postings hand-labelled blind, then compared against scores already in the
database. No API calls: `jobfit eval` reads stored scores, so this costs nothing
to re-run.

**The set.** 22 `apply`, 17 `skip`, **0 `borderline`**. Every one has a score —
nothing labelled went unscored. All 39 come from the 2026-08-23 run, rubric
`f227c97dba81`, `claude-sonnet-5`.

### The headline

At the threshold the tool actually shipped with, it was surfacing almost nothing.

| | threshold 70 (as shipped) | threshold 25 |
|---|---|---|
| Precision | 1 of 1 = 100% | 13 of 15 = 87% ±17% |
| Recall | 1 of 22 = 5% | 13 of 22 = 59% ±21% |
| False positives | 0 | 2 |
| False negatives | 21 | 9 |

**The ranking is sound; the scale was not.** The model separates the two classes
well — `apply` scores run 15–78 with a median of 34, `skip` scores run 8–26 with a
median of 18. What was wrong is where the line sat. The rubric is written as 100
points, but the model never used the top half of it: the highest score in 135
postings was 78 and the mass sits between 15 and 40. A threshold of 70 on a
scale that behaves like 0–50 rejects nearly everything.

### Threshold sweep

Same 39 labels, same stored scores, no re-scoring.

| threshold | TP | FP | FN | TN | precision | recall |
|---|---|---|---|---|---|---|
| 70 | 1 | 0 | 21 | 17 | 1/1 = 100% | 1/22 = 5% |
| 60 | 2 | 0 | 20 | 17 | 2/2 = 100% | 2/22 = 9% |
| 50 | 3 | 0 | 19 | 17 | 3/3 = 100% | 3/22 = 14% |
| 40 | 8 | 0 | 14 | 17 | 8/8 = 100% | 8/22 = 36% |
| 35 | 10 | 0 | 12 | 17 | 10/10 = 100% | 10/22 = 45% |
| 30 | 12 | 0 | 10 | 17 | 12/12 = 100% | 12/22 = 55% |
| 25 | 13 | 2 | 9 | 15 | 13/15 = 87% | 13/22 = 59% |
| 20 | 17 | 4 | 5 | 13 | 17/21 = 81% | 17/22 = 77% |
| 15 | 22 | 11 | 0 | 6 | 22/33 = 67% | 22/22 = 100% |

The SPEC targets — precision > 0.8, recall > 0.6 — are met between 20 and 25 and
nowhere near 70. 25 is the shipped value: it holds precision at 87% with two
false positives, and the sample flatters precision (see below), so the more
conservative of the two passing cuts is the honest choice.

### Where the model and I disagreed

**At threshold 25 — false positives.** Surfaced, but I would not apply:

- **26** — Revenuecat, Senior DevOps / DevEx Engineer
- **26** — Edfinity, Senior Software Engineer, remote

**At threshold 25 — false negatives.** I would apply; the model buried them:

- **22** — Tiugo Technologies, Principal Product Engineer
- **22** — Samsara, Staff Software Engineer
- **22** — Mitre Media, Tech Lead Full-Stack Rails Engineer
- **20** — Collaboration.Ai, Senior AI Engineer - Agentic Systems & Data Pipelines
- **18** — Intellectsoft, Senior Shopify Full-stack Developer (IR-471)
- **18** — Ci&t, [Job -26953] Senior Full Stack Developer (React/.Net)
- **18** — Aker Systems, Principal Software Engineer - Product team
- **18** — Collaboration.Ai, Senior Software Engineer
- **15** — Base.com, Full-stack Developer (BL paczka)

The false negatives are the more interesting half. Several are exactly the
target profile — a Staff Software Engineer at Samsara, a Tech Lead Full-Stack
role — scoring 22 while the rubric's own weights say seniority (25) and location
eligibility (20) alone should carry them past 40. That gap is the rubric defect
recorded below, not a threshold problem.

### What this measurement does not support

Read these before quoting the numbers anywhere.

- **n = 39.** Precision at threshold 25 is 87% ±17%. The interval is wider than
  most of the differences anyone would want to draw from it.
- **No borderline labels.** The set was meant to hold ~10 postings I could not
  call, precisely to study disagreement. It holds none, so this measures easy
  separation and says nothing about the hard middle.
- **The base rate is inflated.** 22 of 39 are `apply` — 56%. In the real corpus
  I reject far more than half. Precision is easier at a high base rate, so 87%
  is an upper bound on what to expect in production, not an estimate of it.
- **One posting is counted twice.** Huzzle "Full-Stack Developer (Python,
  React, AI)" appears under two URLs. It is labelled `apply` in both, so it
  contributes twice to recall. 38 distinct postings, not 39.

### The rubric defect this exposed

Not fixed here — a rubric change and a threshold change must not land together,
or the next set of numbers cannot be attributed to either.

Two of the three worked examples in `score_system.md` contradict their own
arithmetic. The ML Engineer example lists components 8 + 15 + 4 + 2 + 5 = 34 and
declares a score of 24. The Full Stack Developer example lists 18 + 10 + 8 + 0 +
0 = 36 and declares 31. Only the high-scoring example adds up. Combined with the
rule *"If you cannot find a genuine concern, the score is too high; lower it"*,
the rubric teaches the model to sum at the top and shade down everywhere else —
which is the compression the scores show. Fixing the examples is the next
change, and it needs a re-score ($1.31) to measure.

---

# Eval results

Every rubric change gets a row. Never change the rubric and the threshold in
the same run — if the numbers move, you need to know which one did it.

Counts, not ratios: with a forty-posting set the third decimal is noise.

| date | rubric | threshold | precision | recall | FP | FN | what changed |
|---|---|---|---|---|---|---|---|
| 2026-09-07 | f227c97dba81 | 70 | 1 of 1 | 1 of 22 | 0 | 21 | baseline: the threshold the tool shipped with |
| 2026-09-07 | f227c97dba81 | 25 | 13 of 15 | 13 of 22 | 2 | 9 | swept on the same stored scores; no re-score |
