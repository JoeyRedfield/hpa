"""M5 组合测试：一个 tick 串起 同步 → 规则分诊 → 模型流水线；
确认发送后导出 Markdown；第二次 tick 不重复处理。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hpa import daemon, export, kb, mail, send, store
from hpa.config import Config

PROMPTS = {
    "mail-classify.md": "# 任务：邮件分诊\n只输出 JSON。",
    "mail-extract.md": "# 任务：抽取待办与检索线索\n只输出 JSON。",
    "draft-reply.md": "# 任务：起草邮件回复\n只输出 JSON。",
}


class FakeLLM:
    def complete(self, system: str, user: str) -> str:
        if "分诊" in system:
            needs = "需要回复" in user
            return json.dumps({"needs_reply": needs, "is_fyi": not needs, "confidence": 0.9})
        if "抽取" in system:
            return json.dumps(
                {
                    "todos": [{"title": "确认复盘时间", "deadline": "2026-11-13"}],
                    "kb_queries": ["复盘"],
                }
            )
        if "起草" in system:
            return json.dumps({"subject": "Re: 需要回复：Q4 复盘", "body": "复盘时间定周四 [1]。"})
        raise AssertionError(system[:40])


class FakeMailbox:
    def __init__(self, emails: list[mail.Email]) -> None:
        self.emails = emails
        self.calls: list[tuple[str, int]] = []

    def fetch_since(self, folder: str, last_uid: int, limit: int | None = None) -> tuple[list[mail.Email], int]:
        self.calls.append((folder, last_uid))
        fresh = [e for e in self.emails if e.uid > last_uid]
        return fresh, 1


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    def send(self, from_addr: str, to_addr: str, subject: str, body: str) -> None:
        self.sent.append((from_addr, to_addr, subject, body))


def _emails() -> list[mail.Email]:
    return [
        mail.Email(
            message_id="<need@x>",
            folder="INBOX",
            uid=1,
            uidvalidity=1,
            received_at="2026-11-11T10:00:00+08:00",
            from_addr="boss@corp.com",
            to_addr="me@qq.com",
            subject="需要回复：Q4 复盘",
            body="复盘会议时间需要确认，请回复。",
        ),
        mail.Email(
            message_id="<fyi@x>",
            folder="INBOX",
            uid=2,
            uidvalidity=1,
            received_at="2026-11-11T11:00:00+08:00",
            from_addr="news@corp.com",
            to_addr="me@qq.com",
            subject="本周通知",
            body="全员大会改到周五。",
        ),
    ]


class FlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        (self.root / "prompts").mkdir()
        (self.root / "rules").mkdir()
        for name, text in PROMPTS.items():
            (self.root / "prompts" / name).write_text(text, encoding="utf-8")
        (self.root / "rules" / "reply-style.md").write_text("# 风格\n- 简洁\n", encoding="utf-8")
        (self.root / "rules" / "mail-triage.md").write_text("```rules\n```\n", encoding="utf-8")
        self.vault.mkdir()
        (self.vault / "项目").mkdir()
        (self.vault / "项目" / "复盘.md").write_text("# Q4 复盘\n\n复盘会议定周四上午。\n", encoding="utf-8")
        self.cfg = Config(vault=self.vault, project_root=self.root)
        kb.build_index(self.cfg)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_tick_then_confirm_then_export(self) -> None:
        mailbox = FakeMailbox(_emails())
        first = daemon.tick(self.cfg, mailbox=mailbox, llm=FakeLLM())
        self.assertEqual(first.synced, 2)
        self.assertEqual(first.classified, 2)
        self.assertEqual(first.drafted, 1)
        self.assertEqual(first.errors, [])

        conn = store.connect(self.cfg)
        try:
            draft = conn.execute("SELECT * FROM drafts ORDER BY id LIMIT 1").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(draft)
        self.assertEqual(draft["status"], "proposed")

        sender = FakeSender()
        result = send.confirm_and_send(self.cfg, draft["id"], sender, from_addr="me@qq.com")
        self.assertEqual(result["status"], "sent")
        self.assertEqual(len(sender.sent), 1)

        paths = export.export_sent(self.cfg)
        self.assertEqual(len(paths), 1)
        text = paths[0].read_text(encoding="utf-8")
        self.assertIn("## 回复（v1 · sent）", text)
        self.assertIn("复盘时间定周四", text)
        self.assertIn("## 待办", text)
        self.assertIn("## 引用", text)
        self.assertIn("项目/复盘.md", text)
        self.assertEqual(paths[0].parent, self.root / export.OUTBOX)

    def test_second_tick_is_idempotent(self) -> None:
        mailbox = FakeMailbox(_emails())
        daemon.tick(self.cfg, mailbox=mailbox, llm=FakeLLM())
        second = daemon.tick(self.cfg, mailbox=mailbox, llm=FakeLLM())
        self.assertEqual(second.synced, 0)
        self.assertEqual(second.classified, 0)
        self.assertEqual(second.drafted, 0)

    def test_export_unknown_message(self) -> None:
        with self.assertRaises(KeyError):
            export.export_message(self.cfg, "<nope@x>")

    def test_export_without_sent_is_empty(self) -> None:
        self.assertEqual(export.export_sent(self.cfg), [])

    def test_daemon_run_once(self) -> None:
        daemon.run(
            self.cfg,
            once=True,
            mailbox=FakeMailbox(_emails()),
            llm=FakeLLM(),
        )
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(store.get_meta(conn, "last_tick_at") is not None, True)
            self.assertEqual(store.count_messages(conn), 2)
        finally:
            conn.close()

    def test_notify_without_url_is_noop(self) -> None:
        self.assertFalse(daemon.notify("", "x"))

    def test_llm_failure_keeps_messages_for_next_tick(self) -> None:
        result = daemon.tick(self.cfg, mailbox=FakeMailbox(_emails()), llm=None)
        self.assertEqual(result.synced, 2)
        self.assertEqual(result.classified, 0)
        self.assertEqual(result.errors, [])


if __name__ == "__main__":
    unittest.main()
