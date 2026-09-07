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

### Stage 1 — ingest

Pull postings from feeds. Normalize to a common schema. Dedupe. Store raw.

Sources (implement in this order, ship with whatever works by day 3):

| Source | Access | Notes |
|---|---|---|
| Remotive | Public JSON API | Easiest, start here |
| We Work Remotely | RSS per category | `remote-programming-jobs` feed |
| Hacker News "Who is Hiring" | Algolia HN API | Monthly thread, parse top-level comments |
| Working Nomads | RSS | Lower signal, add last |

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

**Model:** `claude-sonnet-4-6`. Do not use Opus here — the task is classification
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
   - **Precision at threshold 70** — of what it surfaces, how much is real?
   - **Recall** — of the 15 good ones, how many did it surface?
   - **Correlation on the borderline 10** — where it disagrees with me, who's right?
3. Optimize for **precision over recall**. A false positive costs 20 minutes of
   my time and a wasted application. A false negative costs one missed posting
   out of hundreds. Target: precision > 0.8, recall > 0.6.
4. Re-run evals after every rubric change. Log the numbers in `evals/results.md`
   with the date and what changed. That log is the README's most credible content.

---

## Stack

- Python 3.11+, `uv` for deps
- `anthropic` SDK
- `httpx`, `feedparser`, `selectolax` for ingest
- SQLite via stdlib `sqlite3` — no ORM
- `pydantic` for the score schema
- `pytest` for the eval harness
- Config in `config.yaml`, secrets in `.env` (gitignored)

**No web framework. No frontend. No Docker. No cloud deploy.** Runs from `cron`
on my laptop. If I want a UI later that's a different project.

---

## Milestones

| Day | Deliverable | Done means |
|---|---|---|
| 1-2 | Ingest + SQLite schema | 200+ real postings in the DB from 2 sources |
| 2 | Eval set labeled | 40 postings in `evals/labeled.jsonl` |
| 3-4 | Prefilter + scorer | End-to-end run produces a scored queue |
| 5 | Eval loop + rubric tuning | Precision > 0.8 logged in `evals/results.md` |
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
- A web UI or dashboard
- Multi-user support, auth, or anything that implies other people using it
- LinkedIn scraping (against their ToS for profile data, and the account risk is
  not worth it)
- Deployment to AWS, containerization, CI/CD
- Email/Slack notifications
- Company research enrichment (Glassdoor, Crunchbase, funding data)
- An agent loop for scoring — see the design decision above
- Drafting cover letters or screening answers — stage 4, dropped 2026-09-07
- Fine-tuning or embeddings-based matching

If one of these turns out to matter, it goes in `IDEAS.md` and gets built in
November.

---

## Constraints

- Cost target: under $5/month for a nightly run. Log actual token spend per run
  in `out/costs.csv`. If a run costs more than $0.30, something in the funnel is
  wrong — probably the prefilter letting too much through.
- Respect every source's robots.txt and rate limits. This repo is going to be
  public and read by people who might hire me.
- Never commit the CV, the profile, or the queue files. `profile/` and `queue/`
  are gitignored; ship `profile/*.example.md` instead.
