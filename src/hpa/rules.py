"""规则引擎：把 `rules/*.md` 里的 ` ```rules ` 代码块解析成确定性判断。

设计目标（spec §5.2）：规则优先、可读可改、可测试；规则未命中时才交给 LLM。
规则语法故意保持窄，宁可让模型兜底，也不在这里长出一个小语言。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .config import Config

BLOCK_RE = re.compile(r"```rules\n(.*?)```", re.S)
HEAD_RE = re.compile(r"^(?P<action>[a-z_]+)\s+if\s+(?P<cond>.+)$")
COND_RE = re.compile(
    r'(?P<field>from|to|subject|body|all)\s+'
    r'(?P<neg>not\s+)?'
    r'(?P<op>contains|startswith|endswith|equals)\s+'
    r'"(?P<value>(?:[^"\\]|\\.)*)"'
)
ACTIONS = {"ignore", "needs_reply", "fyi"}
FIELDS = {"from", "to", "subject", "body", "all"}


class RuleSyntaxError(ValueError):
    def __init__(self, path: Path, line_no: int, line: str, why: str) -> None:
        self.path = path
        self.line_no = line_no
        self.line = line
        super().__init__(f"{path.name}:{line_no} 规则语法错误（{why}）：{line.strip()}")


@dataclass(frozen=True)
class Condition:
    field: str
    op: str
    value: str
    negate: bool = False


@dataclass(frozen=True)
class Rule:
    action: str
    conditions: tuple[Condition, ...]
    line_no: int
    raw: str


@dataclass(frozen=True)
class Match:
    action: str
    rule: Rule


def parse_rules_text(text: str, path: Path | None = None) -> list[Rule]:
    rules: list[Rule] = []
    for block in BLOCK_RE.finditer(text):
        for offset, raw in enumerate(block.group(1).splitlines(), start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            rules.append(_parse_line(line, path or Path("<rules>"), offset, raw))
    return rules


def _parse_line(line: str, path: Path, line_no: int, raw: str) -> Rule:
    head = HEAD_RE.match(line)
    if not head:
        raise RuleSyntaxError(path, line_no, raw, "应为『动作 if 条件』")
    action = head.group("action")
    if action not in ACTIONS:
        raise RuleSyntaxError(path, line_no, raw, f"未知动作 {action!r}，可用 {sorted(ACTIONS)}")
    conditions = tuple(_parse_condition(m, path, line_no, raw) for m in COND_RE.finditer(head.group("cond")))
    if not conditions:
        raise RuleSyntaxError(path, line_no, raw, "没有解析出任何条件")
    # 条件必须覆盖整个 right-hand side，防止拼错被静默忽略
    joined = " and ".join(m.group(0) for m in COND_RE.finditer(head.group("cond")))
    if joined.replace("  ", " ") != head.group("cond").strip().replace("  ", " "):
        raise RuleSyntaxError(path, line_no, raw, "条件里有解析不了的部分")
    return Rule(action=action, conditions=conditions, line_no=line_no, raw=raw)


def _parse_condition(m: re.Match, path: Path, line_no: int, raw: str) -> Condition:
    field = m.group("field")
    if field not in FIELDS:  # pragma: no cover - 正则已限定
        raise RuleSyntaxError(path, line_no, raw, f"未知字段 {field!r}")
    return Condition(
        field=field,
        op=m.group("op"),
        value=_unescape(m.group("value")),
        negate=bool(m.group("neg")),
    )


def _unescape(value: str) -> str:
    return value.replace('\\"', '"').replace("\\\\", "\\")


def load_rules(cfg: Config, name: str = "mail-triage.md") -> list[Rule]:
    path = cfg.project_root / "rules" / name
    if not path.exists():
        return []
    return parse_rules_text(path.read_text(encoding="utf-8"), path)


def _field_text(email, field: str) -> str:
    if field == "from":
        return email.from_addr or ""
    if field == "to":
        return email.to_addr or ""
    if field == "subject":
        return email.subject or ""
    if field == "body":
        return email.body or ""
    return "\n".join(
        [email.from_addr or "", email.to_addr or "", email.subject or "", email.body or ""]
    )


def _matches(cond: Condition, email) -> bool:
    text = _field_text(email, cond.field).casefold()
    value = cond.value.casefold()
    if cond.op == "contains":
        hit = value in text
    elif cond.op == "startswith":
        hit = text.startswith(value)
    elif cond.op == "endswith":
        hit = text.endswith(value)
    else:  # equals
        hit = text == value
    return not hit if cond.negate else hit


def classify(rules: list[Rule], email) -> Match | None:
    for rule in rules:
        if all(_matches(cond, email) for cond in rule.conditions):
            return Match(action=rule.action, rule=rule)
    return None
