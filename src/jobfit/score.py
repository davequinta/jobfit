"""Stage 3 — score.

One structured call per posting. No agent loop: scoring is classification with a
fixed input shape — no branching, no state carried between postings, no step
where the model decides what to do next. An agent here would add latency, cost
and nondeterminism and buy nothing.

The prompt shape is load-bearing and is enforced by tests:

    system: [ rubric + CV + stack profile ]   ← cache_control breakpoint
    user:   [ the posting ]

Everything before the breakpoint is byte-identical across every call in a run,
so it is cached and billed at ~10% on every posting after the first. Moving CV
content into the user message, or letting any per-posting text in before the
breakpoint, breaks that silently — no error, roughly 10x the cost. `tests/
test_score.py` asserts the system block is identical for two different postings,
which is the cheapest possible guard against that.

The client is injected rather than constructed here. That keeps every test
offline, and it is the seam where a different provider or a local model plugs in
without touching the rubric or the storage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from jobfit import ingest

log = logging.getLogger("jobfit.score")

TEMPLATES = Path(__file__).parent / "templates"

# Where a tuned rubric lives once `jobfit init` has copied it out. The rubric is
# a plain markdown file so it versions and diffs like code — prompt changes are
# the changes most likely to move the eval numbers.
LOCAL_RUBRIC = Path("prompts/score_system.md")

# Sonnet, not Opus: this is classification against an explicit rubric, not
# open-ended reasoning. Whether Haiku is good enough is an eval question, not a
# guess — swap it here and re-run `pytest evals/` to find out.
MODEL = "claude-sonnet-5"

# Enough for the bullets in the schema and nothing more. The output is small and
# bounded; a large ceiling here only buys a bigger bill on a runaway response.
MAX_TOKENS = 2048

# Classification against a written rubric does not need deep reasoning, and
# effort is the biggest lever on per-posting cost.
EFFORT = "low"


class Score(BaseModel):
    """The scorer's output. Mirrors the schema in SPEC.md."""

    fit_score: int = Field(ge=0, le=100)
    confidence: Literal["high", "medium", "low"]
    seniority_match: Literal["below", "match", "above"]
    stack_overlap: list[str]
    stack_gaps: list[str]
    ai_role_signal: bool
    location_eligible: bool
    comp_range: str | None
    why_fit: list[str]
    why_not: list[str]
    red_flags: list[str]


@dataclass(frozen=True)
class Rubric:
    """Everything that goes before the cache breakpoint.

    Frozen and assembled in a fixed order because its bytes are the cache key.
    """

    instructions: str  # prompts/score_system.md
    cv: str            # profile/cv.md
    stack: str         # the stack names from profile/stack.yaml


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class ScoreResult:
    score: Score
    usage: Usage
    prompt_version: str


# --- the prompt --------------------------------------------------------------


def system_text(rubric: Rubric) -> str:
    """Assemble the cached prefix. Deterministic order, no interpolation."""
    return (
        f"{rubric.instructions.rstrip()}\n\n"
        f"# Candidate CV\n\n{rubric.cv.strip()}\n\n"
        f"# Candidate stack profile\n\n{rubric.stack.strip()}\n"
    )


def prompt_version(rubric: Rubric) -> str:
    """Short hash of the cached prefix, stored alongside every score.

    A rubric edit changes this, so two generations of scores never silently mix
    in the database — which is what makes `evals/results.md` trustworthy.
    """
    return hashlib.sha256(system_text(rubric).encode("utf-8")).hexdigest()[:12]


def user_text(posting) -> str:
    """The only part that varies per call. Everything here is after the cache.

    Indexes with `[]` rather than `.get()` so a `sqlite3.Row` works: the CLI
    reads postings with `SELECT p.*`, and Row has no `.get()`.
    """
    fields = [
        f"Company: {posting['company']}",
        f"Title: {posting['title']}",
        f"Location: {posting['location_raw'] or 'not stated'}",
        f"Salary: {posting['salary_raw'] or 'not stated'}",
        f"URL: {posting['url']}",
        "",
        posting["description_text"],
    ]
    return "\n".join(fields)


def build_request(rubric: Rubric, posting: dict, cache_ttl: str | None = None) -> dict:
    """The complete kwargs for `client.messages.parse`.

    `cache_ttl="1h"` is for the batch path: batch requests can sit in the queue
    longer than the 5-minute default, so the entry has to outlive the wait.
    """
    cache_control = {"type": "ephemeral"}
    if cache_ttl:
        cache_control["ttl"] = cache_ttl

    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "output_config": {"effort": EFFORT},
        "system": [
            {"type": "text", "text": system_text(rubric), "cache_control": cache_control}
        ],
        "messages": [{"role": "user", "content": user_text(posting)}],
        "output_format": Score,
    }


# --- guardrails --------------------------------------------------------------


def apply_guardrails(score: Score) -> Score:
    """`why_not` is mandatory; an empty one on a high score is a failure signal.

    The rubric tells the model every posting has something wrong with it. When
    it returns nothing anyway on a posting it rates highly, the result is not
    trustworthy — so the score stands but the confidence does not.
    """
    if score.why_not or score.fit_score < 70:
        return score
    log.warning(
        "empty why_not on a score of %d — downgrading confidence to low", score.fit_score
    )
    return score.model_copy(update={"confidence": "low"})


# --- the synchronous path ----------------------------------------------------


def score_posting(client, rubric: Rubric, posting: dict, cache_ttl: str | None = None) -> ScoreResult:
    """Score one posting. Used for development and for the eval loop.

    The nightly run uses the Batches API instead — same request shape, 50% of
    the price, no latency requirement because the queue is read in the morning.
    """
    response = client.messages.parse(**build_request(rubric, posting, cache_ttl))
    usage = Usage(
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
        cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
    )
    return ScoreResult(
        score=apply_guardrails(response.parsed_output),
        usage=usage,
        prompt_version=prompt_version(rubric),
    )


# --- storage -----------------------------------------------------------------


def store_score(conn: sqlite3.Connection, posting_id: int, result: ScoreResult, now: str) -> None:
    score = result.score
    conn.execute(
        """INSERT INTO scores (
               posting_id, fit_score, confidence, seniority_match, stack_overlap_json,
               stack_gaps_json, ai_role_signal, location_eligible, comp_range,
               why_fit_json, why_not_json, red_flags_json, model, prompt_version,
               input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, scored_at
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(posting_id) DO UPDATE SET
               fit_score = excluded.fit_score,
               confidence = excluded.confidence,
               seniority_match = excluded.seniority_match,
               stack_overlap_json = excluded.stack_overlap_json,
               stack_gaps_json = excluded.stack_gaps_json,
               ai_role_signal = excluded.ai_role_signal,
               location_eligible = excluded.location_eligible,
               comp_range = excluded.comp_range,
               why_fit_json = excluded.why_fit_json,
               why_not_json = excluded.why_not_json,
               red_flags_json = excluded.red_flags_json,
               model = excluded.model,
               prompt_version = excluded.prompt_version,
               input_tokens = excluded.input_tokens,
               output_tokens = excluded.output_tokens,
               cache_read_tokens = excluded.cache_read_tokens,
               cache_write_tokens = excluded.cache_write_tokens,
               scored_at = excluded.scored_at""",
        (
            posting_id, score.fit_score, score.confidence, score.seniority_match,
            json.dumps(score.stack_overlap), json.dumps(score.stack_gaps),
            int(score.ai_role_signal), int(score.location_eligible), score.comp_range,
            json.dumps(score.why_fit), json.dumps(score.why_not), json.dumps(score.red_flags),
            MODEL, result.prompt_version, result.usage.input_tokens,
            result.usage.output_tokens, result.usage.cache_read_tokens,
            result.usage.cache_write_tokens, now,
        ),
    )
    conn.commit()


# --- config ------------------------------------------------------------------


def resolve_rubric_path(explicit: str | None) -> str:
    """An explicit path, else a tuned local rubric, else the bundled default.

    An installed user has no `prompts/` directory until they run `init`, and
    dying on a missing file that only exists in the source repository is a poor
    first run. Which rubric was used is logged, never assumed.
    """
    if explicit:
        return explicit
    if LOCAL_RUBRIC.is_file():
        return str(LOCAL_RUBRIC)
    return str(TEMPLATES / "score_system.md")


def require_files(paths: dict[str, str]) -> list[str]:
    """Return human-readable complaints for every missing input."""
    return [
        f"{path} is missing — {why}"
        for path, why in paths.items()
        if not Path(path).is_file()
    ]


def load_rubric(instructions_path: str, cv_path: str, stack_path: str) -> Rubric:
    """Read the three cached inputs off disk.

    The CV is read at run time rather than baked in, so editing `profile/cv.md`
    and re-running is all it takes to re-score against a new version of it.
    """
    stack = yaml.safe_load(Path(stack_path).read_text())
    stack_summary = "\n".join(
        f"- {group['name']}: {', '.join(group['aliases'])}" for group in stack["stack"]
    )
    return Rubric(
        instructions=Path(instructions_path).read_text(),
        cv=Path(cv_path).read_text(),
        stack=stack_summary,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 3 — score postings that survived stage 2.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--profile", default="profile/stack.yaml")
    parser.add_argument("--cv", default="profile/cv.md")
    parser.add_argument("--rubric", help="defaults to prompts/score_system.md, else the bundled rubric")
    parser.add_argument("--db", help="override db_path from the config")
    parser.add_argument("--limit", type=int, help="score at most N postings (development)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )
    rubric_path = resolve_rubric_path(args.rubric)
    problems = require_files({
        args.config: "run `jobfit init` to create it",
        args.profile: "run `jobfit init` to create it",
        args.cv: "run `jobfit init` to scaffold it, then replace it with your CV",
        rubric_path: "run `jobfit init` to create it",
    })
    if problems:
        for problem in problems:
            print(f"jobfit score: {problem}", file=sys.stderr)
        return 2

    import anthropic

    config = yaml.safe_load(Path(args.config).read_text())
    log.info("rubric %s", rubric_path)
    rubric = load_rubric(rubric_path, args.cv, args.profile)
    client = anthropic.Anthropic()
    conn = ingest.connect(args.db or config["db_path"])

    pending = conn.execute(
        """SELECT p.* FROM postings p
             JOIN prefilter_verdicts v ON v.posting_id = p.id
            WHERE v.rejected_reason IS NULL
            ORDER BY p.published_at DESC"""
    ).fetchall()
    if args.limit:
        pending = pending[: args.limit]

    log.info("scoring %d postings with %s (prompt %s)", len(pending), MODEL,
             prompt_version(rubric))
    scored = 0
    try:
        for posting in pending:
            result = score_posting(client, rubric, posting)
            store_score(conn, posting["id"], result, ingest.iso_now())
            scored += 1
            log.info("%3d  %-24.24s %s", result.score.fit_score,
                     posting["company"], posting["title"][:44])
    finally:
        conn.close()

    log.info("scored %d of %d postings", scored, len(pending))
    return 0 if scored == len(pending) else 1


if __name__ == "__main__":
    raise SystemExit(main())
