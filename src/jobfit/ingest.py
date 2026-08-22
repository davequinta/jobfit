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

import argparse
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import httpx
import yaml
from selectolax.parser import HTMLParser

log = logging.getLogger("jobfit.ingest")

SCHEMA = Path(__file__).with_name("schema.sql")

# A feed that drops more than this share of its records has probably changed
# shape rather than had a bad day. Above it, the run is degraded.
MAX_DROP_RATIO = 0.2

SAMPLE_CHARS = 800


# --- shapes ------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    name: str          # source name stored on the posting
    kind: str          # remotive_json | rss
    url: str
    robots_exemption: str = ""  # non-empty = deliberately ignore robots.txt, with a reason


@dataclass
class Posting:
    dedupe_key: str
    source: str
    source_id: str | None
    feed_url: str
    url: str
    canonical_url: str
    company: str
    title: str
    location_raw: str | None
    category: str | None
    job_type: str | None
    tags: list[str]
    salary_raw: str | None
    description_html: str | None
    description_text: str
    published_at: str
    raw_json: str


@dataclass
class Issue:
    source: str
    feed_url: str | None
    kind: str
    detail: str
    sample: str | None = None


@dataclass
class ParseResult:
    postings: list[Posting] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    fetched: int = 0  # records the feed offered, including the ones we dropped


@dataclass
class StoreResult:
    inserted: int
    duplicates: int


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


# --- identity ----------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_field(value: str) -> str:
    """Casefold, drop punctuation, collapse whitespace.

    "Acme,  Inc. " and "acme inc" are the same employer as far as dedupe cares.
    """
    return _SPACE.sub(" ", _PUNCT.sub(" ", value.casefold())).strip()


def canonical_url(url: str) -> str:
    """Strip the parts of a URL that do not identify the posting.

    Feeds hand out the same posting with different tracking parameters; without
    this the dedupe key changes every time the source tweaks its UTM tags.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def dedupe_key(company: str, title: str, url: str) -> str:
    joined = f"{normalize_field(company)}|{normalize_field(title)}|{canonical_url(url)}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --- text and time -----------------------------------------------------------


def html_to_text(html: str) -> str:
    text = HTMLParser(html).text(separator="\n") if html else ""
    return _SPACE.sub(" ", text).strip()


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:  # feeds that omit a zone are UTC by convention
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def sample_of(record: object) -> str:
    text = record if isinstance(record, str) else json.dumps(record, default=str)
    return text[:SAMPLE_CHARS]


def _require(record: dict, keys: tuple[str, ...]) -> str | None:
    """Return the name of the first required key that is missing or empty."""
    for key in keys:
        if not str(record.get(key) or "").strip():
            return key
    return None


# --- parsers -----------------------------------------------------------------
#
# Both parsers take bytes and return a ParseResult. They never raise for bad
# input and never touch the network, which is what makes the whole stage
# testable against recorded fixtures.


def parse_remotive(body: bytes, feed_url: str, source: str) -> ParseResult:
    result = ParseResult()
    try:
        payload = json.loads(body)
        records = payload["jobs"]
        if not isinstance(records, list):
            raise TypeError(f"'jobs' is {type(records).__name__}, expected list")
    except Exception as exc:
        result.issues.append(
            Issue(source, feed_url, "parse_error",
                  f"{type(exc).__name__}: {exc}", sample_of(body[:SAMPLE_CHARS].decode(errors="replace")))
        )
        return result

    result.fetched = len(records)
    if not records:
        result.issues.append(
            Issue(source, feed_url, "empty_feed", "feed returned zero records")
        )
        return result

    for record in records:
        missing = _require(record, ("company_name", "title", "url", "publication_date"))
        if missing:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"record has no usable '{missing}'", sample_of(record))
            )
            continue
        try:
            published = iso_utc(datetime.fromisoformat(record["publication_date"]))
        except ValueError as exc:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"unparseable 'publication_date': {exc}", sample_of(record))
            )
            continue

        html = record.get("description") or ""
        result.postings.append(
            Posting(
                dedupe_key=dedupe_key(record["company_name"], record["title"], record["url"]),
                source=source,
                source_id=str(record["id"]) if record.get("id") is not None else None,
                feed_url=feed_url,
                url=record["url"],
                canonical_url=canonical_url(record["url"]),
                company=record["company_name"].strip(),
                title=record["title"].strip(),
                location_raw=record.get("candidate_required_location") or None,
                category=record.get("category") or None,
                job_type=record.get("job_type") or None,
                tags=[str(t) for t in record.get("tags") or []],
                salary_raw=record.get("salary") or None,
                description_html=html or None,
                description_text=html_to_text(html),
                published_at=published,
                raw_json=json.dumps(record, ensure_ascii=False),
            )
        )
    return result


def parse_wwr(body: bytes, feed_url: str, source: str) -> ParseResult:
    """We Work Remotely RSS.

    WWR has no company element; it packs the employer into the title as
    "Company: Role". That convention is the fragile part of this parser, so an
    item that does not follow it is dropped and reported rather than stored with
    the whole string as a title.
    """
    result = ParseResult()
    feed = feedparser.parse(body)
    if feed.bozo and not feed.entries:
        result.issues.append(
            Issue(source, feed_url, "parse_error",
                  f"unparseable feed: {feed.bozo_exception}",
                  sample_of(body[:SAMPLE_CHARS].decode(errors="replace")))
        )
        return result

    result.fetched = len(feed.entries)
    if not feed.entries:
        result.issues.append(
            Issue(source, feed_url, "empty_feed", "feed returned zero items")
        )
        return result

    for entry in feed.entries:
        record = {k: entry.get(k) for k in ("title", "link", "published", "region", "category", "guid")}
        missing = _require(record, ("title", "link", "published"))
        if missing:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"item has no usable '{missing}'", sample_of(record))
            )
            continue

        company, separator, title = entry["title"].partition(":")
        if not separator or not title.strip():
            result.issues.append(
                Issue(source, feed_url, "unparsed_title",
                      "title does not follow the 'Company: Role' convention",
                      sample_of(record))
            )
            continue

        try:
            published = iso_utc(parsedate_to_datetime(entry["published"]))
        except (TypeError, ValueError) as exc:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"unparseable 'pubDate': {exc}", sample_of(record))
            )
            continue

        html = entry.get("description") or entry.get("summary") or ""
        result.postings.append(
            Posting(
                dedupe_key=dedupe_key(company, title, entry["link"]),
                source=source,
                source_id=entry.get("guid") or None,
                feed_url=feed_url,
                url=entry["link"],
                canonical_url=canonical_url(entry["link"]),
                company=company.strip(),
                title=title.strip(),
                location_raw=entry.get("region") or None,
                category=entry.get("category") or None,
                job_type=entry.get("type") or None,
                tags=[],
                salary_raw=None,
                description_html=html or None,
                description_text=html_to_text(html),
                published_at=published,
                raw_json=json.dumps(dict(entry), ensure_ascii=False, default=str),
            )
        )
    return result


PARSERS = {"remotive_json": parse_remotive, "rss": parse_wwr}


# --- politeness --------------------------------------------------------------


class RateLimiter:
    """One request per host per `min_interval` seconds. No parallel fetching."""

    def __init__(self, min_interval: float = 1.0, monotonic=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        previous = self._last.get(host)
        now = self._monotonic()
        if previous is not None:
            remaining = self.min_interval - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last[host] = now


def robots_allows(robots_txt: str | None, url: str, user_agent: str) -> bool:
    """Evaluate a robots.txt body against a URL. Missing file means allowed."""
    if robots_txt is None:
        return True
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())
    return parser.can_fetch(user_agent, url)


class PoliteFetcher:
    """The real HTTP client: rate limited, identified, robots.txt aware."""

    def __init__(self, user_agent: str, min_interval: float = 1.0, timeout: float = 30.0):
        self.user_agent = user_agent
        self.limiter = RateLimiter(min_interval)
        self.client = httpx.Client(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True
        )
        self._robots: dict[str, str | None] = {}

    def allowed(self, url: str) -> bool:
        return robots_allows(self._robots_for(url), url, self.user_agent)

    def _robots_for(self, url: str) -> str | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self.limiter.wait(parts.netloc)
            try:
                response = self.client.get(f"{origin}/robots.txt")
                self._robots[origin] = response.text if response.status_code == 200 else None
            except httpx.HTTPError as exc:
                log.warning("could not read %s/robots.txt (%s); assuming allowed", origin, exc)
                self._robots[origin] = None
        return self._robots[origin]

    def get(self, url: str) -> bytes:
        self.limiter.wait(urlsplit(url).netloc)
        response = self.client.get(url)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self.client.close()


# --- storage -----------------------------------------------------------------


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    return conn


def start_run(conn: sqlite3.Connection, now: str) -> int:
    cursor = conn.execute("INSERT INTO ingest_runs (started_at) VALUES (?)", (now,))
    conn.commit()
    return cursor.lastrowid


def store(conn: sqlite3.Connection, postings: list[Posting], run_id: int, now: str) -> StoreResult:
    """Insert new postings; bump `last_seen_at` on ones we already have.

    A posting that is already stored is left otherwise untouched — that is what
    "seen before is skipped, not re-scored" means downstream.
    """
    inserted = 0
    for posting in postings:
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
    conn.commit()
    return StoreResult(inserted=inserted, duplicates=len(postings) - inserted)


def record_issues(conn: sqlite3.Connection, run_id: int, issues: list[Issue], now: str) -> None:
    conn.executemany(
        """INSERT INTO ingest_issues (run_id, source, feed_url, kind, detail, sample, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        [(run_id, i.source, i.feed_url, i.kind, i.detail, i.sample, now) for i in issues],
    )
    conn.commit()


# --- the run -----------------------------------------------------------------


def ingest(conn: sqlite3.Connection, feeds: list[Feed], fetcher, now: str) -> RunSummary:
    """Fetch every feed, store what parses, and report loudly on what does not.

    One broken feed never stops the others, but it does leave the run
    `degraded`, which the CLI turns into a non-zero exit code so a cron failure
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
            body = fetcher.get(feed.url)
        except Exception as exc:
            log.error("%s: fetch failed for %s — %s: %s", feed.name, feed.url, type(exc).__name__, exc)
            record_issues(conn, run_id,
                          [Issue(feed.name, feed.url, "fetch_error", f"{type(exc).__name__}: {exc}")], now)
            degraded = True
            continue

        result = PARSERS[feed.kind](body, feed.url, feed.name)
        stored = store(conn, result.postings, run_id, now)
        record_issues(conn, run_id, result.issues, now)

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


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    parser = argparse.ArgumentParser(description="Stage 1 — ingest job postings into SQLite.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", help="override db_path from the config")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )
    config = load_config(args.config)
    fetcher = PoliteFetcher(
        user_agent=user_agent_from(config),
        min_interval=float(config.get("rate_limit_seconds", 1.0)),
    )
    conn = connect(args.db or config["db_path"])
    try:
        summary = ingest(conn, config["feeds"], fetcher, iso_now())
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
