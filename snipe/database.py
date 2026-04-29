"""SQLite database — subscribers, seen items, preferences, scan logs, cooldowns."""

import json
import sqlite3
from pathlib import Path

from .config import CATEGORIES

DB_PATH = Path(__file__).parent.parent / "snipe.db"


class Database:
    def __init__(self, db_path: Path | None = None):
        self.conn = sqlite3.connect(str(db_path or DB_PATH))
        self.conn.row_factory = sqlite3.Row
        self._migrate()

    # ── Schema ───────────────────────────────────────────────

    def _migrate(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen (
                url TEXT PRIMARY KEY,
                title TEXT,
                category TEXT DEFAULT 'other',
                seen_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                active INTEGER DEFAULT 1,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS preferences (
                chat_id INTEGER PRIMARY KEY,
                categories TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS scan_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scanned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sources_checked INTEGER DEFAULT 0,
                found INTEGER DEFAULT 0,
                sent INTEGER DEFAULT 0,
                errors INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cooldowns (
                chat_id INTEGER PRIMARY KEY,
                last_scan_at REAL DEFAULT 0
            );
        """)
        self.conn.commit()

    # ── Seen items ───────────────────────────────────────────

    def is_seen(self, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,)).fetchone() is not None

    def mark_seen(self, url: str, title: str, category: str = "other"):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (url, title, category) VALUES (?, ?, ?)",
            (url, title, category),
        )
        self.conn.commit()

    def seen_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen").fetchone()[0]

    # ── Subscribers ──────────────────────────────────────────

    def add_subscriber(self, chat_id: int):
        self.conn.execute(
            "INSERT OR REPLACE INTO subscribers (chat_id, active) VALUES (?, 1)", (chat_id,)
        )
        # Set default preferences (all categories enabled)
        self.conn.execute(
            "INSERT OR IGNORE INTO preferences (chat_id, categories) VALUES (?, ?)",
            (chat_id, json.dumps(list(CATEGORIES))),
        )
        self.conn.commit()

    def remove_subscriber(self, chat_id: int):
        self.conn.execute("UPDATE subscribers SET active = 0 WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    def is_subscribed(self, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT active FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row is not None and row["active"] == 1

    def get_active_subscribers(self) -> list[int]:
        rows = self.conn.execute("SELECT chat_id FROM subscribers WHERE active = 1").fetchall()
        return [r["chat_id"] for r in rows]

    def subscriber_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM subscribers WHERE active = 1").fetchone()[0]

    # ── Category preferences ─────────────────────────────────

    def get_categories(self, chat_id: int) -> list[str]:
        row = self.conn.execute(
            "SELECT categories FROM preferences WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            return json.loads(row["categories"])
        return list(CATEGORIES)

    def set_categories(self, chat_id: int, categories: list[str]):
        self.conn.execute(
            "INSERT OR REPLACE INTO preferences (chat_id, categories) VALUES (?, ?)",
            (chat_id, json.dumps(categories)),
        )
        self.conn.commit()

    def toggle_category(self, chat_id: int, category: str) -> list[str]:
        cats = self.get_categories(chat_id)
        if category in cats:
            cats.remove(category)
        else:
            cats.append(category)
        self.set_categories(chat_id, cats)
        return cats

    # ── Scan log ─────────────────────────────────────────────

    def log_scan(self, sources_checked: int = 0, found: int = 0, sent: int = 0, errors: int = 0):
        self.conn.execute(
            "INSERT INTO scan_log (sources_checked, found, sent, errors) VALUES (?, ?, ?, ?)",
            (sources_checked, found, sent, errors),
        )
        self.conn.commit()

    def get_last_scan(self) -> dict | None:
        row = self.conn.execute("SELECT * FROM scan_log ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def get_stats(self) -> dict:
        return {
            "total_seen": self.seen_count(),
            "subscribers": self.subscriber_count(),
            "last_scan": self.get_last_scan(),
            "total_scans": self.conn.execute("SELECT COUNT(*) FROM scan_log").fetchone()[0],
        }

    # ── Cooldowns ────────────────────────────────────────────

    def get_cooldown(self, chat_id: int) -> float:
        row = self.conn.execute(
            "SELECT last_scan_at FROM cooldowns WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row["last_scan_at"] if row else 0.0

    def set_cooldown(self, chat_id: int, timestamp: float):
        self.conn.execute(
            "INSERT OR REPLACE INTO cooldowns (chat_id, last_scan_at) VALUES (?, ?)",
            (chat_id, timestamp),
        )
        self.conn.commit()

    # ── Maintenance ──────────────────────────────────────────

    def prune_old_seen(self, days: int = 90):
        self.conn.execute(
            "DELETE FROM seen WHERE seen_at < datetime('now', ?)", (f"-{days} days",)
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
