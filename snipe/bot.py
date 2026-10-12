"""Telegram bot — command handlers, inline keyboards, delivery, and scheduled scanning."""

import asyncio
import html
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import AIORateLimiter, Application, CallbackQueryHandler, CommandHandler, ContextTypes

from .config import CATEGORIES, CATEGORY_LABELS, Config
from .database import Database
from .formatter import Alert, Formatter, FormatterError, render_batch
from .preferences import (
    DEADLINE_PRESETS,
    DIGEST_HOURS,
    LOCATION_MODES,
    QUIET_PRESETS,
    UserSettings,
    in_quiet_hours,
    next_in_cycle,
    parse_word_list,
    wants,
)
from .scanner import Scanner

logger = logging.getLogger(__name__)

SCAN_COOLDOWN = 300  # seconds
LLM_CALLS_PER_ALERT = 4  # LLM budget per scan = this × max_alerts_per_scan
FIRST_SCAN_DELAY = 30  # seconds after startup
BATCH_THRESHOLD = 3  # more alerts than this for one user in one go → a single combined message
REMINDER_LEAD = timedelta(hours=24)  # "starts soon" ping this long before a CTF begins

BOT_COMMANDS = [
    ("start", "Activate alerts"),
    ("settings", "Delivery, quiet hours, location & more"),
    ("filter", "Choose alert categories"),
    ("interests", "Only get alerts about these topics"),
    ("mute", "Never get alerts about these topics"),
    ("scan", "Scan for opportunities now"),
    ("status", "Check your status"),
    ("stats", "View scan statistics"),
    ("stop", "Pause alerts"),
    ("help", "Show all commands"),
]


@dataclass
class ScanOutcome:
    new_alerts: int = 0
    delivered: dict[int, int] = field(default_factory=dict)  # chat_id -> alerts sent now
    queued: dict[int, int] = field(default_factory=dict)  # chat_id -> alerts held for digest/quiet hours
    skipped: bool = False  # another scan was already running


def _now(config: Config) -> datetime:
    return datetime.now(config.timezone)


# ── Helpers ──────────────────────────────────────────────────


def _filter_keyboard(enabled: list[str]) -> InlineKeyboardMarkup:
    """Build inline keyboard with toggle buttons for each category."""
    buttons = []
    for cat in CATEGORIES:
        icon = "✅" if cat in enabled else "❌"
        label = CATEGORY_LABELS.get(cat, cat.title())
        buttons.append([InlineKeyboardButton(f"{icon} {label}", callback_data=f"filter:{cat}")])
    return InlineKeyboardMarkup(buttons)


def _filter_text(cats: list[str]) -> str:
    return (
        "🎯 <b>Choose your categories</b>\n\n"
        "Tap to toggle on/off.\n"
        f"Currently tracking <b>{len(cats)}</b> of {len(CATEGORIES)} categories."
    )


def _hour(h: int) -> str:
    return f"{h:02d}:00"


def _setting_labels(s: UserSettings) -> dict[str, str]:
    location = {
        "any": "Anywhere",
        "online": "Online only",
        "near": f"Near {s.city} + online" if s.city else "Near me (set /city first)",
    }[s.location]
    internships = "Any"
    if s.min_stipend:
        internships = f"₹{s.min_stipend:,}+/month"
    elif s.paid_only:
        internships = "Paid only"
    return {
        "delivery": "Instant" if s.delivery == "instant" else f"Daily digest at {_hour(s.digest_hour)}",
        "quiet": f"{_hour(s.quiet[0])}–{_hour(s.quiet[1])}" if s.quiet else "Off",
        "location": location,
        "internships": internships,
        "days": f"{s.min_days_left} days" if s.min_days_left else "Off",
        "interests": ", ".join(s.interests) or "Everything",
        "muted": ", ".join(s.muted) or "Nothing",
    }


def _settings_text(s: UserSettings) -> str:
    lbl = {k: html.escape(v) for k, v in _setting_labels(s).items()}
    return (
        "⚙️ <b>Your settings</b>\n\n"
        f"🔔 Delivery: <b>{lbl['delivery']}</b>\n"
        f"🌙 Quiet hours: <b>{lbl['quiet']}</b>\n"
        f"📍 Location: <b>{lbl['location']}</b>\n"
        f"💼 Internships: <b>{lbl['internships']}</b>\n"
        f"⏳ Hide if closing within: <b>{lbl['days']}</b>\n"
        f"🏷 Interests: <b>{lbl['interests']}</b>\n"
        f"🔇 Muted: <b>{lbl['muted']}</b>\n\n"
        "Tap a button to change it, or type:\n"
        "/interests ai, web dev — only these topics\n"
        "/mute mba, sales — never these topics\n"
        "/city Chennai — your city for “Near me”\n"
        "/stipend 10000 — minimum monthly stipend\n"
        "/quiet 22 7 — custom quiet hours"
    )


def _settings_keyboard(s: UserSettings) -> InlineKeyboardMarkup:
    lbl = _setting_labels(s)
    rows = [("delivery", f"🔔 Delivery: {'Instant' if s.delivery == 'instant' else 'Daily digest'}")]
    if s.delivery == "digest":
        rows.append(("digest_hour", f"🕗 Digest time: {_hour(s.digest_hour)}"))
    rows += [
        ("quiet", f"🌙 Quiet hours: {lbl['quiet']}"),
        ("location", f"📍 Location: {lbl['location']}"),
        ("paid", f"💼 Paid internships only: {'On' if s.paid_only else 'Off'}"),
        ("reminders", f"⏰ CTF reminders (day before): {'On' if s.ctf_reminders else 'Off'}"),
        ("days", f"⏳ Hide if closing within: {lbl['days']}"),
    ]
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data=f"set:{key}")] for key, text in rows])


def _commands_text(config: Config) -> str:
    return (
        "<b>Commands:</b>\n"
        "/start — activate alerts\n"
        "/stop — pause alerts\n"
        "/status — check your status\n"
        "/filter — choose alert categories\n"
        "/settings — delivery, quiet hours, location &amp; more\n"
        "/interests — only alerts about certain topics\n"
        "/mute — block topics you don't care about\n"
        "/city — your city for location filtering\n"
        "/stipend — minimum internship stipend\n"
        "/quiet — custom quiet hours\n"
        "/scan — scan for opportunities now\n"
        "/stats — view scan statistics\n"
        "/help — show this message\n\n"
        f"Scans run automatically every {config.scan_interval} minutes."
    )


async def _require_subscribed(update: Update, db: Database) -> bool:
    if db.is_subscribed(update.effective_chat.id):
        return True
    await update.message.reply_text("Send /start first to activate Snipe.")
    return False


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
        "You'll get alerts for:\n"
        "• 🏆 Hackathons\n"
        "• 💼 Internships\n"
        "• 🎓 Fellowships\n"
        "• 🏅 Competitions &amp; grants\n"
        "• 🐛 Bug Bounties\n"
        "• 🚩 CTF Events\n"
        "• 📅 Events &amp; workshops (incl. LinkedIn)\n\n"
        "🌙 No alerts between 23:00 and 07:00 — anything found overnight arrives as one message in the morning.\n"
        "📦 When lots drop at once, you get one combined message, not a flood.\n\n"
        "<b>Make it yours:</b>\n"
        "/filter — choose categories\n"
        "/settings — daily digest, quiet hours, location, paid-only &amp; more\n"
        "/interests — only topics you care about\n"
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
        labels = [CATEGORY_LABELS.get(c, c) for c in cats] or ["nothing — use /filter"]
        lbl = {k: html.escape(v) for k, v in _setting_labels(db.get_settings(chat_id)).items()}
        queued = db.queue_size(chat_id)
        await update.message.reply_text(
            f"✅ <b>Alerts: ON</b>\n"
            f"🔄 Scanning every {config.scan_interval} minutes\n"
            f"📋 Tracking: {', '.join(labels)}\n"
            f"🔔 Delivery: {lbl['delivery']} · 🌙 Quiet hours: {lbl['quiet']}\n"
            f"🏷 Interests: {lbl['interests']}\n"
            + (f"📥 Waiting for you: {queued}\n" if queued else "")
            + "\nSend /filter or /settings to change things.\nSend /stop to pause.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(
            "🔕 <b>Alerts: OFF</b>\n\nSend /start to turn on.",
            parse_mode=ParseMode.HTML,
        )


async def cmd_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return

    cats = db.get_categories(update.effective_chat.id)
    await update.message.reply_text(
        _filter_text(cats),
        parse_mode=ParseMode.HTML,
        reply_markup=_filter_keyboard(cats),
    )


async def handle_filter_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline keyboard taps for /filter."""
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id
    db: Database = context.bot_data["db"]

    category = query.data.split(":", 1)[1]
    if category not in CATEGORIES:
        return

    cats = db.toggle_category(chat_id, category)
    await query.edit_message_text(
        _filter_text(cats),
        parse_mode=ParseMode.HTML,
        reply_markup=_filter_keyboard(cats),
    )


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return
    s = db.get_settings(update.effective_chat.id)
    await update.message.reply_text(_settings_text(s), parse_mode=ParseMode.HTML, reply_markup=_settings_keyboard(s))


def apply_setting_tap(s: UserSettings, key: str, now: datetime) -> UserSettings:
    """Advance one /settings button to its next value."""
    if key == "delivery":
        s.delivery = "digest" if s.delivery == "instant" else "instant"
        if s.delivery == "digest" and now.hour >= s.digest_hour:
            s.last_digest = now.date().isoformat()  # first digest at the next digest time, not right now
    elif key == "digest_hour":
        s.digest_hour = next_in_cycle(DIGEST_HOURS, s.digest_hour)
    elif key == "quiet":
        s.quiet_start, s.quiet_end = next_in_cycle(QUIET_PRESETS, s.quiet) or (None, None)
    elif key == "location":
        s.location = next_in_cycle(LOCATION_MODES, s.location)
    elif key == "paid":
        s.paid_only = not s.paid_only
        if not s.paid_only:
            s.min_stipend = 0
    elif key == "days":
        s.min_days_left = next_in_cycle(DEADLINE_PRESETS, s.min_days_left)
    elif key == "reminders":
        s.ctf_reminders = not s.ctf_reminders
    return s


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    db: Database = context.bot_data["db"]
    config: Config = context.bot_data["config"]

    s = apply_setting_tap(db.get_settings(chat_id), query.data.split(":", 1)[1], _now(config))
    db.save_settings(chat_id, s)
    await query.edit_message_text(_settings_text(s), parse_mode=ParseMode.HTML, reply_markup=_settings_keyboard(s))


async def _set_word_list(update: Update, context: ContextTypes.DEFAULT_TYPE, attr: str):
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return
    chat_id = update.effective_chat.id
    s = db.get_settings(chat_id)
    raw = " ".join(context.args)
    command = "interests" if attr == "interests" else "mute"

    if not raw:
        current = ", ".join(getattr(s, attr)) or "none"
        example = "ai, web dev, blockchain" if attr == "interests" else "mba, sales, marketing"
        what = "Only alerts mentioning one of these" if attr == "interests" else "Never alerts mentioning any of these"
        await update.message.reply_text(
            f"{what}.\n\nCurrent: <b>{html.escape(current)}</b>\n\n"
            f"Set: /{command} {example}\nClear: /{command} clear",
            parse_mode=ParseMode.HTML,
        )
        return

    words = [] if raw.strip().lower() in ("clear", "none", "off") else parse_word_list(raw)
    setattr(s, attr, words)
    db.save_settings(chat_id, s)
    if attr == "interests":
        reply = f"🏷 Only alerts about: <b>{html.escape(', '.join(words))}</b>" if words else "🏷 Interests cleared — you'll get everything."
    else:
        reply = f"🔇 Muted: <b>{html.escape(', '.join(words))}</b>" if words else "🔇 Nothing muted."
    await update.message.reply_text(reply, parse_mode=ParseMode.HTML)


async def cmd_interests(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_word_list(update, context, "interests")


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _set_word_list(update, context, "muted")


async def cmd_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return
    city = " ".join(" ".join(context.args).split())[:40]
    if not city:
        await update.message.reply_text("Usage: /city Chennai")
        return
    s = db.get_settings(update.effective_chat.id)
    s.city, s.location = city, "near"
    db.save_settings(update.effective_chat.id, s)
    await update.message.reply_text(
        f"📍 Showing online events plus in-person ones in <b>{html.escape(city)}</b>.\n"
        "Change it anytime in /settings.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stipend(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return
    raw = "".join(context.args).replace(",", "").replace("₹", "").lower().removesuffix("k")
    if not raw.isdigit():
        await update.message.reply_text("Usage: /stipend 10000  (or /stipend 0 for any)")
        return
    amount = int(raw) * (1000 if "".join(context.args).lower().endswith("k") else 1)
    s = db.get_settings(update.effective_chat.id)
    s.min_stipend, s.paid_only = amount, amount > 0
    db.save_settings(update.effective_chat.id, s)
    await update.message.reply_text(
        f"💼 Internships paying at least <b>₹{amount:,}/month</b> (unpaid ones hidden)." if amount
        else "💼 Showing all internships, paid or not.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_quiet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return
    s = db.get_settings(update.effective_chat.id)
    args = [a.removesuffix(":00") for a in context.args]
    if args and args[0].lower() == "off":
        s.quiet_start = s.quiet_end = None
        reply = "🌙 Quiet hours off — alerts arrive any time."
    elif len(args) == 2 and all(a.isdigit() and 0 <= int(a) <= 23 for a in args) and args[0] != args[1]:
        s.quiet_start, s.quiet_end = int(args[0]), int(args[1])
        reply = f"🌙 No alerts from {_hour(s.quiet_start)} to {_hour(s.quiet_end)}. Anything found then arrives after."
    else:
        await update.message.reply_text("Usage: /quiet 23 7  (from 23:00 to 07:00)  ·  /quiet off")
        return
    db.save_settings(update.effective_chat.id, s)
    await update.message.reply_text(reply)


async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    db: Database = context.bot_data["db"]
    if not await _require_subscribed(update, db):
        return

    now = time.time()
    remaining = SCAN_COOLDOWN - (now - db.get_cooldown(chat_id))
    if remaining > 0:
        mins = int(remaining // 60) + 1
        await update.message.reply_text(f"⏳ Please wait {mins} more minute(s) before scanning again.")
        return

    if context.bot_data["scan_lock"].locked():
        await update.message.reply_text("🔄 A scan is already running — new finds will arrive shortly.")
        return

    db.set_cooldown(chat_id, now)
    await update.message.reply_text("🔍 Scanning for opportunities...")

    outcome = await run_scan(context.application)
    sent, queued = outcome.delivered.get(chat_id, 0), outcome.queued.get(chat_id, 0)
    s = db.get_settings(chat_id)

    if outcome.skipped:
        await update.message.reply_text("🔄 A scan is already running — new finds will arrive shortly.")
    elif sent:
        await update.message.reply_text(f"✅ Sent {sent} new opportunit{'y' if sent == 1 else 'ies'}!")
    elif queued and s.delivery == "digest":
        await update.message.reply_text(f"📬 Found {queued} new — saved for your {_hour(s.digest_hour)} digest.")
    elif queued:
        await update.message.reply_text(f"🌙 Found {queued} new — holding them until quiet hours end at {_hour(s.quiet_end)}.")
    else:
        await update.message.reply_text("😴 Nothing new for you right now. I'll alert you when something drops.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db: Database = context.bot_data["db"]
    stats = db.get_stats()

    last = stats.get("last_scan")
    last_time = f"{last['scanned_at']} UTC" if last else "Never"
    last_found = last["found"] if last else 0

    await update.message.reply_text(
        f"📊 <b>Snipe Stats</b>\n\n"
        f"👥 Active subscribers: <b>{stats['subscribers']}</b>\n"
        f"📦 Opportunities alerted: <b>{stats['total_seen']}</b>\n"
        f"🗑 Filtered out as noise: <b>{stats['total_rejected']}</b>\n"
        f"🔄 Total scans: <b>{stats['total_scans']}</b>\n\n"
        f"🕐 Last scan: {last_time}\n"
        f"📥 Matched last scan: {last_found}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    config: Config = context.bot_data["config"]
    await update.message.reply_text(
        "🎯 <b>Snipe — Opportunity Hunter</b>\n\n"
        f"{_commands_text(config)}\n\n"
        "<b>Categories:</b>\n"
        "🏆 Hackathons · 💼 Internships · 🎓 Fellowships · 🏅 Competitions\n"
        "💰 Grants · 🐛 Bug Bounties · 🚩 CTFs · 📅 Events",
        parse_mode=ParseMode.HTML,
    )


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled error while processing %s", update, exc_info=context.error)


# ── Core scan logic (shared by manual + scheduled) ──────────


async def run_scan(app: Application) -> ScanOutcome:
    """Scan all sources and deliver new alerts to every subscriber who wants them."""
    lock: asyncio.Lock = app.bot_data["scan_lock"]
    if lock.locked():
        return ScanOutcome(skipped=True)
    async with lock:
        return await _scan_and_broadcast(app)


async def _scan_and_broadcast(app: Application) -> ScanOutcome:
    db: Database = app.bot_data["db"]
    config: Config = app.bot_data["config"]
    scanner: Scanner = app.bot_data["scanner"]
    formatter: Formatter = app.bot_data["formatter"]
    outcome = ScanOutcome()

    subscribers = db.get_subscriber_categories()
    if not subscribers:
        return outcome
    settings = db.get_all_settings(subscribers)

    try:
        # Scanning and LLM calls are blocking I/O — keep them off the event loop
        report = await asyncio.to_thread(scanner.scan)
    except Exception:
        logger.exception("scan() failed")
        db.log_scan(errors=1)
        return outcome

    errors = report.errors
    llm_budget = config.max_alerts_per_scan * LLM_CALLS_PER_ALERT
    today = _now(config).date()
    pending: dict[int, list[Alert]] = defaultdict(list)

    for item in report.items:
        if outcome.new_alerts >= config.max_alerts_per_scan or llm_budget <= 0:
            logger.info("Per-scan limit reached — remaining items roll over to the next scan")
            break
        if db.is_seen(item.url):
            continue

        if not item.trusted:
            llm_budget -= 1
        try:
            alert = await asyncio.to_thread(formatter.format_alert, item)
        except FormatterError as e:
            errors += 1
            logger.warning("%s", e)
            continue  # not marked seen, so it's retried next scan

        if alert is None:
            db.mark_seen(item.url, item.title, item.category, status="rejected")
            continue

        db.mark_seen(item.url, item.title, alert.category)
        outcome.new_alerts += 1
        for chat_id, cats in subscribers.items():
            if alert.category in cats and wants(settings[chat_id], alert, today):
                pending[chat_id].append(alert)

    # Deliver per user, after the loop, so many finds become one message instead of a flood
    now = _now(config)
    hour = now.hour
    for chat_id, alerts in pending.items():
        s = settings[chat_id]
        if s.ctf_reminders:
            for a in alerts:
                # Skip events starting so soon that the reminder would land right after the alert
                if a.starts_at and a.starts_at - now > REMINDER_LEAD + timedelta(hours=6):
                    db.add_reminder(chat_id, a.url, a.starts_at, a.compact)
        entries = [(a.message, a.compact) for a in alerts]
        if s.delivery == "digest" or in_quiet_hours(s, hour):
            for message, compact in entries:
                db.enqueue(chat_id, message, compact)
            outcome.queued[chat_id] = len(entries)
            continue
        sent = await _deliver_now(app, db, chat_id, entries, _batch_header(len(entries)))
        outcome.delivered[chat_id] = sent
        errors += len(entries) - sent

    db.log_scan(
        sources_checked=report.sources_checked,
        found=len(report.items),
        sent=sum(outcome.delivered.values()),
        errors=errors,
    )
    return outcome


def _batch_header(n: int) -> str:
    return f"🎯 <b>{n} new opportunities</b> — tap a name to open it"


async def flush_queues(app: Application):
    """Send queued alerts that are due: digests at the user's hour, everything else once quiet hours end."""
    db: Database = app.bot_data["db"]
    config: Config = app.bot_data["config"]
    now = _now(config)
    today = now.date().isoformat()
    await _send_reminders(app, db, now)

    for chat_id in db.queued_chat_ids():
        rows = db.peek_queue(chat_id)
        if not db.is_subscribed(chat_id):
            db.drop_queued([r["id"] for r in rows])
            continue
        s = db.get_settings(chat_id)
        if s.delivery == "digest":
            if now.hour < s.digest_hour or s.last_digest == today:
                continue
            header = f"📬 <b>Your daily digest</b> — {len(rows)} new opportunit{'y' if len(rows) == 1 else 'ies'}"
        else:
            if in_quiet_hours(s, now.hour):
                continue
            header = f"☀️ <b>While you were away</b> — {len(rows)} new opportunit{'y' if len(rows) == 1 else 'ies'}"

        entries = [(r["message"], r["compact"]) for r in rows]
        # A digest is always one combined message, however few items it has
        force_batch = s.delivery == "digest"
        if await _deliver_now(app, db, chat_id, entries, header, force_batch=force_batch):
            db.drop_queued([r["id"] for r in rows])
            if s.delivery == "digest":
                s.last_digest = today
                db.save_settings(chat_id, s)


async def _send_reminders(app: Application, db: Database, now: datetime):
    """One "starting soon" message per user for CTFs beginning within REMINDER_LEAD."""
    due: dict[int, list] = defaultdict(list)
    expired = []
    for r in db.reminders_before(now + REMINDER_LEAD):
        (expired if datetime.fromisoformat(r["starts_at"]) <= now else due[r["chat_id"]]).append(r)
    db.drop_reminders([r["rowid"] for r in expired])

    for chat_id, rows in due.items():
        s = db.get_settings(chat_id)
        if not db.is_subscribed(chat_id) or not s.ctf_reminders:
            db.drop_reminders([r["rowid"] for r in rows])
            continue
        if in_quiet_hours(s, now.hour):
            continue  # try again next hour
        entries = []
        for r in sorted(rows, key=lambda r: r["starts_at"]):
            hours = max(1, round((datetime.fromisoformat(r["starts_at"]) - now).total_seconds() / 3600))
            entries.append(f"{r['compact']}\n⏳ starts in about {hours} h")
        header = f"⏰ <b>Starting soon</b> — {len(rows)} CTF{'s' if len(rows) != 1 else ''} you were alerted about"
        sent = True
        for chunk in render_batch(header, entries):
            sent = sent and await _send(app, db, chat_id, chunk, preview=False)
        if sent:
            db.drop_reminders([r["rowid"] for r in rows])


async def _deliver_now(
    app: Application, db: Database, chat_id: int, entries: list[tuple[str, str]], header: str,
    force_batch: bool = False,
) -> int:
    """Send alerts to one chat: full messages if only a few, else one combined message. Returns alerts sent."""
    if len(entries) <= BATCH_THRESHOLD and not force_batch:
        sent = 0
        for message, _ in entries:
            if not await _send(app, db, chat_id, message, preview=True):
                break  # blocked or broken chat — don't hammer it
            sent += 1
        return sent

    for chunk in render_batch(header, [compact for _, compact in entries]):
        if not await _send(app, db, chat_id, chunk, preview=False):
            return 0
    return len(entries)


async def _send(app: Application, db: Database, chat_id: int, text: str, preview: bool) -> bool:
    try:
        await app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=not preview),
        )
        return True
    except Forbidden:
        logger.info("chat_id=%s blocked the bot — unsubscribing", chat_id)
        db.remove_subscriber(chat_id)
    except BadRequest as e:
        if "chat not found" in str(e).lower():
            logger.info("chat_id=%s no longer exists — unsubscribing", chat_id)
            db.remove_subscriber(chat_id)
        else:
            logger.exception("Telegram rejected message for chat_id=%s", chat_id)
    except Exception:
        logger.exception("Failed to send message to chat_id=%s", chat_id)
    return False


# ── Scheduled jobs ───────────────────────────────────────────


async def scheduled_scan(app: Application):
    """Auto-scan triggered by APScheduler."""
    db: Database = app.bot_data["db"]
    if not db.subscriber_count():
        return

    logger.info("Scheduled scan starting — %d subscriber(s)", db.subscriber_count())
    db.prune_old_seen(days=90)
    outcome = await run_scan(app)
    logger.info("Scheduled scan complete — %d new alert(s), %d user(s) queued",
                outcome.new_alerts, len(outcome.queued))


# ── Application builder ─────────────────────────────────────


def create_app(config: Config) -> Application:
    """Build and configure the Telegram Application."""
    db = Database(config.db_path)
    scheduler = AsyncIOScheduler(timezone=config.timezone)

    async def on_startup(application: Application):
        scheduler.add_job(
            scheduled_scan,
            "interval",
            minutes=config.scan_interval,
            args=[application],
            next_run_time=datetime.now(config.timezone) + timedelta(seconds=FIRST_SCAN_DELAY),
            max_instances=1,
            coalesce=True,
        )
        # Hourly, on the hour: digests and the end of quiet hours both fall on whole hours
        scheduler.add_job(flush_queues, "cron", minute=0, args=[application], max_instances=1, coalesce=True)
        scheduler.start()
        try:
            await application.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
        except Exception:
            logger.warning("Couldn't register the command menu with Telegram", exc_info=True)
        logger.info("Snipe started — interval=%dm, subscribers=%d", config.scan_interval, db.subscriber_count())

    async def on_shutdown(application: Application):
        logger.info("Shutting down — closing database")
        if scheduler.running:  # not started if startup failed (e.g. a rejected token)
            scheduler.shutdown(wait=False)
        db.close()

    app = (
        Application.builder()
        .token(config.telegram_token)
        .rate_limiter(AIORateLimiter(max_retries=3))
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )
    app.bot_data["db"] = db
    app.bot_data["config"] = config
    app.bot_data["scanner"] = Scanner(config)
    app.bot_data["formatter"] = Formatter(config)
    app.bot_data["scan_lock"] = asyncio.Lock()

    # Commands
    for name, handler in [
        ("start", cmd_start), ("stop", cmd_stop), ("status", cmd_status), ("filter", cmd_filter),
        ("settings", cmd_settings), ("interests", cmd_interests), ("mute", cmd_mute), ("city", cmd_city),
        ("stipend", cmd_stipend), ("quiet", cmd_quiet), ("scan", cmd_scan), ("stats", cmd_stats),
        ("help", cmd_help),
    ]:
        app.add_handler(CommandHandler(name, handler))

    # Inline keyboard callbacks
    app.add_handler(CallbackQueryHandler(handle_filter_callback, pattern=r"^filter:"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^set:"))
    app.add_error_handler(on_error)

    return app
