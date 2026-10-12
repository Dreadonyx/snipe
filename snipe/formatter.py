"""Formatter — uses Groq LLM to classify and format opportunity alerts."""

import html
import json
import logging
import re
from dataclasses import dataclass
from datetime import date

from groq import Groq

from .config import CATEGORIES, Config

logger = logging.getLogger(__name__)

CATEGORY_EMOJI = {
    "hackathon": "🏆",
    "internship": "💼",
    "fellowship": "🎓",
    "competition": "🏅",
    "grant": "💰",
    "bounty": "🐛",
    "ctf": "🚩",
    "event": "📅",
    "other": "🎯",
}


class FormatterError(Exception):
    """The LLM call failed in a way worth retrying on a later scan."""


@dataclass
class Alert:
    message: str            # full HTML message, sent on its own
    category: str
    compact: str = ""       # short HTML entry for batched messages and digests
    # Facts for per-user filtering (see preferences.wants)
    text: str = ""
    mode: str = ""
    city: str = ""
    paid: bool | None = None
    stipend: int = 0
    deadline_date: date | None = None


class Formatter:
    """Classifies and formats raw scan results into Telegram alert messages."""

    def __init__(self, config: Config, client: Groq | None = None):
        self.groq = client or Groq(api_key=config.groq_api_key, max_retries=3)
        self.model = config.groq_model
        self.reasoning_effort = config.groq_reasoning_effort
        self.profile = config.profile

    def format_alert(self, item) -> Alert | None:
        """Return an Alert, or None if the item isn't a real opportunity.

        Raises FormatterError on transient LLM failures so the item can be retried.
        """
        if item.trusted:
            return _make_alert(item, item.title, _shorten(item.snippet), item.category)

        data = self._classify(item)
        if not data or not data.get("is_opportunity"):
            return None

        category = str(data.get("type", "other")).lower()
        if category == "scholarship":
            category = "grant"
        if category not in CATEGORIES:
            category = item.category  # source's tag, e.g. LinkedIn items default to "event"
        if category not in CATEGORIES:
            return None

        if not item.prize and data.get("prize_or_stipend"):
            item.prize = str(data["prize_or_stipend"])
        deadline = str(data.get("deadline") or "")
        if not item.deadline and deadline.lower() not in ("", "not mentioned", "null", "none"):
            item.deadline = deadline

        return _make_alert(item, str(data.get("name") or item.title), str(data.get("one_line") or ""), category)

    def _classify(self, item) -> dict | None:
        details = "\n".join(
            f"{label}: {value}"
            for label, value in [
                ("Title", item.title), ("Organizer", item.organizer), ("When", item.when),
                ("Location", item.location), ("Snippet", item.snippet), ("URL", item.url),
            ]
            if value
        )
        prompt = (
            f"You are filtering opportunities for a {self.profile.get('type', 'student')} "
            f"in {self.profile.get('location', 'India')}.\n\n"
            f"{details}\n\n"
            "Is this a REAL opportunity a student can apply to or register for right now? "
            "Count hackathons, internships, fellowships, competitions, grants, scholarships, "
            "bug bounties, CTFs, and tech events/workshops/meetups. News articles, product launches, "
            "funding announcements, sales webinars, and past events do NOT count.\n\n"
            "If YES, return JSON:\n"
            "{\n"
            '  "is_opportunity": true,\n'
            '  "name": "short name",\n'
            '  "type": "hackathon" | "internship" | "fellowship" | "competition" | "grant" | "bounty" | "ctf" | "event",\n'
            '  "prize_or_stipend": "amount or null",\n'
            '  "deadline": "date or Not mentioned",\n'
            '  "one_line": "one sentence description"\n'
            "}\n\n"
            "If NO, return:\n"
            '{"is_opportunity": false}\n\n'
            "Return ONLY valid JSON."
        )

        extra = {"reasoning_effort": self.reasoning_effort} if self.reasoning_effort else {}
        try:
            resp = self.groq.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                # Reasoning models spend hidden tokens before answering; too low a cap truncates the JSON
                max_tokens=1024,
                response_format={"type": "json_object"},
                **extra,
            )
        except Exception as e:
            raise FormatterError(f"Groq call failed for {item.url}: {e}") from e

        text = (resp.choices[0].message.content or "").strip()
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            return json.loads(match.group()) if match else None
        except json.JSONDecodeError:
            logger.warning("LLM returned invalid JSON for %s: %.200s", item.url, text)
            return None


def _shorten(text: str, limit: int = 220) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(".,;:") + "…"


def _make_alert(item, name: str, one_line: str, category: str) -> Alert:
    return Alert(
        message=_render(item, name, one_line, category),
        category=category,
        compact=_render_compact(item, name, category),
        text=" ".join([name, item.title, item.snippet, one_line, item.organizer]),
        mode=item.mode,
        city=item.city,
        paid=item.paid,
        stipend=item.stipend,
        deadline_date=item.deadline_date,
    )


def _render_compact(item, name: str, category: str) -> str:
    esc = html.escape
    facts = [f"⏰ {item.deadline}" if item.deadline else "", f"📆 {item.when}" if item.when else "",
             f"💰 {item.prize}" if item.prize else "", f"📍 {item.location}" if item.location else ""]
    line = f'{CATEGORY_EMOJI.get(category, "🎯")} <b><a href="{esc(item.url, quote=True)}">{esc(name)}</a></b>'
    details = " · ".join(esc(f) for f in facts if f)
    return f"{line}\n{details}" if details else line


def render_batch(header: str, entries: list[str], limit: int = 4000) -> list[str]:
    """Join compact entries under a header, split into messages under Telegram's 4096-char cap."""
    messages, current = [], header
    for entry in entries:
        if len(current) + len(entry) + 2 > limit:
            messages.append(current)
            current = entry
        else:
            current += "\n\n" + entry
    messages.append(current)
    return messages


def _render(item, name: str, one_line: str, category: str) -> str:
    esc = html.escape
    lines = [
        f"{CATEGORY_EMOJI.get(category, '🎯')} <b>{esc(category.upper())}</b>",
        "",
        f"<b>{esc(name)}</b>",
    ]
    if one_line:
        lines.append(esc(one_line))
    lines.append("")
    for emoji, label, value in [
        ("🏢", "By", item.organizer),
        ("📆", "When", item.when),
        ("📍", "Where", item.location),
        ("💰", "Prize/Stipend", item.prize),
        ("⏰", "Deadline", item.deadline),
    ]:
        if value:
            lines.append(f"{emoji} <b>{label}:</b> {esc(value)}")
    lines.append(f"📡 <b>Source:</b> {esc(item.source)}")
    action = "Register here" if category == "event" else "Apply here"
    lines.append(f'🔗 <a href="{esc(item.url, quote=True)}">{action}</a>')
    return "\n".join(lines)
