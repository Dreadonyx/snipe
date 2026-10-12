import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest
from telegram.error import Forbidden

from snipe import bot as bot_mod
from snipe.bot import flush_queues, run_scan
from snipe.database import Database
from snipe.formatter import Alert, FormatterError
from snipe.preferences import UserSettings
from snipe.scanner import ScanReport, ScanResult


class FakeBot:
    def __init__(self, blocked=()):
        self.sent, self.blocked = [], set(blocked)

    async def send_message(self, chat_id, text, **kwargs):
        if chat_id in self.blocked:
            raise Forbidden("Forbidden: bot was blocked by the user")
        self.sent.append((chat_id, text))


class FakeFormatter:
    """Category from the title prefix; 'noise' is rejected, 'boom' fails transiently."""

    def __init__(self):
        self.calls = []

    def format_alert(self, item):
        self.calls.append(item.url)
        kind = item.title.split()[0]
        if kind == "noise":
            return None
        if kind == "boom":
            raise FormatterError("groq down")
        return Alert(f"alert: {item.title}", kind, compact=f"• {item.title}", text=item.title, mode=item.mode)


@pytest.fixture
def clock(monkeypatch):
    """Pin the bot's clock (defaults to noon, outside the default 23:00–07:00 quiet hours)."""
    state = {"now": datetime(2026, 10, 12, 12, 0)}
    monkeypatch.setattr(bot_mod, "_now", lambda config: state["now"])
    return state


@pytest.fixture
def app(tmp_path, make_config, clock):
    def _make(items, blocked=(), max_alerts=15):
        db = Database(tmp_path / "bot.db")
        config = make_config(f"max_alerts_per_scan: {max_alerts}")
        report = ScanReport(items=items, sources_checked=1)
        return SimpleNamespace(
            bot=FakeBot(blocked),
            bot_data={
                "db": db, "config": config, "formatter": FakeFormatter(),
                "scanner": SimpleNamespace(scan=lambda: report), "scan_lock": asyncio.Lock(),
            },
        )
    return _make


def result(title, n, **kw):
    return ScanResult(title=title, url=f"https://example.com/{n}", **kw)


def test_alerts_broadcast_to_every_matching_subscriber(app):
    a = app([result("hackathon A", 1), result("ctf B", 2), result("noise C", 3), result("boom D", 4)])
    db = a.bot_data["db"]
    for chat in (1, 2):
        db.add_subscriber(chat)
    db.set_categories(2, ["ctf"])

    outcome = asyncio.run(run_scan(a))

    assert sorted(a.bot.sent) == [(1, "alert: ctf B"), (1, "alert: hackathon A"), (2, "alert: ctf B")]
    assert outcome.delivered == {1: 2, 2: 1} and outcome.new_alerts == 2
    assert db.is_seen("https://example.com/3")      # rejection remembered: no LLM call next time
    assert not db.is_seen("https://example.com/4")  # transient failure: retried next scan

    a.bot.sent.clear()
    asyncio.run(run_scan(a))
    assert a.bot.sent == []
    assert a.bot_data["formatter"].calls.count("https://example.com/3") == 1
    assert a.bot_data["formatter"].calls.count("https://example.com/4") == 2


def test_per_user_settings_filter_alerts(app):
    a = app([result("hackathon AI build", 1, mode="offline"), result("hackathon Web3 sprint", 2, mode="online")])
    db = a.bot_data["db"]
    for chat in (1, 2, 3):
        db.add_subscriber(chat)
    db.save_settings(1, UserSettings(interests=["ai"]))
    db.save_settings(2, UserSettings(location="online"))
    asyncio.run(run_scan(a))
    assert sorted(a.bot.sent) == [
        (1, "alert: hackathon AI build"),
        (2, "alert: hackathon Web3 sprint"),
        (3, "alert: hackathon AI build"), (3, "alert: hackathon Web3 sprint"),
    ]


def test_many_alerts_become_one_message(app):
    a = app([result("hackathon", n) for n in range(6)])
    a.bot_data["db"].add_subscriber(1)
    outcome = asyncio.run(run_scan(a))
    assert outcome.delivered == {1: 6}
    assert len(a.bot.sent) == 1
    text = a.bot.sent[0][1]
    assert text.startswith("🎯 <b>6 new opportunities</b>") and text.count("• hackathon") == 6


def test_quiet_hours_hold_alerts_until_morning(app, clock):
    a = app([result("hackathon A", 1), result("ctf B", 2)])
    db = a.bot_data["db"]
    db.add_subscriber(1)  # default quiet hours: 23:00–07:00

    clock["now"] = datetime(2026, 10, 12, 2, 30)
    outcome = asyncio.run(run_scan(a))
    assert a.bot.sent == [] and outcome.queued == {1: 2}

    clock["now"] = datetime(2026, 10, 12, 6, 0)
    asyncio.run(flush_queues(a))
    assert a.bot.sent == []  # still quiet

    clock["now"] = datetime(2026, 10, 12, 7, 0)
    asyncio.run(flush_queues(a))
    assert a.bot.sent == [(1, "alert: hackathon A"), (1, "alert: ctf B")]  # few → full messages
    assert db.queue_size(1) == 0


def test_daily_digest(app, clock):
    a = app([result("hackathon A", 1)])
    db = a.bot_data["db"]
    db.add_subscriber(1)
    db.save_settings(1, UserSettings(delivery="digest", digest_hour=18))

    asyncio.run(run_scan(a))
    assert a.bot.sent == [] and db.queue_size(1) == 1

    asyncio.run(flush_queues(a))  # noon: too early
    assert a.bot.sent == []

    clock["now"] = datetime(2026, 10, 12, 18, 0)
    asyncio.run(flush_queues(a))
    assert len(a.bot.sent) == 1
    assert a.bot.sent[0][1] == "📬 <b>Your daily digest</b> — 1 new opportunity\n\n• hackathon A"
    assert db.get_settings(1).last_digest == "2026-10-12"

    db.enqueue(1, "late", "• late find")
    clock["now"] = datetime(2026, 10, 12, 21, 0)
    asyncio.run(flush_queues(a))
    assert len(a.bot.sent) == 1  # one digest per day

    clock["now"] = datetime(2026, 10, 13, 18, 0)
    asyncio.run(flush_queues(a))
    assert a.bot.sent[-1][1].endswith("• late find")


def test_queue_dropped_for_unsubscribed_users(app):
    a = app([])
    db = a.bot_data["db"]
    db.add_subscriber(1)
    db.enqueue(1, "m", "c")
    db.remove_subscriber(1)
    asyncio.run(flush_queues(a))
    assert a.bot.sent == [] and db.queue_size(1) == 0


def test_blocked_users_are_unsubscribed(app):
    a = app([result("hackathon A", 1)], blocked={2})
    db = a.bot_data["db"]
    db.add_subscriber(1)
    db.add_subscriber(2)
    asyncio.run(run_scan(a))
    assert db.get_active_subscribers() == [1]


def test_alert_cap_rolls_items_over_to_next_scan(app):
    a = app([result("hackathon", n) for n in range(5)], max_alerts=2)
    a.bot_data["db"].add_subscriber(1)
    assert asyncio.run(run_scan(a)).new_alerts == 2
    assert asyncio.run(run_scan(a)).new_alerts == 2
    assert asyncio.run(run_scan(a)).new_alerts == 1


def test_overlapping_scans_are_skipped(app):
    a = app([result("hackathon", 1)])
    a.bot_data["db"].add_subscriber(1)

    async def scenario():
        async with a.bot_data["scan_lock"]:
            return await run_scan(a)

    assert asyncio.run(scenario()).skipped


def test_no_subscribers_means_no_scan(app):
    a = app([result("hackathon", 1)])
    a.bot_data["scanner"].scan = lambda: pytest.fail("should not scan without subscribers")
    assert asyncio.run(run_scan(a)).new_alerts == 0


def test_shutdown_after_failed_startup_does_not_mask_the_error(tmp_path, make_config):
    from snipe.bot import create_app

    config = make_config("", TELEGRAM_TOKEN="123:abc", GROQ_API_KEY="k", SNIPE_DB_PATH=str(tmp_path / "x.db"))
    app = create_app(config)
    asyncio.run(app.post_shutdown(app))  # post_init never ran, so the scheduler never started
