# snipe 🎯

> Telegram bot that hunts down hackathons, internships, fellowships, and competitions — and texts you before the rest of the internet finds out.

Most opportunity aggregators are noisy or slow. snipe runs every 30 minutes, filters results through an LLM, and only pings you when something is actually worth your time.

## How it works

1. Scans RSS feeds + live DuckDuckGo search results every 30 minutes
2. Groq's LLaMA 3.1 filters out the noise — only real, actionable opportunities pass
3. You get a Telegram message with the name, prize/stipend, deadline, and apply link
4. SQLite makes sure you never see the same thing twice

## Commands

| Command | Description |
|---|---|
| `/start` | Subscribe to alerts |
| `/stop` | Unsubscribe |
| `/status` | Last scan info |
| `/scan` | Trigger a manual scan right now |

## Setup

```bash
git clone https://github.com/Dreadonyx/snipe
cd snipe
pip install -r requirements.txt
cp config.example.yaml config.yaml
# fill in your Telegram bot token + Groq API key
python bot.py
```

You'll need a Telegram bot token (from @BotFather) and a free Groq API key.

## Stack

- Python + python-telegram-bot
- Groq API (LLaMA 3.1)
- DuckDuckGo live search
- SQLite
- APScheduler
