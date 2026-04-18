import yaml
import asyncio
import logging
from pathlib import Path
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from database import Database
from scanner import Scanner

logging.basicConfig(level=logging.WARNING)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]

    if db.is_subscribed(chat_id):
        await update.message.reply_text("✅ You're already receiving alerts!")
        return

    db.add_subscriber(chat_id)
    await update.message.reply_text(
        "🎯 *Snipe is ON!*\n\n"
        "You'll get instant alerts for:\n"
        "• Hackathons\n"
        "• Internships\n"
        "• Fellowships\n"
        "• Competitions & grants\n\n"
        "Commands:\n"
        "/stop — pause alerts\n"
        "/status — check your status\n"
        "/scan — scan for opportunities right now",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]
    db.remove_subscriber(chat_id)
    await update.message.reply_text(
        "🔕 *Alerts paused.*\n\nSend /start anytime to turn them back on.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]
    config = load_config()
    interval = config.get("scan_interval", 30)

    if db.is_subscribed(chat_id):
        await update.message.reply_text(
            f"✅ *Alerts: ON*\n"
            f"🔄 Scanning every {interval} minutes\n\n"
            f"Send /stop to pause.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "🔕 *Alerts: OFF*\n\nSend /start to turn on.",
            parse_mode=ParseMode.MARKDOWN
        )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]

    if not db.is_subscribed(chat_id):
        await update.message.reply_text("Send /start first to activate Snipe.")
        return

    await update.message.reply_text("🔍 Scanning for opportunities...")
    config = load_config()
    scanner = Scanner(config)

    found = 0
    items = scanner.scan()
    for item in items:
        if db.is_seen(item["url"]):
            continue
        alert = scanner.format_alert(item)
        if alert:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=alert,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False
                )
                db.mark_seen(item["url"], item["title"])
                found += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass

    if found == 0:
        await update.message.reply_text("😴 Nothing new right now. I'll alert you when something drops.")
    else:
        await update.message.reply_text(f"✅ Sent {found} new opportunit{'y' if found == 1 else 'ies'}!")


async def scheduled_scan(app: Application):
    """Auto-scan and push to all active subscribers."""
    db: Database = app.bot_data["db"]
    subscribers = db.get_active_subscribers()
    if not subscribers:
        return

    config = load_config()
    scanner = Scanner(config)
    items = scanner.scan()

    for item in items:
        if db.is_seen(item["url"]):
            continue
        alert = scanner.format_alert(item)
        if not alert:
            continue

        db.mark_seen(item["url"], item["title"])

        for chat_id in subscribers:
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=alert,
                    parse_mode=ParseMode.MARKDOWN,
                    disable_web_page_preview=False
                )
                await asyncio.sleep(0.3)
            except Exception:
                pass


def main():
    config = load_config()
    db = Database()
    interval = config.get("scan_interval", 30)

    app = Application.builder().token(config["telegram"]["token"]).build()
    app.bot_data["db"] = db

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("scan", cmd_scan))

    # Schedule auto-scan
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_scan,
        "interval",
        minutes=interval,
        args=[app],
    )
    scheduler.start()

    print(f"🎯 Snipe is running — scanning every {interval} minutes")
    print(f"   Bot: t.me/Snipeoppbot")
    print(f"   Subscribers: {db.subscriber_count()}")
    print("   Press Ctrl+C to stop\n")

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
