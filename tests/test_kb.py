"""M1 回归测试：索引、检索、隐私过滤、增量更新。

用标准库 unittest（不引入 pytest 依赖）。运行方式：

    PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from hpa import kb
from hpa.config import Config, classify_tier


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class TierTest(unittest.TestCase):
    def test_classify_tier(self) -> None:
        self.assertEqual(classify_tier("学习/README.md"), "P0")
        self.assertEqual(classify_tier("private/日记.md"), "P1")
        self.assertEqual(classify_tier("private/邮箱密码.md"), "P2")
        self.assertEqual(classify_tier("private/token-备忘.md"), "P2")


class KbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        write(
            self.vault / "学习" / "lab1.md",
            "---\ntags: [课程, 验收]\n---\n# Lab1 验收标准\n\n"
            "验收标准：五项能力都要能演示，并且新会话可复用。\n",
        )
        write(
            self.vault / "private" / "工作日志.md",
            "# 工作日志\n\n今天的验收标准讨论记在 private 里，不应进 prompt。\n",
        )
        write(
            self.vault / "private" / "邮箱密码.md",
            "# 凭据\n\nQQ 邮箱密码：不该被索引进 prompt。\n",
        )
        self.cfg = Config(vault=self.vault, project_root=self.root)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_build_and_prompt_filter(self) -> None:
        res = kb.build_index(self.cfg)
        self.assertEqual(res["total"], 3)
        self.assertEqual(res["by_tier"], {"P0": 1, "P1": 1, "P2": 1})

        prompt_hits = kb.search(self.cfg, "验收标准")
        self.assertEqual([h.tier for h in prompt_hits], ["P0"])
        self.assertEqual(prompt_hits[0].path, "学习/lab1.md")
        self.assertIn(":4", prompt_hits[0].citation)  # frontmatter 3 行 + 标题行

        local_hits = kb.search(self.cfg, "验收标准", purpose="local")
        self.assertEqual({h.tier for h in local_hits}, {"P0", "P1"})
        # P2 即使 local 也保持可检索（本机填表/人工查看），但不含凭据正文进 prompt 路径
        all_hits = kb.search(self.cfg, "QQ 邮箱密码", purpose="local")
        self.assertTrue(any(h.tier == "P2" for h in all_hits))

    def test_short_query_like_fallback(self) -> None:
        kb.build_index(self.cfg)
        # 2 字查询：trigram MATCH 会静默 0 条，必须走 LIKE 兜底
        hits = kb.search(self.cfg, "验收")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].tier, "P0")

    def test_incremental_add_update_delete(self) -> None:
        kb.build_index(self.cfg)

        write(self.vault / "项目" / "新增.md", "# 新增笔记\n\n增量新增的内容。\n")
        res = kb.build_index(self.cfg)
        self.assertEqual((res["added"], res["updated"], res["deleted"]), (1, 0, 0))

        write(self.vault / "学习" / "lab1.md", "# Lab1 验收标准\n\n内容已更新。\n")
        res = kb.build_index(self.cfg)
        self.assertEqual((res["added"], res["updated"]), (0, 1))
        hits = kb.search(self.cfg, "内容已更新")
        self.assertTrue(hits)

        (self.vault / "项目" / "新增.md").unlink()
        res = kb.build_index(self.cfg)
        self.assertEqual(res["deleted"], 1)
        self.assertEqual(res["total"], 3)  # 剩回最初的 3 篇
        self.assertFalse(kb.search(self.cfg, "增量新增"))

    def test_rebuild_and_empty_query(self) -> None:
        kb.build_index(self.cfg)
        res = kb.build_index(self.cfg, rebuild=True)
        self.assertEqual(res["total"], 3)
        self.assertEqual(kb.search(self.cfg, "   "), [])

    def test_stats(self) -> None:
        kb.build_index(self.cfg)
        st = kb.stats(self.cfg)
        self.assertEqual(st["total"], 3)
        self.assertGreater(st["db_bytes"], 0)
        self.assertIsNotNone(st["last_index_at"])


class VaultIgnoreTest(unittest.TestCase):
    def test_skips_hidden_and_non_md(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            write(vault / ".obsidian" / "x.md", "# 不该索引\n")
            write(vault / "a.md", "# A\n")
            write(vault / "b.txt", "not markdown")
            cfg = Config(vault=vault, project_root=root)
            docs = list(kb.iter_docs(cfg))
            self.assertEqual([d.path for d in docs], ["a.md"])


if __name__ == "__main__":
    unittest.main()
