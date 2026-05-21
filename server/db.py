import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "poimenas.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    anki_cards INTEGER DEFAULT 0,
    seterra_active_seconds INTEGER DEFAULT 0,
    duolingo_active_seconds INTEGER DEFAULT 0,
    gaming_seconds INTEGER DEFAULT 0,
    last_heartbeat_ts REAL,
    agent_version TEXT
);

CREATE TABLE IF NOT EXISTS lock_overrides (
    id INTEGER PRIMARY KEY DEFAULT 1,
    locked INTEGER DEFAULT 0,
    reason TEXT DEFAULT '',
    until_ts REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pending_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    text TEXT NOT NULL,
    delivered INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    type TEXT NOT NULL,
    anki_target INTEGER DEFAULT 0,
    seterra_target_seconds INTEGER DEFAULT 0,
    duolingo_target_seconds INTEGER DEFAULT 0,
    gaming_cap_seconds INTEGER DEFAULT 7200,
    earn_rate REAL DEFAULT 2.0,
    priority INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS dns_allowlist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    domain TEXT NOT NULL UNIQUE
);
"""

DEFAULT_DOMAINS = ["seterra.com", "duolingo.com"]


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO lock_overrides (id, locked, reason, until_ts) VALUES (1, 0, '', 0)"
        )
        for domain in DEFAULT_DOMAINS:
            conn.execute("INSERT OR IGNORE INTO dns_allowlist (domain) VALUES (?)", (domain,))
        conn.commit()
