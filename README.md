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

## Status — read this before installing

| Stage | State |
|---|---|
| 1 — ingest | **Working.** 211 postings from 2 sources on the first real run. |
| 2 — prefilter | **Working.** Cuts 211 to 39 on real data. |
| 3 — score | **Built, never run.** Prompt, schema, storage and tests are done; it has not yet made a single real API call. |
| Queue output | **Working.** Writes `queue/YYYY-MM-DD.md` and appends to `out/applications.csv`. |
| 4 — draft | **Not built.** Queue entries carry no cover-letter opener. |
| Evals | **Harness built, unlabelled.** `jobfit label` / `jobfit eval` work; nobody has hand-labelled a set yet, so no precision or recall numbers exist. |

Being blunt about what that means: the funnel runs end to end and produces a
queue you can read, but nobody has measured whether the scores are any good.
Until `evals/results.md` exists, treat the numbers as a starting point to tune,
not as a verdict.

## This is a workflow, not an agent

Scoring a posting is a classification task with a fixed input shape: no
branching, no state carried between postings, no step where the model decides
what to do next. An agent loop would add latency, cost, and nondeterminism to
buy nothing, so stage 3 is a single structured call per posting. The one place
agentic behaviour might earn its keep is drafting application answers (stage 4),
and even there it starts as a plain call and only grows tools if the evals show
it needs them.

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
jobfit ingest               # stage 1 — free, writes data/jobfit.db
jobfit prefilter            # stage 2 — free, writes verdicts
jobfit score --limit 5      # stage 3 — costs money, start small
jobfit queue                # writes queue/YYYY-MM-DD.md — this is what you read
```

Open `queue/YYYY-MM-DD.md`. That is the product.

Stages 1 and 2 cost nothing and need no API key. Only `score` calls the API.

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
In the repository                      Created by you, gitignored
─────────────────────────────────      ──────────────────────────────────
src/jobfit/                            config.yaml        ← jobfit init
  ingest.py  prefilter.py              profile/
  score.py   queue.py                    stack.yaml       ← jobfit init
  cli.py     schema.sql                  cv.md            ← jobfit init
  templates/                           prompts/
    config.yaml  stack.yaml               score_system.md ← jobfit init
    cv.md        env                    .env               ← jobfit init
    score_system.md
tests/                                 data/jobfit.db     ← jobfit ingest
.env.example                           queue/2026-08-22.md ← jobfit queue
SPEC.md  CLAUDE.md  README.md          out/applications.csv ← jobfit queue
```

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
repo. `robots.txt` is fetched once per host and honoured.

| Source | Access | robots.txt |
|---|---|---|
| Remotive | Public JSON API | Disallows `/api/*` — see below |
| We Work Remotely | RSS, 5 engineering category feeds | Allowed |

**The Remotive exception, stated plainly.** `remotive.com/robots.txt` carries
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

On the first real corpus:

```
  211  postings evaluated
  -98  stale
  -52  title_excluded
  -17  no_stack_overlap
   -3  junior
   -2  location_ineligible
   39  survive  (82% cut)
```

Rules run cheapest-to-verify first, and the first one that fires is the one
recorded, so auditing a rejection starts with the reason that takes the least
effort to confirm by eye. The run exits non-zero if the cut falls outside 50–85%:
below that the funnel is not paying for itself, above it the filter is probably
eating good postings.

`stale` dominating is an artifact of the first run. RSS category feeds carry
months of backlog, so the first ingest pulls a lot of history; later runs see
far fewer stale postings and the mix shifts.

### Two matching rules, both learned from the data

**Word boundaries, never substrings.** `LIKE '%Go%'` claims 173 of 211 postings
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
jobfit score --limit 5      # start small; this one costs money
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
`why_not` on a score of 70 or above, the score stands but confidence is
downgraded to `low` and the event is logged.

### What a run costs

Measured against 39 surviving postings with an average description of ~1,040
tokens and a ~2,900-token cached prefix:

**Measured**, scoring 39 real postings on Sonnet 5:

| | tokens | note |
|---|---|---|
| Cache reads | 193,762 | 38 of 39 calls, billed at ~10% |
| Cache writes | 5,099 | once, the first call |
| Uncached input | 57,253 | the posting text, ~1,470 per posting |
| Output | 23,653 | ~606 per posting |
| **Total** | | **$0.615**, or ~$0.31 with the Batch API |

That is nearly double an earlier estimate, and the reason is worth recording:
output tokens ran ~600 per posting rather than the ~300 guessed, because honest
`why_fit` and `why_not` bullets are not short. Output is billed at 5x input, so
it dominates. Caching worked exactly as designed — one write, 38 reads — and the
cached prefix is only ~72% of the input, so the funnel shape and the Batch API
are the bigger levers. Which model is good enough is an eval question, not a guess;
the client is injected, so swapping it is one line.

`prompt_version` — a hash of the cached prefix — is stored with every score, so
editing the rubric or the CV is visible in the database instead of silently
mixing two generations of results.

## The queue

```bash
jobfit queue                      # threshold 70 by default
jobfit queue --threshold 60       # widen it
jobfit queue --day 2026-08-22     # re-render a specific day
```

Two artifacts, neither of which touches the database — re-running is always safe.

**`queue/YYYY-MM-DD.md`** — ranked, best first, designed to be read top to bottom
in about thirty minutes. Every entry carries what a skip-or-apply decision needs:

```markdown
## 88 — Acme — Senior Full Stack Engineer

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
       (SELECT count(*) FROM scores WHERE fit_score >= 70) AS queued;
-- 211 | 39 | 39 | 16

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
| The queue cutoff | `jobfit queue --threshold N` |

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
| `score` fails on authentication | `ANTHROPIC_API_KEY` is unset or wrong. A Claude Code or Claude.ai subscription is **not** API access. |
| Queue is empty but postings scored | Nothing cleared the threshold. Check the prefilter first — an over-aggressive filter looks identical to a quiet day. |
| `sum(cache_read_tokens)` is 0 | Prompt caching broke. Run the tests: three of them exist specifically to catch this. |

## Development

```bash
pip install -e . && pip install pytest
pytest                       # 93 tests, no network, no API calls, no tokens
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
jobfit label                  # appends unlabelled postings to evals/labeled.jsonl
$EDITOR evals/labeled.jsonl   # fill in: apply | skip | borderline, plus one line of reason
jobfit eval                   # offline — measures stored scores against your labels
jobfit eval --note "widened stack aliases"   # also logs a row to evals/results.md
```

`jobfit eval` never calls the API. It compares scores already in the database
against your labels, so the loop costs nothing to re-run and the whole suite
works on a plane.

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
stored scores come from more than one rubric version.

## What is deliberately not here

- **No web UI or dashboard.** Config is YAML, the CV is markdown, the output is
  a file. Everything you would configure changes monthly at most.
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
