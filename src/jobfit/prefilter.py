"""Stage 2 — prefilter.

Deterministic rules that kill obvious non-matches before spending tokens. No
network, no model, no cost. Reads `postings`, writes `prefilter_verdicts`, and
touches nothing else.

Every rejection stores a `rejected_reason` and the exact phrase that triggered
it, because the failure mode of a prefilter is invisible: a rule that is too
aggressive silently deletes good postings and nothing downstream ever knows they
existed. The reason column is how you audit that.

Two matching rules, both learned from real feed data:

- Phrases match on word boundaries, never as substrings. `LIKE '%Go%'` claimed
  173 of 211 real postings required Go, because "going" and "Google" exist.
- A few junior signals ("junior", "graduate") appear innocently in senior
  postings — "you will mentor junior engineers" — so they only count in titles.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

import yaml

log = logging.getLogger("jobfit.prefilter")

# Words that are ambiguous in a job description but decisive in a title.
TITLE_ONLY_SIGNALS = {"junior", "graduate"}

# From the SPEC: the filter should cut most of the volume, but a filter that
# cuts almost everything is a bug, not a win.
MIN_CUT_RATIO = 0.50
MAX_CUT_RATIO = 0.85


@dataclass(frozen=True)
class Profile:
    stack: dict[str, list[str]]
    title_exclusions: list[str]
    junior_signals: list[str]
    location_exclusions: list[str]
    location_allowlist: list[str]
    max_age_days: int


@dataclass
class Verdict:
    rejected_reason: str | None = None
    detail: str = ""
    stack_hits: list[str] = field(default_factory=list)


@dataclass
class RunSummary:
    evaluated: int
    survived: int
    by_reason: dict[str, int]

    @property
    def cut_ratio(self) -> float:
        return 0.0 if not self.evaluated else 1 - self.survived / self.evaluated


# --- matching ----------------------------------------------------------------


@lru_cache(maxsize=2048)
def _pattern(phrase: str) -> re.Pattern[str]:
    """Whole-phrase, case-insensitive, boundary-anchored.

    Lookarounds rather than `\\b` so phrases that end in punctuation still
    anchor correctly — `next.js` has no word boundary after the `s` in some
    contexts, and `\\b` would also mis-handle a leading digit in `0-2 years`.
    """
    return re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)


def matches(phrase: str, text: str) -> bool:
    return bool(text) and _pattern(phrase).search(text) is not None


def first_match(phrases: list[str], text: str) -> str | None:
    return next((p for p in phrases if matches(p, text)), None)


def stack_hits(profile: Profile, text: str) -> list[str]:
    """Names of the stack groups this posting mentions at least once."""
    return sorted(
        name for name, aliases in profile.stack.items()
        if any(matches(alias, text) for alias in aliases)
    )


# --- the rules ---------------------------------------------------------------
#
# Ordered cheapest-to-verify first. The first rule that fires is the one
# reported, so when auditing a rejection you get the reason that takes the least
# effort to confirm by eye.


def evaluate(posting: dict, profile: Profile, now: str) -> Verdict:
    title = posting["title"] or ""
    body = posting["description_text"] or ""
    location = posting["location_raw"] or ""
    haystack = f"{title}\n{body}"

    age_days = (datetime.fromisoformat(now) - datetime.fromisoformat(posting["published_at"])).days
    if age_days > profile.max_age_days:
        return Verdict("stale", f"published {age_days} days ago")

    excluded_title = first_match(profile.title_exclusions, title) or _people_manager(title)
    if excluded_title:
        return Verdict("title_excluded", f"title matches '{excluded_title}'")

    junior = _junior_signal(profile, title, body)
    if junior:
        return Verdict("junior", f"matches '{junior}'")

    ineligible = _location_signal(profile, location, body)
    if ineligible:
        return Verdict("location_ineligible", f"matches '{ineligible}'")

    hits = stack_hits(profile, haystack)
    if not hits:
        return Verdict("no_stack_overlap", "no configured stack keyword appears")

    return Verdict(stack_hits=hits)


def _people_manager(title: str) -> str | None:
    """People-management titles are out; the engineering lead track is in.

    Too general to express as a phrase list — "Manager, Government Compliance &
    Authorization" survives every literal exclusion while "Engineering Manager"
    must not be caught by a bare `manager` entry.
    """
    if not matches("manager", title):
        return None
    if any(matches(marker, title) for marker in ("engineer", "engineering")):
        return None
    return "manager (no engineering marker)"


def _junior_signal(profile: Profile, title: str, body: str) -> str | None:
    in_title = first_match(profile.junior_signals, title)
    if in_title:
        return in_title
    unambiguous = [s for s in profile.junior_signals if s.lower() not in TITLE_ONLY_SIGNALS]
    return first_match(unambiguous, body)


def _location_signal(profile: Profile, location: str, body: str) -> str | None:
    """A posting explicitly open to the world is eligible, full stop.

    The structured region field is far more reliable than prose, so an
    allowlisted value short-circuits the phrase scan. Otherwise a posting open
    to "Anywhere in the World" gets killed because its boilerplate happens to
    mention clearance work on another team.
    """
    if first_match(profile.location_allowlist, location):
        return None
    return first_match(profile.location_exclusions, location) or first_match(
        profile.location_exclusions, body
    )


# --- the run -----------------------------------------------------------------


def run(conn: sqlite3.Connection, profile: Profile, now: str) -> RunSummary:
    """Evaluate every stored posting. Safe to re-run; verdicts are replaced."""
    postings = conn.execute(
        "SELECT id, title, description_text, location_raw, published_at FROM postings"
    ).fetchall()

    by_reason: dict[str, int] = {}
    survived = 0
    for posting in postings:
        verdict = evaluate(posting, profile, now)
        if verdict.rejected_reason:
            by_reason[verdict.rejected_reason] = by_reason.get(verdict.rejected_reason, 0) + 1
        else:
            survived += 1
        conn.execute(
            """INSERT INTO prefilter_verdicts
                   (posting_id, rejected_reason, detail, stack_hits_json, evaluated_at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(posting_id) DO UPDATE SET
                   rejected_reason = excluded.rejected_reason,
                   detail          = excluded.detail,
                   stack_hits_json = excluded.stack_hits_json,
                   evaluated_at    = excluded.evaluated_at""",
            (posting["id"], verdict.rejected_reason, verdict.detail,
             json.dumps(verdict.stack_hits), now),
        )
    conn.commit()
    return RunSummary(evaluated=len(postings), survived=survived, by_reason=by_reason)


# --- config and CLI ----------------------------------------------------------


def load_profile(path: str) -> Profile:
    raw = yaml.safe_load(Path(path).read_text())
    return Profile(
        stack={group["name"]: group["aliases"] for group in raw["stack"]},
        title_exclusions=raw["title_exclusions"],
        junior_signals=[str(s) for s in raw["junior_signals"]],
        location_exclusions=raw["location_exclusions"],
        location_allowlist=raw["location_allowlist"],
        max_age_days=int(raw["max_age_days"]),
    )


def report(summary: RunSummary) -> str:
    lines = [f"{summary.evaluated:>5}  postings evaluated"]
    for reason, count in sorted(summary.by_reason.items(), key=lambda kv: -kv[1]):
        lines.append(f"{-count:>5}  {reason}")
    lines.append(f"{summary.survived:>5}  survive  ({summary.cut_ratio:.0%} cut)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage 2 — prefilter stored postings.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--profile", default="profile/stack.yaml")
    parser.add_argument("--db", help="override db_path from the config")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )
    from jobfit import ingest  # shared connect(); never calls the ingest stage itself

    config = yaml.safe_load(Path(args.config).read_text())
    profile = load_profile(args.profile)
    conn = ingest.connect(args.db or config["db_path"])
    try:
        summary = run(conn, profile, ingest.iso_now())
    finally:
        conn.close()

    print(report(summary))

    # The SPEC's own tripwire: a filter outside this band is mistuned, and the
    # cost of finding that out later is either wasted tokens or lost postings.
    if summary.evaluated and not MIN_CUT_RATIO <= summary.cut_ratio <= MAX_CUT_RATIO:
        log.error(
            "cut ratio %.0f%% is outside the %.0f–%.0f%% band — the rules need tuning",
            summary.cut_ratio * 100, MIN_CUT_RATIO * 100, MAX_CUT_RATIO * 100,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
