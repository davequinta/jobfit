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

Reads `postings` and `scores`. Writes files, never the database — re-running is
always safe.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from pathlib import Path

import yaml

from jobfit import ingest

log = logging.getLogger("jobfit.queue")

QUEUE_DIR = Path("queue")
CSV_PATH = Path("out/applications.csv")

# Fixed by the tracking sheet this feeds. Order is part of the contract.
CSV_COLUMNS = [
    "date_found", "company", "role", "channel", "url", "fit_score", "status",
    "applied_date", "contact", "next_action", "next_action_date", "comp_range", "notes",
]

DEFAULT_THRESHOLD = 70


def entries_above(conn: sqlite3.Connection, threshold: int) -> list[dict]:
    """Scored postings at or above `threshold`, best first."""
    rows = conn.execute(
        """SELECT p.company, p.title, p.url, p.source, p.location_raw, p.published_at,
                  s.fit_score, s.confidence, s.seniority_match, s.comp_range,
                  s.stack_overlap_json, s.stack_gaps_json,
                  s.why_fit_json, s.why_not_json, s.red_flags_json
             FROM scores s
             JOIN postings p ON p.id = s.posting_id
            WHERE s.fit_score >= ?
            ORDER BY s.fit_score DESC, p.published_at DESC""",
        (threshold,),
    ).fetchall()
    return [dict(row) for row in rows]


# --- markdown ----------------------------------------------------------------


def render(entries: list[dict], day: str) -> str:
    if not entries:
        return (
            f"# Review queue — {day}\n\n"
            "No postings cleared the threshold today.\n\n"
            "That is a result, not a failure. If it happens several days running, the\n"
            "threshold or the rubric needs tuning — check `jobfit prefilter` output\n"
            "first, since a filter that is too aggressive looks exactly like this.\n"
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


def write_queue(conn: sqlite3.Connection, threshold: int, day: str) -> Path:
    entries = entries_above(conn, threshold)
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


def append_csv(conn: sqlite3.Connection, threshold: int, day: str) -> int:
    entries = entries_above(conn, threshold)
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
    parser = argparse.ArgumentParser(description="Write the review queue and tracking CSV.")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", help="override db_path from the config")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD)
    parser.add_argument("--day", help="date stamp for the queue file (default: today, UTC)")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )
    config = yaml.safe_load(Path(args.config).read_text())
    day = args.day or ingest.iso_now()[:10]
    conn = ingest.connect(args.db or config["db_path"])
    try:
        path = write_queue(conn, args.threshold, day)
        added = append_csv(conn, args.threshold, day)
        count = len(entries_above(conn, args.threshold))
    finally:
        conn.close()

    print(f"{count} postings at or above {args.threshold} → {path}")
    print(f"{added} new rows → {CSV_PATH}")
    if count == 0:
        print("\nNothing cleared the bar. Check `jobfit prefilter` before lowering the "
              "threshold — an over-aggressive filter looks identical to a quiet day.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
