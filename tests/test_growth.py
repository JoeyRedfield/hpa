"""M4 成长循环测试：纠正 → 规则/记忆 + 回归样例。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from hpa import fitness, growth, rules, store
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




class FitnessGrowthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.root = root
        (root / "rules").mkdir()
        (root / "rules" / "mail-triage.md").write_text(BASE_RULES, encoding="utf-8")
        (root / growth.FITNESS_RULES).parent.mkdir(parents=True, exist_ok=True)
        (root / growth.FITNESS_RULES).write_text(
            "# 健身建议规则\n\n<!-- 由成长循环追加。 -->\n", encoding="utf-8"
        )
        (root / "prompts").mkdir()
        source = Path(__file__).resolve().parents[1] / "prompts" / "fitness-growth.md"
        (root / "prompts" / "fitness-growth.md").write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        (root / "memory").mkdir()
        (root / growth.TRAINING_PROFILE).parent.mkdir(parents=True, exist_ok=True)
        (root / growth.TRAINING_PROFILE).write_text(
            "目标：力量\n每周可练：3\n器械：健身房\n伤病：无\n\n自由备注。\n",
            encoding="utf-8",
        )
        vault = root / "vault"
        vault.mkdir()
        self.cfg = Config(vault=vault, project_root=root)
        conn = store.connect(self.cfg)
        try:
            conn.execute(
                "INSERT INTO corrections(created_at, message_id, raw, scope) VALUES (?, NULL, ?, 'fitness')",
                ("2026-11-13T00:00:00Z", "别每次都让我加重量，我一周只能练两次"),
            )
            conn.commit()
        finally:
            conn.close()

    def _correction(self) -> dict:
        return {"id": 1, "raw": "别每次都让我加重量，我一周只能练两次", "message_id": None, "scope": "fitness"}

    def test_rule_proposal_appends_with_source(self) -> None:
        proposal = growth.Proposal(
            target=growth.FITNESS_RULES,
            kind="rule",
            text="用户每周通常只能练两次，不要默认建议加重量。",
        )
        applied = growth.apply_proposal(self.cfg, proposal, correction_id=1)
        self.assertEqual(applied["target"], growth.FITNESS_RULES)
        text = (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8")
        self.assertIn("- 用户每周通常只能练两次", text)
        self.assertIn("<!-- 来源：纠正 #1 -->", text)

    def test_profile_proposal_rewrites_one_line_without_duplicate(self) -> None:
        proposal = growth.Proposal(
            target=growth.TRAINING_PROFILE,
            kind="profile",
            text="weekly_days=2",
            key="weekly_days",
            value="2",
        )
        growth.apply_proposal(self.cfg, proposal, correction_id=1)
        text = (self.root / growth.TRAINING_PROFILE).read_text(encoding="utf-8")
        self.assertEqual(text.count("每周可练："), 1)
        self.assertIn("每周可练：2", text)
        self.assertIn("自由备注。", text)

    def test_profile_with_rule_updates_both_files(self) -> None:
        proposal = growth.Proposal(
            target=growth.TRAINING_PROFILE,
            kind="profile",
            text="weekly_days=2",
            key="weekly_days",
            value="2",
            rule_text="一周只能练两次时，不要默认建议加重量。",
        )
        growth.apply_proposal(self.cfg, proposal, correction_id=1)
        self.assertIn("每周可练：2", (self.root / growth.TRAINING_PROFILE).read_text(encoding="utf-8"))
        rules_text = (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8")
        self.assertIn("不要默认建议加重量", rules_text)
        self.assertIn("<!-- 来源：纠正 #1 -->", rules_text)

    def test_invalid_fitness_kind_rejected_and_file_unchanged(self) -> None:
        before = (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8")
        llm = FakeLLM(
            json.dumps(
                {"target": growth.FITNESS_RULES, "kind": "profile", "key": "weekly_days", "value": "2"},
                ensure_ascii=False,
            )
        )
        with self.assertRaises(growth.GrowthError):
            growth.propose(self.cfg, llm, self._correction())
        self.assertEqual(before, (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8"))

    def test_unknown_fitness_target_rejected(self) -> None:
        before = (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8")
        llm = FakeLLM(
            json.dumps(
                {"target": "src/hpa/kb.py", "kind": "rule", "text": "x"},
                ensure_ascii=False,
            )
        )
        with self.assertRaises(growth.GrowthError):
            growth.propose(self.cfg, llm, self._correction())
        self.assertEqual(before, (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8"))

    def test_fitness_context_includes_profile_summary_not_note(self) -> None:
        fitness.record_workout(
            self.cfg,
            {
                "performed_on": fitness.local_today(),
                "started_at": "18:00",
                "note": "肩痛，别外传",
                "exercises": [{"exercise": "squat", "sets": [{"weight_kg": 80, "reps": 5}] * 5}],
            },
        )
        llm = FakeLLM(
            json.dumps(
                {"target": growth.FITNESS_RULES, "kind": "rule", "text": "保持重量"},
                ensure_ascii=False,
            )
        )
        growth.propose(self.cfg, llm, self._correction())
        user = llm.calls[0]
        self.assertIn("训练画像", user)
        self.assertIn("最近 4 周训练摘要", user)
        self.assertIn("上次建议", user)
        self.assertIn('"days_per_week": 3', user)
        self.assertNotIn("肩痛", user)

    def test_run_fitness_rule_proposal_from_llm(self) -> None:
        reply = json.dumps(
            {
                "target": growth.FITNESS_RULES,
                "kind": "rule",
                "text": "用户每周只能练两次，不要默认建议加重量。",
                "why": "纠正原文",
            },
            ensure_ascii=False,
        )
        result = growth.run(self.cfg, FakeLLM(reply))
        self.assertEqual(result.errors, [])
        self.assertEqual(result.applied, 1)
        text = (self.root / growth.FITNESS_RULES).read_text(encoding="utf-8")
        self.assertIn("- 用户每周只能练两次", text)
        self.assertIn("<!-- 来源：纠正 #1 -->", text)
        self.assertEqual(growth.list_pending(self.cfg), [])

    def test_profile_target_with_rule_kind_rejected(self) -> None:
        llm = FakeLLM(
            json.dumps(
                {"target": growth.TRAINING_PROFILE, "kind": "rule", "text": "x"},
                ensure_ascii=False,
            )
        )
        with self.assertRaises(growth.GrowthError):
            growth.propose(self.cfg, llm, self._correction())

    def test_profile_apply_rejects_unknown_key_as_growth_error(self) -> None:
        proposal = growth.Proposal(
            target=growth.TRAINING_PROFILE,
            kind="profile",
            text="bogus=2",
            key="bogus",
            value="2",
        )
        with self.assertRaises(growth.GrowthError):
            growth.apply_proposal(self.cfg, proposal, correction_id=1)
        text = (self.root / growth.TRAINING_PROFILE).read_text(encoding="utf-8")
        self.assertIn("每周可练：3", text)


if __name__ == "__main__":
    unittest.main()
