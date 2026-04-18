import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "snipe.db"


class Database:
    def __init__(self):
        self.conn = sqlite3.connect(str(DB_PATH))
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS seen (
                url TEXT PRIMARY KEY,
                title TEXT,
                seen_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                active INTEGER DEFAULT 1,
                joined_at TEXT DEFAULT CURRENT_TIMESTAMP
            );
        """)
        self.conn.commit()

    def is_seen(self, url: str) -> bool:
        return self.conn.execute("SELECT 1 FROM seen WHERE url = ?", (url,)).fetchone() is not None

    def mark_seen(self, url: str, title: str):
        self.conn.execute("INSERT OR IGNORE INTO seen (url, title) VALUES (?, ?)", (url, title))
        self.conn.commit()

    def add_subscriber(self, chat_id: int):
        self.conn.execute("INSERT OR REPLACE INTO subscribers (chat_id, active) VALUES (?, 1)", (chat_id,))
        self.conn.commit()

    def remove_subscriber(self, chat_id: int):
        self.conn.execute("UPDATE subscribers SET active = 0 WHERE chat_id = ?", (chat_id,))
        self.conn.commit()

    def is_subscribed(self, chat_id: int) -> bool:
        row = self.conn.execute(
            "SELECT active FROM subscribers WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return row is not None and row["active"] == 1

    def get_active_subscribers(self) -> list:
        rows = self.conn.execute("SELECT chat_id FROM subscribers WHERE active = 1").fetchall()
        return [r["chat_id"] for r in rows]

    def subscriber_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM subscribers WHERE active = 1").fetchone()[0]

    def close(self):
        self.conn.close()
