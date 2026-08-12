"""Scanner — fetches opportunities from RSS feeds, APIs, and web search."""

import feedparser
import html
import logging
import time
from dataclasses import dataclass, field

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from .config import Config

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Snipe-Bot/2.0"}
TIMEOUT = httpx.Timeout(15.0)
MAX_SOURCE_FAILURES = 3

UNSTOP_API = "https://unstop.com/api/public/opportunity/search-result"
UNSTOP_PER_PAGE = 15
UNSTOP_PAGE_DELAY = 0.5  # seconds between paginated requests

# Map Unstop opportunity types/subtypes to Snipe categories
UNSTOP_TYPE_MAP = {
    "hackathons": "hackathon",
    "competitions": "competition",
    "internships": "internship",
    "scholarships": "grant",
}


@dataclass
class ScanResult:
    """A single raw opportunity found by the scanner."""
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    category: str = "other"


@dataclass
class ScanReport:
    """Aggregated results of a full scan pass."""
    items: list[ScanResult] = field(default_factory=list)
    sources_checked: int = 0
    errors: int = 0
    duration: float = 0.0


class Scanner:
    def __init__(self, config: Config):
        self.config = config
        self.keywords = config.keywords
        self._source_failures: dict[str, int] = {}

    def scan(self) -> ScanReport:
        """Scan all configured sources. Returns a ScanReport."""
        start = time.time()
        report = ScanReport()
        raw: list[ScanResult] = []

        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            for source in self.config.sources.get("rss", []):
                report.sources_checked += 1
                name = source.get("name", source.get("url", "unknown"))

                if self._source_failures.get(name, 0) >= MAX_SOURCE_FAILURES:
                    logger.warning("Skipping %s (failed %d consecutive times)", name, self._source_failures[name])
                    continue

                try:
                    if "ctftime.org" in source["url"]:
                        items = self._scan_ctftime(client, source["url"], name)
                    else:
                        items = self._scan_rss(client, source["url"], name)
                    raw.extend(items)
                    self._source_failures[name] = 0
                except Exception as e:
                    report.errors += 1
                    self._source_failures[name] = self._source_failures.get(name, 0) + 1
                    logger.warning("RSS feed failed: %s — %s", name, e)

            # Unstop (dedicated paginated scanner)
            unstop_cfg = self.config.unstop
            if unstop_cfg.get("enabled", False):
                report.sources_checked += 1
                if self._source_failures.get("Unstop", 0) >= MAX_SOURCE_FAILURES:
                    logger.warning("Skipping Unstop (failed %d consecutive times)", self._source_failures["Unstop"])
                else:
                    try:
                        items = self._scan_unstop_paginated(
                            client,
                            types=unstop_cfg.get("types", ["hackathons", "competitions", "internships", "scholarships"]),
                            max_pages=unstop_cfg.get("max_pages", 3),
                        )
                        raw.extend(items)
                        self._source_failures["Unstop"] = 0
                    except Exception as e:
                        report.errors += 1
                        self._source_failures["Unstop"] = self._source_failures.get("Unstop", 0) + 1
                        logger.warning("Unstop scan failed: %s", e)

        # Web search queries
        for query in self.config.sources.get("search_queries", []):
            report.sources_checked += 1
            try:
                items = self._scan_web(query)
                raw.extend(items)
            except Exception as e:
                report.errors += 1
                logger.warning("Web search failed: %s — %s", query, e)

        # Keyword filter
        for item in raw:
            text = (item.title + " " + item.snippet).lower()
            if any(kw.lower() in text for kw in self.keywords):
                report.items.append(item)

        report.duration = time.time() - start
        logger.info(
            "Scan complete — %d sources, %d raw, %d matched, %d errors, %.1fs",
            report.sources_checked, len(raw), len(report.items), report.errors, report.duration,
        )
        return report

    # ── Source parsers ───────────────────────────────────────

    def _scan_rss(self, client: httpx.Client, url: str, source: str) -> list[ScanResult]:
        resp = self._get(client, url)
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:20]:
            title = html.unescape(entry.get("title", "").strip())
            link = entry.get("link", "")
            snippet = ""
            if hasattr(entry, "summary"):
                snippet = BeautifulSoup(entry.summary, "lxml").get_text()[:300]
            if title and link:
                items.append(ScanResult(title=title, url=link, snippet=snippet, source=source))
        return items

    def _scan_unstop_paginated(
        self, client: httpx.Client, types: list[str], max_pages: int = 3,
    ) -> list[ScanResult]:
        """Scan Unstop search API across multiple opportunity types with pagination."""
        items: list[ScanResult] = []

        for opp_type in types:
            category = UNSTOP_TYPE_MAP.get(opp_type, "other")
            source_label = f"Unstop ({opp_type})"

            for page in range(1, max_pages + 1):
                try:
                    resp = self._get(client, UNSTOP_API, params={
                        "opportunity": opp_type,
                        "per_page": UNSTOP_PER_PAGE,
                        "page": page,
                    })
                    data = resp.json()
                except Exception as e:
                    logger.warning("Unstop %s page %d failed: %s", opp_type, page, e)
                    break  # stop paginating this type, continue with next

                entries = data.get("data", {}).get("data", [])
                if not isinstance(entries, list) or not entries:
                    break  # no more results

                for entry in entries:
                    # Only include live, open-registration opportunities
                    if entry.get("status") != "LIVE" or not entry.get("regn_open"):
                        continue

                    title = entry.get("title", "").strip()
                    link = entry.get("seo_url", "")
                    if not link:
                        public_path = entry.get("public_url", "")
                        if public_path:
                            link = f"https://unstop.com/{public_path}"
                    if not title or not link:
                        continue

                    # Build snippet from details HTML
                    details = entry.get("details", "") or ""
                    snippet = BeautifulSoup(str(details), "lxml").get_text()[:300].strip()

                    # Determine Snipe category — internships have type=jobs, subtype=internships
                    entry_cat = category
                    if entry.get("type") == "jobs" and entry.get("subtype") == "internships":
                        entry_cat = "internship"

                    items.append(ScanResult(
                        title=title, url=link, snippet=snippet,
                        source=source_label, category=entry_cat,
                    ))

                # Check if we've reached the last page
                last_page = data.get("data", {}).get("last_page", 1)
                if page >= last_page:
                    break

                time.sleep(UNSTOP_PAGE_DELAY)

            logger.debug("Unstop %s: collected %d items", opp_type,
                         sum(1 for i in items if i.source == source_label))

        logger.info("Unstop scan: %d items across %d types", len(items), len(types))
        return items

    def _scan_ctftime(self, client: httpx.Client, url: str, source: str) -> list[ScanResult]:
        """Parse CTFtime RSS feed — all results pre-tagged as 'ctf'."""
        resp = self._get(client, url)
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:20]:
            title = html.unescape(entry.get("title", "").strip())
            link = entry.get("link", "")
            snippet = ""
            if hasattr(entry, "summary"):
                snippet = BeautifulSoup(entry.summary, "lxml").get_text()[:300]
            if title and link:
                items.append(ScanResult(
                    title=title, url=link, snippet=snippet,
                    source=source, category="ctf",
                ))
        return items

    def _scan_web(self, query: str) -> list[ScanResult]:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        items = []
        for r in results:
            items.append(ScanResult(
                title=r.get("title", ""),
                url=r.get("href", ""),
                snippet=r.get("body", "")[:300],
                source="Web Search",
            ))
        return items

    # ── HTTP helper with retry ───────────────────────────────

    def _get(self, client: httpx.Client, url: str, retries: int = 3, params: dict | None = None) -> httpx.Response:
        """GET with exponential backoff retry."""
        for attempt in range(retries):
            try:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                return resp
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                if attempt == retries - 1:
                    raise
                wait = 2 ** attempt
                logger.debug("Retry %d/%d for %s (wait %ds): %s", attempt + 1, retries, url, wait, e)
                time.sleep(wait)
        raise RuntimeError("unreachable")  # satisfies type checker
