"""The SQLite connection, and the clock.

Every stage needs a database handle. Before this existed each of them imported
`ingest` to get one, which made the scoring stage depend on the ingest stage for
no reason other than where a function happened to live.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text())
    return conn


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


