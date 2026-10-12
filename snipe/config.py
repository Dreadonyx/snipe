"""Centralized configuration — secrets from env, everything else from YAML."""

import os
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(PROJECT_ROOT / ".env")

CONFIG_PATH = PROJECT_ROOT / "config.yaml"
EXAMPLE_CONFIG_PATH = PROJECT_ROOT / "config.example.yaml"

# All valid opportunity categories
CATEGORIES = [
    "hackathon",
    "internship",
    "fellowship",
    "competition",
    "grant",
    "bounty",
    "ctf",
    "event",
]

CATEGORY_LABELS = {
    "hackathon": "🏆 Hackathon",
    "internship": "💼 Internship",
    "fellowship": "🎓 Fellowship",
    "competition": "🏅 Competition",
    "grant": "💰 Grant / Scholarship",
    "bounty": "🐛 Bug Bounty",
    "ctf": "🚩 CTF",
    "event": "📅 Event / Workshop",
}

CTFTIME_DEFAULTS = {
    "enabled": True,
    "days_ahead": 45,
    "skip_restrictions": ["Prequalified"],  # finals you can't enter without qualifying
}

LINKEDIN_DEFAULTS = {
    "enabled": False,
    "actor": "maximedupre~linkedin-events",
    "interval_hours": 12,
    "max_charge_usd": 0.05,
    "queries": ["hackathon", "tech workshop students"],
    "locations": [],
    "event_format": "any",
}


class Config:
    """Single source of truth for all configuration."""

    def __init__(self, path: Path | None = None):
        path = path or (CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH)
        with open(path) as f:
            self._data = yaml.safe_load(f) or {}

        # --- Secrets (env first, YAML fallback) ---
        self.telegram_token: str = os.environ.get(
            "TELEGRAM_TOKEN",
            self._data.get("telegram", {}).get("token", ""),
        )
        self.groq_api_key: str = os.environ.get(
            "GROQ_API_KEY",
            self._data.get("groq", {}).get("api_key", ""),
        )
        self.apify_token: str = os.environ.get("APIFY_TOKEN", "")

        # --- Non-secret config ---
        self.groq_model: str = self._data.get("groq", {}).get("model", "openai/gpt-oss-20b")
        # For reasoning models (gpt-oss, qwen3). Leave unset for models that don't support it.
        self.groq_reasoning_effort: str | None = self._data.get("groq", {}).get("reasoning_effort", "low")
        self.scan_interval: int = int(os.environ.get("SCAN_INTERVAL", self._data.get("scan_interval", 30)))
        self.max_alerts_per_scan: int = self._data.get("max_alerts_per_scan", 15)
        # Quiet hours and digest times are in this timezone
        self.timezone = ZoneInfo(os.environ.get("TZ_NAME", self._data.get("timezone", "Asia/Kolkata")))
        self.db_path: Path = Path(os.environ.get("SNIPE_DB_PATH", PROJECT_ROOT / "snipe.db"))
        self.profile: dict = self._data.get("profile", {"location": "India", "type": "student"})
        self.keywords: list[str] = self._data.get("opportunity_keywords", [])
        self.sources: dict = self._data.get("sources", {})
        self.unstop: dict = self.sources.get("unstop", {})
        self.linkedin: dict = {**LINKEDIN_DEFAULTS, **(self.sources.get("linkedin") or {})}
        self.ctftime: dict = {**CTFTIME_DEFAULTS, **(self.sources.get("ctftime") or {})}

    def validate(self):
        """Raise ValueError if required config is missing."""
        errors = []
        if not self.telegram_token or self.telegram_token.startswith("YOUR_"):
            errors.append("TELEGRAM_TOKEN not set (use .env or environment variable)")
        if not self.groq_api_key or self.groq_api_key.startswith("YOUR_"):
            errors.append("GROQ_API_KEY not set (use .env or environment variable)")
        if self.scan_interval < 5:
            errors.append("scan_interval must be at least 5 minutes")
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  • {e}" for e in errors))
