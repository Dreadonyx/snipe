"""Formatter — uses Groq LLM to classify and format opportunity alerts."""

import html
import json
import logging
import re

from groq import Groq

from .config import Config

logger = logging.getLogger(__name__)

CATEGORY_EMOJI = {
    "hackathon": "🏆",
    "internship": "💼",
    "fellowship": "🎓",
    "competition": "🏅",
    "grant": "💰",
    "scholarship": "💰",
    "bounty": "🐛",
    "ctf": "🚩",
    "other": "🎯",
}


class Formatter:
    """Classifies and formats raw scan results into Telegram alert messages."""

    def __init__(self, config: Config):
        self.groq = Groq(api_key=config.groq_api_key)
        self.model = config.groq_model
        self.profile = config.profile

    def format_alert(self, item) -> tuple[str | None, str]:
        """Return (html_message, category). Returns (None, 'other') if not a real opportunity."""
        prompt = (
            f"You are filtering opportunities for a student in {self.profile.get('location', 'India')}.\n\n"
            f"Article:\n"
            f"Title: {item.title}\n"
            f"Snippet: {item.snippet}\n"
            f"URL: {item.url}\n\n"
            f"Is this a REAL opportunity a student can apply to right now "
            f"(hackathon, internship, fellowship, competition, grant, scholarship, bug bounty, or CTF)?\n\n"
            f"If YES, return JSON:\n"
            f'{{\n'
            f'  "is_opportunity": true,\n'
            f'  "name": "short name",\n'
            f'  "type": "hackathon" | "internship" | "fellowship" | "competition" | "grant" | "bounty" | "ctf" | "other",\n'
            f'  "prize_or_stipend": "amount or null",\n'
            f'  "deadline": "date or Not mentioned",\n'
            f'  "apply_url": "{item.url}",\n'
            f'  "one_line": "one sentence description"\n'
            f'}}\n\n'
            f'If NO, return:\n'
            f'{{"is_opportunity": false}}\n\n'
            f'Return ONLY valid JSON.'
        )

        try:
            resp = self.groq.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=250,
            )
            text = resp.choices[0].message.content.strip()
            match = re.search(r"\{.*\}", text, re.DOTALL)
            if not match:
                return None, "other"

            data = json.loads(match.group())
            if not data.get("is_opportunity"):
                return None, "other"

            category = data.get("type", "other").lower()
            if category == "scholarship":
                category = "grant"

            emoji = CATEGORY_EMOJI.get(category, "🎯")
            name = html.escape(data.get("name", item.title))
            one_line = html.escape(data.get("one_line", ""))
            prize = data.get("prize_or_stipend")
            deadline = html.escape(str(data.get("deadline", "Not mentioned")))
            url = data.get("apply_url", item.url)

            prize_line = f"💰 <b>Prize/Stipend:</b> {html.escape(str(prize))}\n" if prize else ""
            msg = (
                f"{emoji} <b>{html.escape(category.upper())}</b>\n\n"
                f"<b>{name}</b>\n"
                f"{one_line}\n\n"
                f"{prize_line}"
                f"⏰ <b>Deadline:</b> {deadline}\n"
                f"📍 <b>Source:</b> {html.escape(item.source)}\n"
                f'🔗 <a href="{url}">Apply here</a>'
            )
            return msg, category

        except Exception:
            logger.warning("format_alert failed for: %s", item.url)
            return None, "other"
