"""Polite HTTP: one request per second per host, identified, robots-aware.

Kept apart from the ingest stage so the rate limiter and the robots evaluator
can be tested as the pure things they are, with no feed or database in sight.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import urlsplit

import httpx
from protego import Protego

log = logging.getLogger("jobfit.http")


# --- politeness --------------------------------------------------------------


class RateLimiter:
    """One request per host per `min_interval` seconds. No parallel fetching."""

    def __init__(self, min_interval: float = 1.0, monotonic=time.monotonic, sleep=time.sleep):
        self.min_interval = min_interval
        self._monotonic = monotonic
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        previous = self._last.get(host)
        now = self._monotonic()
        if previous is not None:
            remaining = self.min_interval - (now - previous)
            if remaining > 0:
                self._sleep(remaining)
                now = self._monotonic()
        self._last[host] = now


def robots_allows(robots_txt: str | None, url: str, user_agent: str) -> bool:
    """Evaluate a robots.txt body against a URL. Missing file means allowed.

    Matching follows RFC 9309, the way Google reads it: `*` matches any run of
    characters, a trailing `$` anchors the end, the longest matching rule wins,
    and Allow beats Disallow on a tie. This used to be the standard library's
    robotparser, which before Python 3.14 reads `*` as a literal character — so
    `Disallow: /api/*` did not block `/api/jobs`, and every wildcard rule was
    ignored without a word while the README said robots.txt was honoured.
    """
    if robots_txt is None:
        return True
    return Protego.parse(robots_txt).can_fetch(url, user_agent)


class PoliteFetcher:
    """The real HTTP client: rate limited, identified, robots.txt aware."""

    def __init__(self, user_agent: str, min_interval: float = 1.0, timeout: float = 30.0):
        self.user_agent = user_agent
        self.limiter = RateLimiter(min_interval)
        self.client = httpx.Client(
            headers={"User-Agent": user_agent}, timeout=timeout, follow_redirects=True
        )
        self._robots: dict[str, str | None] = {}

    def allowed(self, url: str) -> bool:
        return robots_allows(self._robots_for(url), url, self.user_agent)

    def _robots_for(self, url: str) -> str | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self.limiter.wait(parts.netloc)
            try:
                response = self.client.get(f"{origin}/robots.txt")
                self._robots[origin] = response.text if response.status_code == 200 else None
            except httpx.HTTPError as exc:
                log.warning("could not read %s/robots.txt (%s); assuming allowed", origin, exc)
                self._robots[origin] = None
        return self._robots[origin]

    def get(self, url: str) -> bytes:
        self.limiter.wait(urlsplit(url).netloc)
        response = self.client.get(url)
        response.raise_for_status()
        return response.content

    def close(self) -> None:
        self.client.close()


