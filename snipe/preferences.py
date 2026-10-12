"""Per-user settings and the rules that decide which alerts a user gets, and when.

Filters only drop an alert when they *know* it doesn't fit: an RSS item with no
location info still reaches someone who asked for "online only".
"""

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date

MAX_WORDS = 20
MAX_WORD_LEN = 40

DIGEST_HOURS = [6, 8, 12, 18, 21]
QUIET_PRESETS = [(23, 7), (22, 8), (0, 8), None]
DEADLINE_PRESETS = [0, 2, 5, 7]
LOCATION_MODES = ["any", "online", "near"]


@dataclass
class UserSettings:
    interests: list[str] = field(default_factory=list)  # only alerts mentioning one of these
    muted: list[str] = field(default_factory=list)      # never alerts mentioning any of these
    location: str = "any"                               # any | online | near (city + online)
    city: str = ""
    paid_only: bool = False                             # internships
    min_stipend: int = 0                                # internships, per month
    min_days_left: int = 0                              # hide if the deadline is closer than this
    delivery: str = "instant"                           # instant | digest
    digest_hour: int = 8
    quiet_start: int | None = 23                        # hours in the bot's timezone; None = off
    quiet_end: int | None = 7
    last_digest: str = ""                               # ISO date of the last digest sent

    @classmethod
    def from_dict(cls, data: dict) -> "UserSettings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def quiet(self) -> tuple[int, int] | None:
        if self.quiet_start is None or self.quiet_end is None:
            return None
        return self.quiet_start, self.quiet_end


def parse_word_list(text: str) -> list[str]:
    """'AI, Web Dev,, ai ' -> ['ai', 'web dev']"""
    words = []
    for raw in text.split(","):
        word = " ".join(raw.split()).lower()[:MAX_WORD_LEN]
        if word and word not in words:
            words.append(word)
    return words[:MAX_WORDS]


def mentions(text: str, word: str) -> bool:
    """Whole-word match, so 'ai' matches 'AI hackathon' but not 'email' or 'chain'."""
    return re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text, re.IGNORECASE) is not None


def wants(settings: UserSettings, alert, today: date) -> bool:
    """Does this user want this alert? `alert` carries the facts from formatter.Alert."""
    if any(mentions(alert.text, w) for w in settings.muted):
        return False
    if settings.interests and not any(mentions(alert.text, w) for w in settings.interests):
        return False

    if settings.location == "online" and alert.mode == "offline":
        return False
    if (settings.location == "near" and settings.city and alert.mode == "offline"
            and alert.city and not mentions(alert.city, settings.city)):
        return False

    if alert.category == "internship":
        if (settings.paid_only or settings.min_stipend) and alert.paid is False:
            return False
        if settings.min_stipend and alert.stipend and alert.stipend < settings.min_stipend:
            return False

    if settings.min_days_left and alert.deadline_date:
        if (alert.deadline_date - today).days < settings.min_days_left:
            return False
    return True


def in_quiet_hours(settings: UserSettings, hour: int) -> bool:
    if settings.quiet is None:
        return False
    start, end = settings.quiet
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight, e.g. 23 → 7


def next_in_cycle(options: list, current):
    try:
        return options[(options.index(current) + 1) % len(options)]
    except ValueError:
        return options[0]
