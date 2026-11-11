"""M5 归档：把一封处理完的邮件导出成 Markdown，落到 `outbox/`。

笔记库是只读的（spec §3），所以助手不直接写 vault：导出到本项目 `outbox/`，
由用户自己搬进笔记库。导出文件带 frontmatter 和原文定位，搬过去就是一条可检索的资料。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import store
from .config import Config

OUTBOX = "outbox"


def _slug(text: str, fallback: str = "mail") -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\s]+', "-", (text or "").strip()).strip("-")
    return cleaned[:40] or fallback


def _yaml_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _load(cfg: Config, message_id: str) -> dict:
    conn = store.connect(cfg)
    try:
        msg = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
        if msg is None:
            raise KeyError(message_id)
        draft = conn.execute(
            "SELECT * FROM drafts WHERE message_id = ? ORDER BY version DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        todos = conn.execute(
            "SELECT title, deadline, status FROM todos WHERE message_id = ? ORDER BY id",
            (message_id,),
        ).fetchall()
        corrections = conn.execute(
            "SELECT id, raw, status, created_at FROM corrections WHERE message_id = ? ORDER BY id",
            (message_id,),
        ).fetchall()
        return {"message": msg, "draft": draft, "todos": todos, "corrections": corrections}
    finally:
        conn.close()


def render(data: dict, exported_at: str) -> str:
    msg = data["message"]
    received = (msg["received_at"] or "")[:10] or "未知日期"
    title = msg["subject"] or "（无主题）"
    lines = [
        "---",
        f"title: {_yaml_value(title)}",
        f"message_id: {_yaml_value(msg['message_id'])}",
        f"received_at: {_yaml_value(msg['received_at'] or '')}",
        f"from: {_yaml_value(msg['from_addr'] or '')}",
        f"to: {_yaml_value(msg['to_addr'] or '')}",
        f"triage: {_yaml_value(msg['triage'] or 'unmatched')}",
        f"triage_by: {_yaml_value(msg['triage_by'] or '')}",
        f"exported_at: {_yaml_value(exported_at)}",
        "tags: [hpa/邮件]",
        "---",
        "",
        f"# {title}",
        "",
        f"- 收件时间：{msg['received_at'] or '未知'}",
        f"- 分诊：{msg['triage'] or 'unmatched'}（{msg['triage_by'] or '未定型'}）",
        "",
        "## 原文",
        "",
        (msg["body"] or "").strip() or "（空）",
        "",
    ]
    todos = data["todos"]
    if todos:
        lines += ["## 待办", ""]
        for todo in todos:
            mark = "x" if todo["status"] != "open" else " "
            deadline = f"（截止：{todo['deadline']}）" if todo["deadline"] else ""
            lines.append(f"- [{mark}] {todo['title']}{deadline}")
        lines.append("")

    draft = data["draft"]
    if draft:
        status = draft["status"]
        lines += [
            f"## 回复（v{draft['version']} · {status}）",
            "",
            f"- 主题：{draft['subject']}",
            f"- 收件人：{draft['to_addr']}",
        ]
        if draft["sent_at"]:
            lines.append(f"- 发送时间：{draft['sent_at']}")
        lines += ["", draft["body"].strip(), ""]
        citations = json.loads(draft["citations"] or "[]")
        if citations:
            lines += ["### 引用", ""]
            lines += [f"- [{i + 1}] {c}" for i, c in enumerate(citations)]
            lines.append("")

    corrections = data["corrections"]
    if corrections:
        lines += ["## 纠正记录", ""]
        for item in corrections:
            lines.append(f"- #{item['id']}（{item['status']}）：{item['raw']}")
        lines.append("")

    lines.append(f"<!-- 由 hpa export 生成于 {exported_at}；来源 {msg['message_id']} -->")
    lines.append("")
    return "\n".join(lines)


def export_message(cfg: Config, message_id: str) -> Path:
    data = _load(cfg, message_id)
    msg = data["message"]
    exported_at = store.utcnow()
    received = (msg["received_at"] or "")[:10] or "0000-00-00"
    digest = hashlib.sha1(message_id.encode()).hexdigest()[:6]
    name = f"{received}-{_slug(msg['subject'])}-{digest}.md"
    outbox = cfg.project_root / OUTBOX
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / name
    path.write_text(render(data, exported_at), encoding="utf-8")
    return path


def export_sent(cfg: Config, limit: int = 20) -> list[Path]:
    """导出所有已发送（status='sent'）的邮件，最近优先。"""
    conn = store.connect(cfg)
    try:
        rows = conn.execute(
            "SELECT DISTINCT message_id FROM drafts WHERE status = 'sent' "
            "ORDER BY sent_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [export_message(cfg, row["message_id"]) for row in rows]
