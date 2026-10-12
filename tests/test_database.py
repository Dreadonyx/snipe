import json
import sqlite3

from snipe.config import CATEGORIES
from snipe.database import Database


def test_v0_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE seen (url TEXT PRIMARY KEY, title TEXT, seen_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE preferences (chat_id INTEGER PRIMARY KEY, categories TEXT NOT NULL DEFAULT '[]');
        INSERT INTO seen (url, title) VALUES ('https://a.example/x/', 'A');
        INSERT INTO seen (url, title) VALUES ('https://b.example/y?utm_source=rss', 'B');
        INSERT INTO seen (url, title) VALUES ('https://b.example/y', 'B dup');
        INSERT INTO preferences VALUES (1, '["hackathon", "ctf"]');
    """)
    conn.close()

    db = Database(path)
    assert db.get_categories(1) == ["hackathon", "ctf", "event"]
    assert db.seen_count("sent") == 2  # existing rows default to "sent"; the B duplicate collapsed
    assert db.is_seen("https://a.example/x") and db.is_seen("https://b.example/y")
    assert db.conn.execute("PRAGMA user_version").fetchone()[0] == 2

    db.toggle_category(1, "event")
    db.close()
    assert Database(path).get_categories(1) == ["hackathon", "ctf"]  # migration runs once


def test_rejected_items_are_seen_but_not_counted_as_alerts(tmp_path):
    db = Database(tmp_path / "s.db")
    db.mark_seen("https://a", "A", "hackathon")
    db.mark_seen("https://b", "B", status="rejected")
    assert db.is_seen("https://b")
    stats = db.get_stats()
    assert stats["total_seen"] == 1 and stats["total_rejected"] == 1


def test_subscriber_categories(tmp_path):
    db = Database(tmp_path / "s.db")
    db.add_subscriber(1)
    db.add_subscriber(2)
    db.set_categories(2, ["ctf"])
    db.add_subscriber(3)
    db.remove_subscriber(3)
    db.conn.execute("INSERT INTO subscribers (chat_id) VALUES (4)")  # legacy row without preferences
    assert db.get_subscriber_categories() == {1: set(CATEGORIES), 2: {"ctf"}, 4: set(CATEGORIES)}


def test_resubscribe_keeps_preferences(tmp_path):
    db = Database(tmp_path / "s.db")
    db.add_subscriber(1)
    db.set_categories(1, ["ctf"])
    db.remove_subscriber(1)
    db.add_subscriber(1)
    assert db.is_subscribed(1) and json.loads(
        db.conn.execute("SELECT categories FROM preferences WHERE chat_id = 1").fetchone()[0]) == ["ctf"]


def test_db_already_at_latest_version_still_gets_missing_columns(tmp_path):
    # The deployed DB was stamped v2 before the "category" repair existed
    path = tmp_path / "stamped.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE seen (url TEXT PRIMARY KEY, title TEXT, seen_at TEXT DEFAULT CURRENT_TIMESTAMP);
        PRAGMA user_version = 2;
    """)
    conn.close()
    db = Database(path)
    db.mark_seen("https://x", "X", "ctf", status="rejected")
    assert db.get_stats()["total_rejected"] == 1
