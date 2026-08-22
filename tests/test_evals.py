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


def test_label_skeleton_carries_enough_to_judge_without_opening_the_link():
    rows = [
        {"url": "https://a", "company": "Acme", "title": "Senior Engineer",
         "location_raw": "Anywhere in the World"},
    ]

    lines = evals.label_skeleton(rows)
    record = json.loads(lines[0])

    assert record["url"] == "https://a"
    assert record["company"] == "Acme"
    assert record["title"] == "Senior Engineer"
    assert record["label"] == ""      # the one field a human fills in
    assert record["reason"] == ""


def test_label_skeleton_omits_postings_already_labelled():
    rows = [{"url": "https://a", "company": "A", "title": "T", "location_raw": None},
            {"url": "https://b", "company": "B", "title": "T", "location_raw": None}]

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
