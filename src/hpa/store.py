"""应用状态库（`state/hpa.sqlite3`）。

与 `.cache/kb.sqlite3` 分开：索引可删可重建，而这里存**不可重建**的状态
（邮件去重记录、待办、草稿、纠正日志）。索引丢了最多重扫一遍，状态丢了就会重复处理邮件。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .config import Config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    message_id  TEXT PRIMARY KEY,
    folder      TEXT NOT NULL,
    uid         INTEGER NOT NULL,
    uidvalidity INTEGER NOT NULL,
    received_at TEXT,
    from_addr   TEXT,
    to_addr     TEXT,
    subject     TEXT,
    body        TEXT,
    triage      TEXT,
    triage_by   TEXT,
    kb_queries  TEXT NOT NULL DEFAULT '[]',
    extracted_at TEXT,
    drafted_at  TEXT,
    seen_at     TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_uid
    ON messages(folder, uidvalidity, uid);

CREATE TABLE IF NOT EXISTS todos (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    title      TEXT NOT NULL,
    deadline   TEXT,
    status     TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages(message_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT NOT NULL,
    version    INTEGER NOT NULL,
    from_addr  TEXT NOT NULL DEFAULT '',
    to_addr    TEXT NOT NULL DEFAULT '',
    subject    TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL,
    citations  TEXT NOT NULL DEFAULT '[]',
    status     TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    sent_at    TEXT,
    last_error TEXT,
    UNIQUE (message_id, version)
);

CREATE TABLE IF NOT EXISTS corrections (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    message_id TEXT,
    raw        TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending',
    applied_at TEXT,
    target     TEXT,
    summary    TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# 旧库迁移：CREATE TABLE IF NOT EXISTS 不会补列，启动时按需 ALTER。
MIGRATIONS: dict[str, dict[str, str]] = {
    "messages": {
        "extracted_at": "TEXT",
        "drafted_at": "TEXT",
        "kb_queries": "TEXT NOT NULL DEFAULT '[]'",
    },
    "drafts": {
        "from_addr": "TEXT NOT NULL DEFAULT ''",
        "to_addr": "TEXT NOT NULL DEFAULT ''",
        "subject": "TEXT NOT NULL DEFAULT ''",
        "citations": "TEXT NOT NULL DEFAULT '[]'",
        "last_error": "TEXT",
    },
    "corrections": {
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "applied_at": "TEXT",
        "target": "TEXT",
        "summary": "TEXT",
    },
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.state_dir / "hpa.sqlite3")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def insert_message(conn: sqlite3.Connection, msg) -> bool:
    """插入一条邮件；`message_id` 已存在则返回 False（幂等去重）。"""
    try:
        conn.execute(
            """INSERT INTO messages(message_id, folder, uid, uidvalidity, received_at,
                                    from_addr, to_addr, subject, body, seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                msg.message_id,
                msg.folder,
                msg.uid,
                msg.uidvalidity,
                msg.received_at,
                msg.from_addr,
                msg.to_addr,
                msg.subject,
                msg.body,
                utcnow(),
            ),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def count_messages(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
