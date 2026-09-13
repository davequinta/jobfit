"""Shared plumbing for the stage commands.

Every stage grew the same six lines: an argument parser with `--config` and
`--db`, the same logging format, then read the config and open the database.
Five copies meant five places to change the log format and five chances for one
of them to drift.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

import yaml

from jobfit import db

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(message)s"

# The score at or above which a posting is worth your attention. Lives here, not
# in a stage, because the queue cuts on it, the eval measures at it and the
# scorer's why_not guardrail fires above it — three copies is three chances to
# drift apart. Set from measurement, not taste: see evals/results.md.
DEFAULT_THRESHOLD = 35


def stage_parser(description: str) -> argparse.ArgumentParser:
    """An argument parser carrying the options every stage accepts."""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--db", help="override db_path from the config")
    return parser


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stderr)


def load_config(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text())


def threshold(args) -> int:
    """The score to cut at: `--threshold` if given, else the config's, else the default.

    A malformed `threshold:` in the config raises rather than falling back — a
    typo that quietly reverts the cut to 35 is the kind of silence this project
    does not allow. A missing key is not a malformation; it means "the default".
    """
    if getattr(args, "threshold", None) is not None:
        return args.threshold
    path = Path(args.config)
    config = load_config(args.config) if path.is_file() else {}
    if "threshold" not in config:
        return DEFAULT_THRESHOLD
    return int(config["threshold"])


def open_db(args) -> sqlite3.Connection:
    """The database this run should use: `--db` if given, else the config's."""
    return db.connect(args.db or load_config(args.config)["db_path"])
