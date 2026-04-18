import feedparser
import httpx
import html
import logging
from bs4 import BeautifulSoup
from ddgs import DDGS
from groq import Groq
import json
import re

logger = logging.getLogger(__name__)

HEADERS = {"User-Agent": "Snipe-Bot/1.0"}
TIMEOUT = httpx.Timeout(10.0)


class Scanner:
    def __init__(self, config: dict):
        self.config = config
        self.keywords = config.get("opportunity_keywords", [])
        self.groq = Groq(api_key=config["groq"]["api_key"])
        self.model = config["groq"]["model"]

    def scan(self) -> list:
        """Scan all sources and return list of opportunity dicts."""
        raw = []

        with httpx.Client(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
            # RSS sources
            for source in self.config.get("sources", {}).get("rss", []):
                try:
                    items = self._scan_rss(client, source["url"], source["name"])
                    raw.extend(items)
                except Exception:
                    logger.warning("RSS feed failed: %s", source.get("name", source.get("url")))

        # Web search queries
        for query in self.config.get("sources", {}).get("search_queries", []):
            try:
                items = self._scan_web(query)
                raw.extend(items)
            except Exception:
                logger.warning("Web search failed: %s", query)

        # Filter: only keep items that look like real opportunities
        opportunities = []
        for item in raw:
            text = (item.get("title", "") + " " + item.get("snippet", "")).lower()
            if any(kw.lower() in text for kw in self.keywords):
                opportunities.append(item)

        return opportunities

    def _scan_rss(self, client: httpx.Client, url: str, source: str) -> list:
        resp = client.get(url)
        feed = feedparser.parse(resp.text)
        items = []
        for entry in feed.entries[:20]:
            title = html.unescape(entry.get("title", "").strip())
            link = entry.get("link", "")
            snippet = ""
            if hasattr(entry, "summary"):
                snippet = BeautifulSoup(entry.summary, "lxml").get_text()[:300]
            if title and link:
                items.append({"title": title, "url": link, "snippet": snippet, "source": source})
        return items

    def _scan_web(self, query: str) -> list:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=5))
        items = []
        for r in results:
            items.append({
                "title": r.get("title", ""),
                "url": r.get("href", ""),
                "snippet": r.get("body", "")[:300],
                "source": "Web Search",
            })
        return items

    def format_alert(self, item: dict) -> str | None:
        """Use Groq to extract clean alert details. Returns None if not a real opportunity."""
        profile = self.config.get("profile", {})
        prompt = f"""You are filtering opportunities for a student in {profile.get('location', 'India')}.

Article:
Title: {item['title']}
Snippet: {item.get('snippet', '')}
URL: {item['url']}

Is this a REAL opportunity a student can apply to right now (hackathon, internship, fellowship, competition, grant)?

If YES, return JSON:
{{
  "is_opportunity": true,
  "name": "short name of the opportunity",
  "type": "hackathon" | "internship" | "fellowship" | "competition" | "grant" | "other",
  "prize_or_stipend": "prize/stipend amount or null",
  "deadline": "deadline date or 'Not mentioned'",
  "apply_url": "{item['url']}",
  "one_line": "one sentence describing it"
}}

If NO (just news, not something to apply to), return:
{{"is_opportunity": false}}

Return ONLY valid JSON."""

        try:
            resp = self.groq.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=250,
            )
            text = resp.choices[0].message.content.strip()
            match = re.search(r'\{.*\}', text, re.DOTALL)
            if not match:
                return None
            data = json.loads(match.group())
            if not data.get("is_opportunity"):
                return None

            opp_type = data.get("type", "opportunity").upper()
            name = data.get("name", item["title"])
            one_line = data.get("one_line", "")
            prize = data.get("prize_or_stipend")
            deadline = data.get("deadline", "Not mentioned")
            url = data.get("apply_url", item["url"])

            prize_line = f"💰 *Prize/Stipend:* {prize}\n" if prize else ""
            msg = (
                f"🎯 *{opp_type}*\n\n"
                f"*{name}*\n"
                f"{one_line}\n\n"
                f"{prize_line}"
                f"⏰ *Deadline:* {deadline}\n"
                f"🔗 [Apply here]({url})"
            )
            return msg

        except Exception:
            logger.warning("format_alert failed for: %s", item.get("url"))
            return None
