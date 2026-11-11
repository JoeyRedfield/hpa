"""M4 成长循环测试：纠正 → 规则/记忆 + 回归样例。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hpa import growth, rules, store
from hpa.config import Config
from hpa.llm import LLMError

BASE_RULES = """# 邮件分诊规则（测试）

```rules
ignore if from contains "noreply"
fyi if subject contains "周报"
```
"""

GROWTH_PROMPT = "你负责把纠正固化成规则，输出 JSON。"


class FakeLLM:
    def __init__(self, reply: str = "") -> None:
        self.reply = reply
        self.calls: list[str] = []

    def complete(self, system: str, user: str) -> str:
        self.calls.append(user)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _proposal_json(
    target: str = growth.TRIAGE_RULES,
    kind: str = "rule",
    text: str = 'needs_reply if subject contains "报价"',
    expected: str = "needs_reply",
) -> str:
    return json.dumps(
        {
            "target": target,
            "kind": kind,
            "text": text,
            "why": "用户说供应商报价必须回",
            "expected_action": expected,
            "sample": {
                "from": "supplier@example.com",
                "to": "me@qq.com",
                "subject": "报价单请确认",
                "body": "附件是最新报价。",
            },
        },
        ensure_ascii=False,
    )


class GrowthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        (root / "rules").mkdir()
        (root / "rules" / "mail-triage.md").write_text(BASE_RULES, encoding="utf-8")
        (root / "rules" / "reply-style.md").write_text("# 风格\n\n中文简洁。\n", encoding="utf-8")
        (root / "prompts").mkdir()
        (root / "prompts" / "growth.md").write_text(GROWTH_PROMPT, encoding="utf-8")
        vault = root / "vault"
        vault.mkdir()
        self.cfg = Config(vault=vault, project_root=root)
        conn = store.connect(self.cfg)
        try:
            conn.execute(
                "INSERT INTO corrections(created_at, message_id, raw) VALUES (?, ?, ?)",
                ("2026-11-11T00:00:00Z", None, "供应商报价的邮件要回，不要当通知"),
            )
            conn.commit()
        finally:
            conn.close()

    def test_apply_rule_proposal_creates_rule_and_case(self) -> None:
        proposal = growth.Proposal(
            target=growth.TRIAGE_RULES,
            kind="rule",
            text='needs_reply if subject contains "报价"',
            sample_from="supplier@example.com",
            sample_to="me@qq.com",
            sample_subject="报价单请确认",
            sample_body="附件是最新报价。",
            sample_expected="needs_reply",
        )
        applied = growth.apply_proposal(self.cfg, proposal, correction_id=1)
        self.assertEqual(applied["target"], growth.TRIAGE_RULES)
        self.assertIn("case", applied)

        rules_path = self.root / growth.TRIAGE_RULES
        text = rules_path.read_text(encoding="utf-8")
        self.assertIn('needs_reply if subject contains "报价"', text)
        self.assertIn("# 来源：纠正 #1", text)
        loaded = rules.load_rules(self.cfg)
        self.assertEqual(len(loaded), 3)

        cases = list((self.root / growth.CASES_DIR).glob("*.md"))
        self.assertEqual(len(cases), 1)
        checked = growth.check(self.cfg)
        self.assertEqual(len(checked), 1, checked)
        self.assertTrue(checked[0]["ok"], checked)

    def test_apply_invalid_rule_rolls_back(self) -> None:
        before = (self.root / growth.TRIAGE_RULES).read_text(encoding="utf-8")
        proposal = growth.Proposal(
            target=growth.TRIAGE_RULES,
            kind="rule",
            text='needs_reply if subject bogus "报价"',
            sample_expected="needs_reply",
        )
        with self.assertRaises(growth.GrowthError):
            growth.apply_proposal(self.cfg, proposal, correction_id=1)
        self.assertEqual(before, (self.root / growth.TRIAGE_RULES).read_text(encoding="utf-8"))
        self.assertFalse(list(self.root.glob(f"{growth.CASES_DIR}/*.md")))

    def test_note_goes_to_memory(self) -> None:
        proposal = growth.Proposal(
            target=growth.MEMORY,
            kind="note",
            text="称呼供应商用「您」，不要用「亲」。",
        )
        growth.apply_proposal(self.cfg, proposal, correction_id=1)
        text = (self.root / growth.MEMORY).read_text(encoding="utf-8")
        self.assertIn("称呼供应商", text)
        self.assertIn("<!-- 来源：纠正 #1 -->", text)

    def test_propose_parses_model_json(self) -> None:
        llm = FakeLLM(_proposal_json())
        correction = {"id": 1, "raw": "供应商报价要回", "message_id": None}
        proposal = growth.propose(self.cfg, llm, correction)
        self.assertEqual(proposal.target, growth.TRIAGE_RULES)
        self.assertEqual(proposal.sample_expected, "needs_reply")
        self.assertIn("供应商报价要回", llm.calls[0])

    def test_run_applies_pending_correction(self) -> None:
        llm = FakeLLM(_proposal_json())
        result = growth.run(self.cfg, llm)
        self.assertEqual(result.pending, 1)
        self.assertEqual(result.applied, 1)
        self.assertEqual(result.errors, [])

        pending = growth.list_pending(self.cfg)
        self.assertEqual(pending, [])
        conn = store.connect(self.cfg)
        try:
            row = conn.execute("SELECT * FROM corrections WHERE id = 1").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["status"], "applied")
        self.assertEqual(row["target"], growth.TRIAGE_RULES)
        self.assertIn("报价", row["summary"])

    def test_run_dry_run_does_not_write(self) -> None:
        before = (self.root / growth.TRIAGE_RULES).read_text(encoding="utf-8")
        result = growth.run(self.cfg, FakeLLM(_proposal_json()), dry_run=True)
        self.assertEqual(result.applied, 1)
        self.assertEqual(before, (self.root / growth.TRIAGE_RULES).read_text(encoding="utf-8"))
        self.assertEqual(len(growth.list_pending(self.cfg)), 1)
        self.assertFalse((self.root / growth.CASES_DIR).exists())

    def test_run_llm_failure_is_recorded(self) -> None:
        result = growth.run(self.cfg, FakeLLM(LLMError("HTTP 500")))
        self.assertEqual(result.applied, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertEqual(len(growth.list_pending(self.cfg)), 1)

    def test_bad_target_rejected(self) -> None:
        llm = FakeLLM(_proposal_json(target="src/hpa/kb.py"))
        with self.assertRaises(growth.GrowthError):
            growth.propose(self.cfg, llm, {"id": 1, "raw": "x", "message_id": None})


if __name__ == "__main__":
    unittest.main()
