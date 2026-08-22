-- jobfit schema. Applied on every connect; every statement is IF NOT EXISTS.
--
-- Stage boundaries are enforced here, not by convention: stage 1 owns these
-- three tables and nothing else writes to them. Scores and prefilter verdicts
-- live in their own tables keyed by postings.id, so `score.py` can be re-run
-- against the same corpus without re-ingesting, and dropping the score table
-- costs nothing.

CREATE TABLE IF NOT EXISTS ingest_runs (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT    NOT NULL,          -- ISO-8601 UTC
    finished_at TEXT,
    status      TEXT    NOT NULL DEFAULT 'running',  -- running | ok | degraded
    fetched     INTEGER NOT NULL DEFAULT 0,  -- records seen in the feeds
    inserted    INTEGER NOT NULL DEFAULT 0,  -- new postings stored
    duplicates  INTEGER NOT NULL DEFAULT 0,  -- already seen, skipped
    dropped     INTEGER NOT NULL DEFAULT 0   -- records we could not normalize
);

CREATE TABLE IF NOT EXISTS postings (
    id               INTEGER PRIMARY KEY,

    -- sha256(normalize(company) + normalize(title) + canonical_url).
    -- UNIQUE is what makes "seen before is skipped, not re-scored" true.
    dedupe_key       TEXT NOT NULL UNIQUE,

    source           TEXT NOT NULL,        -- remotive | weworkremotely
    source_id        TEXT,                 -- upstream id, when the source has one
    feed_url         TEXT NOT NULL,        -- which feed this arrived on

    url              TEXT NOT NULL,        -- link to show a human
    canonical_url    TEXT NOT NULL,        -- query/fragment stripped, for dedupe

    company          TEXT NOT NULL,
    title            TEXT NOT NULL,
    location_raw     TEXT,                 -- verbatim; stage 2 interprets it
    category         TEXT,
    job_type         TEXT,
    tags_json        TEXT NOT NULL DEFAULT '[]',
    salary_raw       TEXT,

    description_html TEXT,
    description_text TEXT NOT NULL,        -- what stage 3 sends to the model
    published_at     TEXT NOT NULL,        -- ISO-8601 UTC; stage 2's 14-day rule

    -- The upstream record, verbatim. If normalization turns out to be wrong we
    -- re-derive columns from this instead of re-fetching a feed that has since
    -- rotated its postings out.
    raw_json         TEXT NOT NULL,

    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,        -- bumped when a feed re-lists it
    first_seen_run   INTEGER NOT NULL REFERENCES ingest_runs(id)
);

CREATE INDEX IF NOT EXISTS idx_postings_source       ON postings(source);
CREATE INDEX IF NOT EXISTS idx_postings_published_at ON postings(published_at);
CREATE INDEX IF NOT EXISTS idx_postings_first_seen   ON postings(first_seen_run);

-- Every record we could not use, kept with a sample of the offending payload.
-- This is the audit trail for "did the feed change shape?" — a run that stores
-- fewer postings than usual should be explainable from this table alone.
CREATE TABLE IF NOT EXISTS ingest_issues (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES ingest_runs(id),
    source     TEXT    NOT NULL,
    feed_url   TEXT,
    kind       TEXT    NOT NULL,  -- fetch_error | parse_error | missing_field
                                  -- | unparsed_title | empty_feed | robots_blocked
    detail     TEXT    NOT NULL,
    sample     TEXT,              -- truncated raw record
    created_at TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_issues_run ON ingest_issues(run_id);

-- Stage 2 (prefilter) owns this table. One verdict per posting, replaced on
-- re-run. `rejected_reason` NULL means the posting survives to scoring; the
-- non-NULL values are the audit trail for whether the filter is too aggressive.
CREATE TABLE IF NOT EXISTS prefilter_verdicts (
    posting_id      INTEGER PRIMARY KEY REFERENCES postings(id),
    rejected_reason TEXT,              -- NULL = survived
    detail          TEXT,              -- the exact phrase that triggered it
    stack_hits_json TEXT NOT NULL DEFAULT '[]',
    evaluated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_verdicts_reason ON prefilter_verdicts(rejected_reason);

-- Stage 3 (score) owns this table. One score per posting, replaced on re-score.
-- `prompt_version` is a hash of the cached system prefix (rubric + CV + stack):
-- a rubric edit changes it, so two generations of scores are never silently
-- mixed when reading results back.
CREATE TABLE IF NOT EXISTS scores (
    posting_id         INTEGER PRIMARY KEY REFERENCES postings(id),
    fit_score          INTEGER NOT NULL,
    confidence         TEXT    NOT NULL,   -- high | medium | low
    seniority_match    TEXT    NOT NULL,   -- below | match | above
    stack_overlap_json TEXT    NOT NULL,
    stack_gaps_json    TEXT    NOT NULL,
    ai_role_signal     INTEGER NOT NULL,
    location_eligible  INTEGER NOT NULL,
    comp_range         TEXT,
    why_fit_json       TEXT    NOT NULL,
    why_not_json       TEXT    NOT NULL,   -- never empty; see apply_guardrails
    red_flags_json     TEXT    NOT NULL,

    model              TEXT    NOT NULL,
    prompt_version     TEXT    NOT NULL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,            -- zero across a run means caching broke
    cache_write_tokens INTEGER,
    scored_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scores_fit ON scores(fit_score);

-- Names for sources that expose companies only as an opaque id (Get on Board).
-- Cached across runs so a first ingest pays the lookups once.
CREATE TABLE IF NOT EXISTS source_companies (
    source     TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    name       TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);
