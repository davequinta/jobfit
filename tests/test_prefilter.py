"""Stage 2 tests. Offline: the rules are pure functions over a posting row.

Every rejection rule gets a test that proves it fires AND a test that proves it
does not fire on something it must let through. A prefilter that is too
aggressive is worse than no prefilter, because the postings it kills are never
seen again.
"""

from pathlib import Path

import pytest

from jobfit import db, ingest, prefilter, sources

PROFILE = prefilter.Profile(
    stack={
        "python": ["python", "django", "fastapi"],
        "react": ["react", "next.js", "nextjs"],
        "cloud": ["aws", "terraform"],
    },
    title_exclusions=["intern", "product manager", "designer", "sales"],
    junior_signals=["junior", "entry level", "0-2 years"],
    location_exclusions=["us citizens only", "security clearance", "europe only"],
    location_allowlist=["anywhere in the world", "worldwide", "latam"],
    max_age_days=14,
)

NOW = "2026-08-21T12:00:00+00:00"


def posting(**overrides) -> dict:
    base = {
        "id": 1,
        "title": "Senior Full Stack Engineer",
        "company": "Acme",
        "location_raw": "Anywhere in the World",
        "description_text": "We are looking for a senior engineer with Python and React experience.",
        "published_at": "2026-08-20T09:00:00+00:00",
    }
    return {**base, **overrides}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


# --- the happy path ----------------------------------------------------------


def test_a_matching_senior_posting_survives():
    verdict = prefilter.evaluate(posting(), PROFILE, NOW)

    assert verdict.rejected_reason is None
    assert sorted(verdict.stack_hits) == ["python", "react"]


# --- age ---------------------------------------------------------------------


def test_rejects_a_posting_older_than_the_age_limit():
    verdict = prefilter.evaluate(
        posting(published_at="2026-07-01T09:00:00+00:00"), PROFILE, NOW
    )

    assert verdict.rejected_reason == "stale"


def test_keeps_a_posting_exactly_at_the_age_limit():
    verdict = prefilter.evaluate(
        posting(published_at="2026-08-07T12:00:00+00:00"), PROFILE, NOW
    )

    assert verdict.rejected_reason is None


# --- titles ------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Product Manager",
        "Senior Graphic Designer",
        "Inside Sales Contractor",
        "Engineering Intern",
    ],
)
def test_rejects_non_engineering_titles(title):
    assert prefilter.evaluate(posting(title=title), PROFILE, NOW).rejected_reason == "title_excluded"


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Staff Engineer, Platform",
        "Engineering Manager",       # manager WITH engineer stays; it is the lead track
        "Principal Full Stack Developer",
    ],
)
def test_keeps_engineering_titles(title):
    assert prefilter.evaluate(posting(title=title), PROFILE, NOW).rejected_reason is None


def test_title_exclusions_match_whole_words_only():
    # "designer" must not knock out a role that merely mentions design systems.
    verdict = prefilter.evaluate(
        posting(title="Senior Frontend Engineer, Design Systems"), PROFILE, NOW
    )

    assert verdict.rejected_reason is None


# --- seniority ---------------------------------------------------------------


def test_rejects_junior_titles():
    verdict = prefilter.evaluate(posting(title="Junior Software Engineer"), PROFILE, NOW)

    assert verdict.rejected_reason == "junior"


def test_rejects_junior_signals_in_the_description():
    verdict = prefilter.evaluate(
        posting(description_text="An entry level role using Python and React."), PROFILE, NOW
    )

    assert verdict.rejected_reason == "junior"


def test_does_not_reject_a_senior_role_that_mentions_mentoring_juniors():
    verdict = prefilter.evaluate(
        posting(
            title="Senior Backend Engineer",
            description_text=(
                "You will mentor junior engineers on the team. Python and AWS required."
            ),
        ),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason is None


# --- location ----------------------------------------------------------------


def test_rejects_a_posting_that_excludes_non_citizens():
    verdict = prefilter.evaluate(
        posting(location_raw="USA", description_text="Python role. US citizens only."),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason == "location_ineligible"


def test_an_allowlisted_location_beats_a_phrase_elsewhere_in_the_body():
    # WWR's structured region field is far more reliable than prose. A posting
    # explicitly open to the world does not get killed because the boilerplate
    # mentions a security clearance programme.
    verdict = prefilter.evaluate(
        posting(
            location_raw="Anywhere in the World",
            description_text="Python and React. We also staff security clearance projects.",
        ),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason is None


def test_rejects_a_region_restricted_to_another_continent():
    verdict = prefilter.evaluate(
        posting(location_raw="Europe only", description_text="Python and React."),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason == "location_ineligible"


# --- stack -------------------------------------------------------------------


def test_rejects_a_posting_with_no_stack_overlap():
    verdict = prefilter.evaluate(
        posting(description_text="Seeking a Rust and Elixir engineer for our core team."),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason == "no_stack_overlap"
    assert verdict.stack_hits == []


def test_stack_matching_uses_word_boundaries():
    # The lesson from real data: LIKE '%Go%' claimed 173 of 211 postings needed
    # Go. "going" is not a stack hit, and neither is "Google".
    verdict = prefilter.evaluate(
        posting(description_text="We are going to Google our way to a great product."),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason == "no_stack_overlap"


def test_stack_matching_handles_dotted_aliases():
    verdict = prefilter.evaluate(
        posting(description_text="Frontend role building with Next.js and Tailwind."),
        PROFILE,
        NOW,
    )

    assert verdict.stack_hits == ["react"]


# --- rule ordering -----------------------------------------------------------


def test_the_first_matching_rule_wins_so_the_reason_is_the_cheapest_one():
    # Stale AND a product manager AND ineligible. The reported reason is the one
    # that is cheapest to verify by hand when auditing the filter.
    verdict = prefilter.evaluate(
        posting(
            title="Senior Product Manager",
            published_at="2026-01-01T09:00:00+00:00",
            location_raw="Europe only",
        ),
        PROFILE,
        NOW,
    )

    assert verdict.rejected_reason == "stale"


# --- storage -----------------------------------------------------------------


def test_run_writes_a_verdict_for_every_posting(conn):
    run_id = ingest.start_run(conn, NOW)
    stored = sources.parse_wwr(
        (Path(__file__).parent / "fixtures" / "wwr_ok.rss").read_bytes(),
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "weworkremotely",
    ).postings
    ingest.store(conn, stored, run_id, NOW)

    summary = prefilter.run(conn, PROFILE, NOW)

    assert summary.evaluated == 3
    rows = conn.execute("SELECT posting_id, rejected_reason FROM prefilter_verdicts").fetchall()
    assert len(rows) == 3
    assert summary.survived + sum(1 for r in rows if r["rejected_reason"]) == 3


def test_run_is_idempotent(conn):
    run_id = ingest.start_run(conn, NOW)
    ingest.store(conn, [_fake_posting()], run_id, NOW)

    first = prefilter.run(conn, PROFILE, NOW)
    second = prefilter.run(conn, PROFILE, NOW)

    assert (first.evaluated, second.evaluated) == (1, 1)
    assert conn.execute("SELECT count(*) AS n FROM prefilter_verdicts").fetchone()["n"] == 1


def test_run_reports_the_funnel_by_reason(conn):
    run_id = ingest.start_run(conn, NOW)
    ingest.store(
        conn,
        [
            _fake_posting(key="a", title="Senior Software Engineer"),
            _fake_posting(key="b", title="Senior Product Manager"),
            _fake_posting(key="c", title="Junior Software Engineer"),
        ],
        run_id,
        NOW,
    )

    summary = prefilter.run(conn, PROFILE, NOW)

    assert summary.evaluated == 3
    assert summary.survived == 1
    assert summary.by_reason == {"title_excluded": 1, "junior": 1}


def _fake_posting(key: str = "x", **overrides) -> sources.Posting:
    fields = {
        "dedupe_key": key,
        "source": "test",
        "source_id": None,
        "feed_url": "https://example.com/feed",
        "url": f"https://example.com/{key}",
        "canonical_url": f"https://example.com/{key}",
        "company": "Acme",
        "title": "Senior Software Engineer",
        "location_raw": "Anywhere in the World",
        "category": None,
        "job_type": None,
        "tags": [],
        "salary_raw": None,
        "description_html": None,
        "description_text": "Python and React and AWS.",
        "published_at": "2026-08-20T09:00:00+00:00",
        "raw_json": "{}",
    }
    return sources.Posting(**{**fields, **overrides})


# --- the "manager without engineer" rule -------------------------------------
#
# The SPEC asks for people-management titles to go while the engineering lead
# track stays. Real feed data has "Manager, Government Compliance &
# Authorization" surviving every phrase-based rule, which is what forced this.


def test_rejects_a_manager_title_with_no_engineering_marker():
    verdict = prefilter.evaluate(
        posting(title="Manager, Government Compliance & Authorization"), PROFILE, NOW
    )

    assert verdict.rejected_reason == "title_excluded"
    assert "manager" in verdict.detail


@pytest.mark.parametrize(
    "title",
    ["Engineering Manager", "Senior Manager, Software Engineering", "Engineer Manager"],
)
def test_keeps_manager_titles_on_the_engineering_track(title):
    assert prefilter.evaluate(posting(title=title), PROFILE, NOW).rejected_reason is None


# --- the band judges the rules, not the size of the archive -------------------


def test_the_cut_band_ignores_postings_the_feeds_no_longer_carry(conn):
    """The database keeps everything it has ever seen, and a posting from two
    months ago is stale forever. Counting those makes the cut ratio climb toward
    100% as the archive grows, until the band fails on every run no matter how
    good the rules are. It judges what the feeds are offering now.
    """
    old_run = ingest.start_run(conn, "2026-06-01T09:00:00+00:00")
    archived = sources.parse_wwr(
        (Path(__file__).parent / "fixtures" / "wwr_ok.rss").read_bytes(),
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "weworkremotely",
    ).postings
    ingest.store(conn, archived, old_run, "2026-06-01T09:00:00+00:00")

    # A later ingest that carried nothing the old one did.
    ingest.start_run(conn, NOW)

    summary = prefilter.run(conn, PROFILE, NOW)

    assert summary.evaluated == 3          # everything still gets a verdict
    assert summary.live_evaluated == 0     # but none of it is on offer today
    assert summary.live_cut_ratio == 0.0


def test_with_no_ingest_run_recorded_the_band_falls_back_to_everything(conn):
    """A database built by hand, or by a test, still gets judged."""
    run_id = ingest.start_run(conn, NOW)
    stored = sources.parse_wwr(
        (Path(__file__).parent / "fixtures" / "wwr_ok.rss").read_bytes(),
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "weworkremotely",
    ).postings
    ingest.store(conn, stored, run_id, NOW)

    summary = prefilter.run(conn, PROFILE, NOW)

    assert summary.live_evaluated == 3
    assert summary.live_cut_ratio == summary.cut_ratio
