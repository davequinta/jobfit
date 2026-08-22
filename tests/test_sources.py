"""Parsers for the three sources added after the first two.

Same contract as Remotive and We Work Remotely: bytes in, ParseResult out, never
raises, never touches the network. Each has a `_drift` fixture so a feed
changing shape is a red test.
"""

import json
from pathlib import Path

import pytest

from jobfit import db, ingest, sources

FIXTURES = Path(__file__).parent / "fixtures"

GOB_URL = "https://www.getonbrd.com/api/v0/categories/programming/jobs?remote=true"
ROK_URL = "https://remoteok.com/api"
HN_URL = "https://hn.algolia.com/api/v1/items/49156683"


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# --- Get on Board ------------------------------------------------------------
#
# JSON:API. `company` is a relationship with only an id, and the API supports no
# `include`, so names are resolved separately and cached. The resolver is
# injected so these tests never leave the process.


def resolver(names: dict[int, str]):
    def resolve(company_id: int) -> str | None:
        return names.get(company_id)
    return resolve


def test_getonbrd_normalizes_a_real_record():
    payload = json.loads(fixture("getonbrd_ok.json"))
    ids = [j["attributes"]["company"]["data"]["id"] for j in payload["data"]]

    result = sources.parse_getonbrd(
        fixture("getonbrd_ok.json"), GOB_URL, "getonbrd",
        resolve_company=resolver({i: f"Company {i}" for i in ids}),
    )

    assert result.issues == []
    assert len(result.postings) == 3

    p = result.postings[0]
    assert p.company == f"Company {ids[0]}"
    assert p.title
    assert p.url.startswith("https://www.getonbrd.com/jobs/")
    assert p.published_at.endswith("+00:00")
    assert "<p>" not in p.description_text
    assert json.loads(p.raw_json)["attributes"]["title"] == p.title


def test_getonbrd_carries_the_structured_salary_range():
    # The one source that publishes comp as numbers rather than prose. That
    # feeds the rubric's compensation points directly.
    payload = json.loads(fixture("getonbrd_ok.json"))
    ids = [j["attributes"]["company"]["data"]["id"] for j in payload["data"]]

    result = sources.parse_getonbrd(
        fixture("getonbrd_ok.json"), GOB_URL, "getonbrd",
        resolve_company=resolver({i: "Acme" for i in ids}),
    )

    with_salary = [p for p in result.postings if p.salary_raw]
    assert with_salary, "expected at least one posting with min/max salary"
    assert any(char.isdigit() for char in with_salary[0].salary_raw)


def test_getonbrd_drops_a_record_whose_company_link_disappeared():
    result = sources.parse_getonbrd(
        fixture("getonbrd_drift.json"), GOB_URL, "getonbrd",
        resolve_company=resolver({}),
    )

    assert len(result.postings) == 0          # no name resolves
    assert all(i.kind == "missing_field" for i in result.issues)


def test_getonbrd_reports_a_company_the_resolver_could_not_name():
    payload = json.loads(fixture("getonbrd_ok.json"))
    ids = [j["attributes"]["company"]["data"]["id"] for j in payload["data"]]
    partial = {ids[0]: "Acme"}  # the other two fail to resolve

    result = sources.parse_getonbrd(
        fixture("getonbrd_ok.json"), GOB_URL, "getonbrd",
        resolve_company=resolver(partial),
    )

    assert len(result.postings) == 1
    assert len(result.issues) == 2
    assert "company" in result.issues[0].detail


# --- RemoteOK ----------------------------------------------------------------


def test_remoteok_skips_the_legal_notice_element():
    result = sources.parse_remoteok(fixture("remoteok_ok.json"), ROK_URL, "remoteok")

    # The first array element is a terms-of-service blob, not a posting.
    assert result.issues == []
    assert len(result.postings) == 3
    assert all(p.company for p in result.postings)


def test_remoteok_normalizes_a_real_record():
    result = sources.parse_remoteok(fixture("remoteok_ok.json"), ROK_URL, "remoteok")

    p = result.postings[0]
    assert p.company == "Linsco Ltd"
    assert p.title == "Estimator"
    assert p.url.startswith("https://remoteOK.com/remote-jobs/")
    assert p.published_at == "2026-08-21T15:21:35+00:00"
    assert "<br>" not in p.description_text


def test_remoteok_treats_a_zero_salary_as_not_stated():
    # The API sends 0/0 rather than null when there is no range; storing "0 - 0"
    # would let the scorer read it as a real, terrible offer.
    result = sources.parse_remoteok(fixture("remoteok_ok.json"), ROK_URL, "remoteok")

    assert all(p.salary_raw != "0 - 0" for p in result.postings)


def test_remoteok_reports_an_array_without_any_postings():
    body = json.dumps([{"legal": "terms", "last_updated": 1}]).encode()

    result = sources.parse_remoteok(body, ROK_URL, "remoteok")

    assert result.postings == []
    assert [i.kind for i in result.issues] == ["empty_feed"]


# --- Hacker News "Who is Hiring" ---------------------------------------------
#
# The messiest source and the most valuable: freeform comments, posted directly
# by the company. The convention is `Company | Role | Location | ...` on the
# first line, and a comment that does not follow it is reported rather than
# stored with the whole paragraph as a title.


def test_hn_parses_the_company_out_of_the_first_line():
    result = sources.parse_hn(fixture("hn_ok.json"), HN_URL, "hackernews")

    assert result.issues == []
    assert len(result.postings) == 4
    assert result.postings[0].company == "Snout"


def test_hn_strips_the_url_companies_put_next_to_their_name():
    result = sources.parse_hn(fixture("hn_ok.json"), HN_URL, "hackernews")

    companies = [p.company for p in result.postings]
    assert "Flywheel Motion" in companies
    assert all("http" not in company for company in companies)


def test_hn_links_to_the_comment_permalink():
    result = sources.parse_hn(fixture("hn_ok.json"), HN_URL, "hackernews")

    p = result.postings[0]
    assert p.url.startswith("https://news.ycombinator.com/item?id=")
    assert p.source_id


def test_hn_keeps_the_whole_comment_as_the_description():
    result = sources.parse_hn(fixture("hn_ok.json"), HN_URL, "hackernews")

    # One comment often advertises several roles. Splitting them is guesswork,
    # so the comment goes to the scorer whole and the title is the first line.
    assert all(len(p.description_text) > 40 for p in result.postings)
    assert all("<p>" not in p.description_text for p in result.postings)


def test_hn_reports_a_comment_that_is_not_a_job_posting():
    result = sources.parse_hn(fixture("hn_drift.json"), HN_URL, "hackernews")

    assert len(result.postings) == 3
    assert [i.kind for i in result.issues] == ["unparsed_title"]


def test_hn_reports_a_thread_with_no_comments():
    body = json.dumps({"id": 1, "title": "Ask HN: Who is hiring?", "children": []}).encode()

    result = sources.parse_hn(body, HN_URL, "hackernews")

    assert result.postings == []
    assert [i.kind for i in result.issues] == ["empty_feed"]


# --- every parser keeps the same contract ------------------------------------


@pytest.mark.parametrize(
    "parser",
    [sources.parse_remotive, sources.parse_wwr, sources.parse_remoteok, sources.parse_hn],
)
def test_every_parser_reports_garbage_instead_of_raising(parser):
    result = parser(b"<html>502 Bad Gateway</html>", "https://example.com/feed", "src")

    assert result.postings == []
    assert result.issues and result.issues[0].kind == "parse_error"


def test_the_parser_table_covers_every_configured_kind():
    # config.yaml names a kind per feed; a typo there should fail loudly at
    # startup rather than skip a source in silence.
    assert set(sources.PARSERS) == {
        "remotive_json", "rss", "getonbrd_json", "remoteok_json", "hn_hiring"
    }


# --- HN comments that open with prose ----------------------------------------
#
# Found by auditing a real run: 19 of 243 comments were dropped as "not a
# posting", and almost all of them were real jobs that simply open with a
# sentence instead of pipes. Dropping a real posting is the expensive error.


@pytest.mark.parametrize(
    "text, company",
    [
        ("Sumble is the newco from the founders of Kaggle. We are hiring full stack "
         "engineers and ai/ml engineers.", "Sumble"),
        ("Beacon AI builds intelligent systems that make aviation safer. We are hiring "
         "senior backend engineers.", "Beacon AI"),
        ("Tonic AI ( https://tonic.ai ) builds the data infrastructure behind modern AI. "
         "Hiring platform engineers.", "Tonic AI"),
        ("Open Source Security, Inc. is hiring a Rust developer to work on grsecurity.",
         "Open Source Security, Inc."),
    ],
)
def test_hn_recovers_the_company_from_a_prose_opening(text, company):
    body = json.dumps({"id": 1, "children": [
        {"id": 99, "created_at": "2026-08-10T10:00:00.000Z", "text": f"<p>{text}</p>"}]}).encode()

    result = sources.parse_hn(body, HN_URL, "hackernews")

    assert [p.company for p in result.postings] == [company]
    assert result.postings[0].title


def test_hn_still_drops_a_comment_with_no_posting_in_it():
    body = json.dumps({"id": 1, "children": [
        {"id": 1, "created_at": "2026-08-10T10:00:00.000Z", "text": "<p>[flagged]</p>"},
        {"id": 2, "created_at": "2026-08-10T10:00:00.000Z",
         "text": "<p>Is this thread still active?</p>"}]}).encode()

    result = sources.parse_hn(body, HN_URL, "hackernews")

    assert result.postings == []
    assert len(result.issues) == 2


def test_hn_company_names_stay_short_enough_to_be_a_company():
    """The prose fallback must not swallow a whole paragraph as a company."""
    long_text = "We are a company that does many things " * 10
    body = json.dumps({"id": 1, "children": [
        {"id": 3, "created_at": "2026-08-10T10:00:00.000Z", "text": f"<p>{long_text}</p>"}]}).encode()

    result = sources.parse_hn(body, HN_URL, "hackernews")

    assert all(len(p.company) <= sources.HN_MAX_COMPANY_CHARS for p in result.postings)
