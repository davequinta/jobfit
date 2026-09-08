"""Eval metric tests. Pure arithmetic over labels and scores — no network.

The eval suite is the part of this project most likely to get cut under time
pressure and the part most worth keeping, so its arithmetic gets tests of its
own. A precision number that is quietly computed wrong is worse than no number:
it justifies decisions instead of informing them.
"""

import json

import pytest

from jobfit import evals


def labelled(**kw) -> dict:
    return {"url": kw.get("url", "https://example.com/1"),
            "label": kw["label"],
            "reason": kw.get("reason", "")}


ROW = {
    "url": "https://a", "company": "Acme", "title": "Senior Engineer",
    "location_raw": "Anywhere in the World", "salary_raw": "$120k - $150k",
    "published_at": "2026-08-19T09:00:00+00:00",
    "stack_hits_json": '["python", "react"]',
    "description_text": "We need someone to own our Django backend. " * 40,
}



# --- precision and recall ----------------------------------------------------


def test_precision_is_the_share_of_surfaced_postings_that_were_real():
    # 3 surfaced, 2 of them labelled apply.
    outcome = evals.evaluate(
        labels={"a": "apply", "b": "apply", "c": "skip", "d": "apply"},
        scores={"a": 90, "b": 40, "c": 80, "d": 75},
        threshold=70,
    )

    assert outcome.surfaced == 3          # a, c, d
    assert outcome.true_positives == 2    # a, d
    assert outcome.precision == pytest.approx(2 / 3)


def test_recall_is_the_share_of_real_postings_that_were_surfaced():
    outcome = evals.evaluate(
        labels={"a": "apply", "b": "apply", "c": "skip", "d": "apply"},
        scores={"a": 90, "b": 40, "c": 80, "d": 75},
        threshold=70,
    )

    assert outcome.relevant == 3          # a, b, d are labelled apply
    assert outcome.recall == pytest.approx(2 / 3)


def test_borderline_labels_are_excluded_from_precision_and_recall():
    """The borderline ten are for studying disagreement, not for scoring.

    Counting them as either class would make the headline numbers depend on a
    judgement call the label explicitly declines to make.
    """
    outcome = evals.evaluate(
        labels={"a": "apply", "b": "borderline"},
        scores={"a": 90, "b": 95},
        threshold=70,
    )

    assert outcome.surfaced == 1
    assert outcome.precision == 1.0
    assert outcome.borderline_surfaced == 1


def test_a_posting_with_no_score_is_reported_not_silently_dropped():
    outcome = evals.evaluate(
        labels={"a": "apply", "b": "apply"}, scores={"a": 90}, threshold=70
    )

    # Silently ignoring unscored postings would inflate recall.
    assert outcome.unscored == ["b"]
    assert outcome.recall == pytest.approx(0.5)


def test_precision_is_none_rather_than_zero_when_nothing_was_surfaced():
    outcome = evals.evaluate(labels={"a": "apply"}, scores={"a": 10}, threshold=70)

    # 0/0 is undefined, and reporting it as 0.0 reads as "the scorer was wrong"
    # when the truth is "the scorer said nothing".
    assert outcome.precision is None
    assert outcome.recall == 0.0


# --- honest reporting --------------------------------------------------------


def test_the_report_gives_counts_not_just_ratios():
    """With n=40 the third decimal is noise. Counts are checkable; ratios alone
    invite more confidence than the sample size supports."""
    outcome = evals.evaluate(
        labels={"a": "apply", "b": "skip", "c": "apply"},
        scores={"a": 90, "b": 80, "c": 95},
        threshold=70,
    )

    report = evals.report(outcome, threshold=70)

    assert "2 of 3" in report        # precision as a count
    assert "2 of 2" in report        # recall as a count


def test_the_report_states_the_margin_of_error_for_the_sample_size():
    outcome = evals.evaluate(
        labels={f"k{i}": "apply" for i in range(40)},
        scores={f"k{i}": 90 for i in range(40)},
        threshold=70,
    )

    report = evals.report(outcome, threshold=70)

    # A 40-item set cannot support a two-decimal claim; say so in the artifact
    # rather than hoping the reader knows.
    assert "±" in report


def test_the_report_names_the_disagreements_worth_reading():
    outcome = evals.evaluate(
        labels={"good": "apply", "bad": "skip", "missed": "apply"},
        scores={"good": 90, "bad": 88, "missed": 20},
        threshold=70,
    )

    report = evals.report(outcome, threshold=70)

    assert "bad" in report      # false positive — cost 20 minutes
    assert "missed" in report   # false negative — cost one opportunity


# --- the labelled set on disk ------------------------------------------------


def test_labels_load_from_jsonl(tmp_path):
    path = tmp_path / "labeled.jsonl"
    path.write_text(
        json.dumps(labelled(url="https://a", label="apply", reason="stack match")) + "\n"
        + json.dumps(labelled(url="https://b", label="skip", reason="junior")) + "\n"
    )

    labels = evals.load_labels(path)

    assert labels == {"https://a": "apply", "https://b": "skip"}


def test_an_unknown_label_is_rejected_loudly(tmp_path):
    path = tmp_path / "labeled.jsonl"
    path.write_text(json.dumps({"url": "https://a", "label": "maybe?"}) + "\n")

    with pytest.raises(ValueError, match="maybe"):
        evals.load_labels(path)


def test_a_blank_line_in_the_jsonl_is_skipped(tmp_path):
    path = tmp_path / "labeled.jsonl"
    path.write_text(json.dumps(labelled(url="https://a", label="apply")) + "\n\n")

    assert evals.load_labels(path) == {"https://a": "apply"}


# --- the labelling bootstrap -------------------------------------------------
#
# Hand-labelling forty postings is the step most likely to be skipped, so the
# tool has to make it cheap: emit the skeleton, fill in one word per line.


def test_label_skeleton_omits_postings_already_labelled():
    rows = [{**ROW, "url": "https://a"}, {**ROW, "url": "https://b"}]

    lines = evals.label_skeleton(rows, already={"https://a"})

    assert len(lines) == 1
    assert json.loads(lines[0])["url"] == "https://b"


def test_an_unfilled_label_is_ignored_rather_than_treated_as_a_verdict(tmp_path):
    """A skeleton line the user has not filled in yet must not silently count."""
    path = tmp_path / "labeled.jsonl"
    path.write_text(
        json.dumps({"url": "https://a", "label": "apply"}) + "\n"
        + json.dumps({"url": "https://b", "label": ""}) + "\n"
    )

    assert evals.load_labels(path) == {"https://a": "apply"}


# --- the results log ---------------------------------------------------------


def test_results_entry_records_the_numbers_and_why_they_moved():
    outcome = evals.evaluate(
        labels={"a": "apply", "b": "skip"}, scores={"a": 90, "b": 80}, threshold=70
    )

    entry = evals.results_entry(outcome, threshold=70, day="2026-08-22",
                                prompt_version="abc123", note="widened stack aliases")

    assert "2026-08-22" in entry
    assert "abc123" in entry          # which rubric produced these numbers
    assert "widened stack aliases" in entry
    assert "1 of 2" in entry


# --- labelling blind ---------------------------------------------------------


def test_the_skeleton_carries_enough_context_to_label_without_opening_the_link():
    record = json.loads(evals.label_skeleton([ROW])[0])

    assert record["company"] == "Acme"
    assert record["title"] == "Senior Engineer"
    assert record["location"] == "Anywhere in the World"
    assert record["salary"] == "$120k - $150k"
    assert record["posted"] == "2026-08-19"
    assert record["stack_seen"] == ["python", "react"]
    assert "Django backend" in record["excerpt"]


def test_the_skeleton_never_shows_the_models_verdict():
    """Labels must be independent judgement.

    Seeing "the model said 78" before deciding anchors the label, and measuring
    the model against labels it influenced is circular. Everything in the file
    is either the posting itself or deterministic keyword matching.
    """
    row = {**ROW, "fit_score": 78, "why_not_json": '["not senior enough"]',
           "confidence": "high"}

    record = json.loads(evals.label_skeleton([row])[0])

    assert "fit_score" not in record
    assert "why_not" not in record
    assert "confidence" not in record
    assert "78" not in json.dumps(record)


def test_the_excerpt_is_truncated_so_the_file_stays_scannable():
    record = json.loads(evals.label_skeleton([ROW])[0])

    assert len(record["excerpt"]) <= evals.EXCERPT_CHARS + 1  # +1 for the ellipsis


def test_a_missing_salary_reads_as_not_stated_rather_than_null():
    record = json.loads(evals.label_skeleton([{**ROW, "salary_raw": None}])[0])

    assert record["salary"] == "not stated"


def test_rewriting_keeps_labels_you_have_already_made():
    existing = [
        json.dumps({"url": "https://a", "label": "apply", "reason": "good stack"}),
        json.dumps({"url": "https://b", "label": "", "reason": ""}),
    ]

    lines = evals.rewrite_unlabelled(existing, [{**ROW, "url": "https://b"}])

    assert json.loads(lines[0])["label"] == "apply"        # untouched
    assert json.loads(lines[0])["reason"] == "good stack"
    assert json.loads(lines[1])["company"] == "Acme"       # refreshed with context
    assert json.loads(lines[1])["label"] == ""


# --- the interactive reviewer ------------------------------------------------
#
# Hand-labelling is the step this project most needs a human to do and the step
# a human is most likely to abandon. These tests are about that: nothing is
# asked twice, nothing already decided is lost, and the screen stays blind to
# what the model thought.


def record(url="https://a", label="", **kw) -> dict:
    return {"url": url, "company": "Acme", "title": "Senior Engineer",
            "location": "Anywhere in the World", "salary": "$120k - $150k",
            "posted": "2026-08-19", "stack_seen": ["python", "react"],
            "excerpt": "We need someone to own our Django backend.",
            "label": label, "reason": "", **kw}


def answers(*replies):
    """An `ask` that replies from a script, and remembers what it was asked."""
    scripted = list(replies)
    seen = []

    def ask(entry, position, total):
        seen.append(entry["url"])
        reply = scripted.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    ask.seen = seen
    return ask


def test_the_review_asks_only_about_postings_that_have_no_label_yet():
    records = [record(url="https://done", label="apply"),
               record(url="https://todo")]
    ask = answers(("skip", "wrong stack"))

    evals.review_records(records, ask, save=lambda: None)

    assert ask.seen == ["https://todo"]
    assert records[0]["label"] == "apply"      # untouched
    assert records[1]["label"] == "skip"
    assert records[1]["reason"] == "wrong stack"


def test_every_decision_is_saved_as_it_is_made():
    """Half an hour of labelling must not depend on reaching the last posting."""
    records = [record(url="https://a"), record(url="https://b")]
    saves = []
    ask = answers(("apply", ""), ("skip", ""))

    evals.review_records(records, ask, save=lambda: saves.append(
        [entry["label"] for entry in records]))

    assert saves == [["apply", ""], ["apply", "skip"]]


def test_quitting_early_keeps_the_labels_already_made():
    records = [record(url="https://a"), record(url="https://b")]
    ask = answers(("apply", "strong match"), evals.StopReview())

    done = evals.review_records(records, ask, save=lambda: None)

    assert done == 1
    assert records[0]["label"] == "apply"
    assert records[1]["label"] == ""           # left for later, not guessed


def test_declining_to_decide_leaves_the_posting_unlabelled():
    records = [record(url="https://a")]

    done = evals.review_records(records, answers(None), save=lambda: None)

    assert done == 0
    assert records[0]["label"] == ""


def test_the_review_screen_carries_what_you_need_to_decide():
    screen = evals.format_for_review(record(), position=3, total=39)

    assert "3/39" in screen
    assert "Acme" in screen
    assert "Senior Engineer" in screen
    assert "Anywhere in the World" in screen
    assert "$120k - $150k" in screen
    assert "python" in screen
    assert "Django backend" in screen
    assert "https://a" in screen


def test_the_review_screen_never_shows_the_models_verdict():
    """Same invariant as the skeleton: the label must be independent judgement."""
    screen = evals.format_for_review(
        record(fit_score=78, confidence="high"), position=1, total=1)

    assert "78" not in screen
    assert "fit_score" not in screen
    assert "confidence" not in screen


def test_records_survive_a_round_trip_through_the_file(tmp_path):
    path = tmp_path / "labeled.jsonl"
    original = [record(url="https://a", label="apply"),
                record(url="https://ñ", excerpt="acentuación")]

    evals.write_records(path, original)

    assert evals.read_records(path) == original
    assert "acentuación" in path.read_text()   # not escaped into \u sequences


def test_reviewing_a_file_that_does_not_exist_says_so_rather_than_crashing(tmp_path, capsys):
    code = evals.review_command(tmp_path / "missing.jsonl")

    assert code == 2
    assert "does not exist" in capsys.readouterr().err


def test_reviewing_a_fully_labelled_file_has_nothing_to_ask(tmp_path, capsys):
    path = tmp_path / "labeled.jsonl"
    evals.write_records(path, [record(label="apply")])

    code = evals.review_command(path)

    assert code == 0
    assert "nothing to review" in capsys.readouterr().out


def test_going_back_reopens_the_previous_posting():
    """The gap the CLI had: a criterion you refine at posting 19 is worth
    applying to posting 12, and there was no way back to it."""
    records = [record(url="https://a"), record(url="https://b")]
    ask = answers(("apply", "first call"), evals.GoBack(),
                  ("skip", "changed my mind"), ("apply", ""))

    decided = evals.review_records(records, ask, save=lambda: None)

    assert records[0]["label"] == "skip"
    assert records[0]["reason"] == "changed my mind"
    assert records[1]["label"] == "apply"
    assert decided == 2
    assert ask.seen == ["https://a", "https://b", "https://a", "https://b"]


def test_going_back_clears_the_label_rather_than_leaving_it_standing():
    """Stepping back and then quitting must not leave the old verdict behind."""
    records = [record(url="https://a"), record(url="https://b")]
    ask = answers(("apply", ""), evals.GoBack(), evals.StopReview())

    decided = evals.review_records(records, ask, save=lambda: None)

    assert records[0]["label"] == ""
    assert decided == 0


def test_an_undo_is_written_to_disk_like_any_other_change():
    records = [record(url="https://a"), record(url="https://b")]
    saves = []
    ask = answers(("apply", ""), evals.GoBack(), evals.StopReview())

    evals.review_records(records, ask,
                         save=lambda: saves.append([r["label"] for r in records]))

    assert saves == [["apply", ""], ["", ""]]


# --- revisiting a verdict you already gave ------------------------------------


def test_reviewing_everything_revisits_the_labels_already_made():
    """`u` only reaches backwards within one session. A verdict you want to
    change tomorrow needs a way in from the front."""
    records = [record(url="https://done", label="apply"), record(url="https://todo")]
    ask = answers(("skip", "reconsidered"), ("apply", ""))

    evals.review_records(records, ask, save=lambda: None, include_labelled=True)

    assert ask.seen == ["https://done", "https://todo"]
    assert records[0]["label"] == "skip"
    assert records[0]["reason"] == "reconsidered"


def test_deferring_a_posting_you_already_judged_keeps_that_judgement():
    """Enter means "leave it alone", which for an existing verdict means keep
    it — not clear it."""
    records = [record(url="https://done", label="apply", reason="strong stack")]

    evals.review_records(records, answers(None), save=lambda: None,
                         include_labelled=True)

    assert records[0]["label"] == "apply"
    assert records[0]["reason"] == "strong stack"


def test_the_screen_shows_the_verdict_you_gave_before_but_never_the_models():
    screen = evals.format_for_review(
        record(label="apply", reason="strong stack overlap", fit_score=78),
        position=1, total=1)

    assert "apply" in screen
    assert "strong stack overlap" in screen
    assert "78" not in screen
