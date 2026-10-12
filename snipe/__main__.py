"""Entry point — python -m snipe."""

import logging
import sys

from . import __version__
from .config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("snipe")
# httpx logs every request URL at INFO, and Telegram API URLs contain the bot token
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("primp").setLevel(logging.WARNING)  # ddgs logs every search-engine request


BANNER = r"""
   ___  ____  (_)__  ___
  (_-< / __ \/ / _ \/ -_)
 /___//_/ /_/_/ .__/\__/
             /_/   v{version}

  🎯 Opportunity Hunter Bot
"""


def main():
    print(BANNER.replace("{version}", __version__))

    config = Config()
    try:
        config.validate()
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    from .bot import create_app

    app = create_app(config)

    print(f"  ⏱  Scan interval: {config.scan_interval} minutes")
    print(f"  📍 Profile: {config.profile.get('type', 'student')} in {config.profile.get('location', 'India')}")
    extra = [name for name, on in [("Unstop", config.unstop.get("enabled")), ("LinkedIn", config.linkedin["enabled"])] if on]
    print(f"  📡 Sources: {len(config.sources.get('rss', []))} RSS + {len(config.sources.get('search_queries', []))} search queries"
          + (f" + {' + '.join(extra)}" if extra else ""))
    if config.linkedin["enabled"] and not config.apify_token:
        print("  ⚠️  LinkedIn: APIFY_TOKEN not set — events will be sent as links without details")
    print(f"  🔑 Keywords: {len(config.keywords)}")
    print()

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
