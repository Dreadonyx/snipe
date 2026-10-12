"""Scanner — fetches opportunities from RSS feeds, APIs, LinkedIn, and web search."""

import html
import logging
import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from itertools import zip_longest

import feedparser
import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS

from .config import Config
from .urls import normalize_url

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Snipe-Bot/2.0"}
TIMEOUT = httpx.Timeout(15.0)
MAX_SOURCE_FAILURES = 3
SOURCE_BACKOFF = 3600  # seconds a failing source sits out before it is retried

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
CURRENCY_SYMBOLS = {"fa-rupee": "₹", "fa-dollar": "$", "fa-euro": "€"}

APIFY_RUN_SYNC = "https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items"
APIFY_TIMEOUT = 240  # seconds; Apify's sync endpoint gives up at 300
LINKEDIN_EVENT_RE = re.compile(r"^https://([a-z]{2,3}|www)\.linkedin\.com/events/[^/?#]+")


@dataclass
class ScanResult:
    """A single raw opportunity found by the scanner."""
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    category: str = "other"
    curated: bool = False   # purpose-built source: skip the keyword filter
    trusted: bool = False   # structured, known-good data: skip the LLM filter too
    deadline: str = ""
    prize: str = ""
    when: str = ""
    location: str = ""
    organizer: str = ""
    # Structured facts for per-user filters; empty/None means "unknown" and never filters an item out
    mode: str = ""                  # online | offline | hybrid
    city: str = ""
    paid: bool | None = None        # internships only
    stipend: int = 0                # highest monthly stipend, internships only
    deadline_date: date | None = None


@dataclass
class ScanReport:
    """Aggregated results of a full scan pass."""
    items: list[ScanResult] = field(default_factory=list)
    sources_checked: int = 0
    errors: int = 0
    duration: float = 0.0


class Scanner:
    """Long-lived: keeps per-source failure counts and LinkedIn run timing between scans."""

    def __init__(self, config: Config):
        self.config = config
        self.keywords = [kw.lower() for kw in config.keywords]
        self._source_failures: dict[str, int] = {}
        self._skip_until: dict[str, float] = {}
        self._linkedin_last_run = 0.0

    def scan(self) -> ScanReport:
        """Scan all configured sources concurrently. Returns a ScanReport."""
        start = time.time()
        report = ScanReport()

        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            tasks = self._build_tasks(client)
            report.sources_checked = len(tasks)
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(self._run_source, name, fn) for name, fn in tasks]
                batches = []
                for fut in futures:
                    items = fut.result()
                    if items is None:
                        report.errors += 1
                    else:
                        batches.append(items)

        # Interleave sources so a per-scan alert cap doesn't starve any one of them
        raw = [item for group in zip_longest(*batches) for item in group if item is not None]

        seen_urls: set[str] = set()
        for item in raw:
            item.url = normalize_url(item.url)
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            if item.curated or self._matches_keywords(item):
                report.items.append(item)

        report.duration = time.time() - start
        logger.info(
            "Scan complete — %d sources, %d raw, %d matched, %d errors, %.1fs",
            report.sources_checked, len(raw), len(report.items), report.errors, report.duration,
        )
        return report

    def _build_tasks(self, client: httpx.Client) -> list[tuple[str, Callable[[], list[ScanResult]]]]:
        tasks = []
        for source in self.config.sources.get("rss", []):
            name = source.get("name", source.get("url", "unknown"))
            url = source["url"]
            if "ctftime.org" in url:
                tasks.append((name, lambda u=url, n=name: self._scan_ctftime(client, u, n)))
            else:
                tasks.append((name, lambda u=url, n=name: self._scan_rss(client, u, n)))

        unstop_cfg = self.config.unstop
        if unstop_cfg.get("enabled", False):
            tasks.append(("Unstop", lambda: self._scan_unstop_paginated(
                client,
                types=unstop_cfg.get("types", list(UNSTOP_TYPE_MAP)),
                max_pages=unstop_cfg.get("max_pages", 2),
            )))

        if self._linkedin_due():
            # Counted as a run even if it fails: an Apify run that times out still bills
            self._linkedin_last_run = time.time()
            tasks.append(("LinkedIn", lambda: self._scan_linkedin(client)))

        queries = self.config.sources.get("search_queries", [])
        if queries:
            # One task, run sequentially — parallel queries get rate-limited by DuckDuckGo
            tasks.append(("Web Search", lambda: self._scan_web_queries(queries)))
        return tasks

    def _run_source(self, name: str, fn: Callable[[], list[ScanResult]]) -> list[ScanResult] | None:
        """Run one source with a circuit breaker. Returns None on failure, [] if skipped."""
        if time.time() < self._skip_until.get(name, 0):
            logger.info("Skipping %s (backing off after %d failures)", name, self._source_failures[name])
            return []
        try:
            items = fn()
        except Exception as e:
            failures = self._source_failures.get(name, 0) + 1
            self._source_failures[name] = failures
            if failures >= MAX_SOURCE_FAILURES:
                self._skip_until[name] = time.time() + SOURCE_BACKOFF
            logger.warning("Source failed: %s (%d in a row) — %s", name, failures, e)
            return None
        self._source_failures[name] = 0
        self._skip_until.pop(name, None)
        logger.debug("%s: %d items", name, len(items))
        return items

    def _matches_keywords(self, item: ScanResult) -> bool:
        text = (item.title + " " + item.snippet).lower()
        return any(kw in text for kw in self.keywords)

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
                snippet = _html_to_text(entry.summary)[:300]
            if title and link:
                items.append(ScanResult(title=title, url=link, snippet=snippet, source=source))
        return items

    def _scan_unstop_paginated(
        self, client: httpx.Client, types: list[str], max_pages: int = 2,
    ) -> list[ScanResult]:
        """Scan Unstop search API across multiple opportunity types with pagination."""
        by_type: list[list[ScanResult]] = []
        failed_types = 0
        now = datetime.now(UTC)

        for opp_type in types:
            type_items: list[ScanResult] = []
            category = UNSTOP_TYPE_MAP.get(opp_type, "other")
            source_label = f"Unstop ({opp_type})"

            for page in range(1, max_pages + 1):
                try:
                    resp = self._get(client, UNSTOP_API, params={
                        "opportunity": opp_type,
                        "oppstatus": "open",  # without this, most results closed years ago
                        "sort": "newest",
                        "per_page": UNSTOP_PER_PAGE,
                        "page": page,
                    })
                    data = resp.json()
                except Exception as e:
                    failed_types += page == 1
                    logger.warning("Unstop %s page %d failed: %s", opp_type, page, e)
                    break  # keep what earlier pages gave us

                entries = data.get("data", {}).get("data", [])
                if not isinstance(entries, list) or not entries:
                    break  # no more results

                for entry in entries:
                    item = _parse_unstop_entry(entry, category, source_label, now)
                    if item:
                        type_items.append(item)

                # Check if we've reached the last page
                last_page = data.get("data", {}).get("last_page", 1)
                if page >= last_page:
                    break

                time.sleep(UNSTOP_PAGE_DELAY)

            by_type.append(type_items)

        if types and failed_types == len(types):
            raise RuntimeError("Unstop unreachable for every opportunity type")

        # Interleave types so a per-scan alert cap doesn't fill up with hackathons alone
        items = [i for group in zip_longest(*by_type) for i in group if i is not None]
        logger.info("Unstop scan: %d open items across %d types", len(items), len(types))
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
                snippet = _html_to_text(entry.summary)[:300]
            if title and link:
                items.append(ScanResult(
                    title=title, url=link, snippet=snippet,
                    source=source, category="ctf", curated=True, trusted=True,
                ))
        return items

    # ── LinkedIn events ──────────────────────────────────────

    def _linkedin_due(self) -> bool:
        cfg = self.config.linkedin
        if not cfg["enabled"]:
            return False
        return time.time() - self._linkedin_last_run >= cfg["interval_hours"] * 3600

    def _scan_linkedin(self, client: httpx.Client) -> list[ScanResult]:
        """LinkedIn events via Apify when APIFY_TOKEN is set, otherwise bare links via web search.

        LinkedIn puts event pages behind a login wall (and blocks search engines from
        indexing their details), so full details need a third-party scraper.
        """
        if self.config.apify_token:
            return self._scan_linkedin_apify(client)
        logger.info("APIFY_TOKEN not set — LinkedIn events will be links only, without details")
        return self._scan_linkedin_search()

    def _scan_linkedin_apify(self, client: httpx.Client) -> list[ScanResult]:
        cfg = self.config.linkedin
        queries = cfg["queries"]
        # The spending cap is per scan cycle, shared across queries
        charge_per_run = round(cfg["max_charge_usd"] / max(len(queries), 1), 4)
        url = APIFY_RUN_SYNC.format(actor=cfg["actor"])
        items: list[ScanResult] = []

        for query in queries:
            payload = {
                "target": "discover",
                "searchQuery": query,
                "eventTiming": "upcoming",
                "eventFormat": cfg["event_format"],
                "locations": cfg["locations"],
            }
            resp = client.post(
                url,
                json=payload,
                params={"timeout": APIFY_TIMEOUT, "maxTotalChargeUsd": charge_per_run, "clean": "true"},
                headers={"Authorization": f"Bearer {self.config.apify_token}"},
                timeout=httpx.Timeout(APIFY_TIMEOUT + 30),
            )
            resp.raise_for_status()
            rows = resp.json()
            if not isinstance(rows, list):
                raise ValueError(f"unexpected Apify response: {str(rows)[:200]}")
            for row in rows:
                item = _parse_apify_event(row)
                if item:
                    items.append(item)
            logger.info("LinkedIn (Apify) %r: %d events", query, len(rows))
        return items

    def _scan_linkedin_search(self) -> list[ScanResult]:
        return _each_query(self.config.linkedin["queries"], self._scan_linkedin_query, "LinkedIn search")

    def _scan_linkedin_query(self, query: str) -> list[ScanResult]:
        with DDGS() as ddgs:
            results = list(ddgs.text(f"site:linkedin.com/events {query}", max_results=10))
        items = []
        for r in results:
            match = LINKEDIN_EVENT_RE.match(r.get("href", ""))
            if not match:
                continue
            items.append(ScanResult(
                title=_title_from_linkedin_slug(match.group()) or f"LinkedIn event: {query}",
                url=match.group(),
                source="LinkedIn",
                category="event",
                curated=True,
                trusted=True,  # nothing for the LLM to judge without details
            ))
        return items

    # ── Web search ───────────────────────────────────────────

    def _scan_web_queries(self, queries: list[str]) -> list[ScanResult]:
        return _each_query(queries, self._scan_web, "Web search")

    def _scan_web(self, query: str) -> list[ScanResult]:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        items = []
        for r in results:
            if not r.get("href") or not r.get("title"):
                continue
            items.append(ScanResult(
                title=r["title"],
                url=r["href"],
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


# ── Parsing helpers ──────────────────────────────────────────


def _each_query(queries: list[str], fn: Callable[[str], list[ScanResult]], label: str) -> list[ScanResult]:
    """Run fn per query, tolerating individual failures (ddgs raises on "No results found").

    Raises only if every query failed, so the source's circuit breaker sees real outages.
    """
    items: list[ScanResult] = []
    failures = 0
    for query in queries:
        try:
            items.extend(fn(query))
        except Exception as e:
            failures += 1
            logger.warning("%s failed: %s — %s", label, query, e)
    if queries and failures == len(queries):
        raise RuntimeError(f"all {label.lower()} queries failed")
    return items


def _html_to_text(markup: str) -> str:
    return " ".join(BeautifulSoup(str(markup), "lxml").get_text(" ").split())


def _fmt_money(amount: float, currency: str) -> str:
    return f"{CURRENCY_SYMBOLS.get(currency, '')}{amount:,.0f}"


def _parse_unstop_entry(entry: dict, category: str, source: str, now: datetime) -> ScanResult | None:
    # Unstop's status/regn_open flags go stale, so check the registration deadline ourselves
    if entry.get("status") != "LIVE" or not entry.get("regn_open"):
        return None
    deadline, deadline_date = "", None
    end_regn = (entry.get("regnRequirements") or {}).get("end_regn_dt")
    if end_regn:
        try:
            end_dt = datetime.fromisoformat(end_regn)
        except ValueError:
            end_dt = None
        if end_dt:
            if end_dt < now:
                return None
            deadline, deadline_date = end_dt.strftime("%d %b %Y"), end_dt.date()

    title = (entry.get("title") or "").strip()
    link = entry.get("seo_url", "")
    if not link and entry.get("public_url"):
        link = f"https://unstop.com/{entry['public_url']}"
    if not title or not link:
        return None

    # Internships have type=jobs, subtype=internships
    if entry.get("type") == "jobs" and entry.get("subtype") == "internships":
        category = "internship"

    prize = ""
    cash = sum(p.get("cash") or 0 for p in entry.get("prizes") or [])
    if cash:
        prize = f"{_fmt_money(cash, entry['prizes'][0].get('currency', ''))} in prizes"
    job = entry.get("jobDetail") or {}
    paid = {"paid": True, "unpaid": False}.get(job.get("paid_unpaid"))
    if job.get("paid_unpaid") == "paid" and job.get("show_salary") and job.get("max_salary"):
        lo, hi, cur = job.get("min_salary") or job["max_salary"], job["max_salary"], job.get("currency", "")
        prize = _fmt_money(hi, cur) if lo == hi else f"{_fmt_money(lo, cur)}–{hi:,.0f}"
        prize += "/month"
    elif job.get("paid_unpaid") == "unpaid":
        prize = "Unpaid"

    region = entry.get("region") or ""  # online | offline | hybrid
    city = (entry.get("address_with_country_logo") or {}).get("city") or ""
    location = "Online" if region == "online" else ", ".join(filter(None, [city, region.title()]))

    return ScanResult(
        title=title,
        url=link,
        snippet=_html_to_text(entry.get("details") or "")[:300],
        source=source,
        category=category,
        curated=True,
        trusted=True,
        deadline=deadline,
        prize=prize,
        location=location,
        organizer=(entry.get("organisation") or {}).get("name", ""),
        mode=region if region in ("online", "offline", "hybrid") else "",
        city=city,
        paid=paid if category == "internship" else None,
        stipend=int(job.get("max_salary") or 0) if paid and job.get("show_salary") else 0,
        deadline_date=deadline_date,
    )


def _parse_apify_event(row: dict) -> ScanResult | None:
    url = row.get("eventUrl") or ""
    title = (row.get("title") or "").strip()
    if not url or not title:
        return None
    loc = row.get("location") or {}
    location = loc.get("text") or ", ".join(filter(None, [loc.get("city"), loc.get("country")]))
    fmt = {"virtual": "Online", "in_person": "In person", "hybrid": "Hybrid"}.get(row.get("format") or "", "")
    mode = {"virtual": "online", "in_person": "offline", "hybrid": "hybrid"}.get(row.get("format") or "", "")
    if fmt and fmt.lower() not in location.lower():
        location = f"{location} ({fmt})" if location else fmt
    return ScanResult(
        title=title,
        url=url,
        snippet=" ".join((row.get("description") or "").split())[:500],
        source="LinkedIn",
        category="event",
        curated=True,
        when=row.get("scheduleText") or "",
        location=location,
        organizer=(row.get("organizer") or {}).get("name") or "",
        mode=mode,
        city=loc.get("city") or loc.get("text") or "",
    )


def _title_from_linkedin_slug(url: str) -> str:
    """'.../events/hackoverflow7064825131398811648' -> 'Hackoverflow'. Empty if the slug is just an ID."""
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    words = re.sub(r"\d{6,}$", "", slug).replace("-", " ").strip()
    return words.title() if words else ""
