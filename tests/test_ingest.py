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

from jobfit import db, http, ingest, sources

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
    connection = db.connect(":memory:")
    yield connection
    connection.close()


# --- identity and dedupe -----------------------------------------------------


def test_dedupe_key_ignores_cosmetic_differences_in_company_and_url():
    a = sources.dedupe_key(
        "  Acme,  Inc. ", "Senior Backend Engineer",
        "https://Example.com/jobs/123/?utm_source=rss&ref=x",
    )
    b = sources.dedupe_key(
        "acme inc", "senior backend engineer",
        "https://example.com/jobs/123",
    )
    assert a == b


def test_dedupe_key_separates_different_roles_at_the_same_company():
    a = sources.dedupe_key("Acme", "Senior Backend Engineer", "https://example.com/a")
    b = sources.dedupe_key("Acme", "Staff Backend Engineer", "https://example.com/b")
    assert a != b


# --- Remotive ----------------------------------------------------------------


def test_parse_remotive_normalizes_a_real_record():
    result = sources.parse_remotive(fixture("remotive_ok.json"), REMOTIVE_URL, "remotive")

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
    result = sources.parse_remotive(
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
    result = sources.parse_remotive(body, REMOTIVE_URL, "remotive")

    assert result.postings == []
    assert [i.kind for i in result.issues] == ["empty_feed"]


def test_parse_remotive_reports_unparseable_json_as_a_parse_error():
    result = sources.parse_remotive(b"<html>502 Bad Gateway</html>", REMOTIVE_URL, "remotive")

    assert result.postings == []
    assert [i.kind for i in result.issues] == ["parse_error"]


# --- We Work Remotely --------------------------------------------------------


def test_parse_wwr_splits_the_company_out_of_the_title():
    result = sources.parse_wwr(fixture("wwr_ok.rss"), WWR_URL, "weworkremotely")

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
    result = sources.parse_wwr(fixture("wwr_drift.rss"), WWR_URL, "weworkremotely")

    assert len(result.postings) == 1
    kinds = sorted(i.kind for i in result.issues)
    assert kinds == ["missing_field", "unparsed_title"]


# --- storage -----------------------------------------------------------------


def test_store_inserts_postings_and_assigns_them_to_the_run(conn):
    run_id = ingest.start_run(conn, NOW)
    postings = sources.parse_wwr(fixture("wwr_ok.rss"), WWR_URL, "weworkremotely").postings

    result = ingest.store(conn, postings, run_id, NOW)

    assert (result.inserted, result.duplicates) == (3, 0)
    rows = conn.execute("SELECT company, first_seen_run FROM postings ORDER BY id").fetchall()
    assert [r["company"] for r in rows] == ["Coinbase", "Coinbase", "Twilio"]
    assert {r["first_seen_run"] for r in rows} == {run_id}


def test_store_skips_postings_seen_in_an_earlier_run(conn):
    postings = sources.parse_wwr(fixture("wwr_ok.rss"), WWR_URL, "weworkremotely").postings
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
    assert not http.robots_allows(robots, "https://example.com/api/jobs", "jobfit/0.1")
    assert http.robots_allows(robots, "https://example.com/jobs.rss", "jobfit/0.1")


def test_robots_allows_everything_when_the_file_is_missing():
    assert http.robots_allows(None, "https://example.com/anything", "jobfit/0.1")


def test_robots_a_trailing_dollar_anchors_the_end_of_the_path():
    """Torre's rule, which is why Torre is not a source: the landing page yes,
    every actual search no."""
    robots = "User-agent: *\nAllow: /search/jobs$\nDisallow: /search/jobs?*\n"
    assert http.robots_allows(robots, "https://example.com/search/jobs", "jobfit/0.1")
    assert not http.robots_allows(robots, "https://example.com/search/jobs?q=python", "jobfit/0.1")


def test_robots_a_longer_allow_beats_a_wildcard_disallow():
    robots = "User-agent: *\nDisallow: /api/*\nAllow: /api/public/jobs\n"
    assert http.robots_allows(robots, "https://example.com/api/public/jobs", "jobfit/0.1")
    assert not http.robots_allows(robots, "https://example.com/api/private", "jobfit/0.1")


def test_robots_allow_wins_a_tie_with_disallow():
    robots = "User-agent: *\nDisallow: /jobs\nAllow: /jobs\n"
    assert http.robots_allows(robots, "https://example.com/jobs", "jobfit/0.1")


def test_robots_a_group_naming_this_tool_overrides_the_wildcard_group():
    agent = "jobfit/0.1 (+https://github.com/davequinta/jobfit)"
    named_allows = "User-agent: *\nDisallow: /\n\nUser-agent: jobfit\nAllow: /\n"
    named_blocks = "User-agent: *\nAllow: /\n\nUser-agent: jobfit\nDisallow: /\n"
    assert http.robots_allows(named_allows, "https://example.com/jobs", agent)
    assert not http.robots_allows(named_blocks, "https://example.com/jobs", agent)


def test_robots_a_group_for_another_crawler_does_not_apply():
    """Get on Board and Remote OK block ClaudeBot, GPTBot and CCBot by name and
    allow everyone else. This tool is none of those crawlers."""
    robots = "User-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n"
    assert http.robots_allows(robots, "https://example.com/api", "jobfit/0.1")


class RobotsFetcher(FixtureFetcher):
    """A fixture fetcher that also answers `allowed`, from a robots.txt body."""

    def __init__(self, responses, robots_txt: str):
        super().__init__(responses)
        self.robots_txt = robots_txt

    def allowed(self, url: str) -> bool:
        return http.robots_allows(self.robots_txt, url, "jobfit/0.1")


def test_a_robots_exemption_is_what_lets_remotive_through(conn):
    """Remotive's robots.txt says `Disallow: /api/*`. Before Python 3.14 the
    standard library read `*` literally, that line blocked nothing, and the
    exemption in config.yaml was decorative. Now it is the only reason the feed
    is fetched."""
    robots = "User-agent: *\nDisallow: /api/*\n"
    exempt = ingest.Feed(name="remotive", kind="remotive_json", url=REMOTIVE_URL,
                         robots_exemption="documented public API")
    fetcher = RobotsFetcher({REMOTIVE_URL: fixture("remotive_ok.json")}, robots)
    assert not fetcher.allowed(REMOTIVE_URL)

    summary = ingest.ingest(conn, [exempt], fetcher, NOW)

    assert fetcher.calls == [REMOTIVE_URL]
    assert summary.inserted > 0


def test_without_the_exemption_a_disallowed_feed_is_skipped_and_recorded(conn):
    robots = "User-agent: *\nDisallow: /api/*\n"
    feed = ingest.Feed(name="remotive", kind="remotive_json", url=REMOTIVE_URL)
    fetcher = RobotsFetcher({REMOTIVE_URL: fixture("remotive_ok.json")}, robots)

    summary = ingest.ingest(conn, [feed], fetcher, NOW)

    assert fetcher.calls == []
    assert summary.status == "degraded"
    kinds = [row["kind"] for row in conn.execute("SELECT kind FROM ingest_issues")]
    assert kinds == ["robots_blocked"]


def test_rate_limiter_waits_between_requests_to_the_same_host():
    clock = _FakeClock()
    limiter = http.RateLimiter(min_interval=1.0, monotonic=clock.now, sleep=clock.sleep)

    limiter.wait("example.com")
    limiter.wait("example.com")

    assert clock.slept == [pytest.approx(1.0)]


def test_rate_limiter_does_not_delay_a_different_host():
    clock = _FakeClock()
    limiter = http.RateLimiter(min_interval=1.0, monotonic=clock.now, sleep=clock.sleep)

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


# --- the same job, posted twice ----------------------------------------------
#
# The dedupe key includes the canonical URL, which is what the spec asked for
# and what a board defeats by republishing the same job at `...-ai` and
# `...-ai-1`. Thirteen of the first 846 postings were the same job twice, and
# one of them reached the eval set and counted twice toward recall.


def posting(company="Huzzle", title="Full-Stack Developer", url="https://wwr/a",
            source="weworkremotely"):
    return sources.Posting(
        dedupe_key=sources.dedupe_key(company, title, url),
        source=source, feed_url=WWR_URL, url=url, canonical_url=url,
        company=company, title=title, description_text="body",
        published_at="2026-08-20T09:00:00+00:00", raw_json="{}",
    )


def test_the_same_job_republished_under_a_new_url_is_not_stored_twice(conn):
    run_id = ingest.start_run(conn, NOW)

    result = ingest.store(conn, [posting(url="https://wwr/a"),
                                 posting(url="https://wwr/a-1")], run_id, NOW)

    assert (result.inserted, result.duplicates) == (1, 1)
    assert conn.execute("SELECT count(*) AS n FROM postings").fetchone()["n"] == 1


def test_a_republish_is_reported_rather_than_silently_dropped(conn):
    """Merging two postings is a judgement call, so it leaves a record naming
    both URLs. A wrong merge should be findable, not invisible."""
    run_id = ingest.start_run(conn, NOW)

    result = ingest.store(conn, [posting(url="https://wwr/a"),
                                 posting(url="https://wwr/a-1")], run_id, NOW)

    assert [issue.kind for issue in result.republished] == ["republished"]
    detail = result.republished[0].detail
    assert "https://wwr/a-1" in detail and "https://wwr/a" in detail


def test_two_different_roles_at_one_company_are_both_kept(conn):
    run_id = ingest.start_run(conn, NOW)

    result = ingest.store(conn, [posting(title="Senior Backend Engineer"),
                                 posting(title="Senior Frontend Engineer",
                                         url="https://wwr/b")], run_id, NOW)

    assert result.inserted == 2


def test_the_same_title_at_a_different_source_is_kept(conn):
    """Two boards carrying one job are two listings with different text and
    different rules. Merging across sources is a bigger claim than this makes."""
    run_id = ingest.start_run(conn, NOW)

    result = ingest.store(conn, [posting(source="weworkremotely"),
                                 posting(source="remotive", url="https://rem/a")],
                          run_id, NOW)

    assert result.inserted == 2


def test_seeing_the_same_url_again_is_not_reported_as_a_republish(conn):
    """An ordinary second sighting is not news. Only a genuinely new URL for a
    job already stored is, or every nightly run files hundreds of issues."""
    run_id = ingest.start_run(conn, NOW)
    ingest.store(conn, [posting(url="https://wwr/a")], run_id, NOW)

    result = ingest.store(conn, [posting(url="https://wwr/a")], run_id, NOW)

    assert (result.inserted, result.duplicates) == (0, 1)
    assert result.republished == []
