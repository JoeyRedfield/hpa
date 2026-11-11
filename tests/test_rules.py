"""M2 回归测试：规则解析与规则优先分诊。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hpa import mail, rules, store, triage
from hpa.config import Config

RULES_MD = """# 测试规则

```rules
ignore if from contains "noreply"
ignore if subject contains "退订"
needs_reply if subject startswith "Re:"
fyi if subject contains "周报" and from contains "boss"
```
"""


def make_email(uid: int, subject: str = "标题", from_addr: str = "a@example.com",
               body: str = "正文") -> mail.Email:
    return mail.Email(
        message_id=f"<m{uid}@x>",
        folder="INBOX",
        uid=uid,
        uidvalidity=1,
        received_at="2026-11-11T10:00:00+08:00",
        from_addr=from_addr,
        to_addr="me@example.com",
        subject=subject,
        body=body,
    )


class ParseRulesTest(unittest.TestCase):
    def test_parse_and_count(self) -> None:
        parsed = rules.parse_rules_text(RULES_MD, Path("rules.md"))
        self.assertEqual(len(parsed), 4)
        self.assertEqual(parsed[0].action, "ignore")
        self.assertEqual(parsed[3].conditions[0].field, "subject")

    def test_negation_and_multi_condition(self) -> None:
        parsed = rules.parse_rules_text(
            '```rules\nignore if subject not contains "会议" and from endswith "@spam.com"\n```'
        )
        self.assertTrue(parsed[0].conditions[0].negate)
        self.assertEqual(len(parsed[0].conditions), 2)

    def test_syntax_error_reports_line(self) -> None:
        with self.assertRaises(rules.RuleSyntaxError) as ctx:
            rules.parse_rules_text("```rules\nreply if subject contains \"x\"\n```", Path("r.md"))
        self.assertIn("r.md:1", str(ctx.exception))

    def test_garbage_after_condition_rejected(self) -> None:
        with self.assertRaises(rules.RuleSyntaxError):
            rules.parse_rules_text('```rules\nignore if subject contains "x" garbage\n```')

    def test_classify_first_match_wins(self) -> None:
        parsed = rules.parse_rules_text(RULES_MD)
        m = rules.classify(parsed, make_email(1, subject="Re: 退订"))
        self.assertEqual(m.action, "ignore")  # 退订规则在前
        self.assertIsNone(rules.classify(parsed, make_email(2, subject="普通邮件")))

    def test_field_scopes(self) -> None:
        parsed = rules.parse_rules_text(RULES_MD)
        m = rules.classify(parsed, make_email(1, subject="周报", from_addr="boss@corp.com"))
        self.assertEqual(m.action, "fyi")
        # body 不参与 from/subject 条件
        m2 = rules.classify(parsed, make_email(2, body="周报", from_addr="x@y.com"))
        self.assertIsNone(m2)


class TriageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "rules").mkdir()
        (self.root / "rules" / "mail-triage.md").write_text(RULES_MD, encoding="utf-8")
        self.cfg = Config(vault=self.root / "vault", project_root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _seed(self) -> None:
        box = type(
            "Box",
            (),
            {
                "emails": [
                    make_email(1, subject="Re: 合同确认"),
                    make_email(2, subject="系统通知", from_addr="noreply@x.com"),
                    make_email(3, subject="本周周报", from_addr="boss@corp.com"),
                    make_email(4, subject="随便聊聊"),
                ]
            },
        )()
        box.fetch_since = lambda folder, last_uid: (
            [e for e in box.emails if e.uid > last_uid],
            1,
        )
        mail.sync(self.cfg, box)

    def test_triage_creates_todo_and_is_idempotent(self) -> None:
        self._seed()
        first = triage.triage_pending(self.cfg)
        self.assertEqual((first.scanned, first.matched, first.unmatched), (4, 3, 1))
        self.assertEqual(first.by_action, {"needs_reply": 1, "ignore": 1, "fyi": 1})
        self.assertEqual(first.todos_created, 1)

        second = triage.triage_pending(self.cfg)
        self.assertEqual(second.scanned, 1)  # 只剩未命中的那封
        self.assertEqual(second.unmatched, 1)
        self.assertEqual(second.todos_created, 0)

        conn = store.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM todos").fetchone()[0], 1)
            triaged = conn.execute("SELECT triage_by FROM messages WHERE uid = 1").fetchone()[0]
            self.assertTrue(triaged.startswith("rule:"))
        finally:
            conn.close()

    def test_dry_run_writes_nothing(self) -> None:
        self._seed()
        res = triage.triage_pending(self.cfg, dry_run=True)
        self.assertEqual(res.matched, 3)
        self.assertEqual(res.todos_created, 0)
        conn = store.connect(self.cfg)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM messages WHERE triage IS NULL").fetchone()[0], 4)
        finally:
            conn.close()

    def test_missing_rules_file_means_no_match(self) -> None:
        (self.root / "rules" / "mail-triage.md").unlink()
        self._seed()
        res = triage.triage_pending(self.cfg)
        self.assertEqual((res.matched, res.unmatched), (0, 4))


if __name__ == "__main__":
    unittest.main()
