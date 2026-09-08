"""Local UI tests. Offline — no socket is bound, no browser is opened.

The page's job is to let you see a cut before you commit to it, so its risks are
about agreement and side effects: numbers on screen that drift from what
`jobfit eval` reports, and a preview that quietly rewrites the database it is
supposed to be previewing. Both are tested here.
"""

import json

import pytest
import yaml

from jobfit import db, evals, ingest, prefilter, runtime, ui

NOW = "2026-08-22T09:00:00+00:00"

SCORED = [
    # (company, title, score, label)
    ("Acme", "Senior Full Stack Engineer", 88, "apply"),
    ("Globex", "Staff Software Engineer", 74, "apply"),
    ("Initech", "Backend Developer", 41, "skip"),
    ("Umbrella", "Junior Developer", 12, "skip"),
]

RULES = {
    "stack": [{"name": "python", "aliases": ["python", "django"]}],
    "title_exclusions": ["intern"],
    "junior_signals": ["junior"],
    "location_exclusions": ["us citizens only"],
    "location_allowlist": ["anywhere in the world"],
    "max_age_days": 14,
}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    run_id = ingest.start_run(connection, NOW)
    for index, (company, title, score, _) in enumerate(SCORED, start=1):
        connection.execute(
            """INSERT INTO postings (id, dedupe_key, source, feed_url, url, canonical_url,
                                     company, title, location_raw, salary_raw, description_text,
                                     published_at, raw_json, first_seen_at, last_seen_at,
                                     first_seen_run)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (index, f"key{index}", "weworkremotely", "https://f",
             f"https://example.com/{index}", f"https://example.com/{index}",
             company, title, "Anywhere in the World", None,
             "We need someone for our Django backend.", "2026-08-20T09:00:00+00:00",
             "{}", NOW, NOW, run_id),
        )
        connection.execute(
            """INSERT INTO scores (posting_id, fit_score, confidence, seniority_match,
                   stack_overlap_json, stack_gaps_json, ai_role_signal, location_eligible,
                   comp_range, why_fit_json, why_not_json, red_flags_json, model,
                   prompt_version, input_tokens, output_tokens, cache_read_tokens,
                   cache_write_tokens, scored_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (index, score, "high", "match", '["python"]', '["kubernetes"]', 0, 1, None,
             '["cites Django"]', '["no team size"]', "[]", "claude-sonnet-5", "abc123",
             1000, 300, 2800, 0, NOW),
        )
    connection.commit()
    yield connection
    connection.close()


@pytest.fixture
def labels(tmp_path):
    path = tmp_path / "labeled.jsonl"
    path.write_text("".join(
        json.dumps({"url": f"https://example.com/{index}", "label": label, "reason": ""}) + "\n"
        for index, (_, _, _, label) in enumerate(SCORED, start=1)))
    return path


@pytest.fixture
def profile_file(tmp_path):
    path = tmp_path / "stack.yaml"
    path.write_text(yaml.safe_dump(RULES))
    return path


# --- the numbers on screen must be the numbers jobfit eval reports -----------


def test_every_threshold_matches_what_the_eval_suite_computes(conn, labels):
    """The page slides a value; it must never do the arithmetic itself.

    A second implementation in JavaScript would drift from the one the eval
    tests cover, and the precision on screen would stop being the precision in
    evals/results.md.
    """
    label_map = evals.load_labels(labels)
    scores = {posting["url"]: posting["score"] for posting in ui.scored_postings(conn)}

    for row in ui.metrics_by_threshold(label_map, scores):
        expected = evals.evaluate(label_map, scores, row["threshold"])
        assert row["precision"] == expected.precision
        assert row["recall"] == expected.recall
        assert row["true_positives"] == expected.true_positives
        assert row["false_positives"] == len(expected.false_positives)


def test_the_metrics_table_covers_the_whole_slider(conn, labels):
    rows = ui.metrics_by_threshold(evals.load_labels(labels), {})

    assert [row["threshold"] for row in rows] == list(range(0, 101))


def test_state_carries_the_label_next_to_each_posting(conn, labels, profile_file):
    state = ui.build_state(conn, labels, profile_file, threshold=25)

    by_company = {posting["company"]: posting for posting in state["postings"]}
    assert by_company["Acme"]["label"] == "apply"
    assert by_company["Initech"]["label"] == "skip"
    assert state["labelled"] == 4


def test_postings_arrive_best_first(conn, labels, profile_file):
    state = ui.build_state(conn, labels, profile_file, threshold=25)

    assert [p["score"] for p in state["postings"]] == [88, 74, 41, 12]


def test_an_unlabelled_set_reports_no_metrics_rather_than_zeroes(conn, tmp_path, profile_file):
    """No labels is "nothing measured", not "measured as bad"."""
    state = ui.build_state(conn, tmp_path / "absent.jsonl", profile_file, threshold=25)

    assert state["metrics"] == []
    assert state["labelled"] == 0


# --- previewing a rule change must not change anything -----------------------


def test_previewing_rules_writes_no_verdicts(conn, profile_file):
    profile = prefilter.load_profile(profile_file)

    ui.preview_rules(conn, profile, NOW)

    verdicts = conn.execute("SELECT count(*) FROM prefilter_verdicts").fetchone()[0]
    assert verdicts == 0


def test_the_preview_agrees_with_the_stage_it_previews(conn, profile_file):
    profile = prefilter.load_profile(profile_file)

    preview = ui.preview_rules(conn, profile, NOW)
    committed = prefilter.run(conn, profile, NOW)

    assert preview["survived"] == committed.survived
    assert preview["by_reason"] == committed.by_reason
    # The page must not show a red band while the stage exits clean.
    assert preview["cut_ratio"] == committed.live_cut_ratio
    assert preview["within_expected_band"] == (
        prefilter.MIN_CUT_RATIO <= committed.live_cut_ratio <= prefilter.MAX_CUT_RATIO)


def test_a_tightened_rule_shows_a_smaller_cut_before_it_is_saved(conn, profile_file):
    loose = prefilter.load_profile(profile_file)
    strict = ui.profile_from_payload({**RULES, "title_exclusions": ["intern", "developer"]})

    assert ui.preview_rules(conn, strict, NOW)["survived"] < \
           ui.preview_rules(conn, loose, NOW)["survived"]


# --- saving ------------------------------------------------------------------


def test_saving_the_threshold_leaves_the_rest_of_the_config_alone(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"db_path": "data/jobfit.db", "threshold": 70,
                                    "feeds": [{"name": "remotive"}]}))

    ui.save_threshold(path, 25)

    saved = yaml.safe_load(path.read_text())
    assert saved["threshold"] == 25
    assert saved["db_path"] == "data/jobfit.db"
    assert saved["feeds"] == [{"name": "remotive"}]


def test_saved_rules_load_back_as_the_profile_the_stage_uses(profile_file):
    ui.save_rules(profile_file, {**RULES, "max_age_days": 7,
                                 "title_exclusions": ["intern", "recruiter"]})

    profile = prefilter.load_profile(profile_file)
    assert profile.max_age_days == 7
    assert profile.title_exclusions == ["intern", "recruiter"]
    assert profile.stack == {"python": ["python", "django"]}


# --- where the cut comes from ------------------------------------------------


class Args:
    def __init__(self, config, threshold=None):
        self.config, self.threshold = str(config), threshold


def test_an_explicit_flag_beats_the_configured_cut(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"threshold": 30}))

    assert runtime.threshold(Args(config, threshold=55)) == 55


def test_the_configured_cut_beats_the_built_in_default(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"threshold": 30}))

    assert runtime.threshold(Args(config)) == 30


def test_a_missing_key_means_the_default_not_an_error(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"db_path": "x"}))

    assert runtime.threshold(Args(config)) == runtime.DEFAULT_THRESHOLD


def test_a_malformed_cut_raises_rather_than_silently_reverting(tmp_path):
    """A typo that quietly restores the old cut is the bug this whole session
    was about. It fails loudly instead."""
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"threshold": "twenty-five"}))

    with pytest.raises(ValueError):
        runtime.threshold(Args(config))
