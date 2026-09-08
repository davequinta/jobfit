"""Stage 1 — ingest.

Pulls postings from configured feeds, normalizes them to one shape, dedupes, and
stores them in SQLite. Reads nothing from other stages and writes nothing they
own; `python src/ingest.py` is always safe to re-run.

Two rules drive most of the code here:

1. No silent fallbacks. A record we cannot normalize is dropped, counted, and
   written to `ingest_issues` with a sample of the payload. A feed that is down
   does not stop the other feeds, but it does make the run exit non-zero.
2. The upstream record is stored verbatim in `postings.raw_json`. Normalization
   is a guess about someone else's schema; keeping the original means a wrong
   guess costs a re-parse rather than a re-crawl.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jobfit.db import connect, iso_now
from jobfit.http import PoliteFetcher
from jobfit.sources import PARSERS, Issue, Posting, normalize_field

from jobfit import runtime

log = logging.getLogger("jobfit.ingest")

# A feed that drops more than this share of its records has probably changed
# shape rather than had a bad day. Above it, the run is degraded.
MAX_DROP_RATIO = 0.2



# --- shapes ------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    name: str          # source name stored on the posting
    kind: str          # see PARSERS
    url: str
    pages: int = 1     # paginated sources fetch url&page=1..pages
    robots_exemption: str = ""  # non-empty = deliberately ignore robots.txt, with a reason


@dataclass
class StoreResult:
    inserted: int
    duplicates: int
    republished: list = field(default_factory=list)


@dataclass
class RunSummary:
    run_id: int
    status: str
    fetched: int
    inserted: int
    duplicates: int
    dropped: int

    @property
    def exit_code(self) -> int:
        return 0 if self.status == "ok" else 1


# --- source-specific plumbing ------------------------------------------------


def latest_hn_thread(fetcher) -> str:
    """URL of the most recent "Who is hiring" thread.

    The thread is monthly and its id changes, so it is discovered rather than
    configured — a hard-coded id silently goes stale after four weeks.
    """
    listing = json.loads(fetcher.get(
        "https://hn.algolia.com/api/v1/search_by_date"
        "?tags=story,author_whoishiring&hitsPerPage=10"))
    for hit in listing["hits"]:
        if "who is hiring" in (hit.get("title") or "").lower():
            return f"https://hn.algolia.com/api/v1/items/{hit['objectID']}"
    raise ValueError("no 'Who is hiring' thread found in the last 10 whoishiring stories")


def company_resolver(conn: sqlite3.Connection, fetcher, source: str):
    """Look up Get on Board company names, caching them across runs.

    The API exposes company only as a relationship id and supports no `include`,
    so a first run costs one request per unique company. Caching means every
    later run costs almost none.
    """
    def resolve(company_id) -> str | None:
        row = conn.execute(
            "SELECT name FROM source_companies WHERE source = ? AND source_id = ?",
            (source, str(company_id)),
        ).fetchone()
        if row:
            return row["name"]
        try:
            payload = json.loads(
                fetcher.get(f"https://www.getonbrd.com/api/v0/companies/{company_id}"))
            name = payload["data"]["attributes"]["name"]
        except Exception as exc:
            log.warning("%s: could not resolve company %s — %s", source, company_id, exc)
            return None
        conn.execute(
            "INSERT OR REPLACE INTO source_companies (source, source_id, name, fetched_at)"
            " VALUES (?,?,?,?)", (source, str(company_id), name, iso_now()))
        conn.commit()
        return name

    return resolve


def fetch_feed(fetcher, feed: Feed) -> bytes:
    """One feed's body. Paginated sources come back as a single merged payload."""
    if feed.kind == "hn_hiring":
        return fetcher.get(latest_hn_thread(fetcher))
    if feed.pages <= 1:
        return fetcher.get(feed.url)

    separator = "&" if "?" in feed.url else "?"
    merged: list = []
    for page in range(1, feed.pages + 1):
        payload = json.loads(fetcher.get(f"{feed.url}{separator}page={page}"))
        records = payload["data"] if isinstance(payload, dict) else payload
        if not records:
            break
        merged.extend(records)
    return json.dumps({"data": merged}).encode()


def start_run(conn: sqlite3.Connection, now: str) -> int:
    cursor = conn.execute("INSERT INTO ingest_runs (started_at) VALUES (?)", (now,))
    conn.commit()
    return cursor.lastrowid


def store(conn: sqlite3.Connection, postings: list[Posting], run_id: int, now: str) -> StoreResult:
    """Insert new postings; bump `last_seen_at` on ones we already have.

    A posting that is already stored is left otherwise untouched — that is what
    "seen before is skipped, not re-scored" means downstream.

    Two things count as already stored. The dedupe key catches the same URL
    seen again. A second check catches the same job republished under a new
    one: boards reissue a listing at `...-ai` and then `...-ai-1`, which is a
    different canonical URL and therefore a different key, and thirteen of the
    first 846 postings were one job twice. Matching on source, company and
    title instead is a judgement — two roles really can share a title — so
    every merge is reported rather than made quietly.
    """
    stored_rows = conn.execute(
        "SELECT dedupe_key, source, company, title, url FROM postings").fetchall()
    known_keys = {row["dedupe_key"] for row in stored_rows}
    known = {
        (row["source"], normalize_field(row["company"]), normalize_field(row["title"])):
            row["url"]
        for row in stored_rows
    }

    inserted = 0
    republished: list[Issue] = []
    for posting in postings:
        republish = (posting.source, normalize_field(posting.company),
                     normalize_field(posting.title))
        # Only a new URL for a job already stored is news. The same URL seen
        # again is an ordinary second sighting, and reporting those would file
        # hundreds of issues on every run.
        if posting.dedupe_key not in known_keys and republish in known:
            republished.append(Issue(
                posting.source, posting.feed_url, "republished",
                f"{posting.company} — {posting.title}: {posting.url} is already "
                f"stored as {known[republish]}",
            ))
            conn.execute(
                "UPDATE postings SET last_seen_at = ? WHERE url = ?",
                (now, known[republish]),
            )
            continue

        cursor = conn.execute(
            """
            INSERT INTO postings (
                dedupe_key, source, source_id, feed_url, url, canonical_url,
                company, title, location_raw, category, job_type, tags_json,
                salary_raw, description_html, description_text, published_at,
                raw_json, first_seen_at, last_seen_at, first_seen_run
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(dedupe_key) DO NOTHING
            """,
            (
                posting.dedupe_key, posting.source, posting.source_id, posting.feed_url,
                posting.url, posting.canonical_url, posting.company, posting.title,
                posting.location_raw, posting.category, posting.job_type,
                json.dumps(posting.tags), posting.salary_raw, posting.description_html,
                posting.description_text, posting.published_at, posting.raw_json,
                now, now, run_id,
            ),
        )
        if cursor.rowcount == 1:
            inserted += 1
        else:
            # Already stored. Only the sighting timestamp moves, so downstream
            # stages never see a posting change under them.
            conn.execute(
                "UPDATE postings SET last_seen_at = ? WHERE dedupe_key = ?",
                (now, posting.dedupe_key),
            )
        if cursor.rowcount == 1:
            known[republish] = posting.url
            known_keys.add(posting.dedupe_key)
    conn.commit()
    return StoreResult(inserted=inserted, duplicates=len(postings) - inserted,
                       republished=republished)


def record_issues(conn: sqlite3.Connection, run_id: int, issues: list[Issue], now: str) -> None:
    conn.executemany(
        """INSERT INTO ingest_issues (run_id, source, feed_url, kind, detail, sample, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        [(run_id, i.source, i.feed_url, i.kind, i.detail, i.sample, now) for i in issues],
    )
    conn.commit()


# --- the run -----------------------------------------------------------------


def _parse(feed: Feed, body: bytes, resolve_company) -> ParseResult:
    """Dispatch to the feed's parser, passing the extras only that parser needs."""
    parser = PARSERS[feed.kind]
    if feed.kind == "getonbrd_json":
        return parser(body, feed.url, feed.name, resolve_company=resolve_company)
    return parser(body, feed.url, feed.name)


def ingest(conn: sqlite3.Connection, feeds: list[Feed], fetcher, now: str,
           resolve_company=None) -> RunSummary:
    """Fetch every feed, store what parses, and report loudly on what does not.

    One broken feed never stops the others, but it does leave the run
    `degraded`, which the CLI turns into a non-zero exit code so a failure
    is visible without reading the database.
    """
    run_id = start_run(conn, now)
    fetched = inserted = duplicates = dropped = 0
    degraded = False

    for feed in feeds:
        if feed.robots_exemption:
            log.warning(
                "%s: fetching %s under a documented robots.txt exemption (%s)",
                feed.name, feed.url, feed.robots_exemption,
            )
        elif hasattr(fetcher, "allowed") and not fetcher.allowed(feed.url):
            log.error("%s: robots.txt disallows %s — skipping", feed.name, feed.url)
            record_issues(conn, run_id,
                          [Issue(feed.name, feed.url, "robots_blocked", "disallowed by robots.txt")], now)
            degraded = True
            continue

        try:
            body = fetch_feed(fetcher, feed)
        except Exception as exc:
            log.error("%s: fetch failed for %s — %s: %s", feed.name, feed.url, type(exc).__name__, exc)
            record_issues(conn, run_id,
                          [Issue(feed.name, feed.url, "fetch_error", f"{type(exc).__name__}: {exc}")], now)
            degraded = True
            continue

        result = _parse(feed, body, resolve_company)
        stored = store(conn, result.postings, run_id, now)
        record_issues(conn, run_id, result.issues + stored.republished, now)

        for issue in stored.republished:
            log.info("%s: %s", feed.name, issue.detail)

        feed_dropped = result.fetched - len(result.postings)
        fetched += result.fetched
        inserted += stored.inserted
        duplicates += stored.duplicates
        dropped += feed_dropped

        for issue in result.issues:
            log.error("%s: %s — %s", feed.name, issue.kind, issue.detail)

        if result.fetched == 0 or feed_dropped / result.fetched > MAX_DROP_RATIO:
            log.error(
                "%s: %d of %d records unusable from %s — treat this as a format change",
                feed.name, feed_dropped, result.fetched, feed.url,
            )
            degraded = True
        else:
            log.info(
                "%s: %d fetched, %d new, %d already seen, %d dropped",
                feed.name, result.fetched, stored.inserted, stored.duplicates, feed_dropped,
            )

    status = "degraded" if degraded else "ok"
    conn.execute(
        """UPDATE ingest_runs
              SET finished_at = ?, status = ?, fetched = ?, inserted = ?, duplicates = ?, dropped = ?
            WHERE id = ?""",
        (iso_now(), status, fetched, inserted, duplicates, dropped, run_id),
    )
    conn.commit()
    return RunSummary(run_id, status, fetched, inserted, duplicates, dropped)


# --- config and CLI ----------------------------------------------------------


def load_config(path: str) -> dict:
    config = yaml.safe_load(Path(path).read_text())
    config["feeds"] = [Feed(**feed) for feed in config["feeds"]]
    return config


def user_agent_from(config: dict) -> str:
    """Contact address comes from the environment so it stays out of the repo."""
    agent = config["user_agent"]
    contact = os.environ.get(config.get("contact_env", "JOBFIT_CONTACT"), "").strip()
    if not contact:
        log.warning(
            "%s is unset — sending a User-Agent with no contact address. "
            "Set it in .env before running against real feeds.",
            config.get("contact_env", "JOBFIT_CONTACT"),
        )
        return agent
    return f"{agent[:-1]}; {contact})" if agent.endswith(")") else f"{agent} ({contact})"


def main(argv: list[str] | None = None) -> int:
    parser = runtime.stage_parser("Stage 1 — ingest job postings into SQLite.")
    args = parser.parse_args(argv)

    runtime.configure_logging()
    config = load_config(args.config)
    fetcher = PoliteFetcher(
        user_agent=user_agent_from(config),
        min_interval=float(config.get("rate_limit_seconds", 1.0)),
    )
    conn = connect(args.db or config["db_path"])
    try:
        summary = ingest(conn, config["feeds"], fetcher, iso_now(),
                         resolve_company=company_resolver(conn, fetcher, "getonbrd"))
    finally:
        fetcher.close()
        conn.close()

    log.info(
        "run %d %s: %d fetched, %d new, %d already seen, %d dropped",
        summary.run_id, summary.status, summary.fetched,
        summary.inserted, summary.duplicates, summary.dropped,
    )
    return summary.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
