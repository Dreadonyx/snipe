"""SQLite database — subscribers, seen items, preferences, scan logs, cooldowns."""

import json
import sqlite3
from pathlib import Path

from .config import CATEGORIES
from .preferences import UserSettings
from .urls import normalize_url

DB_PATH = Path(__file__).parent.parent / "snipe.db"


class Database:
    def __init__(self, db_path: Path | str | None = None):
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

        # Columns added after the first release. Checked on every start (not gated on
        # user_version) because CREATE TABLE IF NOT EXISTS never alters an existing table.
        self._ensure_columns("seen", {"category": "TEXT DEFAULT 'other'", "status": "TEXT DEFAULT 'sent'"})

        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version < 1:
            # v1: remember LLM rejections so they aren't re-classified every scan,
            # and opt existing subscribers into the new "event" category.
            # URLs are now normalized before dedup; rewrite old rows so they still match
            for row in self.conn.execute("SELECT url FROM seen").fetchall():
                canonical = normalize_url(row["url"])
                if canonical != row["url"]:
                    self.conn.execute("UPDATE OR IGNORE seen SET url = ? WHERE url = ?", (canonical, row["url"]))
                    self.conn.execute("DELETE FROM seen WHERE url = ?", (row["url"],))  # lost a collision
            for row in self.conn.execute("SELECT chat_id, categories FROM preferences").fetchall():
                cats = json.loads(row["categories"])
                if "event" not in cats:
                    self.set_categories(row["chat_id"], cats + ["event"], commit=False)
            self.conn.execute("PRAGMA user_version = 1")
        if version < 2:
            # v2: per-user settings and a queue for digests / quiet hours
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS user_settings (
                    chat_id INTEGER PRIMARY KEY,
                    data TEXT NOT NULL DEFAULT '{}'
                );

                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    compact TEXT NOT NULL,
                    queued_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS queue_chat ON queue (chat_id);
            """)
            self.conn.execute("PRAGMA user_version = 2")
        self.conn.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]):
        existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ── Seen items ───────────────────────────────────────────

    def is_seen(self, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,)).fetchone() is not None

    def mark_seen(self, url: str, title: str, category: str = "other", status: str = "sent"):
        self.conn.execute(
            "INSERT OR IGNORE INTO seen (url, title, category, status) VALUES (?, ?, ?, ?)",
            (url, title, category, status),
        )
        self.conn.commit()

    def seen_count(self, status: str = "sent") -> int:
        return self.conn.execute("SELECT COUNT(*) FROM seen WHERE status = ?", (status,)).fetchone()[0]

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

    def get_subscriber_categories(self) -> dict[int, set[str]]:
        """Every active subscriber mapped to the categories they want, in one query."""
        rows = self.conn.execute("""
            SELECT s.chat_id, p.categories FROM subscribers s
            LEFT JOIN preferences p ON p.chat_id = s.chat_id
            WHERE s.active = 1
        """).fetchall()
        return {
            r["chat_id"]: set(json.loads(r["categories"])) if r["categories"] else set(CATEGORIES)
            for r in rows
        }

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

    def set_categories(self, chat_id: int, categories: list[str], commit: bool = True):
        self.conn.execute(
            "INSERT OR REPLACE INTO preferences (chat_id, categories) VALUES (?, ?)",
            (chat_id, json.dumps(categories)),
        )
        if commit:
            self.conn.commit()

    def toggle_category(self, chat_id: int, category: str) -> list[str]:
        cats = self.get_categories(chat_id)
        if category in cats:
            cats.remove(category)
        else:
            cats.append(category)
        self.set_categories(chat_id, cats)
        return cats

    # ── User settings ────────────────────────────────────────

    def get_settings(self, chat_id: int) -> UserSettings:
        row = self.conn.execute("SELECT data FROM user_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        return UserSettings.from_dict(json.loads(row["data"])) if row else UserSettings()

    def save_settings(self, chat_id: int, settings: UserSettings):
        self.conn.execute(
            "INSERT OR REPLACE INTO user_settings (chat_id, data) VALUES (?, ?)",
            (chat_id, json.dumps(settings.to_dict())),
        )
        self.conn.commit()

    def get_all_settings(self, chat_ids) -> dict[int, UserSettings]:
        rows = {r["chat_id"]: r["data"] for r in self.conn.execute("SELECT chat_id, data FROM user_settings")}
        return {c: UserSettings.from_dict(json.loads(rows[c])) if c in rows else UserSettings() for c in chat_ids}

    # ── Delivery queue (digests, quiet hours) ────────────────

    def enqueue(self, chat_id: int, message: str, compact: str):
        self.conn.execute("INSERT INTO queue (chat_id, message, compact) VALUES (?, ?, ?)", (chat_id, message, compact))
        self.conn.commit()

    def queued_chat_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT chat_id FROM queue")]

    def queue_size(self, chat_id: int) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM queue WHERE chat_id = ?", (chat_id,)).fetchone()[0]

    def peek_queue(self, chat_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT id, message, compact FROM queue WHERE chat_id = ? ORDER BY id", (chat_id,)
        ).fetchall()

    def drop_queued(self, ids: list[int]):
        self.conn.executemany("DELETE FROM queue WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

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
            "total_seen": self.seen_count("sent"),
            "total_rejected": self.seen_count("rejected"),
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
        # A digest that never went out (user left, bot was down) shouldn't resurface weeks later
        self.conn.execute("DELETE FROM queue WHERE queued_at < datetime('now', '-7 days')")
        self.conn.commit()

    def close(self):
        self.conn.close()
