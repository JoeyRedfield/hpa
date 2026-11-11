"""M3a 回归测试：规则之后的三步流水线（分类 → 抽取 → 起草）。

用 FakeLLM，不连网；验证幂等、dry-run、失败降级。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hpa import kb, mail, pipeline, store
from hpa.config import Config
from hpa.llm import LLMError

PROMPTS = {
    "mail-classify.md": "# 任务：邮件分诊\n只输出 JSON。",
    "mail-extract.md": "# 任务：抽取待办与检索线索\n只输出 JSON。",
    "draft-reply.md": "# 任务：起草邮件回复\n只输出 JSON。",
}


class FakeLLM:
    def __init__(self, fail_first: bool = False) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_first = fail_first

    def complete(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self.fail_first and len(self.calls) == 1:
            raise LLMError("模拟模型故障")
        if "分诊" in system:
            needs = "需要回复" in user
            return json.dumps(
                {"needs_reply": needs, "is_fyi": not needs, "confidence": 0.9, "why": "测试"}
            )
        if "抽取" in system:
            return json.dumps(
                {
                    "todos": [{"title": "确认复盘会议时间", "deadline": "2026-11-13"}],
                    "entities": ["Q4 复盘"],
                    "kb_queries": ["复盘"],
                }
            )
        if "起草" in system:
            return json.dumps({"subject": "Re: 需要回复的邮件", "body": "结论如下 [1]。"})
        raise AssertionError(f"未预期的提示词：{system[:40]}")


class PipelineTest(unittest.TestCase):
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
        (self.vault / "学习").mkdir()
        (self.vault / "学习" / "复盘.md").write_text(
            "# Q4 复盘\n\n复盘会议定在上周五，结论是缩短发布周期。\n", encoding="utf-8"
        )
        self.cfg = Config(vault=self.vault, project_root=self.root)
        kb.build_index(self.cfg)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self) -> None:
        emails = [
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
                subject="公告：下周停电",
                body="仅通知。",
            ),
        ]
        box = type("Box", (), {"fetch_since": lambda self, folder, last_uid, limit=None, emails=emails: ([e for e in emails if e.uid > last_uid], 1)})()
        mail.sync(self.cfg, box)

    def test_end_to_end_and_idempotent(self) -> None:
        self._seed()
        llm = FakeLLM()
        res = pipeline.process_pending(self.cfg, llm)
        self.assertEqual((res.classified, res.extracted, res.drafted), (2, 2, 1))
        self.assertEqual(res.todos_created, 2)
        self.assertEqual(res.errors, [])

        conn = store.connect(self.cfg)
        try:
            rows = conn.execute("SELECT triage, triage_by FROM messages ORDER BY uid").fetchall()
            self.assertEqual([(r["triage"], r["triage_by"]) for r in rows], [("needs_reply", "llm"), ("fyi", "llm")])
            draft = conn.execute("SELECT * FROM drafts").fetchone()
            self.assertIsNotNone(draft)
            self.assertEqual(draft["to_addr"], "boss@corp.com")
            self.assertEqual(draft["status"], "proposed")
            self.assertIn("学习/复盘.md", json.loads(draft["citations"])[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0], 2)
        finally:
            conn.close()

        # 第二次运行：没有待处理邮件，不产生重复
        again = pipeline.process_pending(self.cfg, llm)
        self.assertEqual((again.classified, again.extracted, again.drafted), (0, 0, 0))
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 1)
        finally:
            conn.close()

    def test_dry_run_writes_nothing(self) -> None:
        self._seed()
        res = pipeline.process_pending(self.cfg, FakeLLM(), dry_run=True)
        self.assertEqual((res.classified, res.drafted), (2, 1))
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM messages WHERE triage IS NULL").fetchone()[0], 2
            )
        finally:
            conn.close()

    def test_llm_failure_is_recorded_not_guessed(self) -> None:
        self._seed()
        res = pipeline.process_pending(self.cfg, FakeLLM(fail_first=True))
        self.assertEqual(res.classified, 1)  # 第二封成功
        self.assertEqual(len(res.errors), 1)
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM messages WHERE triage IS NULL").fetchone()[0], 1
            )
        finally:
            conn.close()

    def test_garbage_json_raises(self) -> None:
        from hpa.llm import LLMError, parse_json

        with self.assertRaises(LLMError):
            parse_json("我觉得这封邮件要回")
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})


if __name__ == "__main__":
    unittest.main()
