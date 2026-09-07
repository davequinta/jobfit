"""A local page for reading a run and choosing where to cut it.

Not a stage and not a service. `jobfit ui` serves one page on 127.0.0.1 from the
standard library, reads the database the stages already wrote, and shuts down
when you close it. No framework, no build step, no dependency the rest of the
tool does not already have.

It exists because of one number. The queue shipped cutting at 70 while the
scorer's real range turned out to be 8-78, so it surfaced 1 posting in 22 that
deserved one, and that went unnoticed for two weeks — a threshold is invisible
in a config file and obvious the moment you can drag it and watch the list and
the precision move together. Reading a `.md` file tells you what the cut
produced; this tells you what the other cuts would have.

Two rules hold it together:

**The arithmetic is not reimplemented in JavaScript.** Precision and recall for
every threshold from 0 to 100 are computed here, by `evals.evaluate` — the same
function `jobfit eval` calls — and shipped to the page as a lookup table. The
page slides a value; it never divides. A second implementation in the browser
would drift from the one the eval suite tests, and the number on the screen
would stop being the number in `evals/results.md`.

**Editing a rule shows the effect before it costs anything.** The rules panel
re-evaluates the stored postings in memory and reports the cut it would produce.
Nothing is written to the database until you run `jobfit prefilter` yourself.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import webbrowser
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from jobfit import db, evals, prefilter, runtime

log = logging.getLogger("jobfit.ui")

TEMPLATE = Path(__file__).parent / "templates" / "ui.html"
DEFAULT_PORT = 8765

# The page draws a curve over this range; scores outside it do not exist.
THRESHOLDS = range(0, 101)


# --- state -------------------------------------------------------------------


def scored_postings(conn: sqlite3.Connection) -> list[dict]:
    """Every scored posting with the verdict that explains its score."""
    rows = conn.execute(
        """SELECT p.url, p.company, p.title, p.location_raw, p.salary_raw,
                  p.published_at, s.fit_score, s.confidence, s.seniority_match,
                  s.location_eligible, s.comp_range, s.stack_overlap_json,
                  s.stack_gaps_json, s.why_fit_json, s.why_not_json,
                  s.red_flags_json
             FROM scores s JOIN postings p ON p.id = s.posting_id
            ORDER BY s.fit_score DESC"""
    ).fetchall()
    return [
        {
            "url": row["url"],
            "company": row["company"],
            "title": row["title"],
            "location": row["location_raw"] or "not stated",
            "salary": row["salary_raw"] or "not stated",
            "posted": (row["published_at"] or "")[:10],
            "score": row["fit_score"],
            "confidence": row["confidence"],
            "seniority": row["seniority_match"],
            "eligible": bool(row["location_eligible"]),
            "comp": row["comp_range"] or "",
            "stack_overlap": json.loads(row["stack_overlap_json"]),
            "stack_gaps": json.loads(row["stack_gaps_json"]),
            "why_fit": json.loads(row["why_fit_json"]),
            "why_not": json.loads(row["why_not_json"]),
            "red_flags": json.loads(row["red_flags_json"]),
        }
        for row in rows
    ]


def metrics_by_threshold(labels: dict[str, str], scores: dict[str, int]) -> list[dict]:
    """Precision and recall at every threshold, from `evals.evaluate`.

    Precomputed here rather than derived in the browser so the page cannot
    disagree with `jobfit eval`. 101 rows of five integers is nothing to send.
    """
    table = []
    for threshold in THRESHOLDS:
        outcome = evals.evaluate(labels, scores, threshold)
        table.append({
            "threshold": threshold,
            "surfaced": outcome.surfaced,
            "relevant": outcome.relevant,
            "true_positives": outcome.true_positives,
            "false_positives": len(outcome.false_positives),
            "false_negatives": len(outcome.false_negatives),
            "precision": outcome.precision,
            "recall": outcome.recall,
        })
    return table


def rules_payload(profile: prefilter.Profile) -> dict:
    return {
        "stack": [{"name": name, "aliases": aliases}
                  for name, aliases in profile.stack.items()],
        "title_exclusions": list(profile.title_exclusions),
        "junior_signals": list(profile.junior_signals),
        "location_exclusions": list(profile.location_exclusions),
        "location_allowlist": list(profile.location_allowlist),
        "max_age_days": profile.max_age_days,
    }


def profile_from_payload(payload: dict) -> prefilter.Profile:
    return prefilter.Profile(
        stack={group["name"]: group["aliases"] for group in payload["stack"]},
        title_exclusions=list(payload["title_exclusions"]),
        junior_signals=[str(s) for s in payload["junior_signals"]],
        location_exclusions=list(payload["location_exclusions"]),
        location_allowlist=list(payload["location_allowlist"]),
        max_age_days=int(payload["max_age_days"]),
    )


def preview_rules(conn: sqlite3.Connection, profile: prefilter.Profile, now: str) -> dict:
    """What these rules would cut, without writing a single verdict.

    `prefilter.run` is the committing version of this loop. Sharing `evaluate`
    means the preview cannot disagree with what the stage will do; not sharing
    `run` means looking costs nothing.
    """
    postings = conn.execute(
        "SELECT id, title, description_text, location_raw, published_at FROM postings"
    ).fetchall()

    by_reason: dict[str, int] = {}
    survived = 0
    for posting in postings:
        verdict = prefilter.evaluate(posting, profile, now)
        if verdict.rejected_reason:
            by_reason[verdict.rejected_reason] = by_reason.get(verdict.rejected_reason, 0) + 1
        else:
            survived += 1

    evaluated = len(postings)
    return {
        "evaluated": evaluated,
        "survived": survived,
        "by_reason": by_reason,
        "cut_ratio": 0.0 if not evaluated else 1 - survived / evaluated,
        "within_expected_band": (
            prefilter.MIN_CUT_RATIO <= (1 - survived / evaluated) <= prefilter.MAX_CUT_RATIO
            if evaluated else False
        ),
    }


def build_state(conn, labels_path, profile_path, threshold) -> dict:
    """Everything the page needs, in one response."""
    postings = scored_postings(conn)
    labels = evals.load_labels(labels_path) if Path(labels_path).is_file() else {}
    scores = {posting["url"]: posting["score"] for posting in postings}
    for posting in postings:
        posting["label"] = labels.get(posting["url"], "")

    profile = prefilter.load_profile(profile_path)
    return {
        "threshold": threshold,
        "postings": postings,
        "labelled": len(labels),
        "metrics": metrics_by_threshold(labels, scores) if labels else [],
        "rules": rules_payload(profile),
        "prefilter": preview_rules(conn, profile, db.iso_now()),
    }


# --- writes ------------------------------------------------------------------


def save_threshold(config_path: str | Path, threshold: int) -> None:
    """Persist the cut so the CLI stages agree with what the page shows.

    Rewrites the one key and leaves the rest of the file alone.
    """
    path = Path(config_path)
    config = yaml.safe_load(path.read_text())
    config["threshold"] = int(threshold)
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))


def save_rules(profile_path: str | Path, payload: dict) -> None:
    path = Path(profile_path)
    raw = yaml.safe_load(path.read_text())
    raw["stack"] = payload["stack"]
    raw["title_exclusions"] = payload["title_exclusions"]
    raw["junior_signals"] = payload["junior_signals"]
    raw["location_exclusions"] = payload["location_exclusions"]
    raw["location_allowlist"] = payload["location_allowlist"]
    raw["max_age_days"] = int(payload["max_age_days"])
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))


# --- server ------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """Four routes. Anything else is a 404, including a favicon."""

    protocol_version = "HTTP/1.1"

    def __init__(self, *args, context: dict, **kwargs):
        self.context = context
        super().__init__(*args, **kwargs)

    # Quiet by default: one line per request is noise when a page makes four.
    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict, status: int = 200) -> None:
        self._send(json.dumps(payload).encode("utf-8"), "application/json", status)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length) or b"{}")

    def _open_db(self) -> sqlite3.Connection:
        return db.connect(self.context["db_path"])

    def do_GET(self):
        if self.path == "/":
            self._send(TEMPLATE.read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            conn = self._open_db()
            try:
                self._json(build_state(
                    conn, self.context["labels"], self.context["profile"],
                    self.context["threshold"]))
            finally:
                conn.close()
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self):
        try:
            if self.path == "/api/threshold":
                threshold = int(self._body()["threshold"])
                save_threshold(self.context["config"], threshold)
                self.context["threshold"] = threshold
                log.info("threshold saved: %d", threshold)
                self._json({"saved": threshold})
            elif self.path == "/api/rules/preview":
                conn = self._open_db()
                try:
                    self._json(preview_rules(
                        conn, profile_from_payload(self._body()), db.iso_now()))
                finally:
                    conn.close()
            elif self.path == "/api/rules":
                payload = self._body()
                save_rules(self.context["profile"], payload)
                log.info("rules saved to %s", self.context["profile"])
                self._json({"saved": True})
            else:
                self._send(b"not found", "text/plain", 404)
        except Exception as error:            # noqa: BLE001 — surfaced, not swallowed
            log.exception("%s failed", self.path)
            self._json({"error": f"{type(error).__name__}: {error}"}, status=400)


def serve(context: dict, port: int, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(Handler, context=context))
    url = f"http://127.0.0.1:{port}/"
    print(f"jobfit ui on {url}   (ctrl-c to stop)")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=[url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = runtime.stage_parser("Read a run and choose where to cut it, in a browser.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--labels", default=str(evals.LABELS_PATH))
    parser.add_argument("--profile", default="profile/stack.yaml")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    runtime.configure_logging()

    config = runtime.load_config(args.config)
    serve(
        {
            "db_path": args.db or config["db_path"],
            "config": args.config,
            "labels": args.labels,
            "profile": args.profile,
            "threshold": runtime.threshold(args),
        },
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
