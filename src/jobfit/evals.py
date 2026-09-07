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

import json
import math
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path


from jobfit import db, runtime

LABELS = {"apply", "skip", "borderline"}

# Long enough to judge, short enough that the file stays scannable in an editor.
EXCERPT_CHARS = 320


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


def _excerpt(text: str) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= EXCERPT_CHARS else flat[:EXCERPT_CHARS] + "…"


def label_skeleton(rows, already: set[str] | None = None) -> list[str]:
    """JSONL lines for postings that still need a hand label.

    Hand-labelling forty postings is the step most likely to be skipped, so each
    line carries enough context to decide without opening the link.

    What it deliberately does NOT carry is the model's verdict. Seeing "the
    model said 78" before deciding anchors the label, and measuring the model
    against labels it influenced is circular. Everything here is either the
    posting itself or `stack_seen`, which is stage 2's deterministic keyword
    match — no judgement in it.
    """
    already = already or set()
    return [
        json.dumps({
            "url": row["url"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location_raw"] or "not stated",
            "salary": row["salary_raw"] or "not stated",
            "posted": (row["published_at"] or "")[:10],
            "stack_seen": json.loads(row["stack_hits_json"] or "[]"),
            "excerpt": _excerpt(row["description_text"]),
            "label": "",           # apply | skip | borderline
            "reason": "",          # one line, for your future self
        }, ensure_ascii=False)
        for row in rows if row["url"] not in already
    ]


def rewrite_unlabelled(existing_lines: list[str], rows) -> list[str]:
    """Keep every line you have already labelled; refresh the rest with context.

    Lets an existing bare skeleton be upgraded in place without losing work.
    """
    kept, unlabelled = [], set()
    for line in existing_lines:
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("label"):
            kept.append(json.dumps(record, ensure_ascii=False))
        else:
            unlabelled.add(record["url"])

    labelled_urls = {json.loads(line)["url"] for line in kept}
    fresh = [
        line for line in label_skeleton(rows)
        if json.loads(line)["url"] not in labelled_urls
    ]
    return kept + fresh


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


# --- hand labelling ----------------------------------------------------------
#
# The file this writes is the ground truth everything else is measured against,
# so the reviewer's job is to make a human's judgement cheap to record — never
# to supply one.


class StopReview(Exception):
    """Raised by the prompt to stop the review and keep what is already decided."""


KEYSTROKES = {"a": "apply", "s": "skip", "b": "borderline"}


def read_records(path: str | Path) -> list[dict]:
    """Every entry in the label file, labelled or not, in file order."""
    return [
        json.loads(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]


def write_records(path: str | Path, records: list[dict]) -> None:
    Path(path).write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    )


def format_for_review(record: dict, position: int, total: int) -> str:
    """One posting as a screen to judge.

    Names the fields it prints rather than dumping the record, so a model
    verdict that somehow reached the file cannot leak onto the screen and
    anchor the label. Same invariant as `label_skeleton`, enforced twice
    because it is the one that makes the numbers mean anything.
    """
    stack = ", ".join(record.get("stack_seen") or []) or "none matched"
    excerpt = textwrap.fill(record.get("excerpt", ""), width=76,
                            initial_indent="    ", subsequent_indent="    ")
    return "\n".join([
        "",
        f"[{position}/{total}]  {record['company']} — {record['title']}",
        f"    {record['location']} · {record['salary']} · posted {record['posted'] or 'unknown'}",
        f"    stack seen: {stack}",
        f"    {record['url']}",
        "",
        excerpt,
        "",
    ])


def review_records(records: list[dict], ask, save) -> int:
    """Ask for a verdict on every record that has no label yet.

    Saves after each decision rather than at the end. Labelling forty postings
    takes half an hour, and a crash at posting thirty that loses the first
    twenty-nine is how this step gets abandoned.
    """
    pending = [record for record in records if not record.get("label")]
    decided = 0
    for position, record in enumerate(pending, start=1):
        try:
            answer = ask(record, position, len(pending))
        except StopReview:
            break
        if answer is None:
            continue          # deferred; a label is never guessed on your behalf
        record["label"], record["reason"] = answer
        decided += 1
        save()
    return decided


def ask_at_terminal(record: dict, position: int, total: int):
    """Print a posting and read a verdict. Enter defers it, q ends the review."""
    print(format_for_review(record, position, total))
    while True:
        try:
            answer = input("    [a]pply  [s]kip  [b]orderline  [enter] later  [q]uit: ")
        except EOFError:
            raise StopReview from None
        answer = answer.strip().lower()
        if answer in ("q", "quit"):
            raise StopReview
        if not answer:
            return None
        if answer in KEYSTROKES:
            try:
                reason = input("    why, one line for your future self: ").strip()
            except EOFError:
                reason = ""
            return KEYSTROKES[answer], reason
        print("    not one of a, s, b, enter or q")


# --- commands ----------------------------------------------------------------

LABELS_PATH = Path("evals/labeled.jsonl")
RESULTS_PATH = Path("evals/results.md")


def _open_db(args):
    return runtime.open_db(args)


def label_main(argv: list[str] | None = None) -> int:
    """Emit JSONL skeleton lines for postings that still need a hand label."""
    parser = runtime.stage_parser("Append unlabelled postings to the eval set for hand labelling.")
    parser.add_argument("--out", default=str(LABELS_PATH))
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--rewrite", action="store_true",
                        help="refresh unlabelled entries with full context, keeping your labels")
    parser.add_argument("--review", action="store_true",
                        help="label the collected postings one at a time in the terminal")
    args = parser.parse_args(argv)

    out = Path(args.out)
    if args.review:
        return review_command(out)

    already = set(load_labels(out)) if out.is_file() else set()
    conn = _open_db(args)
    try:
        rows = conn.execute(
            """SELECT p.url, p.company, p.title, p.location_raw, p.salary_raw,
                      p.published_at, p.description_text, v.stack_hits_json
                 FROM postings p
                 JOIN prefilter_verdicts v ON v.posting_id = p.id
                WHERE v.rejected_reason IS NULL
                ORDER BY p.published_at DESC"""
        ).fetchall()
    finally:
        conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    if args.rewrite and out.is_file():
        lines = rewrite_unlabelled(out.read_text().splitlines(), rows)
        out.write_text("\n".join(lines) + "\n")
        print(f"{out} rewritten: {len(lines)} entries, your labels kept")
    else:
        present = already | (
            {json.loads(line)["url"] for line in out.read_text().splitlines() if line.strip()}
            if out.is_file() else set()
        )
        lines = label_skeleton(rows, present)[: args.limit]
        with out.open("a") as handle:
            for line in lines:
                handle.write(line + "\n")
        print(f"{len(lines)} postings appended to {out}")
    print('Fill in "label" with apply | skip | borderline, and one line of "reason".')
    print("Aim for roughly 15 apply, 15 skip, 10 borderline — the borderline ones")
    print("are where you learn whether the scorer or you is wrong.")
    return 0


def review_command(out: Path) -> int:
    """Walk the unlabelled entries in the terminal, saving after each verdict.

    Collecting postings and judging them are separate jobs: the first needs the
    database, the second needs you. This one only ever touches the file.
    """
    if not out.is_file():
        print(f"jobfit label: {out} does not exist — run `jobfit label` first to "
              "collect postings to review", file=sys.stderr)
        return 2

    records = read_records(out)
    pending = sum(1 for record in records if not record.get("label"))
    if not pending:
        print(f"{out}: every entry is labelled — nothing to review.")
        return 0

    print(f"{pending} postings to label. Enter defers one, q stops and keeps your work.")
    print("Aim for roughly 15 apply, 15 skip, 10 borderline.")
    decided = review_records(records, ask_at_terminal,
                             save=lambda: write_records(out, records))

    left = sum(1 for record in records if not record.get("label"))
    print(f"\n{decided} labelled this session; {left} still unlabelled in {out}.")
    if not left:
        print('Set complete — run `jobfit eval --note "first labelled set"`.')
    return 0


def eval_main(argv: list[str] | None = None) -> int:
    """Score the hand-labelled set against stored scores. Never calls the API."""
    parser = runtime.stage_parser("Measure the scorer against hand labels. Offline; uses stored scores.")
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
        entry = results_entry(outcome, args.threshold, db.iso_now()[:10],
                              versions[0] if versions else "unknown", args.note)
        with RESULTS_PATH.open("a") as handle:
            handle.write(entry + "\n")
        print(f"logged to {RESULTS_PATH}")

    return 0
