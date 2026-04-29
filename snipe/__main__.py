"""Entry point — python -m snipe."""

import logging
import sys

from .config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("snipe")


BANNER = r"""
   ___  ____  (_)__  ___
  (_-< / __ \/ / _ \/ -_)
 /___//_/ /_/_/ .__/\__/
             /_/   v2.0

  🎯 Opportunity Hunter Bot
"""


def main():
    print(BANNER)

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
    print(f"  📡 Sources: {len(config.sources.get('rss', []))} RSS + {len(config.sources.get('search_queries', []))} search queries")
    print(f"  🔑 Keywords: {len(config.keywords)}")
    print()

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
