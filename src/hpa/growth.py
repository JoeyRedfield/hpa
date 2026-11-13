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

from . import fitness, rules, store
from .config import Config
from .llm import LLM, LLMError, load_prompt, parse_json

TRIAGE_RULES = "rules/mail-triage.md"
REPLY_STYLE = "rules/reply-style.md"
MEMORY = "memory/preferences.md"
FITNESS_RULES = "rules/fitness-advice.md"
TRAINING_PROFILE = "memory/training-profile.md"
CASES_DIR = "tests/cases"
MAIL_TARGETS = (TRIAGE_RULES, REPLY_STYLE, MEMORY)
FITNESS_TARGETS = (FITNESS_RULES, TRAINING_PROFILE)


class GrowthError(RuntimeError):
    pass


@dataclass
class Proposal:
    target: str
    kind: str  # "rule" | "note" | "profile"
    text: str
    why: str = ""
    sample_from: str = ""
    sample_to: str = ""
    sample_subject: str = ""
    sample_body: str = ""
    sample_expected: str = ""
    key: str = ""
    value: str = ""
    rule_text: str = ""


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


_PROFILE_KEYS = {
    "goal": "goal",
    "目标": "goal",
    "weekly_days": "days_per_week",
    "days_per_week": "days_per_week",
    "每周可练": "days_per_week",
    "equipment": "equipment",
    "器械": "equipment",
    "injuries": "injuries",
    "伤病": "injuries",
}


def _fitness_context(cfg: Config, correction: dict) -> str:
    """健身纠正只带画像、4 周摘要和上次建议；备注不进 prompt。"""
    data = fitness.growth_context(cfg)
    return "\n\n".join(
        [
            f"## 纠正原文\n{correction['raw']}",
            "## 训练画像\n" + json.dumps(data["profile"], ensure_ascii=False, indent=2),
            "## 最近 4 周训练摘要\n" + json.dumps(data["summary"], ensure_ascii=False, indent=2),
            "## 上次建议\n"
            + (json.dumps(data["last_advice"], ensure_ascii=False, indent=2) if data["last_advice"] else "（还没有建议）"),
        ]
    )


def _propose_fitness(cfg: Config, llm: LLM, correction: dict) -> Proposal:
    system = load_prompt(cfg, "fitness-growth.md")
    data = parse_json(llm.complete(system, _fitness_context(cfg, correction)))
    target = str(data.get("target", "")).strip()
    kind = str(data.get("kind", "")).strip()
    why = str(data.get("why", "")).strip()
    if target not in FITNESS_TARGETS:
        raise GrowthError(f"模型给了不认识的健身目标：{target}")
    if target == FITNESS_RULES:
        if kind != "rule":
            raise GrowthError(f"健身规则的 kind 必须是 rule，收到：{kind}")
        text = str(data.get("text", "")).strip()
        if not text:
            raise GrowthError("模型没有给出健身规则文本")
        return Proposal(target=target, kind="rule", text=text, why=why)
    if target == TRAINING_PROFILE:
        if kind != "profile":
            raise GrowthError(f"训练画像的 kind 必须是 profile，收到：{kind}")
        key = str(data.get("key", "")).strip()
        value = str(data.get("value", "")).strip()
        if key not in _PROFILE_KEYS:
            raise GrowthError(f"模型给了不认识的画像字段：{key}")
        if not value:
            raise GrowthError("模型没有给出画像值")
        return Proposal(
            target=target,
            kind="profile",
            text=f"{key}={value}",
            why=why,
            key=key,
            value=value,
            rule_text=str(data.get("rule", "")).strip(),
        )
    raise GrowthError(f"模型给了不认识的健身目标：{target}")


def propose(cfg: Config, llm: LLM, correction: dict) -> Proposal:
    if (correction.get("scope") or "mail") == "fitness":
        return _propose_fitness(cfg, llm, correction)
    system = load_prompt(cfg, "growth.md")
    data = parse_json(llm.complete(system, _context(cfg, correction)))
    target = str(data.get("target", TRIAGE_RULES)).strip()
    kind = str(data.get("kind", "rule")).strip()
    text = str(data.get("text", "")).strip()
    if target not in MAIL_TARGETS:
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


def _append(
    cfg: Config,
    path: str,
    text: str,
    correction_id: int,
    *,
    default_header: str | None = None,
    bullet: bool = False,
) -> None:
    file = cfg.project_root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    if file.exists():
        body = file.read_text(encoding="utf-8")
    elif path == TRIAGE_RULES:
        body = "```rules\n```\n"
    else:
        body = default_header or "# 成长循环补充的记忆\n"
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
        line = text.rstrip()
        if bullet and not line.startswith("- "):
            line = "- " + line
        body = body.rstrip() + f"\n\n{line}\n<!-- 来源：纠正 #{correction_id} -->\n"
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


def _append_fitness_rule(cfg: Config, text: str, correction_id: int) -> None:
    if not text.strip():
        raise GrowthError("模型没有给出健身规则文本")
    _append(
        cfg,
        FITNESS_RULES,
        text,
        correction_id,
        default_header="# 健身建议规则\n",
        bullet=True,
    )


def _apply_fitness(cfg: Config, proposal: Proposal, correction_id: int) -> dict:
    if proposal.target == FITNESS_RULES:
        if proposal.kind != "rule":
            raise GrowthError("健身规则只接受 rule")
        _append_fitness_rule(cfg, proposal.text, correction_id)
        return {"target": proposal.target}
    if proposal.target == TRAINING_PROFILE:
        if proposal.kind != "profile":
            raise GrowthError("训练画像只接受 profile")
        current = fitness.read_profile(cfg)
        if not current.get("complete"):
            raise GrowthError("训练画像还不完整，请先在健身 tab 补齐四项")
        try:
            field = _PROFILE_KEYS[proposal.key]
        except KeyError as exc:
            raise GrowthError(f"训练画像没有这个字段：{proposal.key}") from exc
        payload = {
            "goal": current["goal"],
            "days_per_week": current["days_per_week"],
            "equipment": current["equipment"],
            "injuries": current["injuries"],
        }
        if field == "days_per_week":
            try:
                payload[field] = int(proposal.value)
            except (TypeError, ValueError) as exc:
                raise GrowthError(f"每周可练必须是 1–7 的整数：{proposal.value}") from exc
        else:
            payload[field] = proposal.value
        try:
            fitness.record_profile(cfg, payload)
        except ValueError as exc:
            raise GrowthError(f"训练画像没改成：{exc}") from exc
        if proposal.rule_text:
            _append_fitness_rule(cfg, proposal.rule_text, correction_id)
            return {"target": proposal.target, "targets": [FITNESS_RULES, TRAINING_PROFILE]}
        return {"target": proposal.target}
    raise GrowthError(f"不是健身目标：{proposal.target}")


def apply_proposal(cfg: Config, proposal: Proposal, correction_id: int) -> dict:
    """校验后写入；返回 {target, case?}。写坏规则时不改动文件。"""
    if proposal.target in FITNESS_TARGETS:
        return _apply_fitness(cfg, proposal, correction_id)
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
            targets = [proposal.target]
            if proposal.rule_text:
                targets.insert(0, FITNESS_RULES)
            result.applied += 1
            result.applied_items.append(
                f"#{correction['id']}（预演）{' + '.join(targets)}: {proposal.text}"
            )
            continue
        try:
            applied = apply_proposal(cfg, proposal, correction["id"])
        except GrowthError as exc:
            result.errors.append(f"纠正 #{correction['id']}：{exc}")
            continue
        target_label = " + ".join(applied.get("targets", [applied["target"]]))
        mark_applied(cfg, correction["id"], target_label, proposal.text)
        result.applied += 1
        item = f"#{correction['id']} → {target_label}: {proposal.text}"
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
