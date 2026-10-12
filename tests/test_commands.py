import asyncio
from types import SimpleNamespace

import pytest

from snipe import bot as bot_mod
from snipe.database import Database


class Chat:
    def __init__(self, tmp_path, make_config):
        self.db = Database(tmp_path / "cmd.db")
        self.db.add_subscriber(42)
        self.bot_data = {"db": self.db, "config": make_config("")}
        self.replies = []

    def run(self, handler, *args):
        async def reply_text(text, **kwargs):
            self.replies.append((text, kwargs.get("reply_markup")))

        update = SimpleNamespace(effective_chat=SimpleNamespace(id=42), message=SimpleNamespace(reply_text=reply_text))
        context = SimpleNamespace(args=list(args), bot_data=self.bot_data)
        asyncio.run(handler(update, context))
        return self.replies[-1][0]

    def tap(self, key):
        async def answer():
            pass

        async def edit(text, **kwargs):
            self.replies.append((text, kwargs.get("reply_markup")))

        query = SimpleNamespace(data=f"set:{key}", answer=answer, edit_message_text=edit, message=SimpleNamespace(chat_id=42))
        asyncio.run(bot_mod.handle_settings_callback(SimpleNamespace(callback_query=query), SimpleNamespace(bot_data=self.bot_data)))
        return self.replies[-1]

    @property
    def settings(self):
        return self.db.get_settings(42)


@pytest.fixture
def chat(tmp_path, make_config):
    return Chat(tmp_path, make_config)


def test_interests_and_mute(chat):
    assert "Current: <b>none</b>" in chat.run(bot_mod.cmd_interests)
    reply = chat.run(bot_mod.cmd_interests, "AI,", "Web", "Dev")
    assert chat.settings.interests == ["ai", "web dev"] and "ai, web dev" in reply
    chat.run(bot_mod.cmd_interests, "clear")
    assert chat.settings.interests == []
    chat.run(bot_mod.cmd_mute, "MBA,", "sales")
    assert chat.settings.muted == ["mba", "sales"]


def test_interests_are_html_escaped(chat):
    assert "&lt;b&gt;" in chat.run(bot_mod.cmd_interests, "<b>")


@pytest.mark.parametrize("args, amount", [(["10000"], 10000), (["10k"], 10000), (["₹12,500"], 12500), (["0"], 0)])
def test_stipend(chat, args, amount):
    chat.run(bot_mod.cmd_stipend, *args)
    assert chat.settings.min_stipend == amount and chat.settings.paid_only is (amount > 0)


def test_stipend_rejects_garbage(chat):
    assert chat.run(bot_mod.cmd_stipend, "lots").startswith("Usage")
    assert chat.settings.min_stipend == 0


def test_quiet(chat):
    chat.run(bot_mod.cmd_quiet, "22", "6")
    assert chat.settings.quiet == (22, 6)
    chat.run(bot_mod.cmd_quiet, "off")
    assert chat.settings.quiet is None
    for bad in (["25", "6"], ["7", "7"], ["night"]):
        assert chat.run(bot_mod.cmd_quiet, *bad).startswith("Usage")


def test_city_switches_location_to_near(chat):
    chat.run(bot_mod.cmd_city, "Chennai")
    assert (chat.settings.city, chat.settings.location) == ("Chennai", "near")


def test_settings_menu_and_buttons(chat):
    chat.run(bot_mod.cmd_settings)
    text, keyboard = chat.replies[-1]
    assert "Quiet hours: <b>23:00–07:00</b>" in text
    assert [row[0].callback_data for row in keyboard.inline_keyboard] == [
        "set:delivery", "set:quiet", "set:location", "set:paid", "set:days"]

    text, keyboard = chat.tap("delivery")
    assert chat.settings.delivery == "digest" and "Daily digest at 08:00" in text
    assert keyboard.inline_keyboard[1][0].callback_data == "set:digest_hour"  # digest time appears

    chat.tap("paid")
    assert chat.settings.paid_only


def test_commands_require_subscription(chat):
    chat.db.remove_subscriber(42)
    assert chat.run(bot_mod.cmd_settings) == "Send /start first to activate Snipe."
