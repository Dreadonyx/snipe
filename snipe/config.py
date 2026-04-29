"""Centralized configuration — secrets from env, everything else from YAML."""

import os
import yaml
from pathlib import Path
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
]

CATEGORY_LABELS = {
    "hackathon": "🏆 Hackathon",
    "internship": "💼 Internship",
    "fellowship": "🎓 Fellowship",
    "competition": "🏅 Competition",
    "grant": "💰 Grant / Scholarship",
    "bounty": "🐛 Bug Bounty",
    "ctf": "🚩 CTF",
}


class Config:
    """Single source of truth for all configuration."""

    def __init__(self):
        path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG_PATH
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

        # --- Non-secret config ---
        self.groq_model: str = self._data.get("groq", {}).get("model", "llama-3.1-8b-instant")
        self.scan_interval: int = self._data.get("scan_interval", 30)
        self.profile: dict = self._data.get("profile", {"location": "India", "type": "student"})
        self.keywords: list[str] = self._data.get("opportunity_keywords", [])
        self.sources: dict = self._data.get("sources", {})

    def validate(self):
        """Raise ValueError if required config is missing."""
        errors = []
        if not self.telegram_token or self.telegram_token.startswith("YOUR_"):
            errors.append("TELEGRAM_TOKEN not set (use .env or environment variable)")
        if not self.groq_api_key or self.groq_api_key.startswith("YOUR_"):
            errors.append("GROQ_API_KEY not set (use .env or environment variable)")
        if errors:
            raise ValueError("Config errors:\n" + "\n".join(f"  • {e}" for e in errors))
