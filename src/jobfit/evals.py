"""Eval metrics — how good is the scorer, measured against hand labels.

This is the part of the project that separates it from a demo, and the part most
likely to get cut under time pressure. It is deliberately small: load labels,
compare against stored scores, report counts.

Three choices worth defending:

**Counts before ratios.** With forty hand-labelled postings the third decimal of
a precision figure is noise. The report leads with "2 of 3", because a count is
checkable and a ratio invites more confidence than the sample supports.

**Borderline labels are excluded.** The borderline postings exist to study
disagreement, not to be graded. Folding them into either class would make the
headline number depend on a judgement the label explicitly declines to make.

**Precision is None, not zero, when nothing was surfaced.** 0/0 is undefined,
and printing 0.0 reads as "the scorer was wrong" when the truth is "the scorer
said nothing".

Optimise for precision over recall. A false positive costs twenty minutes and a
wasted application; a false negative costs one posting out of hundreds.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from jobfit import ingest, score as score_stage

LABELS = {"apply", "skip", "borderline"}


@dataclass
class Outcome:
    surfaced: int                     # scored at or above the threshold, excluding borderline
    relevant: int                     # labelled apply
    true_positives: int
    false_positives: list[str] = field(default_factory=list)
    false_negatives: list[str] = field(default_factory=list)
    borderline_surfaced: int = 0
    unscored: list[str] = field(default_factory=list)

    @property
    def precision(self) -> float | None:
        return None if self.surfaced == 0 else self.true_positives / self.surfaced

    @property
    def recall(self) -> float | None:
        return None if self.relevant == 0 else self.true_positives / self.relevant


def load_labels(path: str | Path) -> dict[str, str]:
    """Read `evals/labeled.jsonl` into {url: label}.

    An unrecognised label raises rather than being coerced — a typo silently
    read as "skip" would quietly change every number downstream.
    """
    labels: dict[str, str] = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        record = json.loads(line)
        label = record["label"]
        if not label:
            continue  # a skeleton line nobody has filled in yet is not a verdict
        if label not in LABELS:
            raise ValueError(
                f"{path}:{number}: unknown label {label!r}; expected one of {sorted(LABELS)}"
            )
        labels[record["url"]] = label
    return labels


def evaluate(labels: dict[str, str], scores: dict[str, int], threshold: int) -> Outcome:
    graded = {url: label for url, label in labels.items() if label != "borderline"}

    unscored = sorted(url for url in graded if url not in scores)
    surfaced = [url for url, label in graded.items() if scores.get(url, -1) >= threshold]

    true_positives = [url for url in surfaced if graded[url] == "apply"]
    false_positives = sorted(url for url in surfaced if graded[url] == "skip")
    false_negatives = sorted(
        url for url, label in graded.items()
        if label == "apply" and url not in surfaced and url in scores
    )
    borderline = sum(
        1 for url, label in labels.items()
        if label == "borderline" and scores.get(url, -1) >= threshold
    )

    return Outcome(
        surfaced=len(surfaced),
        relevant=sum(1 for label in graded.values() if label == "apply"),
        true_positives=len(true_positives),
        false_positives=false_positives,
        false_negatives=false_negatives,
        borderline_surfaced=borderline,
        unscored=unscored,
    )


def margin_of_error(successes: int, trials: int) -> float | None:
    """Rough 95% interval half-width for a proportion.

    Normal approximation, which is crude at these sample sizes — that is the
    point. It exists to stop a two-decimal number from being read as precise.
    """
    if trials == 0:
        return None
    p = successes / trials
    return 1.96 * math.sqrt(max(p * (1 - p), 0.0) / trials)


def report(outcome: Outcome, threshold: int) -> str:
    lines = [f"Threshold {threshold}", ""]

    lines.append(_ratio_line("Precision", outcome.true_positives, outcome.surfaced,
                             "of what it surfaced was real"))
    lines.append(_ratio_line("Recall", outcome.true_positives, outcome.relevant,
                             "of the good ones were surfaced"))
    lines.append("")

    if outcome.false_positives:
        lines += ["False positives — each one costs 20 minutes:", ""]
        lines += [f"  {url}" for url in outcome.false_positives]
        lines.append("")
    if outcome.false_negatives:
        lines += ["False negatives — each one is a missed opportunity:", ""]
        lines += [f"  {url}" for url in outcome.false_negatives]
        lines.append("")
    if outcome.borderline_surfaced:
        lines.append(
            f"{outcome.borderline_surfaced} borderline postings surfaced. These are "
            "excluded from the numbers above; read them and decide who was right."
        )
        lines.append("")
    if outcome.unscored:
        lines += ["Labelled but never scored — these would inflate recall if ignored:", ""]
        lines += [f"  {url}" for url in outcome.unscored]
        lines.append("")

    return "\n".join(lines)


def _ratio_line(name: str, numerator: int, denominator: int, gloss: str) -> str:
    if denominator == 0:
        return f"{name}: n/a — nothing to measure ({gloss})"
    ratio = numerator / denominator
    error = margin_of_error(numerator, denominator)
    return (
        f"{name}: {numerator} of {denominator} ({ratio:.0%} ±{error:.0%}) — {gloss}"
    )


def label_skeleton(rows, already: set[str] | None = None) -> list[str]:
    """JSONL lines for postings that still need a hand label.

    Hand-labelling forty postings is the step most likely to be skipped, so each
    line carries enough context to judge without opening the link — fill in one
    word and move on.
    """
    already = already or set()
    return [
        json.dumps({
            "url": row["url"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location_raw"] or "",
            "label": "",           # apply | skip | borderline
            "reason": "",          # one line, for your future self
        }, ensure_ascii=False)
        for row in rows if row["url"] not in already
    ]


def results_entry(outcome: Outcome, threshold: int, day: str,
                  prompt_version: str, note: str) -> str:
    """One row for `evals/results.md`.

    The rubric version is recorded alongside the numbers because a change in
    either the rubric or the threshold moves them, and without knowing which one
    changed the log is a list of numbers rather than evidence.
    """
    precision = f"{outcome.true_positives} of {outcome.surfaced}" if outcome.surfaced else "n/a"
    recall = f"{outcome.true_positives} of {outcome.relevant}" if outcome.relevant else "n/a"
    return (
        f"| {day} | {prompt_version} | {threshold} | {precision} | {recall} | "
        f"{len(outcome.false_positives)} | {len(outcome.false_negatives)} | {note} |"
    )


RESULTS_HEADER = (
    "# Eval results\n\n"
    "Every rubric change gets a row. Never change the rubric and the threshold in\n"
    "the same run — if the numbers move, you need to know which one did it.\n\n"
    "Counts, not ratios: with a forty-posting set the third decimal is noise.\n\n"
    "| date | rubric | threshold | precision | recall | FP | FN | what changed |\n"
    "|---|---|---|---|---|---|---|---|\n"
)


# --- commands ----------------------------------------------------------------

LABELS_PATH = Path("evals/labeled.jsonl")
RESULTS_PATH = Path("evals/results.md")


def _open_db(args):
    config = yaml.safe_load(Path(args.config).read_text())
    return ingest.connect(args.db or config["db_path"])


def label_main(argv: list[str] | None = None) -> int:
    """Emit JSONL skeleton lines for postings that still need a hand label."""
    parser = argparse.ArgumentParser(
        description="Append unlabelled postings to the eval set for hand labelling.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db")
    parser.add_argument("--out", default=str(LABELS_PATH))
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args(argv)

    out = Path(args.out)
    already = set(load_labels(out)) if out.is_file() else set()
    conn = _open_db(args)
    try:
        rows = conn.execute(
            """SELECT p.url, p.company, p.title, p.location_raw
                 FROM postings p
                 JOIN prefilter_verdicts v ON v.posting_id = p.id
                WHERE v.rejected_reason IS NULL
                ORDER BY p.published_at DESC"""
        ).fetchall()
    finally:
        conn.close()

    lines = label_skeleton(rows, already)[: args.limit]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as handle:
        for line in lines:
            handle.write(line + "\n")

    print(f"{len(lines)} postings appended to {out}")
    print('Fill in "label" with apply | skip | borderline, and one line of "reason".')
    print("Aim for roughly 15 apply, 15 skip, 10 borderline — the borderline ones")
    print("are where you learn whether the scorer or you is wrong.")
    return 0


def eval_main(argv: list[str] | None = None) -> int:
    """Score the hand-labelled set against stored scores. Never calls the API."""
    parser = argparse.ArgumentParser(
        description="Measure the scorer against hand labels. Offline; uses stored scores.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db")
    parser.add_argument("--labels", default=str(LABELS_PATH))
    parser.add_argument("--threshold", type=int, default=70)
    parser.add_argument("--note", help="what changed since the last run; logs a row to results.md")
    args = parser.parse_args(argv)

    labels_path = Path(args.labels)
    if not labels_path.is_file():
        print(f"jobfit eval: {labels_path} does not exist — run `jobfit label` first",
              file=sys.stderr)
        return 2

    labels = load_labels(labels_path)
    if not labels:
        print(f"jobfit eval: {labels_path} has no filled-in labels yet", file=sys.stderr)
        return 2

    conn = _open_db(args)
    try:
        scores = {
            row["url"]: row["fit_score"]
            for row in conn.execute(
                "SELECT p.url, s.fit_score FROM scores s JOIN postings p ON p.id = s.posting_id")
        }
        versions = [row["prompt_version"] for row in conn.execute(
            "SELECT DISTINCT prompt_version FROM scores")]
    finally:
        conn.close()

    outcome = evaluate(labels, scores, args.threshold)
    print(report(outcome, args.threshold))

    if len(versions) > 1:
        print(f"WARNING: scores come from {len(versions)} different rubric versions "
              f"({', '.join(versions)}). Re-score before trusting these numbers.",
              file=sys.stderr)

    if args.note:
        RESULTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        if not RESULTS_PATH.is_file():
            RESULTS_PATH.write_text(RESULTS_HEADER)
        entry = results_entry(outcome, args.threshold, ingest.iso_now()[:10],
                              versions[0] if versions else "unknown", args.note)
        with RESULTS_PATH.open("a") as handle:
            handle.write(entry + "\n")
        print(f"logged to {RESULTS_PATH}")

    return 0
