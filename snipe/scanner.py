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
                    if "unstop.com/api" in source["url"]:
                        items = self._scan_unstop(client, source["url"], name)
                    elif "ctftime.org" in source["url"]:
                        items = self._scan_ctftime(client, source["url"], name)
                    else:
                        items = self._scan_rss(client, source["url"], name)
                    raw.extend(items)
                    self._source_failures[name] = 0
                except Exception as e:
                    report.errors += 1
                    self._source_failures[name] = self._source_failures.get(name, 0) + 1
                    logger.warning("RSS feed failed: %s — %s", name, e)

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

    def _scan_unstop(self, client: httpx.Client, url: str, source: str) -> list[ScanResult]:
        """Parse Unstop JSON API response."""
        resp = self._get(client, url)
        data = resp.json()
        items = []
        entries = data.get("data", {}).get("data", [])
        if isinstance(entries, list):
            for entry in entries[:20]:
                title = entry.get("title", "").strip()
                link = entry.get("public_url", "")
                if not link:
                    slug = entry.get("seo_url") or entry.get("slug", "")
                    opp_type = entry.get("type", "competition")
                    if slug:
                        link = f"https://unstop.com/{opp_type}/{slug}"
                snippet = entry.get("seo_details", {}).get("seo_description", "")
                if not snippet:
                    snippet = entry.get("subtitle", "") or entry.get("description", "")
                snippet = BeautifulSoup(str(snippet), "lxml").get_text()[:300]
                if title and link:
                    items.append(ScanResult(title=title, url=link, snippet=snippet, source=source))
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

    def _get(self, client: httpx.Client, url: str, retries: int = 3) -> httpx.Response:
        """GET with exponential backoff retry."""
        for attempt in range(retries):
            try:
                resp = client.get(url)
                resp.raise_for_status()
                return resp
            except (httpx.HTTPError, httpx.TimeoutException) as e:
                if attempt == retries - 1:
                    raise
                wait = 2 ** attempt
                logger.debug("Retry %d/%d for %s (wait %ds): %s", attempt + 1, retries, url, wait, e)
                time.sleep(wait)
        raise RuntimeError("unreachable")  # satisfies type checker
