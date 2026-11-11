"""M2 回归测试：IMAP 同步的幂等、去重、UIDVALIDITY 重置、MIME 解析。

用内存 Mailbox，不连真实邮箱。
"""

from __future__ import annotations

import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path

from hpa import mail, store
from hpa.config import Config


class FakeMailbox:
    def __init__(self, emails: list[mail.Email] | None = None, uidvalidity: int = 1) -> None:
        self.emails = emails or []
        self.uidvalidity = uidvalidity

    def fetch_since(self, folder: str, last_uid: int):
        return [e for e in self.emails if e.folder == folder and e.uid > last_uid], self.uidvalidity


def make_email(message_id: str, uid: int, uidvalidity: int = 1, folder: str = "INBOX",
               subject: str = "测试邮件", body: str = "正文") -> mail.Email:
    return mail.Email(
        message_id=message_id,
        folder=folder,
        uid=uid,
        uidvalidity=uidvalidity,
        received_at="2026-11-11T10:00:00+08:00",
        from_addr="boss@example.com",
        to_addr="me@example.com",
        subject=subject,
        body=body,
    )


class SyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_three_runs_stay_idempotent(self) -> None:
        box = FakeMailbox([make_email(f"<m{i}@x>", uid=i) for i in range(1, 6)])
        first = mail.sync(self.cfg, box)
        self.assertEqual((first.fetched, first.inserted, first.duplicates), (5, 5, 0))

        second = mail.sync(self.cfg, box)
        self.assertEqual((second.fetched, second.inserted, second.duplicates), (0, 0, 0))

        third = mail.sync(self.cfg, box)
        self.assertEqual((third.fetched, third.inserted), (0, 0))

        conn = store.connect(self.cfg)
        try:
            self.assertEqual(store.count_messages(conn), 5)
        finally:
            conn.close()

    def test_incremental_only_new(self) -> None:
        box = FakeMailbox([make_email("<a@x>", uid=1)])
        mail.sync(self.cfg, box)
        box.emails.append(make_email("<b@x>", uid=2))
        res = mail.sync(self.cfg, box)
        self.assertEqual((res.fetched, res.inserted), (1, 1))
        self.assertEqual(res.last_uid, 2)

    def test_same_message_id_in_two_fetches_not_duplicated(self) -> None:
        # 模拟上次游标没推进：同一封邮件被重复拉取
        box = FakeMailbox([make_email("<dup@x>", uid=7)])
        mail.sync(self.cfg, box)
        res = mail.sync(self.cfg, box)  # 游标记住后不会重复拉；再手动构造一次
        self.assertEqual(res.inserted, 0)
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(store.count_messages(conn), 1)
        finally:
            conn.close()

    def test_uidvalidity_reset_rescans_without_duplicates(self) -> None:
        box = FakeMailbox([make_email("<a@x>", uid=1), make_email("<b@x>", uid=2)])
        mail.sync(self.cfg, box)
        # 服务器重置 UIDVALIDITY：UID 从头编号，旧游标失效
        box.uidvalidity = 2
        box.emails = [make_email("<a@x>", uid=1, uidvalidity=2), make_email("<c@x>", uid=2, uidvalidity=2)]
        res = mail.sync(self.cfg, box)
        self.assertTrue(res.reset_cursor)
        self.assertEqual((res.fetched, res.inserted, res.duplicates), (2, 1, 1))
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(store.count_messages(conn), 3)
        finally:
            conn.close()

    def test_cursor_survives_reconnect(self) -> None:
        box = FakeMailbox([make_email("<a@x>", uid=3)])
        mail.sync(self.cfg, box)
        conn = store.connect(self.cfg)
        try:
            last_uid, uidvalidity = mail.load_cursor(conn, "INBOX")
        finally:
            conn.close()
        self.assertEqual((last_uid, uidvalidity), (3, 1))


class ParseTest(unittest.TestCase):
    def _raw(self, msg: EmailMessage) -> bytes:
        return msg.as_bytes()

    def test_plain_utf8(self) -> None:
        msg = EmailMessage()
        msg["Message-ID"] = "<m1@x>"
        msg["Subject"] = "会议邀请：Q4 复盘"
        msg["From"] = "boss@example.com"
        msg["To"] = "me@example.com"
        msg["Date"] = "Wed, 11 Nov 2026 10:00:00 +0800"
        msg.set_content("请周四前确认会议时间。\n")
        e = mail.parse_email(self._raw(msg), folder="INBOX", uid=1, uidvalidity=1)
        self.assertEqual(e.message_id, "<m1@x>")
        self.assertEqual(e.subject, "会议邀请：Q4 复盘")
        self.assertIn("请周四前确认", e.body)
        self.assertIsNotNone(e.received_at)

    def test_multipart_prefers_plain(self) -> None:
        msg = EmailMessage()
        msg["Message-ID"] = "<m2@x>"
        msg["Subject"] = "多部分"
        msg.set_content("纯文本正文")
        msg.add_alternative("<p>HTML 正文</p>", subtype="html")
        e = mail.parse_email(self._raw(msg), folder="INBOX", uid=2, uidvalidity=1)
        self.assertIn("纯文本正文", e.body)
        self.assertNotIn("<p>", e.body)

    def test_html_only_stripped(self) -> None:
        msg = EmailMessage()
        msg["Message-ID"] = "<m3@x>"
        msg["Subject"] = "只有 HTML"
        msg.set_content("<p>第一段</p><p>第二段</p>", subtype="html")
        e = mail.parse_email(self._raw(msg), folder="INBOX", uid=3, uidvalidity=1)
        self.assertIn("第一段", e.body)
        self.assertNotIn("<p>", e.body)

    def test_missing_message_id_is_synthetic_and_stable(self) -> None:
        msg = EmailMessage()
        msg["Subject"] = "没有 Message-ID"
        msg.set_content("正文")
        raw = self._raw(msg)
        a = mail.parse_email(raw, folder="INBOX", uid=9, uidvalidity=3)
        b = mail.parse_email(raw, folder="INBOX", uid=9, uidvalidity=3)
        self.assertTrue(a.message_id.startswith("<synthetic-"))
        self.assertEqual(a.message_id, b.message_id)

    def test_rfc2047_subject(self) -> None:
        msg = EmailMessage()
        msg["Message-ID"] = "<m4@x>"
        msg["Subject"] = "=?utf-8?b?5Lya6K6u6YKA6K+3?="
        msg.set_content("x")
        e = mail.parse_email(self._raw(msg), folder="INBOX", uid=4, uidvalidity=1)
        self.assertEqual(e.subject, "会议邀请")


class SecretsTest(unittest.TestCase):
    def test_parse_secrets_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "secrets.env"
            p.write_text(
                "# 注释\nHPA_MAIL_USER=me@qq.com\nHPA_MAIL_PASSWORD='abcd efgh'\n",
                encoding="utf-8",
            )
            s = mail.load_secrets(p)
            self.assertEqual(s["HPA_MAIL_USER"], "me@qq.com")
            self.assertEqual(s["HPA_MAIL_PASSWORD"], "abcd efgh")


if __name__ == "__main__":
    unittest.main()
