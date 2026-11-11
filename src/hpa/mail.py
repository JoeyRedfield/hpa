"""IMAP 同步：增量拉取、Message-ID 去重、UIDVALIDITY + UID 游标。

幂等规则（spec §4）：
1. 邮件以 `Message-ID` 去重；
2. 游标用 `UIDVALIDITY + UID`，不用「最新 N 封」；
3. 只有整批写库成功后才推进游标——中途失败下次重拉，靠 1 兜住。

真实邮箱只在 `ImapMailbox` 里出现；同步逻辑 `sync()` 只依赖 `Mailbox` 协议，
测试用内存实现即可，不需要连网。
"""

from __future__ import annotations

import hashlib
import imaplib
import json
import os
import re
from dataclasses import dataclass, field
from email import message_from_bytes
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Protocol

from . import store
from .config import Config

SECRETS_PATH = Path.home() / ".config" / "hpa" / "secrets.env"


@dataclass(frozen=True)
class Email:
    message_id: str
    folder: str
    uid: int
    uidvalidity: int
    received_at: str | None
    from_addr: str
    to_addr: str
    subject: str
    body: str


class Mailbox(Protocol):
    def fetch_since(
        self, folder: str, last_uid: int, limit: int | None = None
    ) -> tuple[list[Email], int]:
        """返回 (UID > last_uid 的邮件, 当前 UIDVALIDITY)。

        `limit` 只用于首次同步：只取最近 N 封，避免为了一个助手把几千封历史
        邮件连附件一起下载下来；之后的增量永远是全量拉。
        """
        ...


class ImapMailbox:
    """QQ 邮箱 / 标准 IMAP（SSL）。只在调用时连接，失败即抛错。"""

    def __init__(self, host: str, port: int, user: str, password: str) -> None:
        self.host = host
        self.port = port
        self.user = user
        self.password = password

    def fetch_since(
        self, folder: str, last_uid: int, limit: int | None = None
    ) -> tuple[list[Email], int]:
        conn = imaplib.IMAP4_SSL(self.host, self.port)
        try:
            conn.login(self.user, self.password)
            typ, data = conn.select(f'"{folder}"', readonly=True)
            if typ != "OK":
                raise RuntimeError(f"无法打开邮箱文件夹 {folder}: {data!r}")
            uidvalidity = _uidvalidity(conn)
            typ, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
            uids = sorted(int(x) for x in (data[0].split() if data and data[0] else []))
            uids = [uid for uid in uids if uid > last_uid]
            if limit and len(uids) > limit:
                uids = uids[-limit:]  # 首次同步从最近 N 封开始，历史不追
            emails: list[Email] = []
            for uid in uids:
                typ, fetched = conn.uid("FETCH", str(uid), "(RFC822)")
                if typ != "OK" or not fetched or fetched[0] is None:
                    raise RuntimeError(f"拉取 UID {uid} 失败: {typ}")
                raw = fetched[0][1]
                emails.append(parse_email(raw, folder=folder, uid=uid, uidvalidity=uidvalidity))
            return emails, uidvalidity
        finally:
            try:
                conn.logout()
            except Exception:
                pass


def _uidvalidity(conn: imaplib.IMAP4_SSL) -> int:
    typ, data = conn.response("UIDVALIDITY")
    if typ != "UIDVALIDITY" or not data or data[0] is None:
        raise RuntimeError("IMAP 服务器未返回 UIDVALIDITY，无法安全使用游标")
    return int(data[0])


def parse_email(raw: bytes, folder: str, uid: int, uidvalidity: int) -> Email:
    msg = message_from_bytes(raw)
    subject = _decode_header(msg.get("Subject", ""))
    from_addr = _decode_header(msg.get("From", ""))
    to_addr = _decode_header(msg.get("To", ""))
    message_id = (msg.get("Message-ID") or "").strip()
    if not message_id:
        # 缺失 Message-ID 时用「文件夹 + UIDVALIDITY + UID」合成稳定 ID，保持幂等
        digest = hashlib.sha1(f"{folder}|{uidvalidity}|{uid}".encode()).hexdigest()[:16]
        message_id = f"<synthetic-{digest}@hpa>"
    received_at = _received_at(msg)
    return Email(
        message_id=message_id,
        folder=folder,
        uid=uid,
        uidvalidity=uidvalidity,
        received_at=received_at,
        from_addr=from_addr,
        to_addr=to_addr,
        subject=subject,
        body=extract_text(msg),
    )


def _decode_header(value: str) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _received_at(msg) -> str | None:
    for header in ("Date", "Received"):
        value = msg.get(header)
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(value.split(";")[-1].strip())
            if dt is not None:
                return dt.isoformat(timespec="seconds")
        except (TypeError, ValueError):
            continue
    return None


def extract_text(msg) -> str:
    """优先 text/plain；没有则退到 text/html 去标签；都失败返回空串。"""
    plain: list[str] = []
    html: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            (plain if ctype == "text/plain" else html).append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")
            (html if msg.get_content_type() == "text/html" else plain).append(text)
    if plain:
        return "\n".join(plain).strip()
    return _strip_html("\n".join(html)).strip()


def _strip_html(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


@dataclass
class SyncResult:
    folder: str
    fetched: int = 0
    inserted: int = 0
    duplicates: int = 0
    last_uid: int = 0
    uidvalidity: int = 0
    reset_cursor: bool = False
    errors: list[str] = field(default_factory=list)


def _cursor_key(folder: str) -> str:
    return f"imap_cursor:{folder}"


def load_cursor(conn, folder: str) -> tuple[int, int]:
    raw = store.get_meta(conn, _cursor_key(folder))
    if not raw:
        return 0, 0
    try:
        data = json.loads(raw)
        return int(data["last_uid"]), int(data["uidvalidity"])
    except (ValueError, KeyError, TypeError):
        return 0, 0


def save_cursor(conn, folder: str, last_uid: int, uidvalidity: int) -> None:
    store.set_meta(conn, _cursor_key(folder), json.dumps({"last_uid": last_uid, "uidvalidity": uidvalidity}))


def sync(
    cfg: Config,
    mailbox: Mailbox,
    folder: str = "INBOX",
    bootstrap_limit: int | None = 50,
) -> SyncResult:
    conn = store.connect(cfg)
    try:
        last_uid, cursor_uidvalidity = load_cursor(conn, folder)
        limit = bootstrap_limit if last_uid == 0 else None
        emails, uidvalidity = mailbox.fetch_since(folder, last_uid, limit=limit)
        result = SyncResult(folder=folder, fetched=len(emails), uidvalidity=uidvalidity)

        if cursor_uidvalidity and uidvalidity != cursor_uidvalidity:
            # UIDVALIDITY 变了说明 UID 空间重置：旧 UID 全部失效，必须从 0 重扫，
            # 靠 Message-ID 去重保证不重复处理。
            result.reset_cursor = True
            last_uid = 0
            emails, uidvalidity = mailbox.fetch_since(folder, 0, limit=limit)
            result.fetched = len(emails)
            result.uidvalidity = uidvalidity

        for msg in emails:
            if store.insert_message(conn, msg):
                result.inserted += 1
            else:
                result.duplicates += 1

        if emails:
            last_uid = max(last_uid, max(m.uid for m in emails))
            result.last_uid = last_uid
            save_cursor(conn, folder, last_uid, uidvalidity)
        store.set_meta(conn, "last_sync_at", store.utcnow())
        conn.commit()
        return result
    finally:
        conn.close()


def from_row(row) -> Email:
    """从 `messages` 表行还原 Email（同步、分诊、流水线共用）。"""
    return Email(
        message_id=row["message_id"],
        folder=row["folder"],
        uid=row["uid"],
        uidvalidity=row["uidvalidity"],
        received_at=row["received_at"],
        from_addr=row["from_addr"] or "",
        to_addr=row["to_addr"] or "",
        subject=row["subject"] or "",
        body=row["body"] or "",
    )


def load_secrets(path: Path = SECRETS_PATH) -> dict[str, str]:
    """读 `~/.config/hpa/secrets.env`（KEY=VALUE，允许 # 注释）。"""
    secrets: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            secrets[key.strip()] = value.strip().strip("'\"")
    return secrets


def mailbox_from_env() -> ImapMailbox:
    """从环境变量或 secrets.env 组装 QQ 邮箱客户端；缺字段时给出可操作的报错。"""
    secrets = load_secrets()
    user = os.environ.get("HPA_MAIL_USER") or secrets.get("HPA_MAIL_USER", "")
    password = os.environ.get("HPA_MAIL_PASSWORD") or secrets.get("HPA_MAIL_PASSWORD", "")
    host = os.environ.get("HPA_IMAP_HOST") or secrets.get("HPA_IMAP_HOST", "imap.qq.com")
    port = int(os.environ.get("HPA_IMAP_PORT") or secrets.get("HPA_IMAP_PORT", "993"))
    missing = [name for name, value in (("HPA_MAIL_USER", user), ("HPA_MAIL_PASSWORD", password)) if not value]
    if missing:
        raise RuntimeError(
            "缺少邮箱凭据：" + "、".join(missing) + "。\n"
            f"请写入 {SECRETS_PATH}（KEY=VALUE 每行一条），"
            "或在 QQ 邮箱网页版 设置 → 账户 开启 IMAP 并生成授权码。"
        )
    if "@" not in user:
        raise RuntimeError(f"HPA_MAIL_USER 需要完整邮箱地址，当前为 {user!r}")
    return ImapMailbox(host, port, user, password)
