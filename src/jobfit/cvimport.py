"""Turn the CV you already have into the one the scorer reads.

`jobfit init` scaffolds a blank `profile/cv.md`, and filling it in by hand is the
biggest piece of setup friction in the whole tool — everybody already has a CV,
it is just a PDF. One conversion call fixes that.

Two guardrails, because this is the one place where a helpful-sounding failure
does real damage:

- The instructions forbid inventing anything. A converter that smooths a sparse
  CV into an impressive one hands its owner a document they cannot defend in an
  interview, and they will not notice until they are in one.
- A document that extracts to no text is refused rather than converted. A
  scanned PDF has no text layer, and converting nothing produces a confident,
  entirely fictional CV.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

log = logging.getLogger("jobfit.cv")

CV_PATH = Path("profile/cv.md")
SUPPORTED = (".pdf", ".md", ".txt")

MODEL = "claude-sonnet-5"
MAX_TOKENS = 8192

SYSTEM = """You convert a CV into the markdown format a job-scoring tool reads.

Rules, in order of importance:

1. Do not invent anything. Every date, employer, title, technology and
   achievement must come from the source. If the source is vague, stay vague.
   Never add a metric, team size, or outcome that is not there. The person will
   be asked about every line of this in an interview.
2. Do not omit work history. Every role in the source appears in the output.
3. Do not editorialise. You are reformatting, not improving.

Structure the output as:

# Name

**Title/positioning line**

**Location:** Country (UTC offset). Remote or not. State the working-hours
overlap, and state where the person is NOT eligible if the source implies it —
"no US work authorization" for someone with no US address or visa mentioned.
Infer the country from a phone country code or address if that is all there is,
and say it is inferred. This section scores 20 of 100 points in the tool and
almost no CV states it explicitly, so build it from whatever evidence exists.

Get the overlap arithmetic right, because it is usually the person's strongest
argument and it is easy to state backwards. A small offset means a large
overlap: two hours from US Eastern means a standard working day overlaps almost
entirely, just shifted — not "two hours of overlap". Say how much of a 9-to-5 in
the target timezone the person is awake for, not how many hours the clocks
differ.

One summary paragraph, from the source's own summary if it has one.

## Work experience

### Title — Company
**Start – End.** What they owned. Bullets for specifics.

## Tech stack
Grouped bullets: frontend, backend, cloud, databases, other.

## Education
## Certifications
## Community          (omit any section the source has nothing for)

## Seniority target
The levels this person should be targeting, and how many years of experience
the dates add up to. Today's date is given in the user message — compute from
the earliest start date to that date and show the arithmetic. Never estimate
this: a wrong figure here costs real points against postings that state a
minimum. If the source states a number that disagrees with its own dates, use
the dates and note the discrepancy.

Omit referees and other people's contact details entirely — they are third-party
personal data and irrelevant to scoring.

Return only the markdown. No preamble, no code fence."""


def extract_text(path: str | Path) -> str:
    """Plain text out of a PDF, markdown or text file."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED:
        raise ValueError(
            f"{path.suffix or 'that format'} is not supported — use one of {', '.join(SUPPORTED)}"
        )

    if path.suffix.lower() == ".pdf":
        try:
            import pypdf
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise ValueError("reading PDFs needs pypdf: pip install pypdf") from exc
        reader = pypdf.PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    else:
        text = path.read_text()

    if not text.strip():
        raise ValueError(
            f"{path} extracted no text. A scanned PDF has no text layer — export a "
            "text-based PDF, or paste the CV into a .md file and pass that instead."
        )
    return text


def build_request(raw: str, today: str | None = None) -> dict:
    """`today` is passed in because the model does not reliably know the date.

    Left to itself it computes years of experience against its training cutoff
    and undercounts — which is exactly the error this converter exists to stop.
    """
    today = today or date.today().isoformat()
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM,
        "messages": [{"role": "user",
                      "content": f"Today's date is {today}.\n\n{raw}"}],
    }


def convert(client, raw: str, today: str | None = None) -> str:
    response = client.messages.create(**build_request(raw, today))
    return "".join(block.text for block in response.content if block.type == "text")


def main(argv: list[str] | None = None, client=None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a CV (PDF, markdown or text) into profile/cv.md.")
    parser.add_argument("source", help="path to your existing CV")
    parser.add_argument("--out", default=str(CV_PATH))
    parser.add_argument("--force", action="store_true", help="overwrite an existing profile/cv.md")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", stream=sys.stderr
    )

    source = Path(args.source)
    if not source.is_file():
        print(f"jobfit cv: {source} does not exist", file=sys.stderr)
        return 2

    out = Path(args.out)
    if out.exists() and not args.force:
        print(f"jobfit cv: {out} already exists — pass --force to overwrite it",
              file=sys.stderr)
        return 2

    try:
        raw = extract_text(source)
    except ValueError as exc:
        print(f"jobfit cv: {exc}", file=sys.stderr)
        return 2

    if client is None:  # pragma: no cover - real runs only
        import anthropic
        client = anthropic.Anthropic()

    log.info("converting %s (%d characters)", source, len(raw))
    markdown = convert(client, raw)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown)
    print(f"wrote {out} ({len(markdown)} characters)")

    if "location:" not in markdown.lower():
        print(
            "\nWARNING: the result has no Location line. Eligibility is 20 of the 100\n"
            "rubric points, so add one by hand — country, UTC offset, and where you\n"
            "are not authorised to work.",
            file=sys.stderr,
        )
    print("Read it before scoring: it is what every posting gets compared against.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
