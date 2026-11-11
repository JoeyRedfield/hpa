"""发送闸门（M3b）：只有用户在手机端明确点「确认发送」才会真的发信。

状态机（spec §4 幂等第 3 条）：

    proposed --确认--> sending --SMTP 成功--> sent
                         └------SMTP 失败----> proposed（记录 last_error，可重试）

崩溃在 `sending`：**不自动重发**。界面标「状态未知，请到邮箱核对」，
由人核对后调用 `resolve_sending(sent=True/False)` 收尾，避免重复发送。
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from typing import Protocol

from . import store
from .config import Config
from .mail import SECRETS_PATH, load_secrets

DEFAULT_SMTP = {"smtp.qq.com": 465, "smtp.exmail.qq.com": 465}


class SendError(RuntimeError):
    pass


class Sender(Protocol):
    def send(self, from_addr: str, to_addr: str, subject: str, body: str) -> None:
        ...


@dataclass
class SmtpSender:
    user: str
    password: str
    host: str = "smtp.qq.com"
    port: int = 465
    timeout: float = 60.0

    def send(self, from_addr: str, to_addr: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["From"] = from_addr
        msg["To"] = to_addr
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid(domain=from_addr.split("@")[-1] or "hpa.local")
        msg.set_content(body)
        try:
            with smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.login(self.user, self.password)
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise SendError(f"SMTP 发送失败：{exc}") from exc


def sender_from_env() -> SmtpSender:
    secrets = load_secrets()
    user = os.environ.get("HPA_MAIL_USER") or secrets.get("HPA_MAIL_USER", "")
    password = os.environ.get("HPA_MAIL_PASSWORD") or secrets.get("HPA_MAIL_PASSWORD", "")
    missing = [n for n, v in (("HPA_MAIL_USER", user), ("HPA_MAIL_PASSWORD", password)) if not v]
    if missing:
        raise SendError("缺少发信凭据：" + "、".join(missing) + f"。请写入 {SECRETS_PATH}。")
    host = os.environ.get("HPA_SMTP_HOST") or secrets.get("HPA_SMTP_HOST", "smtp.qq.com")
    port = int(
        os.environ.get("HPA_SMTP_PORT") or secrets.get("HPA_SMTP_PORT", str(DEFAULT_SMTP.get(host, 465)))
    )
    return SmtpSender(user=user, password=password, host=host, port=port)


def confirm_and_send(cfg: Config, draft_id: int, sender: Sender, from_addr: str = "") -> dict:
    """用户确认后发送。返回 {status, id, error?}。

    并发/重复点击安全：状态用 `UPDATE ... WHERE status='proposed'` 原子抢占。
    `from_addr` 缺省用草稿记录的发件地址（即收到原邮件的邮箱）。
    """
    conn = store.connect(cfg)
    try:
        claimed = conn.execute(
            "UPDATE drafts SET status = 'sending', confirmed_at = ? "
            "WHERE id = ? AND status = 'proposed'",
            (store.utcnow(), draft_id),
        ).rowcount
        conn.commit()
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            return {"status": "missing", "id": draft_id}
        if not claimed:
            return {"status": row["status"], "id": draft_id, "already": True}

        sender_addr = from_addr or row["from_addr"] or getattr(sender, "user", "")
        if not sender_addr or "@" not in sender_addr:
            conn.execute(
                "UPDATE drafts SET status = 'proposed', last_error = ? WHERE id = ?",
                ("发件地址缺失", draft_id),
            )
            conn.commit()
            return {"status": "failed", "id": draft_id, "error": "发件地址缺失"}
        try:
            sender.send(sender_addr, row["to_addr"], row["subject"], row["body"])
        except Exception as exc:  # noqa: BLE001 - 外部 SMTP 不可信
            conn.execute(
                "UPDATE drafts SET status = 'proposed', last_error = ? WHERE id = ?",
                (str(exc)[:500], draft_id),
            )
            conn.commit()
            return {"status": "failed", "id": draft_id, "error": str(exc)[:500]}

        conn.execute(
            "UPDATE drafts SET status = 'sent', sent_at = ?, last_error = NULL WHERE id = ?",
            (store.utcnow(), draft_id),
        )
        conn.commit()
        return {"status": "sent", "id": draft_id}
    finally:
        conn.close()


def resolve_sending(cfg: Config, draft_id: int, sent: bool) -> dict:
    """人工核对后收尾崩溃在 `sending` 的草稿。"""
    conn = store.connect(cfg)
    try:
        row = conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            return {"status": "missing", "id": draft_id}
        if row["status"] != "sending":
            return {"status": row["status"], "id": draft_id, "already": True}
        if sent:
            conn.execute(
                "UPDATE drafts SET status = 'sent', sent_at = ? WHERE id = ?",
                (store.utcnow(), draft_id),
            )
            new_status = "sent"
        else:
            conn.execute(
                "UPDATE drafts SET status = 'proposed', last_error = ? WHERE id = ?",
                ("人工核对：未发出，可重新确认", draft_id),
            )
            new_status = "proposed"
        conn.commit()
        return {"status": new_status, "id": draft_id}
    finally:
        conn.close()


def edit_draft(cfg: Config, draft_id: int, subject: str, body: str) -> dict:
    """用户改草稿：旧版本置 `superseded`，新版本号 +1 进队列（仍须再确认）。"""
    subject = subject.strip()
    body = body.strip()
    if not body:
        raise ValueError("草稿正文不能为空")
    conn = store.connect(cfg)
    try:
        row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            return {"status": "missing", "id": draft_id}
        if row["status"] != "proposed":
            return {"status": row["status"], "id": draft_id, "already": True}
        next_version = conn.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM drafts WHERE message_id = ?",
            (row["message_id"],),
        ).fetchone()[0]
        conn.execute("UPDATE drafts SET status = 'superseded' WHERE id = ?", (draft_id,))
        cur = conn.execute(
            """INSERT INTO drafts(message_id, from_addr, version, to_addr, subject, body,
                                  citations, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'proposed', ?)""",
            (
                row["message_id"],
                row["from_addr"],
                next_version,
                row["to_addr"],
                subject or row["subject"],
                body,
                row["citations"],
                store.utcnow(),
            ),
        )
        conn.commit()
        return {"status": "edited", "id": cur.lastrowid, "version": next_version}
    finally:
        conn.close()


def reject_draft(cfg: Config, draft_id: int, reason: str = "") -> dict:
    conn = store.connect(cfg)
    try:
        row = conn.execute("SELECT status FROM drafts WHERE id = ?", (draft_id,)).fetchone()
        if row is None:
            return {"status": "missing", "id": draft_id}
        if row["status"] not in ("proposed", "sending"):
            return {"status": row["status"], "id": draft_id, "already": True}
        conn.execute(
            "UPDATE drafts SET status = 'rejected', last_error = ? WHERE id = ?",
            (reason[:500] or "用户驳回", draft_id),
        )
        conn.commit()
        return {"status": "rejected", "id": draft_id}
    finally:
        conn.close()
