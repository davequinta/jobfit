"""Command line entry point.

Not a stage — argument plumbing only. Each stage keeps its own `main(argv)` and
stays runnable on its own (`python -m jobfit.ingest`), so this file never grows
logic that belongs in a stage.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from jobfit import cvimport, evals, ingest, prefilter, queue, score, ui

TEMPLATES = Path(__file__).parent / "templates"

# (bundled template, path it lands on, one-line description for the summary)
SCAFFOLD = [
    ("config.yaml", "config.yaml", "feeds, database path, User-Agent"),
    ("stack.yaml", "profile/stack.yaml", "your stack, title exclusions, filter rules"),
    ("cv.md", "profile/cv.md", "your CV — stage 3 scores postings against it"),
    ("score_system.md", "prompts/score_system.md", "the scoring rubric — edit and diff it"),
    ("env", ".env", "contact address and API key — gitignored"),
]

COMMANDS = {
    "cv": cvimport.main,
    "ingest": ingest.main,
    "prefilter": prefilter.main,
    "score": score.main,
    "queue": queue.main,
    "label": evals.label_main,
    "eval": evals.eval_main,
    "ui": ui.main,
}

USAGE = """usage: jobfit <command> [options]

commands:
  init        write starter config files into the current directory
  cv          convert your existing CV (PDF/md/txt) into profile/cv.md
  ingest      stage 1 — pull postings from the configured feeds into SQLite
  prefilter   stage 2 — apply the deterministic rules to stored postings
  score       stage 3 — score surviving postings against your CV (uses the API)
  queue       write queue/YYYY-MM-DD.md and append to out/applications.csv
  label       append unlabelled postings to the eval set for hand labelling
  eval        measure the scorer against your labels (offline, no API calls)
  ui          read a run and choose where to cut it, in a browser (local only)

`jobfit <command> --help` describes a command's options."""


def cmd_init(argv: list[str]) -> int:
    """Copy the bundled templates into the current directory.

    Never overwrites: someone re-running `init` after tuning their rules for a
    week should not lose them, and a scaffolding command is exactly the kind of
    thing people re-run by accident.

    It parses its arguments even though it takes none, because it used to ignore
    them — and `jobfit init --help`, typed to find out what the command does
    before running it, scaffolded the directory instead of answering.
    """
    argparse.ArgumentParser(
        prog="jobfit init",
        description="Scaffold config.yaml, profile/, prompts/ and .env into the "
                    "current directory. Never overwrites a file that exists.",
    ).parse_args(argv)

    written, kept = [], []
    for template, destination, description in SCAFFOLD:
        target = Path(destination)
        if target.exists():
            kept.append(destination)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(TEMPLATES / template, target)
        written.append((destination, description))

    for destination, description in written:
        print(f"  created  {destination:<22} {description}")
    for destination in kept:
        print(f"  kept     {destination:<22} already exists, left untouched")

    if written:
        print(
            "\nNext:\n"
            "  1. put a contact address in .env — feeds see it in the User-Agent\n"
            "  2. edit profile/stack.yaml to match your stack\n"
            "  3. replace profile/cv.md with your CV\n"
            "  4. jobfit ingest && jobfit prefilter    (free, no API key)\n"
            "\n"
            "profile/ and .env hold your CV and your API key. If this directory is\n"
            "under version control, add them to .gitignore before committing."
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2

    command, rest = argv[0], argv[1:]
    if command == "init":
        return cmd_init(rest)
    if command in COMMANDS:
        return COMMANDS[command](rest)

    print(f"jobfit: unknown command '{command}'\n\n{USAGE}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
