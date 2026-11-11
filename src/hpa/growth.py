"""M4 成长循环：把「纠正」固化成可读可改的规则 / 记忆，并留回归样例。

流程（spec §5）：

    用户在手机端记录纠正 → `hpa growth run` 让模型写成一条规则或一条记忆 →
    追加到 `rules/*.md` 或 `memory/*.md`（带来源注释）→ 为可判定的规则生成
    `tests/cases/*.md` 回归样例 → 用户在 `git diff` 里审 → 提交即生效。

安全：只写本仓库的 `rules/`、`memory/`、`tests/cases/`，绝不碰 vault。
校验：规则必须先通过 `rules.parse_rules_text`；写坏规则就拒绝整条提案。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import rules, store
from .config import Config
from .llm import LLM, LLMError, load_prompt, parse_json

TRIAGE_RULES = "rules/mail-triage.md"
REPLY_STYLE = "rules/reply-style.md"
MEMORY = "memory/preferences.md"
CASES_DIR = "tests/cases"
VALID_TARGETS = (TRIAGE_RULES, REPLY_STYLE, MEMORY)


class GrowthError(RuntimeError):
    pass


@dataclass
class Proposal:
    target: str
    kind: str  # "rule" | "note"
    text: str
    why: str = ""
    sample_from: str = ""
    sample_to: str = ""
    sample_subject: str = ""
    sample_body: str = ""
    sample_expected: str = ""


@dataclass
class GrowthResult:
    pending: int = 0
    applied: int = 0
    skipped: int = 0
    errors: list[str] | None = None
    applied_items: list[str] | None = None

    def __post_init__(self) -> None:
        self.errors = self.errors or []
        self.applied_items = self.applied_items or []


def list_pending(cfg: Config, limit: int = 20) -> list[dict]:
    conn = store.connect(cfg)
    try:
        rows = conn.execute(
            "SELECT * FROM corrections WHERE status = 'pending' ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _context(cfg: Config, correction: dict) -> str:
    """把纠正原文 + 原邮件 + 最新草稿拼成给模型的上下文。"""
    parts = [f"## 纠正原文\n{correction['raw']}"]
    message_id = correction.get("message_id")
    if not message_id:
        return "\n\n".join(parts)
    conn = store.connect(cfg)
    try:
        msg = conn.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()
        if msg:
            parts.append(
                "## 原邮件\n"
                f"发件人：{msg['from_addr']}\n主题：{msg['subject']}\n\n{(msg['body'] or '')[:2000]}"
            )
        draft = conn.execute(
            "SELECT * FROM drafts WHERE message_id = ? ORDER BY version DESC LIMIT 1",
            (message_id,),
        ).fetchone()
        if draft:
            parts.append(f"## 当时的草稿\n主题：{draft['subject']}\n\n{draft['body'][:2000]}")
    finally:
        conn.close()
    return "\n\n".join(parts)


def propose(cfg: Config, llm: LLM, correction: dict) -> Proposal:
    system = load_prompt(cfg, "growth.md")
    data = parse_json(llm.complete(system, _context(cfg, correction)))
    target = str(data.get("target", TRIAGE_RULES)).strip()
    kind = str(data.get("kind", "rule")).strip()
    text = str(data.get("text", "")).strip()
    if target not in VALID_TARGETS:
        raise GrowthError(f"模型给了不认识的目标：{target}")
    if kind not in ("rule", "note"):
        raise GrowthError(f"模型给了不认识的类型：{kind}")
    if not text:
        raise GrowthError("模型没有给出规则文本")
    sample = data.get("sample") or {}
    expected = str(data.get("expected_action") or sample.get("expected_action") or "").strip()
    return Proposal(
        target=target,
        kind=kind,
        text=text,
        why=str(data.get("why", "")).strip(),
        sample_from=str(sample.get("from", "")).strip(),
        sample_to=str(sample.get("to", "")).strip(),
        sample_subject=str(sample.get("subject", "")).strip(),
        sample_body=str(sample.get("body", "")).strip(),
        sample_expected=expected,
    )


def _append(cfg: Config, path: str, text: str, correction_id: int) -> None:
    file = cfg.project_root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.exists():
        body = file.read_text(encoding="utf-8")
    elif path == TRIAGE_RULES:
        body = "```rules\n```\n"
    else:
        body = "# 成长循环补充的记忆\n"
    if path == TRIAGE_RULES:
        # 追加进 ```rules 代码块：在最后一个 ``` 之前插入；来源写成 # 注释行（解析器会跳过）
        idx = body.rfind("```")
        if idx < 0:
            raise GrowthError(f"{path} 缺少 ```rules 代码块，拒绝写入")
        block = f"# 来源：纠正 #{correction_id}\n{text.rstrip()}\n"
        body = body[:idx] + block + body[idx:]
        if not body.endswith("\n"):
            body += "\n"
    else:
        body = body.rstrip() + f"\n\n{text.rstrip()}\n<!-- 来源：纠正 #{correction_id} -->\n"
    file.write_text(body, encoding="utf-8")


def _validate_triage(cfg: Config, path: str) -> None:
    rules.load_rules(cfg)


def _case_slug(correction_id: int, text: str) -> str:
    digest = hashlib.sha1(text.encode()).hexdigest()[:6]
    return f"{correction_id:04d}-{digest}"


def _write_case(cfg: Config, proposal: Proposal, correction_id: int) -> Path:
    slug = _case_slug(correction_id, proposal.text)
    path = cfg.project_root / CASES_DIR / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    content = (
        f"# 回归样例 {slug}\n\n"
        f"- 来源纠正：#{correction_id}\n"
        f"- 规则：{proposal.text}\n\n"
        "## 输入\n"
        f"- from: {proposal.sample_from}\n"
        f"- to: {proposal.sample_to}\n"
        f"- subject: {proposal.sample_subject}\n"
        f"- body: {proposal.sample_body}\n\n"
        "## 期望\n"
        f"- action: {proposal.sample_expected}\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


def apply_proposal(cfg: Config, proposal: Proposal, correction_id: int) -> dict:
    """校验后写入；返回 {target, case?}。写坏规则时不改动文件。"""
    if proposal.target == TRIAGE_RULES and proposal.sample_expected not in ("", "ignore", "needs_reply", "fyi"):
        raise GrowthError(f"期望动作不合法：{proposal.sample_expected}")
    file = cfg.project_root / proposal.target
    before = file.read_text(encoding="utf-8") if file.exists() else None
    _append(cfg, proposal.target, proposal.text, correction_id)
    try:
        if proposal.target == TRIAGE_RULES:
            _validate_triage(cfg, proposal.target)
    except rules.RuleSyntaxError as exc:
        if before is None:
            file.unlink(missing_ok=True)
        else:
            file.write_text(before, encoding="utf-8")
        raise GrowthError(f"生成的规则语法错误，已回滚：{exc}") from exc

    result = {"target": proposal.target}
    if proposal.target == TRIAGE_RULES and proposal.sample_expected:
        case = _write_case(cfg, proposal, correction_id)
        result["case"] = str(case.relative_to(cfg.project_root))
    return result


def mark_applied(cfg: Config, correction_id: int, target: str, summary: str) -> None:
    conn = store.connect(cfg)
    try:
        conn.execute(
            "UPDATE corrections SET status = 'applied', applied_at = ?, target = ?, summary = ? "
            "WHERE id = ?",
            (store.utcnow(), target, summary[:500], correction_id),
        )
        conn.commit()
    finally:
        conn.close()


def run(cfg: Config, llm: LLM, limit: int = 10, dry_run: bool = False) -> GrowthResult:
    result = GrowthResult()
    pending = list_pending(cfg, limit=limit)
    result.pending = len(pending)
    for correction in pending:
        try:
            proposal = propose(cfg, llm, correction)
        except (LLMError, GrowthError) as exc:
            result.errors.append(f"纠正 #{correction['id']}：{exc}")
            continue
        if dry_run:
            result.applied += 1
            result.applied_items.append(f"#{correction['id']}（预演）{proposal.target}: {proposal.text}")
            continue
        try:
            applied = apply_proposal(cfg, proposal, correction["id"])
        except GrowthError as exc:
            result.errors.append(f"纠正 #{correction['id']}：{exc}")
            continue
        mark_applied(cfg, correction["id"], proposal.target, proposal.text)
        result.applied += 1
        item = f"#{correction['id']} → {applied['target']}: {proposal.text}"
        if applied.get("case"):
            item += f"（样例 {applied['case']}）"
        result.applied_items.append(item)
    return result


_CASE_FIELD = re.compile(r"^-\s*(from|to|subject|body|action):\s*(.*)$")


def load_cases(cfg: Config) -> list[dict]:
    """读 `tests/cases/*.md`；格式坏了就跳过（不因样例拖垮测试）。"""
    cases = []
    directory = cfg.project_root / CASES_DIR
    if not directory.is_dir():
        return cases
    for path in sorted(directory.glob("*.md")):
        data: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _CASE_FIELD.match(line.strip())
            if m:
                data[m.group(1)] = m.group(2).strip()
        if data.get("action"):
            data["path"] = path.name
            cases.append(data)
    return cases


def check(cfg: Config) -> list[dict]:
    """跑回归样例：用当前规则分诊样例邮件，比对期望动作。"""
    loaded = rules.load_rules(cfg)
    results = []
    for case in load_cases(cfg):
        email = _Email(
            from_addr=case.get("from", ""),
            to_addr=case.get("to", ""),
            subject=case.get("subject", ""),
            body=case.get("body", ""),
        )
        match = rules.classify(loaded, email)
        actual = match.action if match else "unmatched"
        results.append(
            {"path": case["path"], "expected": case["action"], "actual": actual, "ok": actual == case["action"]}
        )
    return results


class _Email:
    """规则引擎只需要这几个字段。"""

    def __init__(self, from_addr: str, to_addr: str, subject: str, body: str) -> None:
        self.from_addr = from_addr
        self.to_addr = to_addr
        self.subject = subject
        self.body = body
