# jobfit

A local pipeline that ingests remote job postings, filters them with cheap
deterministic rules, and scores the survivors against your CV with one LLM call
each. It runs on your laptop, when you tell it to. **It does not submit
applications.**

Reading ~150 postings a week to find the 15 worth applying to is the bottleneck,
not the applying. This tool does the reading.

Nothing here is specific to one person. The feed list, stack keywords, title
exclusions, eligibility phrases and the scoring rubric are all config or plain
markdown. The defaults are one engineer's and are meant to be replaced.

## What it does

Four commands, run in order, each reading what the last one wrote:

```
                       cost     what it does
  ingest    846        free     5 sources, 9 feeds; normalised, deduped, stored raw
    ↓
  prefilter 135        free     deterministic rules; every rejection records its reason
    ↓
  score     135        $1.31    one structured LLM call each, against your CV
    ↓
  queue       5        free     ranked markdown you read, plus a tracking CSV
```

Those are real numbers from the run of 2026-08-23, measured end to end, not an
illustration. The queue file written that day lists 5 postings, down to a score
of 58; at the shipped threshold of 70 the same scores give 1, and at the
threshold of 25 set on 2026-09-07 they give 45. The corpus has grown since —
1,266 postings stored, 163 surviving stage 2 — and on 2026-09-13 all 163 were
scored under the current rubric, 82 of them at or above 25. What you end up
reading is `queue/YYYY-MM-DD.md`: postings best first, each with its score, the
link, three bullets on why it fits and one to three on why it might not. Half an
hour of reading instead of a week of it.

## Why it is shaped this way

**The funnel exists for cost, and only the middle costs anything.** Ingest and
prefilter are free — no model, no tokens. Sending all 846 postings to the model
would have cost about $8; sending the 135 that survive cheap deterministic rules
cost $1.31. The rules do the volume, the model does the judgement.

**Every stage reads and writes SQLite, and nothing is chained in memory.** You
can re-run scoring without re-ingesting, re-run the prefilter after changing a
rule and see the effect on the whole corpus for free, and re-render the queue at
a different cut without touching either. This is the difference between a
pipeline you can tune and a script you rerun from the top and wait.

**Every rejection is recorded with the phrase that caused it.** A filter that
quietly eats good postings looks exactly like a quiet week. `prefilter_verdicts`
stores the reason and the matched text for every rejection (711 in the
2026-08-23 run: 846 postings less the 135 that survived), so the filter can be
audited instead of trusted — which is how a rule that claimed 173 of 211
postings required Go got caught. (`LIKE '%Go%'` matches "going" and "Google".)

**The scorer is measured, not trusted.** 39 postings were hand-labelled blind —
no model output visible — and the scores compared against them. That measurement
is the whole reason the tool works at all: it shipped cutting at 70 while the
scorer's real range was 3–78, surfacing 1 posting in 22 that deserved one. At 25
it surfaced 13 of 22 and was wrong twice — measured 2026-09-07 on 39 labels,
under the rubric of that date. The numbers, and everything they do not support,
are in [evals/results.md](evals/results.md).

**It never applies for you.** Auto-submitted applications get 1–3% response
rates and get flagged by ATS platforms as spam. The point is to spend the same
human effort on 15 good applications instead of 150 bad ones — so the output is
a thing you read, and every application stays a decision you make.

## Status — read this before installing

| Stage | State |
|---|---|
| 1 — ingest | **Working.** 1,266 postings stored from 5 sources on real data. |
| 2 — prefilter | **Working.** 163 survive; 79% cut over what the feeds still carry. |
| 3 — score | **Working.** 135 postings scored on 2026-08-23 (`claude-sonnet-5`, synchronous path); caching confirmed live at 653,952 cache-read tokens against 170,838 uncached, $1.31 for the run. The rubric changed on 2026-09-07, and on 2026-09-13 everything it had not judged was re-scored under `e39642e40c3c`: the 79 postings in the label file, 53 of them labelled ($0.77, with `--labels-only`) and the other 123 stage-2 survivors ($1.04), $1.81 in all. Nothing is pending. The 96 older postings that stage 2 now rejects as stale keep their 2026-08-23 scores. |
| Queue output | **Working.** Writes `queue/YYYY-MM-DD.md` and appends to `out/applications.csv`. |
| Local UI | **Working.** `jobfit ui` serves one page on 127.0.0.1: the run with a threshold you can drag and watch precision and recall move, the prefilter rules with a live preview of what they would cut, and the labelling panel where the eval set gets made — blind to the scores by construction. |
| 4 — draft | **Dropped**, not pending. Cut on 2026-09-07 rather than left as a stub — the reasoning is in SPEC.md. |
| Evals | **Measured twice, on the same 39 labels, at threshold 25.** 2026-09-07, rubric `f227c97dba81`: precision 13 of 15, recall 13 of 22. 2026-09-13, rubric `e39642e40c3c`: precision 18 of 21, recall 18 of 22. Both are dated records, not running numbers — the label file has since grown to 53, so `jobfit eval` on it reports something different. Every measurement, and everything the numbers do not support, is in [evals/results.md](evals/results.md). |

Being blunt about what that means: the funnel runs end to end, and the scorer
has now been measured against 39 hand labels rather than trusted. That
measurement is what moved the queue threshold from 70 to 25 — at 70 the tool was
surfacing 1 posting in 22 that deserved one. Read `evals/results.md` before
quoting any of it: n is 39, the set has no borderline labels, and its base rate
flatters precision.

## This is a workflow, not an agent

Scoring a posting is a classification task with a fixed input shape: no
branching, no state carried between postings, no step where the model decides
what to do next. An agent loop would add latency, cost, and nondeterminism to
buy nothing, so stage 3 is a single structured call per posting.

The planned counterexample was stage 4, drafting application answers, where a
model would have had reason to go and look things up. That stage was dropped, so
the claim is untested here — the argument stands on the scoring stage, which is
the one that had to be built either way.

**Target roles:** remote Senior Software Engineer and Senior Full Stack, mostly
with US companies. LLM and agentic work is a tie-breaker between two otherwise
equal postings, not what makes a posting a match — the rubric weights reflect
that.

## Quickstart

Not on PyPI — install from the repository.

```bash
git clone https://github.com/davequinta/jobfit && cd jobfit
uv tool install .           # or: pipx install .   or: pip install -e .

mkdir ~/jobsearch && cd ~/jobsearch   # where your data and config will live
jobfit init                 # writes config.yaml, profile/stack.yaml, .env
```

Then fill in three things:

| File | What to put there |
|---|---|
| `.env` | `JOBFIT_CONTACT` (an address feeds can reach you at, sent in the User-Agent) and `ANTHROPIC_API_KEY` for stage 3. See [.env.example](.env.example) for what each one is and where to get it. |
| `profile/stack.yaml` | Your stack, the titles you never want to see, the phrases that mean you are not eligible. |
| `profile/cv.md` | Your CV as markdown. Do not write it by hand — run `jobfit cv path/to/your-cv.pdf` and it converts the one you already have. Stage 3 reads it at run time, so editing it and re-running re-scores everything. |
| `prompts/score_system.md` | The scoring rubric. Tune the weights to the roles you actually want. |

```bash
jobfit cv ~/Documents/my-cv.pdf   # converts your existing CV into profile/cv.md
jobfit ingest               # stage 1 — free, writes data/jobfit.db (~3 min first run)
jobfit prefilter            # stage 2 — free, writes verdicts
jobfit score --limit 5      # stage 3 — costs money, start small
jobfit queue                # writes queue/YYYY-MM-DD.md — this is what you read
jobfit ui                   # read the run in a browser and choose where to cut it
```

Open `queue/YYYY-MM-DD.md`. That is the product.

Stages 1 and 2 cost nothing and need no API key. Only `score` calls the API.

The first `ingest` takes about three minutes: requests are serialised at one per
second per host, and Get on Board needs a name lookup for every company it has
not seen before. Those are cached, so later runs take well under a minute.

**Your Claude Code or Claude.ai subscription does not include API credits** —
they are separate accounts. Get a key at
[console.anthropic.com](https://console.anthropic.com).

`init` never overwrites a file that already exists, so it is safe to re-run

after you have tuned your rules.

### How often to run it

Your call, and the tool does not decide for you — it installs no scheduler and
runs nothing in the background. Every command is one you type.

**Once, when you feel like it.** Run the four commands, read the queue, done.
This is the sane default while you are still tuning your rules, because you want
to see the effect of each change rather than wake up to it.

**On a schedule, if you want it.** Once the rules are settled, the four commands
chain cleanly:

```bash
cd ~/jobsearch && jobfit ingest && jobfit prefilter && jobfit score && jobfit queue
```

Put that behind `cron`, `launchd`, a `Makefile`, or nothing at all. If you do
schedule it, every stage exits non-zero when something is wrong — a feed changed
shape, the filter fell outside its expected band — so a failure reaches you
rather than sitting silently in the database.

Note that `jobfit score` costs money each time it runs. Scheduling it means
scheduling the spend.

## Project layout

The repository holds code and templates. **Everything personal is created on
your machine by `jobfit init` and never committed** — which is why you will not
find a `profile/` directory when you browse this repo.

```
src/jobfit/
  One stage per file          ingest.py  prefilter.py  score.py  queue.py
  Shared, not a stage         sources.py  http.py  db.py  runtime.py
  Not stages                  cli.py  cvimport.py  evals.py  ui.py
  Data                        schema.sql  templates/  templates/ui.html

Tracked here, and written for you by jobfit init
  config.yaml                                               ← jobfit init

Created by you, gitignored
  profile/stack.yaml   profile/cv.md                        ← jobfit init
  prompts/score_system.md   .env                            ← jobfit init
  data/jobfit.db                                            ← jobfit ingest
  queue/2026-08-22.md   out/applications.csv                ← jobfit queue
```

Four files exist so that no stage has to import another one to borrow a
function: `sources.py` holds every feed parser behind one contract, `http.py`
the rate limiter and robots evaluator, `db.py` the connection, and `runtime.py`
the argument and logging boilerplate the stage commands share. Before that split
the scoring stage imported the ingest stage purely to open a database.

`src/jobfit/templates/` is where the blank versions live. `jobfit init` copies
them into your working directory, and from then on they are yours to edit — it
never overwrites a file that already exists.

The scoring rubric is the one exception worth knowing about:
`src/jobfit/templates/score_system.md` is the canonical copy and is versioned
here so prompt changes show up in a diff. `jobfit init` copies it to
`prompts/score_system.md` for you to tune. If you have not run `init`, `jobfit
score` falls back to the bundled copy and logs which one it used.

## Stage 1 — ingest

```bash
jobfit ingest
pytest              # offline, no network, no tokens
```

Exit code is `0` for a clean run and `1` if any feed failed or changed shape, so
a scheduled run surfaces the failure without anyone reading the database.

### What it does

Fetches each configured feed, normalizes records to one shape, dedupes, stores.
Nothing is chained in memory — the stage's only output is SQLite, so scoring can
be re-run without re-ingesting.

**Dedupe key** is `sha256(normalize(company) + normalize(title) + canonical_url)`
under a `UNIQUE` constraint. Normalization casefolds and strips punctuation;
`canonical_url` drops query strings and fragments so a source's UTM tags do not
create a new posting every night. A posting already in the database has only its
`last_seen_at` bumped — nothing downstream sees it change, and it is never
re-scored.

**A republished posting is caught too.** The key above is what the spec asked
for, and a board defeats it by reissuing the same job at `…-python-react-ai` and
then `…-python-react-ai-1`: different canonical URL, different key, two rows.
Among the first 846 postings, ten company-and-title pairs within a source
appeared more than once — sixteen extra rows — and one of them reached the eval
set and counted twice toward recall. A second check matches on source, company
and title, which is a judgement rather than a hash — two roles really can share
a title — so every merge writes a `republished` row to `ingest_issues` naming
both URLs. Merging across *sources* is deliberately not done: two boards
carrying one job are two listings with different text.

**The upstream record is kept verbatim** in `postings.raw_json`. Normalization is
a guess about someone else's schema; keeping the original means a wrong guess
costs a re-parse instead of a re-crawl of a feed that has since rotated its
postings out.

### Schema

Three tables, all owned by this stage. Prefilter verdicts and scores live in
their own tables keyed by `postings.id`, so dropping the score table costs
nothing and re-running stage 3 never touches ingested data. Full definitions in
[src/jobfit/schema.sql](src/jobfit/schema.sql).

- `postings` — one row per unique posting: identity (`dedupe_key`, `source`,
  `url`, `canonical_url`), fields stage 2 and 3 read (`company`, `title`,
  `location_raw`, `tags_json`, `salary_raw`, `description_text`,
  `published_at`), the verbatim `raw_json`, and sighting timestamps.
- `ingest_runs` — one row per run: counts for fetched / inserted / duplicates /
  dropped, and `status` of `ok` or `degraded`.
- `ingest_issues` — one row per record we could not use, with a truncated
  sample of the payload that caused it. A run that stores fewer postings than
  usual should be fully explainable from this table.

### When a feed changes format

Feeds change without warning, and the dangerous failure is not a crash — it is a
run that quietly stores 40 postings instead of 240 and looks fine.

- **Required fields are declared, not defaulted.** A record missing company,
  title, URL, or a parseable date is dropped, never stored with an empty string
  standing in. Each drop writes an `ingest_issues` row naming the field, with a
  sample of the record.
- **Drop rate is the tripwire.** More than 20% of a feed's records unusable, or
  zero records returned, marks the run `degraded` and exits `1`. One bad posting
  is noise; one in five is a schema change.
- **A dead feed never kills the run.** Fetch and parse failures are caught per
  feed, logged at ERROR with the exception, and recorded — the remaining feeds
  still ingest, and the run still exits `1`.
- **The parsers are pure functions over bytes.** They never raise on bad input
  and never touch the network, which is what lets
  [tests/fixtures/](tests/fixtures/) carry `_drift` variants of real responses —
  a renamed field, an empty `<link>`, a title that breaks the `Company: Role`
  convention. Those are red tests, so drift that reaches production is drift we
  chose not to encode, not drift we failed to notice.

The most fragile assumption in the current parsers is that We Work Remotely
packs the employer into the RSS title as `Company: Role`. It has no company
element, so there is no better option — but an item that breaks the convention
is reported rather than stored with the whole string as its title.

### Sources and politeness

Requests are serialized at one per second per host, with a real User-Agent whose
contact address comes from `JOBFIT_CONTACT` in `.env` so it stays out of the
repo. `robots.txt` is fetched once per host and honoured, wildcards included:
`*` and a trailing `$` are patterns and the longest matching rule wins, per RFC
9309, through `protego`. The standard library's parser read `*` literally before
Python 3.14, so until 2026-09-12 a rule like Remotive's `Disallow: /api/*`
blocked nothing on 3.11 or 3.12. Two known gaps: the check runs on each feed's
configured URL, and Hacker News has none (`url: auto`), so its check fails to
fetch anything and allows with a warning; and the requests made after a feed is
cleared — the Hacker News thread lookup, Get on Board's per-company lookups — are
not checked at all. Neither changes a verdict today: `hn.algolia.com` serves no
`robots.txt`, and Get on Board's allows `/`.

| Source | Postings | Access | robots.txt |
|---|---|---|---|
| Hacker News "Who is hiring" | 430 | Public Algolia API | No restrictions |
| Get on Board | 378 | Public JSON:API, 4 pages | `Allow: /`, `ai-train=no` — see below |
| We Work Remotely | 235 | RSS, 5 engineering category feeds | `Allow: /` |
| Remote OK | 200 | Public JSON API | `Allow: /`, `ai-train=no` — see below |
| Remotive | 23 | Public JSON API | Disallows `/api/*` — see below |

**Get on Board** is LATAM-focused and the only source publishing salary as
numbers rather than prose, which feeds the rubric's compensation points
directly. Its API exposes company only as a relationship id and supports no
`include`, so names are resolved one request at a time and cached in
`source_companies` — a first run pays ~115 lookups, later runs pay almost none.

**Six LATAM-focused platforms were assessed and rejected** — Torre, BairesDev,
Tecla, Revelo, Mismo, Mappa. Five need a headless browser or publish no listings
at all. Torre is the interesting one: it is technically easy and still a no,
because its `robots.txt` allows the job-search landing page and disallows every
actual search. It also runs JSON hosts that serve no `robots.txt`, which the
evaluator here would read as permission — and that is exactly why they are not
used. The evidence for each, and the ATS-board direction that would work
instead, is in [issue #1](https://github.com/davequinta/jobfit/issues/1).

**Hacker News** is the highest-signal source, because the postings are written
by the companies themselves rather than relayed by a board — a different
`channel` in the tracking CSV, and a better one. It is also the messiest:
freeform comments with a loose `Company | Role | Location` convention. The
monthly thread id is discovered at run time rather than configured, because a
hard-coded id goes stale after four weeks.

Auditing the first HN run found 19 of 243 comments dropped as "not a posting".
Many were real jobs that simply open with a sentence — "Sumble is the newco from
the founders of Kaggle. We are hiring…" — alongside job seekers' posts and two
`[flagged]` comments. The parser now takes the words before the first verb as
the company, gated on the comment containing a hiring signal at all, because
thread chatter parses just as cleanly as a job ad; for three of the six comments
that recovered, those words are a sentence fragment rather than a name. Re-run
on the same thread four minutes later, it dropped 13 comments where it had
dropped 19 — from 7.8% to 5.3% of the 243 comments the first run read — and 2 of
the 13 that remain are `[flagged]`.

**LinkedIn is deliberately absent.** Scraping it is against their terms for
profile data, they detect and block it, and what you risk is the professional
account you are using to find work.

**Two documented judgement calls.** Both are recorded in
[config.yaml](config.yaml) next to the feed they affect, and both are one
deleted entry away from going away.

*Get on Board and Remote OK* set `Content-Signal: search=yes, ai-train=no,
use=reference` with a general `Allow: /`, and block named AI crawlers
(ClaudeBot, GPTBot, CCBot) by user-agent. This tool is none of those crawlers,
it does not train on the content, and it attributes. But `ai-input` — feeding
text to a model for inference, which stage 3 does — is not specified either
way, and by the policy's own wording that means neither granted nor restricted.
Reading it as permitted for a single user's personal tool at one request per
second is a judgement, not a certainty, and it is written down rather than
buried.

*The Remotive exception, stated plainly.* `remotive.com/robots.txt` carries
`Disallow: /api/*` for all user agents, while the API response itself carries a
legal notice granting developers access on condition that listings link back to
the Remotive URL and credit Remotive as the source. The `Disallow` is aimed at
search indexers rather than API consumers, and the terms shipped with the
endpoint are the more specific grant, so this repo uses the API and records the
decision as a `robots_exemption` field in [config.yaml](config.yaml) with its
reason. Every run logs it at WARNING. Both of Remotive's conditions are met:
`postings.url` is the Remotive URL, and `postings.source` names Remotive in the
queue output. Deleting that one config entry drops the source; nothing else
changes.

Remotive's public API currently returns only 18 postings in total, so volume
comes from We Work Remotely. Sources that would need headless-browser scraping
are dropped rather than maintained.

## Stage 2 — prefilter

```bash
jobfit prefilter    # reads postings, writes prefilter_verdicts
```

Deterministic rules, no network, no model, no cost. Reads `postings`, writes
`prefilter_verdicts`, touches nothing else — so it can be re-run against the
same corpus after every rule change without re-ingesting.

On the real corpus, after two months of accumulation:

```
 1266  postings evaluated
 -851  stale
 -172  no_stack_overlap
  -69  title_excluded
   -7  location_ineligible
   -4  junior
  163  survive  (87% cut)
        of the 792 the feeds still carry, 163 survive (79% cut) — this is what the band judges
```

Rules run cheapest-to-verify first, and the first one that fires is the one
recorded, so auditing a rejection starts with the reason that takes the least
effort to confirm by eye. The run exits non-zero if the cut falls outside 50–85%:
below that the funnel is not paying for itself, above it the filter is probably
eating good postings.

**The band judges what the feeds are still carrying, not the archive.** The
database keeps every posting it has ever seen, and one from two months ago is
stale forever. Counting those makes the cut ratio climb toward 100% as the
archive grows, until the check fails on every run however good the rules are —
which is exactly what it started doing at 86%. Measured against the 792 postings
the last ingest actually saw, the same rules cut 79%, comfortably inside the
band. Both numbers are printed, because the total is the honest description of
the database and the live figure is the one that says anything about the rules.

`stale` dominating is partly an artifact of a first run — RSS category feeds
carry months of backlog — and partly a real mismatch worth knowing about. The
Hacker News thread is monthly, so by the end of the month most of its postings
are older than 14 days even though they are still live. `max_age_days` is a
single global setting; making it per-source would recover much of that.

### Two matching rules, both learned from the data

**Word boundaries, never substrings.** `LIKE '%Go%'` claimed 173 of the first 211 postings
require Go, because "going" and "Google" exist. Every phrase match is anchored.

**Some signals only count in titles.** "You will mentor junior engineers" is a
senior posting. `junior` and `graduate` are decisive in a title and meaningless
in a body, so they are only matched against the title.

### What the audit changed

The `rejected_reason` column exists to catch a filter that is quietly wrong, and
it earned its place on the first run. Reviewing the survivors turned up "Technical
Product Marketer", "Data Analyst", "AI Product Development Coach" and a handful
of Director and VP titles that had passed every rule, and reviewing the
rejections confirmed the one that looked alarming — a `Full Stack Developer`
killed for `no_stack_overlap` — was a Bitcoin wallet role with genuinely no
overlap, not a bug.

That audit added `analyst`, `coach`, `director`, `vp`, `head of`, `marketer` and
`data scientist` to the exclusions, and forced the one rule that cannot be
expressed as a phrase list: **manager without engineer**. "Manager, Government
Compliance & Authorization" survives every literal exclusion, while a bare
`manager` entry would wrongly kill "Engineering Manager". It is a conditional,
so it lives in code with a test on both sides.

Rules and keywords live in `profile/stack.yaml` (gitignored; the
committed template that `jobfit init` copies lives in
[src/jobfit/templates/](src/jobfit/templates/)) so that tuning the filter is a config
edit rather than a code change.

## Stage 3 — score

```bash
jobfit score                # scores what this rubric has not judged yet, plus the eval set
jobfit score --limit 5      # start small; this one costs money
jobfit score --rescore      # judge them all again, and pay again
jobfit score --labels-only  # only the eval set — measure a rubric change for less
```

One structured call per posting against the rubric in
[src/jobfit/templates/score_system.md](src/jobfit/templates/score_system.md). No agent loop — see the
design note at the top. Reads postings that survived stage 2, writes `scores`.

The rubric is a plain markdown file, not a Python string, so it versions and
diffs like code. Prompt changes are the changes most likely to move the numbers,
and they should show up in a diff.

### The prompt shape is load-bearing

```
system: [ rubric + CV + stack profile ]   ← cache_control breakpoint
user:   [ the posting ]
```

Everything before the breakpoint is byte-identical across every call in a run,
so it is cached and billed at ~10% on every posting after the first. Move CV
content into the user message, or let any per-posting text in before the
breakpoint, and caching stops working — with no error and roughly 10x the cost.

That is a silent failure, so it is a test rather than a comment:

```python
def test_the_system_prompt_is_byte_identical_for_two_different_postings():
    """The whole reason caching works. If this fails, cost goes up ~10x."""
```

Alongside it: no posting content appears before the breakpoint, the CV never
reaches the user message, and the last system block carries the breakpoint.
`cache_read_tokens` is stored with every score, so a silent invalidator shows up
as a column of zeros instead of looking like a normal run.

### `why_not` is mandatory

The output schema requires honest negatives. A scorer that only rationalises
matches is useless — you are trying to reject 90% of what you read, and
`why_not` is the field that does that work. When the model returns an empty
`why_not` on a posting it scores at or above the threshold, the score stands but confidence is
downgraded to `low` and the event is logged.

### What a run costs

Measured, not extrapolated: the 135 postings scored on 2026-08-23, on
`claude-sonnet-5` at $2 / $10 per million tokens.

| | tokens | cost | share |
|---|---|---|---|
| Output | 74,975 | $0.75 | 57% |
| Uncached input | 170,838 | $0.34 | 26% |
| Cache reads, billed at 0.1x | 653,952 | $0.13 | 10% |
| Cache writes, billed at 1.25x | 35,763 | $0.09 | 7% |
| **Total** | | **$1.31** | about a cent a posting |

**Caching works.** 76% of the prompt tokens billed were cache reads. The same
run with the breakpoint removed would have cost $2.47, so caching paid for 47%
of it. The cached prefix is 5,109 tokens per call — rubric, CV, stack profile,
the same count on every row of that run — which clears Sonnet's 2,048-token
minimum comfortably. `cache_read_tokens` is stored on every score for exactly
this reason: a breakpoint broken by a stray timestamp shows up as a column of
zeros rather than as a 10x invoice.

**Output is the expensive half.** 555 output tokens per posting costs more than
every input token combined, cached and uncached. The lever on this bill is how
much JSON the rubric asks for, not how many postings reach the model — which is
the opposite of where the funnel's design attention goes.

$1.31 was a cold start: the first ingest pulled a backlog of 846 postings and
135 survived to be scored. A nightly run only pays for what it has not already
judged — **the rubric version is the cache key**. A posting already scored under
the current rubric is skipped, because the same posting and the same rubric
produce the same verdict and buying it again is pure waste. Change the rubric
and every posting comes back automatically, which is also what stops two
generations of verdict from mixing in one eval.

So steady state is roughly 20 new postings a day, ~16% surviving stage 2 —
three or four scored a night, a few cents a month. The re-score after the
2026-09-07 rubric change — the 202 postings it had not judged — cost $1.81 on
2026-09-13; `--rescore` forces a re-score without a rubric change, and the
Batch API would halve either.

`prompt_version` — a hash of the cached prefix — is stored with every score, so
editing the rubric or the CV is visible in the database instead of silently
mixing two generations of results.

## The queue

```bash
jobfit queue                      # threshold 25 by default — see evals/results.md
jobfit queue --threshold 40       # narrow it for one run
jobfit queue --day 2026-08-22     # re-render a specific day
jobfit queue --any-rubric         # include scores from older rubrics too
```

Two artifacts, neither of which touches the database — re-running is always safe.

**What reaches the queue.** A posting needs a score at or above the threshold,
a stage 2 verdict that passed it, and a score from the rubric stage 3 would use
today. The corpus keeps every posting it has ever seen, so without the second
rule a job from two months ago sits at the top of the queue forever; without the
third the list mixes two generations of verdict, which is what `prompt_version`
exists to prevent. Whatever those rules leave out is counted and named in the
command's output, never dropped quietly — and `--any-rubric` queues every
generation when that is what you want.

**`queue/YYYY-MM-DD.md`** — ranked, best first, designed to be read top to bottom
in about thirty minutes. Every entry carries what a skip-or-apply decision needs:

```markdown
## 68 — Acme — Senior Full Stack Engineer

https://example.com/jobs/1

_Anywhere in the World · $120k - $150k · seniority match · high confidence_

**Why it fits**

- Django and Next.js named as the core stack

**Why it might not**

- No team size given, so "own features end to end" may mean solo

**Gaps:** kubernetes
```

**`out/applications.csv`** — append-only, importable into a tracking sheet, and
deduplicated by URL, so running it repeatedly never records the same posting
twice.
`status`, `applied_date`, `contact` and the next-action columns are left empty
on purpose: they are yours to fill in, and a tracking sheet full of invented
state is worse than an empty one.

The column that matters most is **`channel`**. Every row records where the
posting came from, so after four weeks you can measure response rate per channel
and kill the dead ones. Postings ingested from feeds are recorded as
`job_board:<source>`. Add referral and direct-outreach rows by hand — job boards
are the lowest-yield channel most people have, and this column is what proves it
rather than assuming it.

## Reading the results without the queue

The queue is the intended output, but everything is in SQLite and nothing is
hidden. `sqlite3 data/jobfit.db` and:

```sql
-- The whole funnel in one row
SELECT (SELECT count(*) FROM postings) AS ingested,
       (SELECT count(*) FROM prefilter_verdicts WHERE rejected_reason IS NULL) AS survived,
       (SELECT count(*) FROM scores) AS scored,
       (SELECT count(*) FROM scores WHERE fit_score >= 25) AS at_or_above_25;
-- 1266 | 163 | 298 | 133
-- scored includes 96 stale postings still on the previous rubric. `jobfit queue`
-- carries 82 of the 133: stage 2 now rejects the other 51, 30 of them on the
-- previous rubric and 21 on the current one

-- Why a posting was thrown away, with the exact phrase that did it
SELECT p.title, v.rejected_reason, v.detail
  FROM postings p JOIN prefilter_verdicts v ON v.posting_id = p.id
 WHERE v.rejected_reason IS NOT NULL LIMIT 20;

-- Is prompt caching actually working?
SELECT sum(cache_read_tokens) AS cached, sum(input_tokens) AS uncached FROM scores;
```

That last one is the important one. `cached` at or near zero across a run means
caching silently broke and you are paying roughly 10x. It should dominate.

## Making it yours

Everything you would tune is config or markdown. None of it is code.

| To change | Edit |
|---|---|
| Which feeds get pulled | `config.yaml` — add or delete entries under `feeds` |
| What counts as your stack | `profile/stack.yaml` → `stack` |
| Titles you never want to see | `profile/stack.yaml` → `title_exclusions` |
| What makes you ineligible | `profile/stack.yaml` → `location_exclusions` / `location_allowlist` |
| How old a posting can be | `profile/stack.yaml` → `max_age_days` |
| What the scorer rewards | `prompts/score_system.md` — the rubric weights |
| Who you are | `profile/cv.md` |
| The queue cutoff | `jobfit ui` and *Save as default*, or `threshold:` in `config.yaml`. `--threshold N` overrides both for one run. |

**Change one thing at a time.** The rubric and the threshold both move which
postings surface; changing both at once means you cannot tell which one did it.

After editing `profile/stack.yaml`, re-run `jobfit prefilter` — it re-evaluates
every stored posting, so you see the effect immediately and for free. After
editing the rubric or the CV, re-run `jobfit score`; `prompt_version` changes, so
old and new scores never silently mix.

## Troubleshooting

| Symptom | What it means |
|---|---|
| `jobfit ingest` exits 1 with `fetch failed` | One feed is down or moved. The others still ingested — check `ingest_issues` in the database. |
| `ingest` exits 1 with `treat this as a format change` | More than 20% of a feed's records could not be parsed. The feed changed shape; `ingest_issues.sample` has the offending payload. |
| `prefilter` exits 1 with `cut ratio outside the band` | The filter is eating too much or too little. Query `prefilter_verdicts` grouped by `rejected_reason` to see which rule is responsible. |
| `score` says `is missing — run jobfit init` | You are in a directory that was never initialised, or you deleted a scaffolded file. |
| `prefilter` exits 2 with `no postings in the database` | The database is empty. Run `jobfit ingest` — the previous one either never ran or failed. |
| `queue` exits 2 with `nothing has been scored` | Stage 3 has not run. This is distinct from an empty queue, which means nothing cleared the threshold. |
| `queue` exits 2 with `cannot read the rubric` | The queue keeps only scores from the current rubric, and working that out needs the files `jobfit score` reads — the rubric, `profile/cv.md` and `profile/stack.yaml`. Point `--cv` and `--profile` at the ones you scored with, or pass `--any-rubric`. |
| `score` fails on authentication | `ANTHROPIC_API_KEY` is unset or wrong. A Claude Code or Claude.ai subscription is **not** API access. |
| Queue is empty but postings scored | `jobfit queue` prints why. The scores come from an older rubric: run `jobfit score`. Stage 2 now rejects those postings, usually as stale: run `jobfit ingest` for fresh ones. Stage 2 has not judged them: run `jobfit prefilter`. If it names none of those, nothing cleared the threshold: open `jobfit ui` and drag it — if the scores cluster well below the cut, the cut is wrong, not the day; this exact failure shipped for two weeks. |
| The page says every posting is stale | Your corpus is older than `max_age_days`. Re-run `jobfit ingest`. |
| `sum(cache_read_tokens)` is 0 | Prompt caching broke. Run the tests: three of them exist specifically to catch this. |

## Development

```bash
pip install -e . && pip install pytest
pytest                       # 235 tests, no network, no API calls, no tokens
```

Every test runs offline. The feed parsers are pure functions over recorded
fixtures in `tests/fixtures/`, including `_drift` variants — a renamed field, an
empty `<link>`, a title that breaks the `Company: Role` convention — so a feed
changing shape is a red test rather than a silent zero-posting run. Stage 3 uses
an injected fake client, so the eval loop never spends tokens.

Requires Python 3.11 or newer.

## Importing your CV

```bash
jobfit cv ~/Documents/my-cv.pdf      # also accepts .md and .txt
jobfit cv ~/Documents/my-cv.pdf --force   # overwrite an existing profile/cv.md
```

`jobfit init` scaffolds a blank `profile/cv.md`, and filling that in by hand is
the biggest piece of setup friction in the tool — everybody already has a CV, it
is just a PDF. This converts the one you have. One API call, roughly $0.02.

It does three things a copy-paste would not:

- **Builds the Location section.** Eligibility is 20 of the 100 rubric points
  and almost no CV states it, so this infers the country from a phone code or
  address, works out the real working-hours overlap, and says where you are not
  authorised to work. Without it, every posting loses points it should not.
- **Computes years of experience from the dates.** Today's date is passed in,
  because a model left to itself reckons against its training cutoff and
  undercounts. On a real CV that error read "roughly 6.5 years" for someone with
  7 years and 3 months, which is the difference between clearing a "7+ years"
  requirement and failing it.
- **Drops referees.** Other people's names and phone numbers are third-party
  personal data and irrelevant to scoring.

**Read the result before scoring it.** The instructions forbid inventing
anything, and a document that extracts to no text — a scanned PDF has no text
layer — is refused rather than converted into a confident work of fiction. But
this is still a model reformatting your career, and it is the file every posting
gets compared against. Five minutes of reading is cheap.

## Evals

The part that separates this from a demo, and the part most likely to get cut.

```bash
jobfit label                  # collects unlabelled postings into evals/labeled.jsonl
jobfit label --review         # judge them one at a time, in the terminal
jobfit label --review --all   # revisit verdicts you already gave
jobfit label --rewrite        # refresh unlabelled entries, keeping labels you have made
jobfit score --labels-only    # after a rubric change, re-score just the eval set
jobfit eval                   # offline — measures stored scores against your labels
jobfit eval --note "widened stack aliases"   # also logs a row to evals/results.md
```

Each line carries enough to decide without opening the link — title, company,
location, salary, date, the stack keywords stage 2 matched, and an excerpt:

```json
{"url": "...", "company": "Acme", "title": "Senior Engineer",
 "location": "Anywhere in the World", "salary": "$120k - $150k",
 "posted": "2026-08-19", "stack_seen": ["python", "react"],
 "excerpt": "We need someone to own our Django backend…",
 "label": "", "reason": ""}
```

`--review` prints one posting at a time and takes a single keystroke — `a`,
`s`, `b`, `u` to reopen the one before, Enter to defer it, `q` to stop —
followed by one line of reason. It rewrites the file after every verdict, so
quitting halfway keeps what you decided and re-running picks up where you left
off. `u` exists because labelling is a criterion being discovered as you go: the
rule you settle on at posting nineteen is one you want to apply to posting
twelve, and an undo is written to the file like any other change. `u` only
reaches backwards inside one session; `--all` walks the whole file, verdicts
included, showing what you said before so a call you want to change a day later
has a way in from the front. Enter leaves an existing verdict alone rather than
clearing it. Editing the JSONL in an
editor still works; the reviewer exists because `"label"` sits at character 654
of a 676-character line, and forty of those is how an eval set quietly does not
get made.

**It deliberately does not show you the model's score.** Seeing "the model said
78" before you decide anchors the label, and measuring the model against labels
it influenced is circular. `stack_seen` is stage 2's keyword match — there is no
judgement in it.

`jobfit eval` never calls the API. It compares scores already in the database
against your labels, so the loop costs nothing to re-run and the whole suite
works on a plane.

**The eval set never expires.** Stage 2 rejects anything older than
`max_age_days`, so two weeks after you build a set every posting in it is stale
and stage 3 would skip it — meaning no rubric change after that could ever be
measured against it. `jobfit score` therefore also scores whatever is in the
label file, whatever its age, while still skipping what the current rubric has
already judged. A posting's age says nothing about whether the scorer judges it
well.

Postings you have labelled but not yet scored are **named in the report, not
ignored**, because counting them as relevant while they cannot be surfaced
quietly deflates recall.

Three choices in how the numbers are reported, each defensible:

**Counts before ratios.** With forty hand-labelled postings the third decimal of
a precision figure is noise. The report leads with `2 of 3` and prints a margin
of error, because a count is checkable and a bare ratio invites more confidence
than the sample supports.

**Borderline labels are excluded from precision and recall.** They exist to
study disagreement, not to be graded. Folding them into either class would make
the headline number depend on a judgement the label explicitly declines to make.
They are reported separately — read them and decide who was right.

**Unscored postings are named, not dropped.** Silently ignoring a labelled
posting that was never scored would inflate recall.

Optimise for precision over recall. A false positive costs twenty minutes and a
wasted application; a false negative costs one posting out of hundreds.

`evals/results.md` gets one row per run, recording the rubric version alongside
the numbers. Never change the rubric and the threshold in the same run — if the
numbers move you need to know which one did it. `jobfit eval` warns when the
labelled postings were scored under more than one rubric version, and the row
records `mixed` instead of picking one. The rest of the corpus is not asked:
after `--labels-only` it is still on the old rubric, and that is not a mixed
measurement.

## The local page

```bash
jobfit ui                 # http://127.0.0.1:8765
jobfit ui --port 9000 --no-browser
```

It exists because of one number. The queue shipped cutting at 70 while the
scorer's real range turned out to be 3–78, so it surfaced 1 posting in 22 that
deserved one — and that sat unnoticed for two weeks. A threshold is invisible in
a config file and obvious the moment you can drag it and watch the list and the
precision move together.

**Results** ranks every scored posting, dims the ones under the cut rather than
hiding them, and expands to the `why_fit` and `why_not` bullets. Drag the
threshold and the queue size, precision, recall and both error counts update
against your hand labels. *Save as default* writes `threshold:` into
`config.yaml`, which is what `jobfit queue` and `jobfit eval` then read.

**Label** is where the eval set gets made. One posting at a time, with the
*whole* description rather than the 320-character excerpt the JSONL keeps, and
a verdict is one click or one keystroke — `a`, `s`, `b`, arrows to move. It
shows the running mix (apply / skip / borderline / left to judge) and says so
when there are no borderline calls, because finishing a set without any is how
the first one ended up unable to say anything about the hard middle. Changing a
verdict sets the new one directly; the terminal's `u` undoes to empty, and a
verdict cleared by accident is how one went blank.

Nothing on that panel comes from the scorer. The page holds every score — that
is the whole Results tab — so the labelling payload is built field by field
from the posting and never from the `scores` table, and a test asserts no score
reaches it for a posting that has one.

**Rules** edits the stage 2 filters — stack aliases, title exclusions, junior
signals, eligibility phrases, maximum age — and previews the cut they would
produce over the stored postings. Nothing reaches the database until you run
`jobfit prefilter` yourself.

Two things it deliberately does not do. **It does not compute precision in
JavaScript**: every threshold from 0 to 100 is evaluated server-side by the same
`evals.evaluate` the eval suite tests, and shipped as a lookup table, so the
number on screen cannot drift from the number in `evals/results.md`. And **it
does not write to the database** — it reads what the stages wrote and edits
config files, so there is no path where looking at a run changes it.

## What is deliberately not here

- **No hosted dashboard.** There is a local page — `jobfit ui`, served from the
  standard library on 127.0.0.1 and gone when you close it — because one number
  in a config file turned out to be worth seeing against the data it acts on.
  It is not a service: nothing is deployed, nothing listens on a public
  interface, and every stage still runs from the CLI without it.
- **No multi-user support, no hosting, no accounts.** Everyone runs their own.
  Your CV and your API key never leave your machine.
- **No auto-submission, anywhere, under any conditions.** Auto-submitted
  applications get 1–3% response rates and are flagged by ATS platforms as spam.
  The point is to spend the same human effort on 15 good applications instead of
  150 bad ones.
- **No LinkedIn scraping.** Against their ToS for profile data.
- **No agent loop for scoring.** See the design note at the top.

## License

MIT. See [LICENSE](LICENSE).
