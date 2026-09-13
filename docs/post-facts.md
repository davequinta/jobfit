# Post facts — jobfit

Every figure has its source beside it. No interpretation.

- Queries run with `sqlite3 -readonly data/jobfit.db` unless marked
  **snapshot**: `data/jobfit-before-e39642e40c3c.db`, a `.backup` of the database
  taken 2026-09-12 before the re-score, which still holds the 2026-08-23 scores of
  the 39 postings the re-score replaced. `data/`, `queue/` and `profile/` are
  gitignored: facts sourced from them are checkable on the machine that ran the
  tool, not from the repository.
- README line numbers are for `git show 7acccb7:README.md` (main on 2026-09-12).
- Commit hashes are this repository's as of 2026-09-12.

## 1. Chronology

| Event | Value | Source |
|---|---|---|
| Threshold 70 enters | `DEFAULT_THRESHOLD = 70` | `git show debfca9:src/jobfit/queue.py` line 44; commit debfca9, 2026-08-22 00:46:45 -0600 |
| 70 hard-coded in two more places before the lowering | `parser.add_argument("--threshold", type=int, default=70)`; `if score.why_not or score.fit_score < 70:` | `git show 0e71897^:src/jobfit/evals.py` line 438; `git show 0e71897^:src/jobfit/score.py` line 180 |
| Queue file 2026-08-22 | 1 posting (78, Cosuno) | `queue/2026-08-22.md` line 3 |
| Scoring run of the 135 | 2026-08-23T00:34:30Z → 17:58:25Z; rubric f227c97dba81; claude-sonnet-5 | Q4, snapshot |
| Queue file 2026-08-23 | 5 postings (78, 68, 60, 58, 58) | `queue/2026-08-23.md` line 3 and its `## ` headings |
| First labelled eval | commit c1349ac, 2026-09-07 12:26:15 -0600 | `git log -1 --format='%h %ad' --date=iso c1349ac` |
| Threshold 70 → 25 | commit 0e71897, 2026-09-07 12:26:38 -0600 | `git log -1 --format='%h %ad' --date=iso 0e71897` |
| Rubric f227c97dba81 → e39642e40c3c | commit b9043e9, 2026-09-07 19:01:01 -0600 | `git log -1 --format='%h %ad' --date=iso b9043e9` |
| Re-score of the eval set under e39642e40c3c | 2026-09-13T05:41:42Z → 05:53:26Z (2026-09-12 23:41–23:53 -0600), 79 postings | `SELECT min(scored_at), max(scored_at), count(*)` over the labelled URLs, rubric e39642e40c3c |
| Re-score of the other stage-2 survivors under e39642e40c3c | 2026-09-13T05:53:41Z → 06:12:15Z (23:53 -0600 to 00:12 -0600), 123 postings | the same query over rubric e39642e40c3c rows whose URL is not in `evals/labeled.jsonl` |
| debfca9 → 0e71897 | 16 days 11:39:53 | difference of the two commit dates |
| First f227c97dba81 score → 0e71897 | 15 days 17:52:08 | 2026-08-23T00:34:30Z to 2026-09-07T18:26:38Z |
| Calendar days, 2026-08-23 → 2026-09-07 | 15 | date arithmetic |
| At threshold 70, the 135 scored postings | 1 at or above 70 | Q5, snapshot |
| At threshold 70, the 22 postings labelled `apply` | 1 surfaced | `evals/results.md`, 2026-09-07 "Threshold sweep", row 70 |

## 2. Funnel

The README query (README "Reading the results without the queue"):

```sql
SELECT (SELECT count(*) FROM postings) AS ingested,
       (SELECT count(*) FROM prefilter_verdicts WHERE rejected_reason IS NULL) AS survived,
       (SELECT count(*) FROM scores) AS scored,
       (SELECT count(*) FROM scores WHERE fit_score >= 25) AS queued;
```

### The 2026-08-23 run

| Step | Value | Source |
|---|---|---|
| Ingested | 846 | `SELECT sum(inserted), count(*) FROM ingest_runs WHERE started_at < '2026-08-23'` → `846\|4` |
| Survived stage 2 | 135 | Commit 1d096a8 (2026-08-22 11:34:30 -0600) message: "from 39 surviving the prefilter to 135". Verdicts are replaced on every run; all were recomputed 2026-09-08 (Q3) |
| Scored | 135 | Q4, snapshot |
| In the queue that day | 5 | `queue/2026-08-23.md` |
| At or above 70 | 1 | Q5, snapshot |
| At or above 25 | 45 | Q5, snapshot; `queue/2026-09-07.md` line 3 lists 45 |

### The corpus on 2026-09-12, before the re-score

| ingested | survived | scored | queued (≥ 25) | Source |
|---|---|---|---|---|
| 1,266 | 163 | 135 | 45 | README query, snapshot → `1266\|163\|135\|45` |

### The corpus after the re-score, 2026-09-13

| ingested | survived | scored | queued (≥ 25) | Source |
|---|---|---|---|---|
| 1,266 | 163 | 298 | 133 | README query → `1266\|163\|298\|133` |

`scored` and `queued` count two rubrics together:

```sql
SELECT prompt_version, count(*), sum(fit_score >= 25) FROM scores GROUP BY 1;
-- e39642e40c3c|202|103
-- f227c97dba81|96|30
```

The 96 are 2026-08-23 postings that stage 2 now rejects as stale and that are
not in `evals/labeled.jsonl`; neither re-score selected them. Under e39642e40c3c
nothing is pending (`postings_to_score` with the label file returns 0 rows).
`jobfit queue` selects on the same condition as `queued`: `src/jobfit/queue.py`,
`entries_above`, `WHERE s.fit_score >= ?`, with no filter on rubric or stage-2
verdict.

### Supporting queries

```sql
-- Q3: when the stage-2 verdicts were computed
SELECT min(evaluated_at), max(evaluated_at), count(*) FROM prefilter_verdicts;
-- 2026-09-08T01:14:46.414878+00:00|2026-09-08T01:14:46.414878+00:00|1266

-- Q4: score rows by rubric (snapshot)
SELECT prompt_version, model, count(*), min(scored_at), max(scored_at) FROM scores GROUP BY 1, 2;
-- f227c97dba81|claude-sonnet-5|135|2026-08-23T00:34:30.839023+00:00|2026-08-23T17:58:25.895230+00:00
```

## 3. Score distribution, rubric f227c97dba81

```sql
-- Q5 (snapshot)
WITH s AS (SELECT fit_score, row_number() OVER (ORDER BY fit_score) AS rn, count(*) OVER () AS n
             FROM scores WHERE prompt_version = 'f227c97dba81')
SELECT min(fit_score) AS min, max(fit_score) AS max, max(n) AS n,
       (SELECT avg(fit_score) FROM s s2 WHERE s2.rn IN ((s2.n + 1) / 2, (s2.n + 2) / 2)) AS median,
       sum(fit_score >= 70) AS at_or_above_70, sum(fit_score >= 25) AS at_or_above_25
  FROM s;
-- 3|78|135|18.0|1|45
```

The 39 postings labelled in c1349ac, same rubric. Labels from
`git show c1349ac:evals/labeled.jsonl`, joined to `scores` on `postings.url`, snapshot:

| label | n | scores, sorted | min | max | median |
|---|---|---|---|---|---|
| apply | 22 | 15 18 18 18 18 20 22 22 22 28 32 34 38 38 42 42 42 42 47 52 68 78 | 15 | 78 | 33 |
| skip | 17 | 8 8 10 12 14 14 15 16 18 18 18 18 18 22 22 26 26 | 8 | 26 | 18 |

## 4. Score distribution, rubric e39642e40c3c

### The same 39 postings

Same labels and join as section 3, live database.

| label | n | scores, sorted | min | max | median |
|---|---|---|---|---|---|
| apply | 22 | 22 22 22 24 30 31 38 38 38 42 47 48 54 56 58 58 59 63 63 67 71 78 | 22 | 78 | 47.5 |
| skip | 17 | 10 12 13 18 18 18 22 22 22 22 22 22 22 24 31 32 34 | 10 | 34 | 22 |

Paired, f227c97dba81 (snapshot) → e39642e40c3c (live), per posting:

| | n | rose | unchanged | fell | mean change | median change | range |
|---|---|---|---|---|---|---|---|
| all | 39 | 35 | 2 | 2 | +9.08 | +6 | −5 to +45 |
| apply | 22 | 19 | 2 | 1 | +12.41 | | |
| skip | 17 | 16 | 0 | 1 | +4.76 | | |

At or above 70 among the 39: 2 (Cosuno 78, Huzzle 71).

### Every posting scored under e39642e40c3c

```sql
-- Q8: Q5 with 'e39642e40c3c'
-- 0|84|202|25.0|8|103

-- Q9: the same, restricted to postings stage 2 passes today
WITH s AS (SELECT sc.fit_score, row_number() OVER (ORDER BY sc.fit_score) AS rn, count(*) OVER () AS n
             FROM scores sc JOIN prefilter_verdicts v ON v.posting_id = sc.posting_id
            WHERE sc.prompt_version = 'e39642e40c3c' AND v.rejected_reason IS NULL)
SELECT min(fit_score), max(fit_score), max(n),
       (SELECT avg(fit_score) FROM s s2 WHERE s2.rn IN ((s2.n + 1) / 2, (s2.n + 2) / 2)),
       sum(fit_score >= 70), sum(fit_score >= 25)
  FROM s;
-- 0|84|163|25.0|6|82
```

The 202 are not the 135 of section 3: 39 postings are in both sets.

## 5. Cost

Prices: claude-sonnet-5, $2 / MTok input and $10 / MTok output; cache reads 0.1x
and cache writes 1.25x input (README "What a run costs"; commit 0416a26,
2026-09-07 11:17:25 -0600). The per-token rates below are derived: 0.1 × $2 =
$0.20 / MTok, 1.25 × $2 = $2.50 / MTok.

Formula for every row: input × 2 + output × 10 + cache read × 0.2 + cache write × 2.5, divided by 1e6.
Without caching: (input + cache read + cache write) × 2 + output × 10, divided by 1e6.

### 2026-08-23, rubric f227c97dba81, 135 postings

```sql
-- Q6 (snapshot)
SELECT sum(input_tokens), sum(output_tokens), sum(cache_read_tokens), sum(cache_write_tokens)
  FROM scores WHERE prompt_version = 'f227c97dba81';
-- 170838|74975|653952|35763
```

| part | tokens | USD |
|---|---|---|
| Output | 74,975 | 0.7498 |
| Uncached input | 170,838 | 0.3417 |
| Cache reads | 653,952 | 0.1308 |
| Cache writes | 35,763 | 0.0894 |
| **Total** | | **1.3116** |
| Without caching | | 2.4709 |

**"$2.47 without caching"** — commit 0416a26. (170,838 + 653,952 + 35,763) × 2 / 1e6
+ 74,975 × 10 / 1e6 = 1.7211 + 0.7498 = 2.4709. Assumes the same token counts
with the cache breakpoint removed.

**"About $8 to send all 846"** — first appears in commit 5589d7b (2026-09-07
12:42:12 -0600); no commit records the method. 1.3116 × 846 / 135 = 8.22. None of
the other 711 postings was scored.

### 2026-09-12/13, rubric e39642e40c3c

| Run | Postings | Input | Output | Cache read | Cache write | USD | Without caching |
|---|---|---|---|---|---|---|---|
| First attempt, 23:32:32 -0600 | 0 | — | — | — | — | 0.0000 | — |
| `jobfit score --labels-only`, 23:41–23:53 -0600 | 79 | 109,788 | 45,055 | 415,194 | 5,323 | 0.7665 | 1.5112 |
| `jobfit score`, the other 123 stage-2 survivors, 23:53 → 00:12 -0600 | 123 | 121,168 | 66,840 | 654,729 | 0 | 1.0417 | 2.2202 |
| **Both runs** | 202 | 230,956 | 111,895 | 1,069,923 | 5,323 | **1.8082** | 3.7314 |

The first attempt returned `401 authentication_error` on its first request and
wrote no row (the run's terminal output; not saved in the repository). The 79-row
figures: the four token sums over `scores` rows with rubric e39642e40c3c whose
`postings.url` is in `evals/labeled.jsonl`; the 123-row figures, the same sums
where it is not. No row of the 123-posting run has cache-write tokens; its first
score row is timestamped 15 seconds after the previous run's last (05:53:26Z →
05:53:41Z).

## 6. `LIKE '%Go%'`

| Fact | Value | Source |
|---|---|---|
| First written | commit debfca9, 2026-08-22 00:46:45 -0600 | `git show debfca9 \| grep -n '173'` → README.md, SPEC.md, `src/jobfit/prefilter.py` (docstring lines 14–15), `tests/test_prefilter.py` |
| A `LIKE`-based stack matcher in any commit | none | `git log --all -S"LIKE '%" -- src/` → debfca9 only, where the string is in the docstring |

```sql
-- Q7 (SQLite LIKE is case-insensitive for ASCII)
SELECT count(*) AS postings, sum(description_text LIKE '%Go%') AS like_go
  FROM postings WHERE first_seen_run = 1;
-- 211|173
```

## 7. Rubric f227c97dba81 → e39642e40c3c

| Fact | Value | Source |
|---|---|---|
| Changed in | commit b9043e9, 2026-09-07 19:01:01 -0600 | `git show b9043e9 -- src/jobfit/templates/score_system.md` |
| f227c97dba81 is the template before b9043e9 | reproduced | snippet below → `f227c97dba81` |
| e39642e40c3c is the template at b9043e9 | reproduced | snippet below with `src/jobfit/templates/score_system.md` → `e39642e40c3c` |
| What the hash covers | rubric + CV + stack summary | `src/jobfit/score.py`, `system_text` and `prompt_version` |

```bash
# needs the gitignored profile/cv.md and profile/stack.yaml of the machine that ran the tool
git show b9043e9^:src/jobfit/templates/score_system.md > rubric-before.md
python -c "from jobfit import score; print(score.prompt_version(score.load_rubric('rubric-before.md', 'profile/cv.md', 'profile/stack.yaml')))"
```

The three worked examples at f227c97dba81 (`git show b9043e9^:src/jobfit/templates/score_system.md`):

| Example | Components listed | Their sum | Score stated |
|---|---|---|---|
| Senior full stack, Django + Next.js, worldwide | 33, 24, 20, 6, 8 | 91 | 91 |
| ML Engineer, "Remote (US)" | 8, 15, 4, 2, 5 | 34 | 24 |
| Full Stack Developer, agency, equity-heavy | 18, 10, 8, 0, 0 | 36 | 31 |

The examples in the diff:

```diff
-**Score: 91, confidence high.** Stack is an exact match (33). Explicitly senior
+**Score: 91, confidence high.** 33 + 24 + 20 + 6 + 8. Stack is an exact match (33). Explicitly senior

-**Score: 24, confidence high.** This is the trap case. Shares AI vocabulary with
+**Score: 34, confidence high.** 8 + 15 + 4 + 2 + 5. This is the trap case. Shares AI vocabulary with

-**Score: 31, confidence medium.** Partial stack overlap, React yes but no Python
+**Score: 36, confidence medium.** 18 + 10 + 8 + 0 + 0. Partial stack overlap, React yes but no Python
```

The two other changes in the same diff:

```diff
+**The score is the sum of the five components below, and nothing else.** Award
+each category its points, add them, and report the total. Do not apply a further
+adjustment after summing, and do not shade the total down because the posting
+feels unexciting — the shading is already in the component scores, and doing it
+twice compresses every result into the bottom half of the scale.

-you cannot find a genuine concern, the score is too high; lower it.
+you cannot find a genuine concern, you have not read the posting closely enough
+— look again. Do not lower the score to compensate for a thin `why_not`: the
+score is the sum of the components and the bullets are a separate obligation.
```

### The eval, both rubrics, the 39 labels of c1349ac

```bash
git show c1349ac:evals/labeled.jsonl > labels-c1349ac.jsonl   # jobfit eval needs a regular file
jobfit eval --labels labels-c1349ac.jsonl --threshold N        # add --db <snapshot> for f227c97dba81
```

| threshold | f227c97dba81 precision | f227c97dba81 recall | e39642e40c3c precision | e39642e40c3c recall |
|---|---|---|---|---|
| 70 | 1 of 1 | 1 of 22 | 2 of 2 | 2 of 22 |
| 60 | 2 of 2 | 2 of 22 | 5 of 5 | 5 of 22 |
| 50 | 3 of 3 | 3 of 22 | 10 of 10 | 10 of 22 |
| 40 | 8 of 8 | 8 of 22 | 13 of 13 | 13 of 22 |
| 35 | 10 of 10 | 10 of 22 | 16 of 16 | 16 of 22 |
| 30 | 12 of 12 | 12 of 22 | 18 of 21 | 18 of 22 |
| 25 | 13 of 15 | 13 of 22 | 18 of 21 | 18 of 22 |
| 20 | 17 of 21 | 17 of 22 | 22 of 33 | 22 of 22 |
| 15 | 22 of 33 | 22 of 22 | 22 of 36 | 22 of 22 |

Thresholds meeting SPEC's targets, precision > 0.8 and recall > 0.6 (both strict),
checked at every integer 0–100 with `jobfit.evals.evaluate`: f227c97dba81 → 19 and
20; e39642e40c3c → 23 through 38. On f227c97dba81 at 21 and 22 precision is
exactly 80%, which fails the strict inequality.

## 8. Numbers that do not reproduce or are no longer true

### README.md at 7acccb7

| Line | Claim | Found | Source |
|---|---|---|---|
| 27, 30 | Funnel "queue 45", "real numbers from the run of 2026-08-23 … not an illustration" | The queue written that day lists 5. 45 is the count at threshold 25, set 2026-09-07, over the same 135 scores | `queue/2026-08-23.md` → 5; Q5 `at_or_above_25` → 45 |
| 52 | "all 711 rejections" | 1,103 rejected verdicts on 2026-09-12. 711 = 846 − 135 | `SELECT count(*) FROM prefilter_verdicts WHERE rejected_reason IS NOT NULL` → 1103 |
| 189 | `config.yaml` listed under "Created by you, gitignored" | Tracked, not ignored | `git ls-files config.yaml` → `config.yaml`; `git check-ignore config.yaml` → no match |
| 237 | "Sixteen of the first 846 postings were one job twice" | 16 extra rows in 10 (source, company, title) groups. `git show 7acccb7:src/jobfit/ingest.py` line 156 and `git show 7acccb7:tests/test_ingest.py` line 305 say "thirteen" | snippet below → `10 16` |
| 334 | HN drop rate "from 7.8% to 5.3%, and what remains is mostly `[flagged]`" | Both percentages come out of 243 comments, which no table stores. Run 3 (2026-08-22T17:29Z), the first HN run: 224 postings + 19 dropped = 243, so 19 of 243 = 7.8%. Run 4 (17:33Z) read the same thread and dropped 13; 13 of 243 = 5.3% assumes the thread still had those 243 comments. Stored lower bound for run 4: 6 new + 13 dropped + 191 run 3 postings last seen by run 4 = 210 (the other 33 were last seen by run 5). Run 4's 6 new postings are 6 of run 3's dropped comments. "Mostly `[flagged]`" is false: 2 of the 13 | `SELECT first_seen_run, count(*) FROM postings WHERE source = 'hackernews' GROUP BY 1` → `3\|224`, `4\|6`; `SELECT run_id, count(*), sum(sample = '[flagged]') FROM ingest_issues WHERE source = 'hackernews' GROUP BY 1` → `3\|19\|2`, `4\|13\|2`; same thread: `SELECT DISTINCT json_extract(raw_json, '$.story_id') FROM postings WHERE source = 'hackernews' AND first_seen_run IN (3, 4)` → `49156683`; lower bound: `SELECT count(*) FROM postings WHERE source = 'hackernews' AND first_seen_run = 3 AND last_seen_at = (SELECT started_at FROM ingest_runs WHERE id = 4)` → `191`; recovered comments: `substr(ingest_issues.sample, 1, 40) = substr(postings.description_text, 1, 40)` for run 3 issues against run 4 postings → 6 (snapshot) |
| 509 | "The cached prefix is ~4,880 tokens per call" | 5,109 on every 2026-08-23 row. 4,880 = 653,952 / 134 | `SELECT cache_write_tokens, count(*) FROM scores WHERE cache_write_tokens > 0 GROUP BY 1` → `5109\|7`; the same for `cache_read_tokens` → `5109\|128` (snapshot) |
| 648 | "212 tests, no network" | 212 collected. `test_robots_blocks_a_disallowed_path` fails on Python 3.11.13 and 3.12.10 (211 passed, 1 failed); all 212 pass on 3.14.6 | `pytest -q` at 7acccb7 under `/opt/homebrew/bin/python3.11`, `/opt/homebrew/bin/python3.12` and a 3.14.6 venv |

```python
# duplicate count for line 237
import sqlite3, collections
from jobfit.sources import normalize_field
c = sqlite3.connect("file:data/jobfit.db?mode=ro", uri=True)
rows = c.execute("SELECT source, company, title FROM postings WHERE first_seen_run <= 4")
g = collections.Counter((s, normalize_field(co), normalize_field(t)) for s, co, t in rows)
print(sum(1 for n in g.values() if n > 1), sum(n - 1 for n in g.values() if n > 1))   # 10 16
```

### Outside the README

| Where | Claim | Found | Source |
|---|---|---|---|
| `evals/results.md`, 2026-09-07 ("met between 20 and 25"); commit 0e71897 message | Threshold 25 meets precision > 0.8 and recall > 0.6 | Recall at 25 is 13 of 22, 59%. Only 19 and 20 meet both | section 7 table; `jobfit eval --labels labels-c1349ac.jsonl --threshold 25 --db <snapshot>` |
| `evals/results.md`, 2026-09-07 | `apply` scores have "a median of 34" | 33, the mean of the 11th and 12th of 22 values (32, 34) | section 3 |
| SPEC.md, Architecture diagram | ~400 postings → ~120 survive → ~120 scored | Measured: 846 → 135 → 135 (2026-08-23); 1,266 → 163 → 135 (2026-09-12, before the re-score) | section 2 |
| SPEC.md, Evals | 40 labels: 15 apply, 15 no, 10 borderline | Measured set: 39 labels, 22 apply, 17 skip, 0 borderline | `evals/results.md`, 2026-09-07 |

## 9. Not verifiable

| Claim | What is missing |
|---|---|
| The threshold used for `queue/2026-08-23.md` | The default was 70; the file lists five postings down to 58. No record of the value passed |
| 135 postings survived stage 2 on 2026-08-23 | Verdicts from that run; they were replaced. Only the 1d096a8 message and the 135 score rows remain |
| How "$8 for all 846" was computed | The method; section 5 shows one calculation that gives 8.22 |
| "Fifteen days" at threshold 70 | A start point: 16 days 11:39 from debfca9, 15 days 17:52 from the first scored run, 15 calendar days from 2026-08-23 |
| "1 of 22" as what the queue showed | The 22 `apply` labels were written 2026-09-07. The queue at 70 held 1 posting |
| "Labelled blind" | The tooling hides scores (commit a475b6d, 2026-08-22 11:04:48 -0600; `test_the_skeleton_never_shows_the_models_verdict`). Whether the labeller had seen scores elsewhere, such as in the queue files |
| Whether the +6 median change exceeds run-to-run variation | No posting was scored twice under the same rubric |
| The first re-score attempt failed with `401 authentication_error` | The run's terminal output, which was not saved. The database confirms only that it wrote no row |
| hn.algolia.com serves no robots.txt; Get on Board allows `/` | Fetched 2026-09-12 (HTTP 404; `Allow: /`); external and can change |

## 10. Threshold 25 → 35

| Fact | Value | Source |
|---|---|---|
| Changed in | commit 3941a1c, 2026-09-13 17:20:23 -0600 | `git log -1 --format='%h %ad' --date=iso 3941a1c` |
| SPEC rule it follows | "Optimize for **precision over recall**" | `SPEC.md`, section "Evals — do not skip this", point 3 |
| Recommended before it was applied | 35, the lowest cut with no false positives on the 39 labels of c1349ac | `evals/results.md`, 2026-09-13 entry, "Recommendation, not applied" |

### The eval at both thresholds, rubric e39642e40c3c

```bash
jobfit eval --threshold 35        # the 53 labels in evals/labeled.jsonl today
jobfit eval --threshold 25
git show c1349ac:evals/labeled.jsonl > "$TMPDIR/labels-c1349ac.jsonl"   # jobfit eval needs a regular file
jobfit eval --labels "$TMPDIR/labels-c1349ac.jsonl" --threshold 35
jobfit eval --labels "$TMPDIR/labels-c1349ac.jsonl" --threshold 25
```

| labels | threshold | precision | recall |
|---|---|---|---|
| 53, `evals/labeled.jsonl` on 2026-09-13 | 35 | 19 of 20 | 19 of 30 |
| 53, `evals/labeled.jsonl` on 2026-09-13 | 25 | 23 of 28 | 23 of 30 |
| 39, c1349ac | 35 | 16 of 16 | 16 of 22 |
| 39, c1349ac | 25 | 18 of 21 | 18 of 22 |

### What each threshold passes today

```sql
-- Q10: the 163 postings stage 2 passes today, all scored under e39642e40c3c
SELECT count(*), sum(s.fit_score >= 25), sum(s.fit_score >= 35), sum(s.fit_score >= 70)
  FROM scores s JOIN prefilter_verdicts v ON v.posting_id = s.posting_id
 WHERE v.rejected_reason IS NULL AND s.prompt_version = 'e39642e40c3c';
-- 163|82|59|6
```

### Scores of 50 or more on 2026-08-23

```sql
-- Q11 (snapshot): rubric f227c97dba81, 135 rows
SELECT count(*), group_concat(fit_score, ', ')
  FROM (SELECT fit_score FROM scores
         WHERE prompt_version = 'f227c97dba81' AND fit_score >= 50 ORDER BY fit_score);
-- 6|52, 58, 58, 60, 68, 78
```

| Posting | Score (f227c97dba81, snapshot) | In `queue/2026-08-23.md` | Label at c1349ac |
|---|---|---|---|
| Cosuno — Senior Full Stack Developer (TypeScript) | 78 | line 7 | apply |
| Sur — Full Stack Developer | 68 | line 31 | apply |

Source for the two rows: `git show c1349ac:evals/labeled.jsonl`, the entries with
`company` "Cosuno" and "Sur", joined on `url` to `scores` in the snapshot.
