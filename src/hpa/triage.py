"""邮件分诊：规则优先，未命中留给 LLM（M3 接上）。

本模块只做确定性部分——跑 `rules/mail-triage.md`，把命中的邮件定型；
`needs_reply` 同时生成待办。未命中的邮件保持 `triage IS NULL`，等模型判断。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import rules, store
from .config import Config
from .mail import from_row


@dataclass
class TriageResult:
    scanned: int = 0
    matched: int = 0
    unmatched: int = 0
    todos_created: int = 0
    by_action: dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.by_action is None:
            self.by_action = {}


def triage_pending(cfg: Config, limit: int = 50, dry_run: bool = False) -> TriageResult:
    loaded = rules.load_rules(cfg)
    result = TriageResult()
    conn = store.connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM messages WHERE triage IS NULL ORDER BY uid LIMIT ?", (limit,)
        ).fetchall()
        result.scanned = len(rows)
        for row in rows:
            match = rules.classify(loaded, from_row(row))
            if match is None:
                result.unmatched += 1
                continue
            result.matched += 1
            result.by_action[match.action] = result.by_action.get(match.action, 0) + 1
            if dry_run:
                continue
            conn.execute(
                "UPDATE messages SET triage = ?, triage_by = ? WHERE message_id = ?",
                (match.action, f"rule:{match.rule.line_no}", row["message_id"]),
            )
            if match.action == "needs_reply" and not _has_todo(conn, row["message_id"]):
                conn.execute(
                    "INSERT INTO todos(message_id, title, status, created_at) VALUES (?, ?, 'open', ?)",
                    (row["message_id"], row["subject"] or "(无主题)", store.utcnow()),
                )
                result.todos_created += 1
        if not dry_run:
            conn.commit()
        return result
    finally:
        conn.close()


def _has_todo(conn: sqlite3.Connection, message_id: str) -> bool:
    return conn.execute("SELECT 1 FROM todos WHERE message_id = ?", (message_id,)).fetchone() is not None
