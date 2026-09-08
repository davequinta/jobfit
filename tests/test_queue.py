"""Queue output tests. Offline — reads SQLite, writes two files.

This is the stage that turns a database into something a human reads over
coffee. Its failure modes are quiet: a threshold that silently hides everything,
a CSV that duplicates a row every night until the tracking sheet is useless.
"""

import csv
from pathlib import Path

import pytest

from jobfit import db, ingest, queue, score

NOW = "2026-08-22T09:00:00+00:00"

SCORED = [
    # (company, title, fit_score, why_fit, why_not, red_flags)
    ("Acme", "Senior Full Stack Engineer", 88,
     ["Django and Next.js named as the core stack"], ["No team size given"], []),
    ("Globex", "Staff Software Engineer", 74,
     ["Explicitly staff level"], ["Kubernetes depth expected"], []),
    ("Initech", "Backend Developer", 41,
     ["Python mentioned"], ["Mid-level, no seniority signal"], ["equity-only"]),
]


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    connection = db.connect(":memory:")
    run_id = ingest.start_run(connection, NOW)
    for index, (company, title, fit, why_fit, why_not, flags) in enumerate(SCORED, start=1):
        connection.execute(
            """INSERT INTO postings (id, dedupe_key, source, feed_url, url, canonical_url,
                                     company, title, location_raw, salary_raw, description_text,
                                     published_at, raw_json, first_seen_at, last_seen_at,
                                     first_seen_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (index, f"key{index}", "weworkremotely", "https://f", f"https://example.com/{index}",
             f"https://example.com/{index}", company, title, "Anywhere in the World",
             "$120k - $150k" if index == 1 else None, "description", "2026-08-20T09:00:00+00:00",
             "{}", NOW, NOW, run_id),
        )
        connection.execute(
            """INSERT INTO scores (posting_id, fit_score, confidence, seniority_match,
                   stack_overlap_json, stack_gaps_json, ai_role_signal, location_eligible,
                   comp_range, why_fit_json, why_not_json, red_flags_json, model,
                   prompt_version, input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, scored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (index, fit, "high", "match", '["python"]', '["kubernetes"]', 0, 1,
             "$120k - $150k" if index == 1 else None,
             _json(why_fit), _json(why_not), _json(flags), "claude-sonnet-5", "abc123",
             1000, 300, 2800, 0, NOW),
        )
    connection.commit()
    yield connection
    connection.close()


def _json(values: list[str]) -> str:
    import json
    return json.dumps(values)


# --- the markdown queue ------------------------------------------------------


def test_the_queue_holds_only_postings_at_or_above_the_threshold(conn):
    entries = queue.entries_above(conn, threshold=70)

    assert [e["company"] for e in entries] == ["Acme", "Globex"]


def test_the_queue_is_ranked_by_score(conn):
    entries = queue.entries_above(conn, threshold=0)

    assert [e["fit_score"] for e in entries] == [88, 74, 41]


def test_the_markdown_carries_what_a_decision_needs(conn):
    entries = queue.entries_above(conn, threshold=70)

    markdown = queue.render(entries, "2026-08-22")

    assert "88" in markdown
    assert "Acme" in markdown
    assert "https://example.com/1" in markdown
    assert "Django and Next.js named as the core stack" in markdown
    assert "No team size given" in markdown


def test_the_markdown_names_the_stage_that_is_missing(conn):
    """Stage 4 writes the opener. Until it exists, say so rather than
    silently shipping an entry that looks complete."""
    entries = queue.entries_above(conn, threshold=70)

    assert "not built" in queue.render(entries, "2026-08-22").lower()


def test_an_empty_queue_says_so_instead_of_writing_a_blank_file(conn):
    markdown = queue.render([], "2026-08-22")

    assert "no postings" in markdown.lower()


def test_write_queue_lands_on_a_dated_path(conn):
    path = queue.write_queue(conn, threshold=70, day="2026-08-22")

    assert path == Path("queue/2026-08-22.md")
    assert "Acme" in path.read_text()


# --- the tracking CSV --------------------------------------------------------


def test_the_csv_has_the_columns_the_tracking_sheet_expects(conn):
    queue.append_csv(conn, threshold=70, day="2026-08-22")

    with open("out/applications.csv", newline="") as handle:
        header = next(csv.reader(handle))

    assert header == queue.CSV_COLUMNS
    assert "channel" in header


def test_the_csv_records_where_the_posting_came_from(conn):
    queue.append_csv(conn, threshold=70, day="2026-08-22")

    rows = _csv_rows()

    # Channel is the column that decides which sources get killed after four
    # weeks, so it has to say more than "somewhere".
    assert all(row["channel"] == "job_board:weworkremotely" for row in rows)


def test_the_csv_is_append_only_across_runs(conn):
    queue.append_csv(conn, threshold=70, day="2026-08-22")
    queue.append_csv(conn, threshold=0, day="2026-08-23")

    rows = _csv_rows()

    assert [r["company"] for r in rows] == ["Acme", "Globex", "Initech"]


def test_the_csv_never_records_the_same_posting_twice(conn):
    queue.append_csv(conn, threshold=70, day="2026-08-22")
    queue.append_csv(conn, threshold=70, day="2026-08-23")

    # A nightly cron re-runs this every day against the same postings. Without
    # dedupe the tracking sheet fills with duplicates and stops being usable.
    assert len(_csv_rows()) == 2


def test_the_csv_leaves_the_human_columns_empty(conn):
    queue.append_csv(conn, threshold=70, day="2026-08-22")

    row = _csv_rows()[0]

    # These are the user's to fill in — the tool never guesses at them.
    assert row["status"] == ""
    assert row["applied_date"] == ""
    assert row["next_action"] == ""


def _csv_rows() -> list[dict]:
    with open("out/applications.csv", newline="") as handle:
        return list(csv.DictReader(handle))


# --- the command -------------------------------------------------------------


def test_main_writes_both_artifacts_and_reports_them(conn, capsys, monkeypatch):
    monkeypatch.setattr(queue.db, "connect", lambda _: conn)
    Path("config.yaml").write_text("db_path: data/x.db\n")

    exit_code = queue.main(["--day", "2026-08-22"])

    assert exit_code == 0
    assert Path("queue/2026-08-22.md").exists()
    assert Path("out/applications.csv").exists()
    out = capsys.readouterr().out
    assert "queue/2026-08-22.md" in out


def test_main_is_explicit_when_nothing_cleared_the_bar(conn, capsys, monkeypatch):
    monkeypatch.setattr(queue.db, "connect", lambda _: conn)
    Path("config.yaml").write_text("db_path: data/x.db\n")

    queue.main(["--day", "2026-08-22", "--threshold", "99"])

    assert "0 postings" in capsys.readouterr().out


def test_the_queue_command_is_reachable_from_the_cli():
    from jobfit import cli

    assert "queue" in cli.COMMANDS


def test_an_unscored_database_is_an_error_not_an_empty_queue(tmp_path, monkeypatch, capsys):
    """"Nothing cleared the bar" is a real result. "You have not scored
    anything" is a different one, and saying the first when the second is true
    sends you tuning a threshold that was never the problem."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text("db_path: empty.db\n")

    code = queue.main(["--config", "config.yaml"])

    assert code == 2
    assert "jobfit score" in capsys.readouterr().err
