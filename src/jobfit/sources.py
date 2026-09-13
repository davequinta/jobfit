"""Feed parsers — one per source, all with the same contract.

`parse_x(body, feed_url, source) -> ParseResult`. Bytes in, postings and issues
out. A parser never raises and never touches the network, which is what lets the
whole set be tested against recorded fixtures, including the `_drift` variants
that pin what happens when a feed changes shape.

Everything a parser cannot use is dropped, counted, and reported with a sample
of the payload. The dangerous failure for an ingest is not a crash — it is a run
that quietly stores a third of what it should and looks fine.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import feedparser
from selectolax.parser import HTMLParser

SAMPLE_CHARS = 800


@dataclass
class Posting:
    """One normalised posting. Optional fields default so a parser only names
    what its source actually publishes."""

    dedupe_key: str
    source: str
    feed_url: str
    url: str
    canonical_url: str
    company: str
    title: str
    description_text: str
    published_at: str
    raw_json: str
    source_id: str | None = None
    location_raw: str | None = None
    category: str | None = None
    job_type: str | None = None
    tags: list[str] = field(default_factory=list)
    salary_raw: str | None = None
    description_html: str | None = None


@dataclass
class Issue:
    source: str
    feed_url: str | None
    kind: str
    detail: str
    sample: str | None = None


@dataclass
class ParseResult:
    postings: list[Posting] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    fetched: int = 0  # records the feed offered, including the ones we dropped


# --- identity ----------------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def normalize_field(value: str) -> str:
    """Casefold, drop punctuation, collapse whitespace.

    "Acme,  Inc. " and "acme inc" are the same employer as far as dedupe cares.
    """
    return _SPACE.sub(" ", _PUNCT.sub(" ", value.casefold())).strip()


def canonical_url(url: str) -> str:
    """Strip the parts of a URL that do not identify the posting.

    Feeds hand out the same posting with different tracking parameters; without
    this the dedupe key changes every time the source tweaks its UTM tags.
    """
    parts = urlsplit(url.strip())
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def dedupe_key(company: str, title: str, url: str) -> str:
    joined = f"{normalize_field(company)}|{normalize_field(title)}|{canonical_url(url)}"
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


# --- text and time -----------------------------------------------------------


def html_to_text(html: str) -> str:
    text = HTMLParser(html).text(separator="\n") if html else ""
    return _SPACE.sub(" ", text).strip()


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:  # feeds that omit a zone are UTC by convention
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def sample_of(record: object) -> str:
    text = record if isinstance(record, str) else json.dumps(record, default=str)
    return text[:SAMPLE_CHARS]


def _require(record: dict, keys: tuple[str, ...]) -> str | None:
    """Return the name of the first required key that is missing or empty."""
    for key in keys:
        if not str(record.get(key) or "").strip():
            return key
    return None


# --- storage -----------------------------------------------------------------




def load_json(body: bytes, key: str | None, source: str, feed_url: str,
              result: ParseResult):
    """Decode a JSON body, appending a `parse_error` issue instead of raising.

    Three of the five sources repeat this exact shape, and the failure they are
    guarding against is identical: an HTML error page served where JSON was
    promised.
    """
    try:
        payload = json.loads(body)
        records = payload[key] if key else payload
        if not isinstance(records, list):
            raise TypeError(f"{key or 'payload'} is {type(records).__name__}, expected list")
        return records
    except Exception as exc:
        result.issues.append(Issue(
            source, feed_url, "parse_error", f"{type(exc).__name__}: {exc}",
            sample_of(body[:SAMPLE_CHARS].decode(errors="replace"))))
        return None


# --- parsers -----------------------------------------------------------------
#
# Both parsers take bytes and return a ParseResult. They never raise for bad
# input and never touch the network, which is what makes the whole stage
# testable against recorded fixtures.


def parse_remotive(body: bytes, feed_url: str, source: str) -> ParseResult:
    result = ParseResult()
    records = load_json(body, "jobs", source, feed_url, result)
    if records is None:
        return result

    result.fetched = len(records)
    if not records:
        result.issues.append(
            Issue(source, feed_url, "empty_feed", "feed returned zero records")
        )
        return result

    for record in records:
        missing = _require(record, ("company_name", "title", "url", "publication_date"))
        if missing:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"record has no usable '{missing}'", sample_of(record))
            )
            continue
        try:
            published = iso_utc(datetime.fromisoformat(record["publication_date"]))
        except ValueError as exc:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"unparseable 'publication_date': {exc}", sample_of(record))
            )
            continue

        html = record.get("description") or ""
        result.postings.append(
            Posting(
                dedupe_key=dedupe_key(record["company_name"], record["title"], record["url"]),
                source=source,
                source_id=str(record["id"]) if record.get("id") is not None else None,
                feed_url=feed_url,
                url=record["url"],
                canonical_url=canonical_url(record["url"]),
                company=record["company_name"].strip(),
                title=record["title"].strip(),
                location_raw=record.get("candidate_required_location") or None,
                category=record.get("category") or None,
                job_type=record.get("job_type") or None,
                tags=[str(t) for t in record.get("tags") or []],
                salary_raw=record.get("salary") or None,
                description_html=html or None,
                description_text=html_to_text(html),
                published_at=published,
                raw_json=json.dumps(record, ensure_ascii=False),
            )
        )
    return result


def parse_wwr(body: bytes, feed_url: str, source: str) -> ParseResult:
    """We Work Remotely RSS.

    WWR has no company element; it packs the employer into the title as
    "Company: Role". That convention is the fragile part of this parser, so an
    item that does not follow it is dropped and reported rather than stored with
    the whole string as a title.
    """
    result = ParseResult()
    feed = feedparser.parse(body)
    # feedparser is lenient: handed a 502 HTML error page it reports no error
    # and no entries, which would be logged as an empty feed. `version` is
    # empty for anything it did not recognise as a feed at all.
    if not feed.version and not feed.entries:
        result.issues.append(
            Issue(source, feed_url, "parse_error", "response is not a feed",
                  sample_of(body[:SAMPLE_CHARS].decode(errors="replace")))
        )
        return result
    if feed.bozo and not feed.entries:
        result.issues.append(
            Issue(source, feed_url, "parse_error",
                  f"unparseable feed: {feed.bozo_exception}",
                  sample_of(body[:SAMPLE_CHARS].decode(errors="replace")))
        )
        return result

    result.fetched = len(feed.entries)
    if not feed.entries:
        result.issues.append(
            Issue(source, feed_url, "empty_feed", "feed returned zero items")
        )
        return result

    for entry in feed.entries:
        record = {k: entry.get(k) for k in ("title", "link", "published", "region", "category", "guid")}
        missing = _require(record, ("title", "link", "published"))
        if missing:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"item has no usable '{missing}'", sample_of(record))
            )
            continue

        company, separator, title = entry["title"].partition(":")
        if not separator or not title.strip():
            result.issues.append(
                Issue(source, feed_url, "unparsed_title",
                      "title does not follow the 'Company: Role' convention",
                      sample_of(record))
            )
            continue

        try:
            published = iso_utc(parsedate_to_datetime(entry["published"]))
        except (TypeError, ValueError) as exc:
            result.issues.append(
                Issue(source, feed_url, "missing_field",
                      f"unparseable 'pubDate': {exc}", sample_of(record))
            )
            continue

        html = entry.get("description") or entry.get("summary") or ""
        result.postings.append(
            Posting(
                dedupe_key=dedupe_key(company, title, entry["link"]),
                source=source,
                source_id=entry.get("guid") or None,
                feed_url=feed_url,
                url=entry["link"],
                canonical_url=canonical_url(entry["link"]),
                company=company.strip(),
                title=title.strip(),
                location_raw=entry.get("region") or None,
                category=entry.get("category") or None,
                job_type=entry.get("type") or None,
                tags=[],
                salary_raw=None,
                description_html=html or None,
                description_text=html_to_text(html),
                published_at=published,
                raw_json=json.dumps(dict(entry), ensure_ascii=False, default=str),
            )
        )
    return result


def parse_getonbrd(body: bytes, feed_url: str, source: str, resolve_company=None) -> ParseResult:
    """Get on Board — JSON:API, LATAM-focused, with structured salary numbers.

    `company` arrives as a bare relationship id and the API supports no
    `include`, so names come from an injected resolver that the fetcher backs
    with a cache. A company that will not resolve drops the posting rather than
    storing it under an id nobody can read.
    """
    result = ParseResult()
    records = load_json(body, "data", source, feed_url, result)
    if records is None:
        return result

    result.fetched = len(records)
    if not records:
        result.issues.append(Issue(source, feed_url, "empty_feed", "feed returned zero records"))
        return result

    for record in records:
        attributes = record.get("attributes") or {}
        url = (record.get("links") or {}).get("public_url")
        missing = _require(attributes, ("title", "published_at"))
        if missing or not url:
            result.issues.append(Issue(source, feed_url, "missing_field",
                                       f"record has no usable '{missing or 'public_url'}'",
                                       sample_of(record)))
            continue

        company_id = ((attributes.get("company") or {}).get("data") or {}).get("id")
        company = resolve_company(company_id) if (resolve_company and company_id) else None
        if not company:
            result.issues.append(Issue(source, feed_url, "missing_field",
                                       f"could not resolve a company name for id {company_id!r}",
                                       sample_of(record)))
            continue

        html = "\n".join(
            str(attributes.get(part) or "")
            for part in ("description", "functions", "desirable", "projects", "benefits")
        )
        low, high = attributes.get("min_salary"), attributes.get("max_salary")
        salary = f"{low} - {high} USD/month" if low and high else None

        result.postings.append(Posting(
            dedupe_key=dedupe_key(company, attributes["title"], url),
            source=source,
            source_id=str(record.get("id")) if record.get("id") is not None else None,
            feed_url=feed_url,
            url=url,
            canonical_url=canonical_url(url),
            company=company.strip(),
            title=attributes["title"].strip(),
            location_raw=", ".join(attributes.get("countries") or []) or None,
            category=attributes.get("category_name"),
            job_type=attributes.get("remote_modality"),
            tags=[str(p) for p in attributes.get("perks") or []],
            salary_raw=salary,
            description_html=html or None,
            description_text=html_to_text(html),
            published_at=iso_utc(datetime.fromtimestamp(attributes["published_at"], timezone.utc)),
            raw_json=json.dumps(record, ensure_ascii=False),
        ))
    return result


def parse_remoteok(body: bytes, feed_url: str, source: str) -> ParseResult:
    """Remote OK — a flat JSON array whose first element is a legal notice."""
    result = ParseResult()
    records = load_json(body, None, source, feed_url, result)
    if records is None:
        return result

    # The terms-of-service element has no slug; everything else is a posting.
    records = [r for r in records if isinstance(r, dict) and "slug" in r]
    result.fetched = len(records)
    if not records:
        result.issues.append(Issue(source, feed_url, "empty_feed", "no postings in the array"))
        return result

    for record in records:
        missing = _require(record, ("company", "position", "url", "date"))
        if missing:
            result.issues.append(Issue(source, feed_url, "missing_field",
                                       f"record has no usable '{missing}'", sample_of(record)))
            continue
        try:
            published = iso_utc(datetime.fromisoformat(record["date"]))
        except ValueError as exc:
            result.issues.append(Issue(source, feed_url, "missing_field",
                                       f"unparseable 'date': {exc}", sample_of(record)))
            continue

        # The API sends 0/0 rather than null when no range is published; storing
        # "0 - 0" would let the scorer read it as a real and terrible offer.
        low, high = record.get("salary_min") or 0, record.get("salary_max") or 0
        salary = f"{low} - {high}" if low and high else None

        html = record.get("description") or ""
        result.postings.append(Posting(
            dedupe_key=dedupe_key(record["company"], record["position"], record["url"]),
            source=source,
            source_id=str(record.get("id")) if record.get("id") is not None else None,
            feed_url=feed_url,
            url=record["url"],
            canonical_url=canonical_url(record["url"]),
            company=record["company"].strip(),
            title=record["position"].strip(),
            location_raw=record.get("location") or None,
            category=None,
            job_type=None,
            tags=[str(tag) for tag in record.get("tags") or []],
            salary_raw=salary,
            description_html=html or None,
            description_text=html_to_text(html),
            published_at=published,
            raw_json=json.dumps(record, ensure_ascii=False),
        ))
    return result


# HN comments open with "Company | Role | Location | ...". Everything after the
# first separator is treated as the title: one comment often advertises several
# roles, and splitting them is guesswork the scorer does better with full text.
HN_SEPARATORS = re.compile(r"\s*[|]\s*|\s+[-\u2013\u2014]\s+")
HN_TRAILING_URL = re.compile(r"\(?\s*https?://\S+\s*\)?")

# Plenty of comments open with a sentence instead of pipes — "Sumble is the
# newco from the founders of Kaggle. We are hiring…". Auditing a real run found
# 19 of 243 comments dropped; about half were job ads that open with a sentence,
# the rest job seekers' posts, `[flagged]` comments and chatter. So the name is
# recovered from the words before the first verb. Dropping a real posting is
# the expensive error here; a slightly wrong company name is not.
HN_PROSE_VERB = re.compile(
    r"\s+(?:is|are|was|were|has|have|builds?|makes?|provides?|helps?|does|"
    r"powers?|creates?|develops?|runs?|offers?|works?|seeks?|needs?|wants?)\s",
    re.IGNORECASE,
)
HN_MAX_COMPANY_CHARS = 60

# The prose fallback is permissive by design, so it needs a floor: thread
# chatter ("Is this thread still active?") parses just as cleanly as a job ad.
# A comment with none of these words is not an advertisement.
HN_HIRING_SIGNAL = re.compile(
    r"\b(hiring|we're looking|we are looking|seeking|join us|apply|role|roles|"
    r"position|engineer|engineering|developer|full[- ]?stack|backend|frontend)\b",
    re.IGNORECASE,
)


def _hn_from_prose(text: str) -> tuple[str, str]:
    """Company and role out of a comment that opens with a sentence.

    The verb is searched in the running text rather than in a split sentence:
    "Open Source Security, Inc. is hiring…" splits at the abbreviation and loses
    the company's own full stop.
    """
    cleaned = " ".join(HN_TRAILING_URL.sub(" ", text).split())
    if not HN_HIRING_SIGNAL.search(cleaned):
        return "", ""

    verb = HN_PROSE_VERB.search(cleaned[:HN_MAX_COMPANY_CHARS + 40])
    if not verb:
        return "", ""
    company = cleaned[: verb.start()].strip(" ,-–—:")
    if not company or len(company) > HN_MAX_COMPANY_CHARS:
        return "", ""
    return company, cleaned[:200]


def parse_hn(body: bytes, feed_url: str, source: str) -> ParseResult:
    """Hacker News "Who is hiring" — one top-level comment per posting.

    The messiest source and the highest signal: these are posted by the company
    itself rather than relayed by a board. A comment that does not follow the
    pipe convention is almost always thread chatter, so it is reported and
    dropped instead of being stored with a paragraph as its title.
    """
    result = ParseResult()
    children = load_json(body, "children", source, feed_url, result)
    if children is None:
        return result

    comments = [c for c in children if c.get("text")]
    result.fetched = len(comments)
    if not comments:
        result.issues.append(Issue(source, feed_url, "empty_feed", "thread has no comments"))
        return result

    for comment in comments:
        text = html_to_text(comment["text"])
        first_line = text.split(". ")[0]
        parts = [p for p in HN_SEPARATORS.split(first_line) if p.strip()]
        company = HN_TRAILING_URL.sub("", parts[0]).strip() if parts else ""
        title = " | ".join(parts[1:])[:200] if len(parts) > 1 else ""

        if not title:
            company, title = _hn_from_prose(text)

        if not company or not title:
            result.issues.append(Issue(source, feed_url, "unparsed_title",
                                       "comment carries no recognisable company and role",
                                       sample_of(text)))
            continue
        url = f"https://news.ycombinator.com/item?id={comment['id']}"
        result.postings.append(Posting(
            dedupe_key=dedupe_key(company, title, url),
            source=source,
            source_id=str(comment["id"]),
            feed_url=feed_url,
            url=url,
            canonical_url=canonical_url(url),
            company=company,
            title=title,
            location_raw=None,       # stated in prose; stage 2 and 3 read the body
            category=None,
            job_type=None,
            tags=[],
            salary_raw=None,
            description_html=comment["text"],
            description_text=text,
            published_at=iso_utc(datetime.fromisoformat(comment["created_at"])),
            raw_json=json.dumps(comment, ensure_ascii=False),
        ))
    return result


PARSERS = {
    "remotive_json": parse_remotive,
    "rss": parse_wwr,
    "getonbrd_json": parse_getonbrd,
    "remoteok_json": parse_remoteok,
    "hn_hiring": parse_hn,
}


