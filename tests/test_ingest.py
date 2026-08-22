"""Stage 1 tests. Every one of these runs offline against recorded fixtures.

The fixtures in tests/fixtures/ are real responses from Remotive and We Work
Remotely with the long description bodies truncated. The `_drift` variants are
the same responses with a field renamed or emptied — they exist so that a feed
changing shape is a red test, not a silent zero-posting run.
"""

import json
import sqlite3
from pathlib import Path

import pytest

from jobfit import ingest

FIXTURES = Path(__file__).parent / "fixtures"

REMOTIVE_URL = "https://remotive.com/api/remote-jobs?category=software-development"
WWR_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

NOW = "2026-08-21T12:00:00+00:00"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FixtureFetcher:
    """Stands in for the real HTTP client. Raises for any URL not recorded."""

    def __init__(self, responses: dict[str, bytes | Exception]):
        self.responses = responses
        self.calls: list[str] = []

    def get(self, url: str) -> bytes:
        self.calls.append(url)
        body = self.responses[url]
        if isinstance(body, Exception):
            raise body
        return body


@pytest.fixture
def conn():
    connection = ingest.connect(":memory:")
    yield connection
    connection.close()


# --- identity and dedupe -----------------------------------------------------


def test_dedupe_key_ignores_cosmetic_differences_in_company_and_url():
    a = ingest.dedupe_key(
        "  Acme,  Inc. ", "Senior Backend Engineer",
        "https://Example.com/jobs/123/?utm_source=rss&ref=x",
    )
    b = ingest.dedupe_key(
        "acme inc", "senior backend engineer",
        "https://example.com/jobs/123",
    )
    assert a == b


def test_dedupe_key_separates_different_roles_at_the_same_company():
    a = ingest.dedupe_key("Acme", "Senior Backend Engineer", "https://example.com/a")
    b = ingest.dedupe_key("Acme", "Staff Backend Engineer", "https://example.com/b")
    assert a != b


# --- Remotive ----------------------------------------------------------------


def test_parse_remotive_normalizes_a_real_record():
    result = ingest.parse_remotive(fixture("remotive_ok.json"), REMOTIVE_URL, "remotive")

    assert result.issues == []
    assert len(result.postings) == 3

    p = result.postings[0]
    assert p.company == "Shatterproof"
    assert p.title == "Vice President, Technology & Digital Strategy"
    assert p.url.startswith("https://remotive.com/remote-jobs/")
    assert p.source == "remotive"
    assert p.source_id == "2091104"
    assert p.published_at == "2026-08-20T09:54:55+00:00"
    assert p.location_raw == "USA"
    assert p.salary_raw == "175k - 190k"
    assert "cloud" in p.tags
    assert "<p>" not in p.description_text
    assert "Shatterproof" in p.description_text
    # The upstream record is kept verbatim so normalization can be redone
    # without re-fetching.
    assert json.loads(p.raw_json)["company_name"] == "Shatterproof"


def test_parse_remotive_drops_a_record_missing_a_required_field_and_says_so():
    result = ingest.parse_remotive(
        fixture("remotive_drift.json"), REMOTIVE_URL, "remotive"
    )

    assert len(result.postings) == 2
    assert len(result.issues) == 1
    issue = result.issues[0]
    assert issue.kind == "missing_field"
    assert "company_name" in issue.detail
    assert issue.sample  # the offending record travels with the issue


def test_parse_remotive_reports_an_empty_feed_rather_than_returning_nothing():
    body = json.dumps({"job-count": 0, "jobs": []}).encode()
    result = ingest.parse_remotive(body, REMOTIVE_URL, "remotive")

    assert result.postings == []
    assert [i.kind for i in result.issues] == ["empty_feed"]


def test_parse_remotive_reports_unparseable_json_as_a_parse_error():
    result = ingest.parse_remotive(b"<html>502 Bad Gateway</html>", REMOTIVE_URL, "remotive")

    assert result.postings == []
    assert [i.kind for i in result.issues] == ["parse_error"]


# --- We Work Remotely --------------------------------------------------------


def test_parse_wwr_splits_the_company_out_of_the_title():
    result = ingest.parse_wwr(fixture("wwr_ok.rss"), WWR_URL, "weworkremotely")

    assert result.issues == []
    assert len(result.postings) == 3

    p = result.postings[0]
    assert p.company == "Coinbase"
    assert p.title == "Senior Software Engineer, Backend (Consumer - Risk)"
    assert p.url == (
        "https://weworkremotely.com/remote-jobs/"
        "coinbase-senior-software-engineer-backend-consumer-risk"
    )
    assert p.published_at == "2026-07-23T07:03:54+00:00"
    assert p.location_raw == "Anywhere in the World"
    assert p.category == "Back-End Programming"
    assert "&lt;p&gt;" not in p.description_text
    assert "<p>" not in p.description_text


def test_parse_wwr_drops_items_that_break_the_company_title_convention():
    result = ingest.parse_wwr(fixture("wwr_drift.rss"), WWR_URL, "weworkremotely")

    assert len(result.postings) == 1
    kinds = sorted(i.kind for i in result.issues)
    assert kinds == ["missing_field", "unparsed_title"]


# --- storage -----------------------------------------------------------------


def test_store_inserts_postings_and_assigns_them_to_the_run(conn):
    run_id = ingest.start_run(conn, NOW)
    postings = ingest.parse_wwr(fixture("wwr_ok.rss"), WWR_URL, "weworkremotely").postings

    result = ingest.store(conn, postings, run_id, NOW)

    assert (result.inserted, result.duplicates) == (3, 0)
    rows = conn.execute("SELECT company, first_seen_run FROM postings ORDER BY id").fetchall()
    assert [r["company"] for r in rows] == ["Coinbase", "Coinbase", "Twilio"]
    assert {r["first_seen_run"] for r in rows} == {run_id}


def test_store_skips_postings_seen_in_an_earlier_run(conn):
    postings = ingest.parse_wwr(fixture("wwr_ok.rss"), WWR_URL, "weworkremotely").postings
    first = ingest.start_run(conn, "2026-08-20T12:00:00+00:00")
    ingest.store(conn, postings, first, "2026-08-20T12:00:00+00:00")

    second = ingest.start_run(conn, NOW)
    result = ingest.store(conn, postings, second, NOW)

    assert (result.inserted, result.duplicates) == (0, 3)
    assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 3
    row = conn.execute("SELECT first_seen_at, last_seen_at FROM postings LIMIT 1").fetchone()
    assert row["first_seen_at"] == "2026-08-20T12:00:00+00:00"
    assert row["last_seen_at"] == NOW


# --- the run -----------------------------------------------------------------


def test_ingest_stores_both_sources_and_reports_ok(conn):
    feeds = [
        ingest.Feed(name="remotive", kind="remotive_json", url=REMOTIVE_URL),
        ingest.Feed(name="weworkremotely", kind="rss", url=WWR_URL),
    ]
    fetcher = FixtureFetcher(
        {REMOTIVE_URL: fixture("remotive_ok.json"), WWR_URL: fixture("wwr_ok.rss")}
    )

    summary = ingest.ingest(conn, feeds, fetcher, NOW)

    assert summary.status == "ok"
    assert (summary.inserted, summary.duplicates, summary.dropped) == (6, 0, 0)
    assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 6
    sources = conn.execute("SELECT DISTINCT source FROM postings ORDER BY source").fetchall()
    assert [r["source"] for r in sources] == ["remotive", "weworkremotely"]


def test_ingest_keeps_going_when_one_feed_is_down(conn):
    feeds = [
        ingest.Feed(name="remotive", kind="remotive_json", url=REMOTIVE_URL),
        ingest.Feed(name="weworkremotely", kind="rss", url=WWR_URL),
    ]
    fetcher = FixtureFetcher(
        {REMOTIVE_URL: RuntimeError("connect timeout"), WWR_URL: fixture("wwr_ok.rss")}
    )

    summary = ingest.ingest(conn, feeds, fetcher, NOW)

    # The healthy source still lands...
    assert summary.inserted == 3
    # ...and the dead one is loud, not silent.
    assert summary.status == "degraded"
    issues = conn.execute("SELECT source, kind, detail FROM ingest_issues").fetchall()
    assert [(r["source"], r["kind"]) for r in issues] == [("remotive", "fetch_error")]
    assert "connect timeout" in issues[0]["detail"]


def test_ingest_marks_the_run_degraded_when_a_feed_changes_shape(conn):
    feeds = [ingest.Feed(name="remotive", kind="remotive_json", url=REMOTIVE_URL)]
    fetcher = FixtureFetcher({REMOTIVE_URL: fixture("remotive_drift.json")})

    summary = ingest.ingest(conn, feeds, fetcher, NOW)

    # 1 of 3 records dropped is over the tolerance, so the run fails loudly
    # even though two postings were stored.
    assert summary.inserted == 2
    assert summary.dropped == 1
    assert summary.status == "degraded"
    assert summary.exit_code == 1


def test_ingest_records_the_run_row(conn):
    feeds = [ingest.Feed(name="weworkremotely", kind="rss", url=WWR_URL)]
    fetcher = FixtureFetcher({WWR_URL: fixture("wwr_ok.rss")})

    summary = ingest.ingest(conn, feeds, fetcher, NOW)

    row = conn.execute("SELECT * FROM ingest_runs WHERE id = ?", (summary.run_id,)).fetchone()
    assert row["status"] == "ok"
    assert row["started_at"] == NOW
    assert row["finished_at"] is not None
    assert (row["fetched"], row["inserted"], row["duplicates"]) == (3, 3, 0)


# --- politeness --------------------------------------------------------------


def test_robots_blocks_a_disallowed_path():
    robots = "User-agent: *\nDisallow: /api/*\n"
    assert not ingest.robots_allows(robots, "https://example.com/api/jobs", "jobfit/0.1")
    assert ingest.robots_allows(robots, "https://example.com/jobs.rss", "jobfit/0.1")


def test_robots_allows_everything_when_the_file_is_missing():
    assert ingest.robots_allows(None, "https://example.com/anything", "jobfit/0.1")


def test_rate_limiter_waits_between_requests_to_the_same_host():
    clock = _FakeClock()
    limiter = ingest.RateLimiter(min_interval=1.0, monotonic=clock.now, sleep=clock.sleep)

    limiter.wait("example.com")
    limiter.wait("example.com")

    assert clock.slept == [pytest.approx(1.0)]


def test_rate_limiter_does_not_delay_a_different_host():
    clock = _FakeClock()
    limiter = ingest.RateLimiter(min_interval=1.0, monotonic=clock.now, sleep=clock.sleep)

    limiter.wait("example.com")
    limiter.wait("other.com")

    assert clock.slept == []


class _FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds
