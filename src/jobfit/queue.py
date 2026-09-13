"""Queue output — the only stage a human reads.

Turns scored postings into two artifacts:

- `queue/YYYY-MM-DD.md` — ranked, designed to be read top to bottom in thirty
  minutes over coffee. Everything needed to decide *skip or apply* is in the
  entry; nothing needed for that decision is behind a click.
- `out/applications.csv` — append-only, importable into a tracking sheet.

The CSV's `channel` column is the one that matters most. Every row records where
the posting came from, so after four weeks response rate can be measured per
channel and the dead ones killed. Job boards are the lowest-yield channel most
people have; the column is what proves it rather than assuming it.

Reads `postings`, `prefilter_verdicts` and `scores`. Writes files, never the
database — re-running is always safe.

A posting reaches the queue only if stage 2 passed it and the score in front of
it came from the rubric stage 3 would use today. Without the first rule the
queue carries postings the funnel has since rejected as stale; without the
second it mixes two generations of verdict in one list, which is the thing
`prompt_version` exists to prevent. Everything left out is counted and named.
"""

from __future__ import annotations

import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path


from jobfit import db, runtime, score

log = logging.getLogger("jobfit.queue")

QUEUE_DIR = Path("queue")
CSV_PATH = Path("out/applications.csv")

# Fixed by the tracking sheet this feeds. Order is part of the contract.
CSV_COLUMNS = [
    "date_found", "company", "role", "channel", "url", "fit_score", "status",
    "applied_date", "contact", "next_action", "next_action_date", "comp_range", "notes",
]

DEFAULT_THRESHOLD = runtime.DEFAULT_THRESHOLD


def entries_above(conn: sqlite3.Connection, threshold: int,
                  version: str | None = None) -> list[dict]:
    """Queueable postings at or above `threshold`, best first.

    Queueable means stage 2 passed it and, unless `version` is None, the score
    came from that rubric.
    """
    rows = conn.execute(
        """SELECT p.company, p.title, p.url, p.source, p.location_raw, p.published_at,
                  s.fit_score, s.confidence, s.seniority_match, s.comp_range,
                  s.stack_overlap_json, s.stack_gaps_json,
                  s.why_fit_json, s.why_not_json, s.red_flags_json
             FROM scores s
             JOIN postings p ON p.id = s.posting_id
             JOIN prefilter_verdicts v ON v.posting_id = p.id
            WHERE s.fit_score >= ?
              AND v.rejected_reason IS NULL
              AND (? IS NULL OR s.prompt_version = ?)
            ORDER BY s.fit_score DESC, p.published_at DESC""",
        (threshold, version, version),
    ).fetchall()
    return [dict(row) for row in rows]


def left_out(conn: sqlite3.Connection, threshold: int, version: str | None) -> dict[str, int]:
    """Scored postings above the threshold that the queue does not carry, by reason.

    Reported rather than dropped quietly: a queue that shrank because the rubric
    moved looks exactly like a quiet week on the feeds.
    """
    row = conn.execute(
        """SELECT sum(v.posting_id IS NULL) AS unfiltered,
                  sum(v.rejected_reason IS NOT NULL) AS rejected,
                  sum(v.posting_id IS NOT NULL AND v.rejected_reason IS NULL
                      AND ? IS NOT NULL AND s.prompt_version != ?) AS older_rubric
             FROM scores s
             JOIN postings p ON p.id = s.posting_id
             LEFT JOIN prefilter_verdicts v ON v.posting_id = p.id
            WHERE s.fit_score >= ?""",
        (version, version, threshold),
    ).fetchone()
    return {name: row[name] or 0 for name in ("unfiltered", "rejected", "older_rubric")}


def current_version(rubric: str | None, cv: str, profile: str) -> str:
    """The rubric version stage 3 would score with right now.

    Read from `score` rather than redefined here: that stage owns what a rubric
    version is, and calling its pure functions is not chaining stages — nothing
    is scored, stored or passed on.
    """
    return score.prompt_version(
        score.load_rubric(score.resolve_rubric_path(rubric), cv, profile))


# --- markdown ----------------------------------------------------------------


def render(entries: list[dict], day: str) -> str:
    if not entries:
        return (
            f"# Review queue — {day}\n\n"
            "No postings made the queue today.\n\n"
            "That can be a result rather than a failure. `jobfit queue` prints why when\n"
            "scored postings were left out — stale, scored under an older rubric, or\n"
            "never judged by stage 2. If it printed none of those, nothing cleared the\n"
            "threshold: check `jobfit prefilter` before lowering it.\n"
        )

    parts = [
        f"# Review queue — {day}",
        "",
        f"{len(entries)} postings, best first. Read top to bottom and stop when you "
        "stop caring.",
        "",
        "_Cover letter openers come from stage 4, which is not built yet._",
        "",
    ]
    for entry in entries:
        parts.append(_render_entry(entry))
    return "\n".join(parts)


def _render_entry(entry: dict) -> str:
    gaps = json.loads(entry["stack_gaps_json"])
    flags = json.loads(entry["red_flags_json"])

    lines = [
        f"## {entry['fit_score']} — {entry['company']} — {entry['title']}",
        "",
        entry["url"],
        "",
        f"_{entry['location_raw'] or 'location not stated'} · "
        f"{entry['comp_range'] or 'comp not stated'} · "
        f"seniority {entry['seniority_match']} · {entry['confidence']} confidence_",
        "",
        "**Why it fits**",
        "",
    ]
    lines += [f"- {bullet}" for bullet in json.loads(entry["why_fit_json"])]
    lines += ["", "**Why it might not**", ""]
    lines += [f"- {bullet}" for bullet in json.loads(entry["why_not_json"])]

    if gaps:
        lines += ["", f"**Gaps:** {', '.join(gaps)}"]
    if flags:
        lines += ["", f"**Red flags:** {', '.join(flags)}"]
    lines.append("")
    return "\n".join(lines)


def write_queue(conn: sqlite3.Connection, threshold: int, day: str,
                version: str | None = None) -> Path:
    entries = entries_above(conn, threshold, version)
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    path = QUEUE_DIR / f"{day}.md"
    path.write_text(render(entries, day))
    return path


# --- the tracking CSV --------------------------------------------------------


def existing_urls(path: Path) -> set[str]:
    """URLs already recorded, so re-running does not duplicate them."""
    if not path.is_file():
        return set()
    with path.open(newline="") as handle:
        return {row["url"] for row in csv.DictReader(handle)}


def append_csv(conn: sqlite3.Connection, threshold: int, day: str,
               version: str | None = None) -> int:
    entries = entries_above(conn, threshold, version)
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

    already = existing_urls(CSV_PATH)
    fresh = [e for e in entries if e["url"] not in already]

    write_header = not CSV_PATH.is_file()
    with CSV_PATH.open("a", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for entry in fresh:
            writer.writerow(_csv_row(entry, day))
    return len(fresh)


def _csv_row(entry: dict, day: str) -> dict:
    """Everything the tool knows; everything else left blank for a human.

    `status`, `applied_date`, `contact` and the next-action columns are the
    user's to fill in. The tool never guesses at them — a tracking sheet full of
    invented state is worse than an empty one.
    """
    return {
        "date_found": day,
        "company": entry["company"],
        "role": entry["title"],
        "channel": f"job_board:{entry['source']}",
        "url": entry["url"],
        "fit_score": entry["fit_score"],
        "status": "",
        "applied_date": "",
        "contact": "",
        "next_action": "",
        "next_action_date": "",
        "comp_range": entry["comp_range"] or "",
        "notes": "",
    }


# --- CLI ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = runtime.stage_parser("Write the review queue and tracking CSV.")
    parser.add_argument("--threshold", type=int,
                        help=f"override the configured cut (default {DEFAULT_THRESHOLD})")
    parser.add_argument("--day", help="date stamp for the queue file (default: today, UTC)")
    parser.add_argument("--rubric", help="defaults to prompts/score_system.md, else the bundled rubric")
    parser.add_argument("--cv", default="profile/cv.md",
                        help="the CV `jobfit score` was given; it is part of the rubric version")
    parser.add_argument("--profile", default="profile/stack.yaml",
                        help="the stack profile `jobfit score` was given; also part of the version")
    parser.add_argument("--any-rubric", action="store_true",
                        help="queue scores from every rubric version, not only the current one")
    args = parser.parse_args(argv)
    args.threshold = runtime.threshold(args)

    runtime.configure_logging()
    day = args.day or db.iso_now()[:10]
    conn = runtime.open_db(args)
    try:
        # "Nothing cleared the bar" is a real result. "Nothing has been scored"
        # is a different one, and reporting the first when the second is true
        # sends you tuning a threshold that was never the problem.
        if not conn.execute("SELECT count(*) FROM scores").fetchone()[0]:
            print("jobfit queue: nothing has been scored — run `jobfit score` first",
                  file=sys.stderr)
            return 2

        # Which scores are current is a question only the rubric can answer, and
        # guessing would mix two generations of verdict in one list.
        version = None
        if not args.any_rubric:
            try:
                version = current_version(args.rubric, args.cv, args.profile)
            except OSError as exc:
                print(f"jobfit queue: cannot read the rubric to tell which scores are "
                      f"current ({exc}). Pass --any-rubric to queue every score as it is.",
                      file=sys.stderr)
                return 2
            log.info("queueing scores from rubric %s", version)

        path = write_queue(conn, args.threshold, day, version)
        added = append_csv(conn, args.threshold, day, version)
        count = len(entries_above(conn, args.threshold, version))
        skipped = left_out(conn, args.threshold, version)
    finally:
        conn.close()

    print(f"{count} postings at or above {args.threshold} → {path}")
    print(f"{added} new rows → {CSV_PATH}")
    if skipped["rejected"]:
        print(f"{skipped['rejected']} scored postings above the cut are not here: stage 2 "
              "rejects them now, most often as stale")
    if skipped["older_rubric"]:
        print(f"{skipped['older_rubric']} more were scored under an older rubric — run "
              "`jobfit score` to bring them up to date, or `--any-rubric` to queue them as they are")
    if skipped["unfiltered"]:
        print(f"{skipped['unfiltered']} more have no stage 2 verdict — run `jobfit prefilter`")
    # The threshold is only the suspect when nothing was left out for another
    # reason; the lines above already name the fix when something was.
    if count == 0 and not any(skipped.values()):
        print("\nNothing cleared the bar. Check `jobfit prefilter` before lowering the "
              "threshold — an over-aggressive filter looks identical to a quiet day.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
