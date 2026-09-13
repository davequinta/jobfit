# jobfit — job search pipeline

A local pipeline that ingests remote job postings, scores them against my profile,
and produces a daily review queue. It does **not** submit applications.

**Hard deadline: 2026-08-30.** Anything not in "In scope" below is out of scope,
including things that seem like a small addition. See "Explicitly out of scope."

---

## Why this exists

Applying manually to remote roles means reading ~150 postings a week to find the
15 worth applying to. The reading is the bottleneck, not the applying. This tool
does the reading.

It deliberately does not automate submission. Auto-submitted applications have
roughly 1-3% response rates and get flagged by ATS platforms as spam; the point
here is to spend the same human effort on 15 good applications instead of 150
bad ones.

---

## Core design decision: this is a workflow, not an agent

The scoring step could be built as an agent with tools that fetches the posting,
looks up the company, checks Glassdoor, and decides. It should not be.

Job scoring is a **classification task with a fixed input shape**. There is no
branching, no state that carries between postings, and no step where the model
needs to decide what to do next. An agent loop here would add latency, cost, and
nondeterminism to buy nothing. Use a single structured call per posting.

The one place agentic behavior might have earned its keep is drafting the
application answers, where the model would need to pull specifics from my CV,
the company's site, and the posting. That stage was dropped (see below), so the
claim was never tested. Worth saying plainly: the argument above stands on the
scoring stage alone and does not need the counterexample.

**This distinction is the interesting part of the project. Preserve it in the
README.**

---

## Architecture

Three stages. Each stage writes to SQLite so any stage can be re-run
independently. A fourth, `draft`, was planned and then dropped — see below.

```
ingest → prefilter → score
 (free)   (free)     (LLM)
  ~400     ~120       ~120
postings  survive    scored
```

The funnel shape matters for cost. Never send all 400 postings to the model.

*Amended 2026-09-13.* The numbers above are the design estimate. The first full
run was 846 postings → 135 surviving → 135 scored (ingested 2026-08-22, scored
2026-08-23): the prefilter cut 84%, inside its 50–85% band. After the 2026-09-08
ingest the corpus held 1,266 postings, 163 of them surviving stage 2.

### Stage 1 — ingest

Pull postings from feeds. Normalize to a common schema. Dedupe. Store raw.

Sources. *Amended 2026-09-07 to record what actually shipped.*

| Source | Access | Status |
|---|---|---|
| Remotive | Public JSON API | Shipped |
| We Work Remotely | RSS, four category feeds | Shipped — the largest contributor |
| Hacker News "Who is Hiring" | Algolia HN API | Shipped — monthly thread, top-level comments |
| Get on Board | JSON API | Shipped — LATAM-focused, needs a company lookup per new employer |
| Remote OK | Public JSON API | Shipped |
| Working Nomads | RSS | Never built. The five above already produce more volume than the funnel needs. |
| LinkedIn | — | Dropped before any code: scraping profile data is against their ToS and the account risk is not worth it. |

Six LATAM-focused platforms were assessed on 2026-09-07/08 and rejected —
Torre, BairesDev, Tecla, Revelo, Mismo, Mappa — for two different reasons.

Torre is the one worth recording. Its `robots.txt` allows `/search/jobs$` and
disallows `/search/jobs?*`: the landing page yes, every actual search no. It also
runs JSON hosts that serve no `robots.txt`, which this repo's evaluator would
treat as permission — and that is exactly why they must not be used. Fetching
through an undocumented internal endpoint what `robots.txt` forbids on the web
path is the same act with a different transport. The bar this project already
set with Remotive is a *documented* public API whose terms grant access; Torre
publishes none.

The other five fail the headless-browser rule below. BairesDev serves 276KB of
HTML with no job titles in it and Tecla 4.8KB and four scripts, so both need a
browser; Revelo's public job URLs are SEO templates for employers rather than
openings; Mismo is a consultancy site, not a board; the Mappa domain assessed
turned out to be an unrelated e-commerce site.

Company ATS boards (Greenhouse, Lever, Ashby) expose public JSON per employer
and are the direction worth taking instead. Tracked in issue #1.

Rules:
- Respect `robots.txt` and set a real User-Agent with contact info.
- Rate limit to 1 req/sec per host. No parallel hammering.
- If a source needs headless browser scraping to work, **drop the source**. Not
  worth the maintenance inside the deadline.

Dedupe key: `sha256(normalize(company) + normalize(title) + canonical_url)`.
Postings seen before are skipped, not re-scored.

### Stage 2 — prefilter (deterministic, no LLM)

Cheap rules that kill obvious non-matches before spending tokens. Each posting
gets a `rejected_reason` so I can audit whether the filter is too aggressive.

Reject if:
- Location/eligibility text excludes the Americas or requires US work
  authorization. Match on phrases like "US citizens only", "must reside in",
  "EU only", "requires security clearance".
- Title matches an exclusion list (e.g. `intern`, `manager` without `engineer`,
  `product manager`, `sales`, `recruiter`, `designer`). Product management is
  the single largest non-engineering category in the real feed data — 40+ of the
  first 211 postings — so it earns its own rule rather than hiding under
  `manager`.
- Seniority signals junior (`junior`, `entry level`, `graduate`, `0-2 years`).
- Posting is older than 14 days. On real data this is the heaviest single rule:
  RSS category feeds carry months of backlog, so it alone cuts ~45%.
- Required stack has zero overlap with my stack list. Match on word boundaries,
  never substrings — a naive `LIKE '%Go%'` matches "going" and "Google" and
  claims 173 of 211 postings need Go.

Target: cut ~70% of volume. If it cuts less than 50% or more than 85%, the rules
need tuning.

### Stage 3 — score (LLM, the core)

One API call per surviving posting. Structured JSON output.

**Model:** `claude-sonnet-5` (shipped; this line said `claude-sonnet-4-6` until
2026-09-07, while the code had always called Sonnet 5 — it is the current
generation and cheaper, $2/$10 per MTok against $3/$15). Do not use Opus here — the task is classification
against an explicit rubric, not open-ended reasoning.

**Prompt structure — this ordering is required for caching to work:**

```
system: [ RUBRIC + FULL CV + STACK PROFILE ]  ← cache_control breakpoint here
user:   [ job posting text ]
```

Everything before the breakpoint is identical across every call in a run, so it
gets cached. Cache reads bill at 10% of normal input price. My CV plus rubric
should comfortably exceed the 2,048-token minimum Sonnet requires for caching; if
it doesn't, pad the rubric with worked examples rather than trimming it.

**Use the Message Batches API for the nightly run.** Scoring 120 postings has no
latency requirement — the queue gets read in the morning. Batch bills all usage
at 50% of standard price and stacks with prompt caching. For batches, use the
1-hour cache duration rather than the 5-minute default, since batch requests can
take longer than 5 minutes to process.

Keep a synchronous single-posting path too, for development and for the eval loop.

**Output schema:**

```json
{
  "fit_score": 0-100,
  "confidence": "high" | "medium" | "low",
  "seniority_match": "below" | "match" | "above",
  "stack_overlap": ["python", "aws", ...],
  "stack_gaps": ["kubernetes", ...],
  "ai_role_signal": true,
  "location_eligible": true,
  "comp_range": "string or null",
  "why_fit": ["3 bullets, each citing specific posting language"],
  "why_not": ["1-3 bullets, honest"],
  "red_flags": ["equity-only", "unpaid trial", ...]
}
```

`why_not` is mandatory and must be non-empty. A scorer that only rationalizes
matches is useless. If the model returns an empty `why_not` on a high score,
treat it as a low-confidence result.

**Target roles:** remote Senior Software Engineer and Senior Full Stack, mostly
with US companies. Not AI engineer, not ML engineer. LLM and agentic work is a
differentiator that breaks ties between two otherwise equal postings — it is not
what makes a posting a match. Weighting it as though it were surfaces
AI-specialist roles and buries good senior full stack ones.

**Rubric weights** (tune after the eval set exists, do not guess and forget):
- Stack overlap with Python/Django/FastAPI, React/Next/TS, AWS — 35
- Seniority at senior/lead/staff/principal — 25
- Location eligibility for LATAM or worldwide — 20
- LLM/agent/AI-product work in the role — 10 (bonus, not a requirement)
- Company stage and comp signals — 10

### Stage 4 — draft — dropped 2026-09-07

Cut permanently, not deferred. It was to generate, for everything above the
threshold, a cover-letter opener referencing something specific in the posting
plus answers to the five recurring screening questions.

Why it goes rather than waits: nothing yet measures whether the postings that
would reach it are the right ones — the eval set exists but is unlabelled. A
generator stacked on an unmeasured scorer is a second unmeasured stage, and it
makes the first one harder to fix, because a bad draft and a bad score look the
same from the queue. The queue already carries the posting, the score and the
honest `why_not` bullets, which is the part that saves the reading time; the
opener is the cheapest part of an application to write yourself.

The queue and CSV formats keep no column for it.

---

## Output

Two artifacts per run:

1. **`queue/YYYY-MM-DD.md`** — ranked review queue. Each entry: score, company,
   title, link, why_fit bullets and why_not bullets. Designed to be
   read top-to-bottom in 30 minutes over coffee.

2. **`out/applications.csv`** — append-only, importable into the tracking sheet.
   Columns: `date_found, company, role, channel, url, fit_score, status,
   applied_date, contact, next_action, next_action_date, comp_range, notes`.

`channel` is the column that matters most. Every entry must record whether it
came from LinkedIn, a direct ATS, a referral, or a DM, so response rate can be
measured per channel after four weeks and dead channels can be killed.

---

## Evals — do not skip this

This is what separates the project from a demo, and it is the part most likely to
get cut under time pressure. Do not cut it.

**Build the eval set on day 2, before tuning any prompts.**

1. Hand-label 40 postings: 15 that I would definitely apply to, 15 clear no,
   10 genuinely borderline. Store as `evals/labeled.jsonl` with my label and a
   one-line reason.
2. Run the scorer against all 40. Measure:
   - **Precision at the threshold** — of what it surfaces, how much is real?
     (Shipped at 70, measured, moved to 25 on 2026-09-07 and to 35 on
     2026-09-13 — `evals/results.md`.)
   - **Recall** — of the 15 good ones, how many did it surface?
   - **Correlation on the borderline 10** — where it disagrees with me, who's right?
3. Optimize for **precision over recall**. A false positive costs 20 minutes of
   my time and a wasted application. A false negative costs one missed posting
   out of hundreds. Target: precision > 0.8, recall > 0.6.
4. Re-run evals after every rubric change. Log the numbers in `evals/results.md`
   with the date and what changed. That log is the README's most credible content.

*Amended 2026-09-13.* The set measured on 2026-09-07 held 39 labels — 22 apply,
17 skip, none borderline — so the correlation on the borderline postings in
point 2 has never been measured. By 2026-09-13 the label file held 53 labels, 30
apply and 23 skip, still none borderline.

---

## Stack

- Python 3.11+, `uv` for deps
- `anthropic` SDK
- `httpx`, `feedparser`, `selectolax` for ingest; `protego` for robots.txt
- SQLite via stdlib `sqlite3` — no ORM
- `pydantic` for the score schema
- `pytest` for the eval harness
- Config in `config.yaml`, secrets in `.env` (gitignored)

**No web framework. No Docker. No cloud deploy.** Runs from `cron` on my laptop.

*Amended 2026-09-07.* "No frontend" held until a number proved it wrong. The
queue shipped cutting at 70 against a scorer whose real range was 3-78, and that
sat unnoticed for two weeks because a threshold in a config file is invisible.
`jobfit ui` serves one page from the standard library on 127.0.0.1 — no
framework, no dependency, no deploy, gone when you close it. The rule it was
protecting against was a hosted dashboard, and that stays out of scope.

*Amended 2026-09-12.* CI/CD stayed out of scope until a test was red on every
Python before the one on my laptop. The robots.txt matcher ignored `*` on 3.11 to
3.13 — so `Disallow: /api/*` blocked nothing — while the laptop's 3.14 parser read
it, the suite passed locally and the README said 212 tests. `.github/workflows/ci.yml`
runs the offline suite on 3.11 and 3.12 on every push: no secrets, no API calls,
no deploy. The rule was protecting against deployment machinery, and CD, Docker
and cloud deploy stay out of scope.

---

## Milestones

| Day | Deliverable | Done means |
|---|---|---|
| 1-2 | Ingest + SQLite schema | 200+ real postings in the DB from 2 sources |
| 2 | Eval set labeled | 40 postings in `evals/labeled.jsonl` |
| 3-4 | Prefilter + scorer | End-to-end run produces a scored queue |
| 5 | Eval loop + rubric tuning | **Done 2026-09-07** — precision 13 of 15 at threshold 25, logged in `evals/results.md` |
| 6 | Batch + caching path | Nightly run costs measured and logged |
| 7 | ~~Draft stage~~ + CSV export | CSV export shipped; the draft stage was dropped — see Stage 4 |
| 8-10 | README, cleanup, buffer | Repo public |

**On 2026-08-30 the repo goes public in whatever state it is in.** Ship the
README describing what works and what doesn't. An honest "stage 4 is a stub"
reads better than a polished demo with no evals.

---

## Explicitly out of scope

Listed because each of these will feel like a good idea around day 5:

- Auto-submitting applications, anywhere, under any conditions
- A hosted web UI or dashboard. (A local page, `jobfit ui`, is in — see Stack.)
- Multi-user support, auth, or anything that implies other people using it
- LinkedIn scraping (against their ToS for profile data, and the account risk is
  not worth it)
- Deployment to AWS, containerization, CD. (CI that runs the offline tests is in
  — see Stack.)
- Email/Slack notifications
- Company research enrichment (Glassdoor, Crunchbase, funding data)
- An agent loop for scoring — see the design decision above
- Drafting cover letters or screening answers — stage 4, dropped 2026-09-07
- Fine-tuning or embeddings-based matching

If one of these turns out to matter, it goes in `IDEAS.md` and gets built in
November.

---

## Constraints

- Cost target: under $5/month for a nightly run.

  *Measured 2026-09-07, and the guess above was wrong in an instructive way.*
  Scoring the 846-posting cold-start backlog — 135 survivors — cost **$1.31**,
  four times the $0.30 ceiling. But the prefilter was not the culprit: it cut
  84%, inside its band. **Output tokens were 57% of the bill**, 555 per posting,
  more than every input token combined. Postings-through-the-funnel is the lever
  the prefilter pulls; tokens-per-verdict is a lever nobody had looked at.

  Steady state is fine: about 20 new postings a day, ~16% surviving stage 2, so
  three or four scored a night — a few cents a month. The $5 target holds; the
  per-run ceiling only ever binds on a re-score of the whole corpus.

  No `out/costs.csv`. One measured run is a number in the README, not a time
  series, and a one-row CSV nothing reads is a file to maintain rather than
  evidence. If a cost regression ever needs catching, the token counters are
  already stored on every score row.
- Respect every source's robots.txt and rate limits. This repo is going to be
  public and read by people who might hire me.
- Never commit the CV, the profile, or the queue files. `profile/` and `queue/`
  are gitignored; ship `profile/*.example.md` instead.
