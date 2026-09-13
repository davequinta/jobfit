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

> **Erratum, 2026-09-13.** The `apply` median is 33, not 34. With 22 scores the
> median is the mean of the 11th and 12th, 32 and 34; 34 is the upper median.
> The sentence above is left as it was written.

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

> **Erratum, 2026-09-13.** 25 does not meet both targets on these scores. Recall
> at 25 is 13 of 22, 59%, under 0.6. Checked at every integer threshold, only 19
> and 20 meet precision > 0.8 and recall > 0.6; at 21 and 22 precision is exactly
> 80%, which fails the strict target. So the threshold shipped on 2026-09-07 was
> already below the recall target on the scores it was chosen from. The
> paragraph above is left as it was written; the 2026-09-13 entry measures 25
> again under the fixed rubric, where it does meet both.

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

## 2026-09-07 — rubric `f227c97dba81` → `e39642e40c3c`, not yet measured

The arithmetic defect recorded above is fixed. Three changes, all to
`score_system.md`, none to the threshold:

1. The rubric now says outright that the score is the sum of the five
   components and that no further adjustment follows, because it never said so
   and the model was not doing it.
2. The two worked examples that contradicted their own components were
   corrected — the ML Engineer example listed 8 + 15 + 4 + 2 + 5 and declared
   24, the Full Stack Developer example listed 18 + 10 + 8 + 0 + 0 and declared
   31. They now declare 34 and 36, and all three examples show their sum.
3. The `why_not` rule said "if you cannot find a genuine concern, the score is
   too high; lower it", which turned a completeness obligation into downward
   pressure on the number. It now says to look again, and not to move the score
   to compensate for a thin `why_not`.

**No numbers for this yet, and the ones above do not transfer.** Measuring it
costs a re-score of the labelled set — the scores in the database were produced
by `f227c97dba81` and comparing them to a rubric they were not scored under
would be meaningless. `jobfit eval` warns when stored scores come from more than
one rubric version, so this cannot be mixed up silently.

To measure:

```bash
jobfit score      # ~$1.31; the rubric changed, so all 135 come back by themselves
jobfit eval --note "rubric: examples now sum; explicit no-adjustment rule"
```

No `--rescore` needed: stage 3 skips postings already judged by the current
rubric and this rubric is a new one, so every posting is pending again.

The prediction, recorded before the fact so it can be wrong: scores rise
substantially and the threshold that was right for the old scale is no longer
right for the new one. Do not change the threshold in the same step — measure
the rubric change first against threshold 25, then sweep.

## 2026-09-07 — two things that change what the next measurement means

**The set skews DevOps, and stage 2 now removes that category.** Thirteen of the
39 labelled postings are DevOps, SRE or infrastructure roles, and all thirteen
are labelled `skip`. They were surviving stage 2 because the stack list carries
Docker, CI/CD and Kubernetes — true of a full stack engineer, and enough to clear
the zero-overlap rule. `title_exclusions` now rejects them, so in production the
model never sees that category at all. In the eval set they remain, as thirteen
easy true negatives out of seventeen skips. Precision measured on this set is
therefore flattered twice over: by the 56% base rate already recorded, and by a
third of it being a category the funnel no longer admits. The set is worth
rebuilding on a fresh corpus before anyone quotes a number from it again.

**The set had also stopped being scoreable.** All 39 postings are older than
`max_age_days`, so stage 2 marks them stale and stage 3 skipped them — meaning
the rubric change above could not have been measured against them at all. Stage
3 now scores everything in the label file whatever its age, because a posting's
age says nothing about whether the scorer judges it well. Without that, every
eval set silently expires a fortnight after it is built.

## 2026-09-13 — rubric e39642e40c3c, measured

The same 39 labels as the 2026-09-07 entries, scored again under the fixed
rubric. The labels are taken as they were in commit c1349ac, so the rubric is
the only thing that changed; the threshold stays at 25. The row at the bottom is
dated 2026-09-13 because `jobfit eval` stamps UTC — the run was 2026-09-12
23:41–23:53 -0600.

```bash
jobfit score --labels-only        # the 79 postings in evals/labeled.jsonl; c1349ac's 39 are among them
git show c1349ac:evals/labeled.jsonl > "$TMPDIR/labels-c1349ac.jsonl"
jobfit eval --labels "$TMPDIR/labels-c1349ac.jsonl" --note "rubric e39642e40c3c: examples now sum; explicit no-adjustment rule"
jobfit eval --labels "$TMPDIR/labels-c1349ac.jsonl" --threshold N    # each sweep row
```

### The prediction

Written on 2026-09-07, before the run: *scores rise substantially and the
threshold that was right for the old scale is no longer right for the new one.*

**Scores rose — for `apply` far more than for `skip` — and the top of the scale
did not move.** Of the 39 postings, 35 rose, 2 were unchanged and 2 fell; mean
change +9.1, median +6, range −5 to +45.

| | f227c97dba81 | e39642e40c3c | mean change |
|---|---|---|---|
| `apply` (22) | 15–78, median 33 | 22–78, median 47.5 | +12.4 (19 rose, 1 fell) |
| `skip` (17) | 8–26, median 18 | 10–34, median 22 | +4.8 (16 rose, 1 fell) |

Two of the 39 now score 70 or more; the highest is still 78.

**The second half did not hold.** At 25 the new scores meet both SPEC targets —
precision 18 of 21, recall 18 of 22 — and every threshold from 23 to 38 meets
them. The premise was also wrong: on the old scores 25 never met them. Recall
there was 13 of 22, 59%, and only 19 and 20 cleared both targets, not "between 20
and 25" as the first entry says. The old `apply` median was 33, not 34.

### Threshold sweep

Same 39 labels, scores under e39642e40c3c.

| threshold | TP | FP | FN | TN | precision | recall |
|---|---|---|---|---|---|---|
| 70 | 2 | 0 | 20 | 17 | 2/2 = 100% | 2/22 = 9% |
| 60 | 5 | 0 | 17 | 17 | 5/5 = 100% | 5/22 = 23% |
| 50 | 10 | 0 | 12 | 17 | 10/10 = 100% | 10/22 = 45% |
| 40 | 13 | 0 | 9 | 17 | 13/13 = 100% | 13/22 = 59% |
| 35 | 16 | 0 | 6 | 17 | 16/16 = 100% | 16/22 = 73% |
| 30 | 18 | 3 | 4 | 14 | 18/21 = 86% | 18/22 = 82% |
| 25 | 18 | 3 | 4 | 14 | 18/21 = 86% | 18/22 = 82% |
| 20 | 22 | 11 | 0 | 6 | 22/33 = 67% | 22/22 = 100% |
| 15 | 22 | 14 | 0 | 3 | 22/36 = 61% | 22/22 = 100% |

### Where the model and I disagreed, at threshold 25

**False positives** — surfaced, but I would not apply:

- **34** — Edfinity, Senior Software Engineer, remote (was 26)
- **32** — Revenuecat, Senior DevOps / DevEx Engineer (was 26)
- **31** — Tenchi Security, DevOps Engineer (was 22)

**False negatives** — I would apply; the model scored them under 25:

- **24** — Intellectsoft, Senior Shopify Full-stack Developer (IR-471) (was 18)
- **22** — Aker Systems, Principal Software Engineer - Product team (was 18)
- **22** — Base.com, Full-stack Developer (BL paczka) (was 15)
- **22** — Ci&t, [Job -26953] Senior Full Stack Developer (React/.Net) (was 18)

Five of the nine false negatives from 2026-09-07 now surface: Samsara 22 → 67,
Tiugo Technologies 22 → 63, Collaboration.Ai (Senior AI Engineer) 20 → 48,
Mitre Media 22 → 38, Collaboration.Ai (Senior Software Engineer) 18 → 31.

### Recommendation, not applied

> **Applied 2026-09-13 in 3941a1c.** The threshold is now 35; see the entry
> "threshold 25 → 35" below. The paragraph that follows is left as it was written.

The threshold is unchanged in this commit. SPEC.md weights precision over
recall; on this set the lowest cut with no false positives is **35** —
precision 16 of 16, recall 16 of 22 — which gives up two true positives to drop
three false positives. Two of those three are DevOps roles, a category stage 2
now rejects in production before scoring, so on live postings the gain from 35
is smaller than this table shows. A change to 35 belongs in its own commit.

### What this measurement does not support

- **n = 39.** At 25, precision is 86% ±15% and recall 82% ±16%.
- **Still no borderline labels**, and still a 56% `apply` base rate that
  flatters precision.
- **Thirteen of the seventeen skips are DevOps or infrastructure roles** that
  stage 2 now excludes. Two of today's three false positives are among them.
- **Huzzle is still counted twice** — one job under two URLs, both `apply`.
- **One scoring pass per posting.** No posting was scored twice under the same
  rubric, so how much a score moves between identical runs is unknown, and the
  +6 median shift is not separated from that noise.
- **The label file has moved on.** It now holds 53 labels: one of the 39 was
  relabelled (Newrich Network, `apply` → `skip`) and 14 were added. On those 53
  at threshold 25: precision 23 of 28, recall 23 of 30, 5 false positives, 7
  false negatives.

### Cost

79 postings — everything in the label file — scored 2026-09-13T05:41:42Z to
05:53:26Z on `claude-sonnet-5`: 109,788 uncached input, 45,055 output, 415,194
cache-read and 5,323 cache-write tokens. **$0.7665**, against $1.5112 for the same
tokens without caching.

## 2026-09-13 — threshold 25 → 35

Commit 3941a1c moves the threshold to 35, applying the recommendation above in a
commit of its own. SPEC.md's rule for the eval set decides it: optimise for
precision over recall, because a false positive costs twenty minutes and a
wasted application and a false negative one posting out of hundreds. The rubric
is unchanged — still e39642e40c3c — so no posting needed re-scoring and every
number below comes from the scores already stored.

```bash
jobfit eval --threshold 35                 # the 53 labels in evals/labeled.jsonl today
jobfit eval --threshold 25
git show c1349ac:evals/labeled.jsonl > "$TMPDIR/labels-c1349ac.jsonl"
jobfit eval --labels "$TMPDIR/labels-c1349ac.jsonl" --threshold 35
jobfit eval --labels "$TMPDIR/labels-c1349ac.jsonl" --threshold 25
```

| labels | threshold | precision | recall | FP | FN |
|---|---|---|---|---|---|
| 39, c1349ac | 25 | 18 of 21 | 18 of 22 | 3 | 4 |
| 39, c1349ac | 35 | 16 of 16 | 16 of 22 | 0 | 6 |
| 53, the file on 2026-09-13 | 25 | 23 of 28 | 23 of 30 | 5 | 7 |
| 53, the file on 2026-09-13 | 35 | 19 of 20 | 19 of 30 | 1 | 11 |

The 39-label row is the one logged in the table below, so it stays comparable
with every earlier row. The 53-label figures are not comparable with those
rows: they include the 14 labels added after c1349ac (in the file since
2026-09-07) and one of the 39 relabelled since.

**What 35 gives up, on the 39.** The three false positives at 25 go —
Edfinity (34), Revenuecat (32), Tenchi Security (31) — and two true positives go
with them:

- **31** — Collaboration.Ai, Senior Software Engineer
- **30** — Valsoft Corporation, AI-Native Full Stack Product Engineer

The four false negatives that were already below 25 stay: Intellectsoft (24),
Aker Systems (22), Base.com (22), Ci&t (22).

**On the 53, at 35.** One false positive remains — Newrich Network, Senior Full
Stack Developer - PHP Laravel (47) — and eleven false negatives: the six above,
plus Checkr (30), 42Labs (25), Discord (18), Agilistik (15) and Valkyrie Aero (8).

**What the queue carries.** Of the 163 postings stage 2 passes today, 82 score
25 or more and 59 score 35 or more.

```sql
SELECT count(*), sum(s.fit_score >= 25), sum(s.fit_score >= 35)
  FROM scores s JOIN prefilter_verdicts v ON v.posting_id = s.posting_id
 WHERE v.rejected_reason IS NULL AND s.prompt_version = 'e39642e40c3c';
-- 163|82|59
```

The caveats of the 2026-09-13 measurement all still apply: n is 39 (or 53), no
label is borderline, the base rate flatters precision, two of the three false
positives removed are DevOps roles stage 2 already rejects in production, and no
posting has been scored twice, so a move of a few points near the cut is not
separated from noise. The queue written earlier on 2026-09-13 at 25 appended 82
rows to `out/applications.csv`; it is append-only, so the 23 of them between 25
and 35 stay there.

---

# Eval results

Every rubric change gets a row. Never change the rubric and the threshold in
the same run — if the numbers move, you need to know which one did it.

Counts, not ratios: with a forty-posting set the third decimal is noise.

| date | rubric | threshold | precision | recall | FP | FN | what changed |
|---|---|---|---|---|---|---|---|
| 2026-09-07 | f227c97dba81 | 70 | 1 of 1 | 1 of 22 | 0 | 21 | baseline: the threshold the tool shipped with |
| 2026-09-07 | f227c97dba81 | 25 | 13 of 15 | 13 of 22 | 2 | 9 | swept on the same stored scores; no re-score |
| 2026-09-13 | e39642e40c3c | 25 | 18 of 21 | 18 of 22 | 3 | 4 | rubric e39642e40c3c: examples now sum; explicit no-adjustment rule |
| 2026-09-13 | e39642e40c3c | 35 | 16 of 16 | 16 of 22 | 0 | 6 | threshold 25 -> 35 (3941a1c): precision over recall |
