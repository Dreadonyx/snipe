# Snipe 🎯

Telegram bot that alerts you the moment a hackathon, internship, fellowship, or competition drops — before everyone else finds out.

## Setup

```bash
git clone https://github.com/Dreadonyx/snipe
cd snipe
pip install -r requirements.txt
cp config.example.yaml config.yaml
# Edit config.yaml — add your Telegram bot token and Groq API key
python bot.py
```

## Commands (in Telegram)

| Command | What it does |
|---|---|
| `/start` | Turn on alerts |
| `/stop` | Pause alerts |
| `/status` | Check if alerts are on |
| `/scan` | Scan for opportunities right now |

## How it works

1. Every 30 minutes, Snipe scans RSS feeds + live web search
2. Groq AI filters out noise — only real opportunities you can apply to
3. New opportunity found → instant Telegram message with name, prize/stipend, deadline, apply link
4. Already seen it → skipped, no duplicates

## Stack

- Python + python-telegram-bot
- Groq API (LLaMA 3.1) for filtering
- DuckDuckGo live search
- SQLite for dedup
- APScheduler for background scanning
