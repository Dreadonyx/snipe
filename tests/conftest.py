import textwrap

import pytest

from snipe import config as config_mod
from snipe.config import Config


@pytest.fixture
def make_config(tmp_path, monkeypatch):
    """Build a Config from an inline YAML string, isolated from the developer's .env."""
    for var in ("TELEGRAM_TOKEN", "GROQ_API_KEY", "APIFY_TOKEN", "SCAN_INTERVAL", "SNIPE_DB_PATH"):
        monkeypatch.delenv(var, raising=False)
    # Sources that are on by default would hit the real network; tests opt in explicitly
    monkeypatch.setitem(config_mod.CTFTIME_DEFAULTS, "enabled", False)

    def _make(yaml_text: str = "", **env) -> Config:
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        path = tmp_path / "config.yaml"
        path.write_text(textwrap.dedent(yaml_text))
        return Config(path)

    return _make
