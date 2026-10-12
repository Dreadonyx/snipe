import json
import time
from datetime import UTC, datetime

import httpx
import pytest
import respx
from ddgs.exceptions import DDGSException

from snipe import scanner as scanner_mod
from snipe.scanner import (
    UNSTOP_API,
    Scanner,
    _parse_apify_event,
    _parse_unstop_entry,
    _title_from_linkedin_slug,
    normalize_url,
)

NOW = datetime(2026, 10, 12, tzinfo=UTC)

RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Global AI Hackathon opens registration</title><link>https://example.com/ai-hack?utm_source=rss</link>
<description>&lt;p&gt;Win $10k&lt;/p&gt;</description></item>
<item><title>Startup raises Series B</title><link>https://example.com/series-b</link><description>Funding news</description></item>
</channel></rss>"""

CTF_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>CTFtime</title>
<item><title>PwnFest 2026</title><link>https://ctftime.org/event/9999/</link><description>Jeopardy</description></item>
</channel></rss>"""


def unstop_entry(**overrides):
    entry = {
        "title": "Webforge AI",
        "seo_url": "https://unstop.com/hackathons/webforge-ai-1",
        "type": "hackathons",
        "subtype": "online_coding_challenge",
        "status": "LIVE",
        "regn_open": 1,
        "regnRequirements": {"end_regn_dt": "2026-10-22T23:59:00+05:30"},
        "details": "<p>Build an <b>AI</b> app.</p>",
        "prizes": [{"cash": 4000, "currency": "fa-rupee"}, {"cash": 2500, "currency": "fa-rupee"}],
        "region": "hybrid",
        "address_with_country_logo": {"city": "Nagpur"},
        "organisation": {"name": "SVPCET"},
        "jobDetail": {},
    }
    entry.update(overrides)
    return entry


def unstop_page(entries, last_page=1):
    return {"data": {"data": entries, "last_page": last_page}}


# ── Pure helpers ─────────────────────────────────────────────


@pytest.mark.parametrize("raw, expected", [
    ("https://Example.com/a/?utm_source=x&id=3#frag", "https://example.com/a?id=3"),
    ("https://example.com/", "https://example.com/"),
    ("https://in.linkedin.com/events/foo123/", "https://www.linkedin.com/events/foo123"),
    ("https://business.linkedin.com/events/awards/", "https://business.linkedin.com/events/awards"),
    ("https://notlinkedin.com/a", "https://notlinkedin.com/a"),
    ("https://x.com/p?ref=hn&fbclid=abc&page=2", "https://x.com/p?page=2"),
])
def test_normalize_url(raw, expected):
    assert normalize_url(raw) == expected


def test_unstop_entry_full_details():
    item = _parse_unstop_entry(unstop_entry(), "hackathon", "Unstop (hackathons)", NOW)
    assert item.title == "Webforge AI"
    assert item.category == "hackathon"
    assert item.trusted and item.curated
    assert item.deadline == "22 Oct 2026"
    assert item.prize == "₹6,500 in prizes"
    assert item.location == "Nagpur, Hybrid"
    assert item.organizer == "SVPCET"
    assert item.snippet == "Build an AI app."
    assert (item.mode, item.city, item.paid) == ("hybrid", "Nagpur", None)  # paid/unpaid is internships only
    assert str(item.deadline_date) == "2026-10-22"


def test_unstop_entry_with_stale_open_flag_is_dropped():
    # Unstop still reports status=LIVE/regn_open=1 for events that closed long ago
    entry = unstop_entry(regnRequirements={"end_regn_dt": "2025-02-04T23:59:51+05:30"})
    assert _parse_unstop_entry(entry, "hackathon", "Unstop", NOW) is None


@pytest.mark.parametrize("overrides", [{"status": "EXPIRED"}, {"regn_open": 0}, {"title": ""}, {"seo_url": "", "public_url": ""}])
def test_unstop_entry_rejects_closed_or_incomplete(overrides):
    assert _parse_unstop_entry(unstop_entry(**overrides), "hackathon", "Unstop", NOW) is None


def test_unstop_internship_stipend_and_category():
    entry = unstop_entry(
        type="jobs", subtype="internships", prizes=[], region="online",
        jobDetail={"paid_unpaid": "paid", "show_salary": 1, "min_salary": 12000, "max_salary": 15000, "currency": "fa-rupee"},
    )
    item = _parse_unstop_entry(entry, "internship", "Unstop", NOW)
    assert item.category == "internship"
    assert item.prize == "₹12,000–15,000/month"
    assert (item.paid, item.stipend, item.mode) == (True, 15000, "online")
    assert item.location == "Online"


def test_unstop_stipend_unknown_currency_keeps_all_digits():
    entry = unstop_entry(prizes=[], jobDetail={"paid_unpaid": "paid", "show_salary": 1, "min_salary": 5000, "max_salary": 8000, "currency": "fa-yen"})
    assert _parse_unstop_entry(entry, "internship", "Unstop", NOW).prize == "5,000–8,000/month"


def test_unstop_falls_back_to_public_url():
    entry = unstop_entry(seo_url="", public_url="hackathons/foo-1")
    assert _parse_unstop_entry(entry, "hackathon", "Unstop", NOW).url == "https://unstop.com/hackathons/foo-1"


def test_apify_event_parsing():
    item = _parse_apify_event({
        "eventUrl": "https://www.linkedin.com/events/genai-buildathon-7490857366876233728/",
        "title": "GenAI Buildathon",
        "description": "Build   with\nLLMs",
        "scheduleText": "Sat, 24 Oct 2026, 10:00 AM",
        "format": "virtual",
        "organizer": {"name": "GDG Chennai", "url": None},
        "location": None,
    })
    assert item.source == "LinkedIn"
    assert item.category == "event"
    assert item.curated and not item.trusted  # still goes through the LLM filter
    assert item.when == "Sat, 24 Oct 2026, 10:00 AM"
    assert item.location == "Online"
    assert item.organizer == "GDG Chennai"
    assert item.snippet == "Build with LLMs"


def test_apify_event_in_person_location():
    item = _parse_apify_event({
        "eventUrl": "https://www.linkedin.com/events/x-1/", "title": "Meetup", "format": "in_person",
        "location": {"text": None, "city": "Chennai", "country": "India"},
    })
    assert item.location == "Chennai, India (In person)"
    assert (item.mode, item.city) == ("offline", "Chennai")


@pytest.mark.parametrize("row", [{"title": "No URL"}, {"eventUrl": "https://www.linkedin.com/events/1/", "title": None}])
def test_apify_event_incomplete_rows_skipped(row):
    assert _parse_apify_event(row) is None


@pytest.mark.parametrize("url, title", [
    ("https://www.linkedin.com/events/hackoverflow7064825131398811648", "Hackoverflow"),
    ("https://www.linkedin.com/events/genai-buildathon-7490857366876233728/", "Genai Buildathon"),
    ("https://www.linkedin.com/events/7490857366876233728/", ""),
])
def test_title_from_linkedin_slug(url, title):
    assert _title_from_linkedin_slug(url) == title


# ── Full scans with mocked HTTP ──────────────────────────────


@pytest.fixture
def no_sleep(monkeypatch):
    monkeypatch.setattr(scanner_mod.time, "sleep", lambda s: None)


@respx.mock
def test_scan_combines_sources_filters_and_dedupes(make_config, no_sleep):
    config = make_config("""
        opportunity_keywords: [hackathon]
        sources:
          unstop: {enabled: true, max_pages: 1, types: [hackathons]}
          rss:
            - {name: Blog, url: "https://blog.example/rss"}
            - {name: Blog mirror, url: "https://mirror.example/rss"}
            - {name: CTFtime, url: "https://ctftime.org/event/list/upcoming/rss/"}
    """)
    respx.get("https://blog.example/rss").respond(text=RSS)
    respx.get("https://mirror.example/rss").respond(text=RSS)
    respx.get("https://ctftime.org/event/list/upcoming/rss/").respond(text=CTF_RSS)
    unstop = respx.get(UNSTOP_API).respond(json=unstop_page([unstop_entry(title="CodeSprint")]))

    report = Scanner(config).scan()

    titles = sorted(i.title for i in report.items)
    # Funding news fails the keyword filter; curated sources bypass it even without the keyword;
    # the mirrored post is deduplicated after URL normalization.
    assert titles == ["CodeSprint", "Global AI Hackathon opens registration", "PwnFest 2026"]
    assert report.sources_checked == 4 and report.errors == 0
    params = unstop.calls[0].request.url.params
    assert params["oppstatus"] == "open" and params["opportunity"] == "hackathons"
    assert next(i for i in report.items if i.source == "Blog").url == "https://example.com/ai-hack"


@respx.mock
def test_unstop_paginates_until_last_page(make_config, no_sleep):
    config = make_config("sources: {unstop: {enabled: true, max_pages: 5, types: [hackathons]}}")
    route = respx.get(UNSTOP_API).mock(side_effect=[
        httpx.Response(200, json=unstop_page([unstop_entry(seo_url="https://unstop.com/a")], last_page=2)),
        httpx.Response(200, json=unstop_page([unstop_entry(seo_url="https://unstop.com/b")], last_page=2)),
    ])
    report = Scanner(config).scan()
    assert route.call_count == 2
    assert {i.url for i in report.items} == {"https://unstop.com/a", "https://unstop.com/b"}


@respx.mock
def test_failing_source_backs_off_then_recovers(make_config, no_sleep, monkeypatch):
    config = make_config("""
        opportunity_keywords: [hackathon]
        sources: {rss: [{name: Flaky, url: "https://flaky.example/rss"}]}
    """)
    route = respx.get("https://flaky.example/rss").respond(500)
    scanner = Scanner(config)

    for _ in range(scanner_mod.MAX_SOURCE_FAILURES):
        assert scanner.scan().errors == 1
    calls = route.call_count

    assert scanner.scan().errors == 0  # skipped while backing off
    assert route.call_count == calls

    clock = time.time() + scanner_mod.SOURCE_BACKOFF + 1
    monkeypatch.setattr(scanner_mod.time, "time", lambda: clock)
    route.respond(text=RSS)
    assert len(scanner.scan().items) == 1


@respx.mock
def test_linkedin_apify_request_and_interval(make_config):
    config = make_config("""
        sources:
          linkedin: {enabled: true, queries: [hackathon, workshop], max_charge_usd: 0.1, locations: [India]}
    """, APIFY_TOKEN="apify_test_token")
    route = respx.post(url__startswith="https://api.apify.com/v2/acts/maximedupre~linkedin-events/").respond(
        201, json=[{"eventUrl": "https://www.linkedin.com/events/x-1/", "title": "Build Day", "format": "virtual"}],
    )
    scanner = Scanner(config)

    report = scanner.scan()
    assert route.call_count == 2
    request = route.calls[0].request
    assert request.headers["Authorization"] == "Bearer apify_test_token"
    assert "apify_test_token" not in str(request.url)  # never leak the token into URLs/logs
    assert request.url.params["maxTotalChargeUsd"] == "0.05"  # cap split across queries
    body = json.loads(request.content)
    assert body == {"target": "discover", "searchQuery": "hackathon", "eventTiming": "upcoming",
                    "eventFormat": "any", "locations": ["India"]}
    assert [i.title for i in report.items] == ["Build Day"]

    scanner.scan()
    assert route.call_count == 2  # not due again until interval_hours pass


def test_linkedin_without_token_uses_search(make_config, monkeypatch):
    config = make_config("sources: {linkedin: {enabled: true, queries: [hackathon]}}")

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results):
            assert query == "site:linkedin.com/events hackathon"
            return [
                {"href": "https://www.linkedin.com/events/hackoverflow7064825131398811648", "title": "LinkedIn"},
                {"href": "https://business.linkedin.com/events/marketing-awards/about", "title": "Not an event page"},
            ]

    monkeypatch.setattr(scanner_mod, "DDGS", FakeDDGS)
    report = Scanner(config).scan()
    assert [(i.title, i.category, i.trusted) for i in report.items] == [("Hackoverflow", "event", True)]


def test_linkedin_disabled_by_default(make_config):
    assert Scanner(make_config("")).scan().sources_checked == 0


def test_example_config_does_not_use_dead_unstop_feed():
    # The old /feeds endpoint returns 404; the paginated search API replaced it
    from snipe.config import EXAMPLE_CONFIG_PATH

    assert "unstop.com/api/public/opportunity/feeds" not in EXAMPLE_CONFIG_PATH.read_text()


def test_linkedin_search_survives_an_empty_query(make_config, monkeypatch):
    # ddgs raises "No results found" instead of returning [] — one dry query mustn't sink the source
    config = make_config("sources: {linkedin: {enabled: true, queries: [obscure, hackathon]}}")

    class FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def text(self, query, max_results):
            if "obscure" in query:
                raise DDGSException("No results found.")
            return [{"href": "https://www.linkedin.com/events/buildathon-7490857366876233728/", "title": "LinkedIn"}]

    monkeypatch.setattr(scanner_mod, "DDGS", FakeDDGS)
    report = Scanner(config).scan()
    assert report.errors == 0
    assert [i.title for i in report.items] == ["Buildathon"]


@respx.mock
def test_unstop_interleaves_types_and_survives_one_failing(make_config, no_sleep):
    config = make_config("sources: {unstop: {enabled: true, max_pages: 1, types: [hackathons, internships, scholarships]}}")

    def by_type(request):
        kind = request.url.params["opportunity"]
        if kind == "scholarships":
            return httpx.Response(503)
        return httpx.Response(200, json=unstop_page([
            unstop_entry(title=f"{kind} {n}", seo_url=f"https://unstop.com/{kind}/{n}") for n in range(2)]))

    respx.get(UNSTOP_API).mock(side_effect=by_type)
    report = Scanner(config).scan()
    assert report.errors == 0
    assert [i.title for i in report.items] == ["hackathons 0", "internships 0", "hackathons 1", "internships 1"]


@respx.mock
def test_unstop_fully_down_counts_as_a_source_failure(make_config, no_sleep):
    config = make_config("sources: {unstop: {enabled: true, max_pages: 1, types: [hackathons]}}")
    respx.get(UNSTOP_API).respond(503)
    assert Scanner(config).scan().errors == 1
