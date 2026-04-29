"""Telegram bot — command handlers, inline keyboard, and scheduled scanning."""

import asyncio
import logging
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Config, CATEGORIES, CATEGORY_LABELS
from .database import Database
from .formatter import Formatter
from .scanner import Scanner

logger = logging.getLogger(__name__)

SCAN_COOLDOWN = 300  # seconds


# ── Helpers ──────────────────────────────────────────────────


def _filter_keyboard(enabled: list[str]) -> InlineKeyboardMarkup:
    """Build inline keyboard with toggle buttons for each category."""
    buttons = []
    for cat in CATEGORIES:
        icon = "✅" if cat in enabled else "❌"
        label = CATEGORY_LABELS.get(cat, cat.title())
        buttons.append([InlineKeyboardButton(f"{icon} {label}", callback_data=f"filter:{cat}")])
    return InlineKeyboardMarkup(buttons)


# ── Command handlers ─────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]

    if db.is_subscribed(chat_id):
        await update.message.reply_text("✅ You're already receiving alerts!")
        return

    db.add_subscriber(chat_id)
    await update.message.reply_text(
        "🎯 <b>Snipe is ON!</b>\n\n"
        "You'll get instant alerts for:\n"
        "• 🏆 Hackathons\n"
        "• 💼 Internships\n"
        "• 🎓 Fellowships\n"
        "• 🏅 Competitions &amp; grants\n"
        "• 🐛 Bug Bounties\n"
        "• 🚩 CTF Events\n\n"
        "<b>Commands:</b>\n"
        "/filter — choose what alerts you want\n"
        "/scan — scan for opportunities now\n"
        "/stats — view scan statistics\n"
        "/stop — pause alerts\n"
        "/help — all commands",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]
    db.remove_subscriber(chat_id)
    await update.message.reply_text(
        "🔕 <b>Alerts paused.</b>\n\nSend /start anytime to turn them back on.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]
    config: Config = context.bot_data["config"]

    if db.is_subscribed(chat_id):
        cats = db.get_categories(chat_id)
        labels = [CATEGORY_LABELS.get(c, c) for c in cats]
        await update.message.reply_text(
            f"✅ <b>Alerts: ON</b>\n"
            f"🔄 Scanning every {config.scan_interval} minutes\n"
            f"📋 Tracking: {', '.join(labels)}\n\n"
            f"Send /filter to change categories.\n"
            f"Send /stop to pause.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "🔕 <b>Alerts: OFF</b>\n\nSend /start to turn on.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]

    if not db.is_subscribed(chat_id):
        await update.message.reply_text("Send /start first to activate Snipe.")
        return

    cats = db.get_categories(chat_id)
    await update.message.reply_text(
        "🎯 <b>Choose your categories</b>\n\n"
        "Tap to toggle on/off.\n"
        f"Currently tracking <b>{len(cats)}</b> of {len(CATEGORIES)} categories.",
        parse_mode=ParseMode.HTML,
        reply_markup=_filter_keyboard(cats),
    )


async def handle_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /filter."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    db: Database = context.bot_data["db"]

    data = query.data
    if not data.startswith("filter:"):
        return

    category = data.split(":", 1)[1]
    if category not in CATEGORIES:
        return

    cats = db.toggle_category(chat_id, category)
    await query.edit_message_text(
        "🎯 <b>Choose your categories</b>\n\n"
        "Tap to toggle on/off.\n"
        f"Currently tracking <b>{len(cats)}</b> of {len(CATEGORIES)} categories.",
        parse_mode=ParseMode.HTML,
        reply_markup=_filter_keyboard(cats),
    )


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]

    if not db.is_subscribed(chat_id):
        await update.message.reply_text("Send /start first to activate Snipe.")
        return

    now = time.time()
    last = db.get_cooldown(chat_id)
    remaining = SCAN_COOLDOWN - (now - last)
    if remaining > 0:
        mins = int(remaining // 60) + 1
        await update.message.reply_text(f"⏳ Please wait {mins} more minute(s) before scanning again.")
        return

    db.set_cooldown(chat_id, now)
    await update.message.reply_text("🔍 Scanning for opportunities...")

    found = await _run_scan(context, target_chat_ids=[chat_id])

    if found == 0:
        await update.message.reply_text("😴 Nothing new right now. I'll alert you when something drops.")
    else:
        await update.message.reply_text(f"✅ Sent {found} new opportunit{'y' if found == 1 else 'ies'}!")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    stats = db.get_stats()

    last = stats.get("last_scan")
    last_time = last["scanned_at"] if last else "Never"
    last_found = last["found"] if last else 0

    await update.message.reply_text(
        f"📊 <b>Snipe Stats</b>\n\n"
        f"👥 Active subscribers: <b>{stats['subscribers']}</b>\n"
        f"📦 Opportunities tracked: <b>{stats['total_seen']}</b>\n"
        f"🔄 Total scans: <b>{stats['total_scans']}</b>\n\n"
        f"🕐 Last scan: {last_time}\n"
        f"📥 Found last scan: {last_found}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 <b>Snipe — Opportunity Hunter</b>\n\n"
        "<b>Commands:</b>\n"
        "/start — activate alerts\n"
        "/stop — pause alerts\n"
        "/status — check your status\n"
        "/filter — choose alert categories\n"
        "/scan — scan for opportunities now\n"
        "/stats — view scan statistics\n"
        "/help — show this message\n\n"
        "<b>Categories:</b>\n"
        "🏆 Hackathons · 💼 Internships · 🎓 Fellowships\n"
        "🏅 Competitions · 💰 Grants · 🐛 Bug Bounties · 🚩 CTFs\n\n"
        "Scans run automatically every 30 minutes.\n"
        "Use /filter to customize what you receive.",
        parse_mode=ParseMode.HTML,
    )


# ── Core scan logic (shared by manual + scheduled) ──────────


async def _run_scan(context: ContextTypes.DEFAULT_TYPE, target_chat_ids: list[int] | None = None) -> int:
    """Run a scan and push alerts to the given chat IDs (or all subscribers)."""
    db: Database = context.bot_data["db"]
    config: Config = context.bot_data["config"]
    formatter: Formatter = context.bot_data["formatter"]

    if target_chat_ids is None:
        target_chat_ids = db.get_active_subscribers()
    if not target_chat_ids:
        return 0

    scanner = Scanner(config)
    try:
        report = scanner.scan()
    except Exception:
        logger.exception("scan() failed")
        db.log_scan(errors=1)
        return 0

    sent_total = 0
    scan_errors = report.errors

    for item in report.items:
        if db.is_seen(item.url):
            continue

        msg, category = formatter.format_alert(item)
        if not msg:
            continue

        # CTFtime items are pre-tagged
        if item.category != "other":
            category = item.category

        db.mark_seen(item.url, item.title, category)

        for chat_id in target_chat_ids:
            user_cats = db.get_categories(chat_id)
            if category not in user_cats:
                continue

            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=False,
                )
                sent_total += 1
                await asyncio.sleep(0.3)
            except Exception:
                scan_errors += 1
                logger.exception("Failed to send alert to chat_id=%s", chat_id)

    db.log_scan(
        sources_checked=report.sources_checked,
        found=len(report.items),
        sent=sent_total,
        errors=scan_errors,
    )
    return sent_total


# ── Scheduled scan wrapper ───────────────────────────────────


async def scheduled_scan(app: Application):
    """Auto-scan triggered by APScheduler."""
    db: Database = app.bot_data["db"]
    subscribers = db.get_active_subscribers()
    if not subscribers:
        return

    logger.info("Scheduled scan starting — %d subscriber(s)", len(subscribers))

    # Prune old seen entries periodically
    db.prune_old_seen(days=90)

    await _run_scan(app, target_chat_ids=subscribers)
    logger.info("Scheduled scan complete")


# ── Application builder ─────────────────────────────────────


def create_app(config: Config) -> Application:
    """Build and configure the Telegram Application."""
    db = Database()
    formatter = Formatter(config)

    app = Application.builder().token(config.telegram_token).build()
    app.bot_data["db"] = db
    app.bot_data["config"] = config
    app.bot_data["formatter"] = formatter

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("help", cmd_help))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_filter_callback, pattern=r"^filter:"))

    # Scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scheduled_scan,
        "interval",
        minutes=config.scan_interval,
        args=[app],
    )

    async def on_startup(application: Application):
        scheduler.start()
        logger.info("Snipe v2 started — interval=%dm, subscribers=%d",
                     config.scan_interval, db.subscriber_count())

    async def on_shutdown(application: Application):
        logger.info("Shutting down — closing database")
        db.close()

    app.post_init = on_startup
    app.post_shutdown = on_shutdown

    return app
