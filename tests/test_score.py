"""Stage 3 tests. Offline — a fake client returns a recorded response shape.

The tests that matter most here are not about scoring quality (that is the eval
suite's job). They are about the two things that fail *silently*:

- The prompt structure. Moving CV content into the user message, or letting any
  per-posting text in before the cache breakpoint, breaks prompt caching with no
  error and roughly 10x the cost. A test turns that into a red build.
- `why_not`. A scorer that only rationalises matches is useless, and an empty
  `why_not` on a high score is the shape that failure takes.
"""

import json
from pathlib import Path

import pytest

from jobfit import db, ingest, score

RUBRIC = score.Rubric(
    instructions="# Job posting scorer\n\nScore the posting against the profile.\n",
    cv="Jane Doe. Senior engineer. Python, Django, React, Next.js, AWS.\n",
    stack="python, typescript, react, aws\n",
)

POSTING = {
    "id": 7,
    "company": "Acme Robotics",
    "title": "Senior Full Stack Engineer",
    "location_raw": "Anywhere in the World",
    "salary_raw": "$120k - $150k",
    "description_text": "Own features end to end across Django and Next.js on AWS.",
    "url": "https://example.com/jobs/7",
}

OTHER_POSTING = {**POSTING, "id": 8, "company": "Globex", "title": "Staff Engineer",
                 "description_text": "Rust systems work on bare metal."}

RESPONSE = {
    "fit_score": 88,
    "confidence": "high",
    "seniority_match": "match",
    "stack_overlap": ["python", "react", "aws"],
    "stack_gaps": ["kubernetes"],
    "ai_role_signal": False,
    "location_eligible": True,
    "comp_range": "$120k - $150k",
    "why_fit": ["Django backend and Next.js frontend match the CV exactly"],
    "why_not": ["No team size given, so the scope of 'own end to end' is unclear"],
    "red_flags": [],
}


class FakeUsage:
    def __init__(self, **kw):
        self.input_tokens = kw.get("input_tokens", 120)
        self.output_tokens = kw.get("output_tokens", 210)
        self.cache_read_input_tokens = kw.get("cache_read_input_tokens", 4300)
        self.cache_creation_input_tokens = kw.get("cache_creation_input_tokens", 0)


class FakeResponse:
    def __init__(self, payload, usage=None):
        self.parsed_output = score.Score(**payload)
        self.usage = usage or FakeUsage()


class FakeMessages:
    def __init__(self, payload):
        self.payload = payload
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.payload)


class FakeClient:
    """Stands in for anthropic.Anthropic. Records the request it was given."""

    def __init__(self, payload=None):
        self.messages = FakeMessages(payload or RESPONSE)


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    yield connection
    connection.close()


# --- the caching invariant ---------------------------------------------------


def test_the_system_prompt_is_byte_identical_for_two_different_postings():
    """The whole reason caching works. If this fails, cost goes up ~10x."""
    a = score.build_request(RUBRIC, POSTING)
    b = score.build_request(RUBRIC, OTHER_POSTING)

    assert a["system"] == b["system"]


def test_no_posting_content_appears_before_the_cache_breakpoint():
    request = score.build_request(RUBRIC, POSTING)

    system_text = json.dumps(request["system"])
    for leaked in (POSTING["company"], POSTING["title"], POSTING["description_text"]):
        assert leaked not in system_text


def test_the_cv_stays_in_the_system_block_and_out_of_the_user_message():
    request = score.build_request(RUBRIC, POSTING)

    system_text = json.dumps(request["system"])
    assert "Jane Doe" in system_text
    assert "Jane Doe" not in request["messages"][0]["content"]


def test_the_last_system_block_carries_the_cache_breakpoint():
    request = score.build_request(RUBRIC, POSTING)

    assert request["system"][-1]["cache_control"] == {"type": "ephemeral"}


def test_the_batch_path_asks_for_the_one_hour_cache():
    # Batch requests can take longer than the 5-minute default TTL, so the
    # entry has to outlive the queue.
    request = score.build_request(RUBRIC, POSTING, cache_ttl="1h")

    assert request["system"][-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}


def test_the_user_message_carries_the_posting():
    request = score.build_request(RUBRIC, POSTING)

    user = request["messages"][0]
    assert user["role"] == "user"
    assert POSTING["title"] in user["content"]
    assert POSTING["company"] in user["content"]
    assert POSTING["description_text"] in user["content"]


def test_the_prompt_version_changes_when_the_rubric_changes():
    # Scores are stamped with this so a rubric edit is visible in the database
    # rather than silently mixing two generations of results.
    other = score.Rubric(instructions=RUBRIC.instructions + "\nExtra rule.\n",
                         cv=RUBRIC.cv, stack=RUBRIC.stack)

    assert score.prompt_version(RUBRIC) != score.prompt_version(other)


def test_the_prompt_version_is_stable_across_calls():
    assert score.prompt_version(RUBRIC) == score.prompt_version(RUBRIC)


# --- the schema --------------------------------------------------------------


def test_score_rejects_a_fit_score_outside_the_range():
    with pytest.raises(ValueError):
        score.Score(**{**RESPONSE, "fit_score": 140})


def test_score_rejects_an_unknown_confidence_value():
    with pytest.raises(ValueError):
        score.Score(**{**RESPONSE, "confidence": "very high"})


# --- the why_not guardrail ---------------------------------------------------


def test_an_empty_why_not_on_a_high_score_is_downgraded_to_low_confidence():
    raw = score.Score(**{**RESPONSE, "fit_score": 88, "confidence": "high", "why_not": []})

    guarded = score.apply_guardrails(raw)

    assert guarded.confidence == "low"


def test_an_empty_why_not_on_a_low_score_is_left_alone():
    raw = score.Score(**{**RESPONSE, "fit_score": 20, "confidence": "high", "why_not": []})

    assert score.apply_guardrails(raw).confidence == "high"


def test_a_populated_why_not_is_left_alone():
    assert score.apply_guardrails(score.Score(**RESPONSE)).confidence == "high"


# --- the synchronous path ----------------------------------------------------


def test_score_posting_returns_a_validated_score():
    client = FakeClient()

    result = score.score_posting(client, RUBRIC, POSTING)

    assert result.score.fit_score == 88
    assert result.score.why_not  # the guardrail's precondition held
    assert client.messages.calls[0]["model"] == score.MODEL


def test_score_posting_applies_the_guardrail_to_what_the_model_returned():
    client = FakeClient({**RESPONSE, "confidence": "high", "why_not": []})

    result = score.score_posting(client, RUBRIC, POSTING)

    assert result.score.confidence == "low"


def test_score_posting_reports_the_cache_usage():
    client = FakeClient()

    result = score.score_posting(client, RUBRIC, POSTING)

    # Without this the caching work is unverifiable — a silent invalidator would
    # look exactly like a working run.
    assert result.usage.cache_read_tokens == 4300
    assert result.usage.output_tokens == 210


# --- storage -----------------------------------------------------------------


def test_store_score_writes_every_field_and_the_prompt_version(conn):
    _store_posting(conn)
    result = score.score_posting(FakeClient(), RUBRIC, POSTING)

    score.store_score(conn, POSTING["id"], result, "2026-08-22T09:00:00+00:00")

    row = conn.execute("SELECT * FROM scores WHERE posting_id = ?", (POSTING["id"],)).fetchone()
    assert row["fit_score"] == 88
    assert json.loads(row["why_not_json"]) == RESPONSE["why_not"]
    assert row["prompt_version"] == score.prompt_version(RUBRIC)
    assert row["model"] == score.MODEL
    assert row["cache_read_tokens"] == 4300


def test_store_score_replaces_an_earlier_score_for_the_same_posting(conn):
    _store_posting(conn)
    first = score.score_posting(FakeClient(), RUBRIC, POSTING)
    score.store_score(conn, POSTING["id"], first, "2026-08-22T09:00:00+00:00")

    second = score.score_posting(FakeClient({**RESPONSE, "fit_score": 41}), RUBRIC, POSTING)
    score.store_score(conn, POSTING["id"], second, "2026-08-22T10:00:00+00:00")

    rows = conn.execute("SELECT fit_score FROM scores").fetchall()
    assert [r["fit_score"] for r in rows] == [41]


def _store_posting(conn) -> None:
    run_id = ingest.start_run(conn, "2026-08-22T09:00:00+00:00")
    conn.execute(
        """INSERT INTO postings (id, dedupe_key, source, feed_url, url, canonical_url,
                                 company, title, description_text, published_at, raw_json,
                                 first_seen_at, last_seen_at, first_seen_run)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (POSTING["id"], "k", "test", "https://f", POSTING["url"], POSTING["url"],
         POSTING["company"], POSTING["title"], POSTING["description_text"],
         "2026-08-20T09:00:00+00:00", "{}", "2026-08-22T09:00:00+00:00",
         "2026-08-22T09:00:00+00:00", run_id),
    )
    conn.commit()


# --- the rubric on disk ------------------------------------------------------


def test_the_shipped_rubric_is_long_enough_to_cache():
    """Below the model's minimum cacheable prefix, caching silently does nothing.

    Sonnet's minimum is 1024 tokens. Estimating conservatively at 3 characters
    per token, the shipped rubric plus a CV needs to clear that comfortably —
    the SPEC's instruction is to pad the rubric with worked examples rather than
    trim it.
    """
    rubric_md = Path(score.TEMPLATES) / "score_system.md"

    assert len(rubric_md.read_text()) > 1024 * 3


# --- the type the CLI actually passes ----------------------------------------


def test_build_request_accepts_a_sqlite_row(conn):
    """The CLI reads postings with `SELECT p.*`, which yields sqlite3.Row.

    Row supports `[]` but not `.get()`, so a dict-only test suite happily passes
    while the real command crashes on its first posting.
    """
    _store_posting(conn)
    row = conn.execute("SELECT * FROM postings WHERE id = ?", (POSTING["id"],)).fetchone()

    request = score.build_request(RUBRIC, row)

    assert POSTING["title"] in request["messages"][0]["content"]
    assert POSTING["company"] in request["messages"][0]["content"]


def test_build_request_renders_missing_optional_fields_as_not_stated(conn):
    _store_posting(conn)  # inserted without location_raw or salary_raw
    row = conn.execute("SELECT * FROM postings WHERE id = ?", (POSTING["id"],)).fetchone()

    user = score.build_request(RUBRIC, row)["messages"][0]["content"]

    assert "Location: not stated" in user
    assert "Salary: not stated" in user


# --- what an installed user gets ---------------------------------------------


def test_the_bundled_rubric_is_found_when_the_working_directory_has_none(tmp_path, monkeypatch):
    """An installed user has no `prompts/` directory until they run `init`.

    Without a bundled fallback, `jobfit score` dies on a FileNotFoundError for a
    file that only ever existed in the source repository.
    """
    monkeypatch.chdir(tmp_path)

    path = score.resolve_rubric_path(None)

    assert Path(path).is_file()
    assert "Job posting scorer" in Path(path).read_text()


def test_a_local_rubric_wins_over_the_bundled_one(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    local = tmp_path / "prompts" / "score_system.md"
    local.parent.mkdir()
    local.write_text("# my tuned rubric\n")

    # Resolved relative to the working directory, like the other defaults.
    assert Path(score.resolve_rubric_path(None)).resolve() == local.resolve()


def test_a_missing_cv_is_an_actionable_message_not_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("db_path: data/x.db\n")
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "stack.yaml").write_text("stack: []\n")

    exit_code = score.main([])

    assert exit_code == 2
    assert "profile/cv.md" in capsys.readouterr().err


# --- what a run actually pays for ---------------------------------------------
#
# `score` used to send every survivor to the model on every run, so a nightly
# run re-bought verdicts it already had. The rubric version is the natural cache
# key: same rubric and same posting means the same answer.


def _survives_stage_two(conn, reason=None) -> None:
    """`_store_posting` stores a posting but no verdict, and stage 3 only ever
    looks at postings stage 2 passed."""
    conn.execute(
        "INSERT INTO prefilter_verdicts (posting_id, rejected_reason, detail, "
        "stack_hits_json, evaluated_at) VALUES (?,?,'','[]','2026-08-22T09:00:00+00:00')",
        (POSTING["id"], reason),
    )
    conn.commit()


def test_a_posting_already_scored_under_this_rubric_is_not_scored_again(conn):
    _store_posting(conn)
    _survives_stage_two(conn)
    result = score.score_posting(FakeClient(), RUBRIC, POSTING)
    score.store_score(conn, POSTING["id"], result, "2026-08-22T09:00:00+00:00")

    pending = score.postings_to_score(conn, score.prompt_version(RUBRIC))

    assert pending == []


def test_a_posting_scored_under_an_older_rubric_is_scored_again(conn):
    """A rubric change must re-score everything, or the numbers mix two
    generations of verdict."""
    _store_posting(conn)
    _survives_stage_two(conn)
    result = score.score_posting(FakeClient(), RUBRIC, POSTING)
    score.store_score(conn, POSTING["id"], result, "2026-08-22T09:00:00+00:00")

    pending = score.postings_to_score(conn, "a-different-rubric")

    assert [row["id"] for row in pending] == [POSTING["id"]]


def test_rescore_pays_again_on_purpose(conn):
    _store_posting(conn)
    _survives_stage_two(conn)
    result = score.score_posting(FakeClient(), RUBRIC, POSTING)
    score.store_score(conn, POSTING["id"], result, "2026-08-22T09:00:00+00:00")

    pending = score.postings_to_score(conn, score.prompt_version(RUBRIC), rescore=True)

    assert [row["id"] for row in pending] == [POSTING["id"]]


def test_a_posting_rejected_by_stage_two_is_never_scored(conn):
    _store_posting(conn)
    _survives_stage_two(conn, reason="stale")

    assert score.postings_to_score(conn, score.prompt_version(RUBRIC)) == []
