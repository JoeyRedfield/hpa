"""邮件流水线（M3a）：分类 → 抽取 → 检索 → 起草。

只做「准备草稿」，不发送。发送必须经过确认闸门（M3b 的 Web 页面）。
每一步都要求 LLM 返回 JSON；解析失败记入 `errors` 并跳过该邮件，不猜。

幂等：用 `messages.extracted_at` / `messages.drafted_at` 标记阶段完成，
重复运行不会重复建待办或草稿。顺序按 uid 逐封推进，因此 dry-run 也能
完整走完三步（不落任何库）。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from . import kb, llm as llm_mod, store
from .config import Config
from .mail import Email, from_row

MAX_BODY_CHARS = 4000
KB_HITS_PER_QUERY = 3
KB_HITS_TOTAL = 5

# 一封邮件只要有任一阶段未完成，就是候选。
PENDING_SQL = """
    SELECT * FROM messages
    WHERE triage IS NULL
       OR (triage IN ('needs_reply', 'fyi') AND extracted_at IS NULL)
       OR (triage = 'needs_reply' AND drafted_at IS NULL
           AND message_id NOT IN (SELECT message_id FROM drafts))
    ORDER BY uid
    LIMIT ?
"""


@dataclass
class ProcessResult:
    classified: int = 0
    extracted: int = 0
    drafted: int = 0
    todos_created: int = 0
    errors: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "y")
    return bool(value)


def render_email(email: Email) -> str:
    body = email.body or ""
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n…（正文已截断）"
    return (
        f"发件人：{email.from_addr}\n"
        f"收件人：{email.to_addr}\n"
        f"时间：{email.received_at or '未知'}\n"
        f"主题：{email.subject}\n\n"
        f"{body}"
    )


def _retrieve(cfg: Config, queries: list[str]) -> tuple[str, list[str]]:
    """按 kb_queries 检索笔记库，返回（编号后的上下文, 引用列表）。"""
    citations: list[str] = []
    lines: list[str] = []
    for query in queries[:3]:
        if not isinstance(query, str) or not query.strip():
            continue
        for hit in kb.search(cfg, query, purpose="prompt", limit=KB_HITS_PER_QUERY):
            if hit.citation in citations:
                continue
            if len(citations) >= KB_HITS_TOTAL:
                break
            citations.append(hit.citation)
            lines.append(f"[{len(citations)}] {hit.citation}\n{hit.snippet}")
    return "\n".join(lines), citations


def _next_version(conn: sqlite3.Connection, message_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(version), 0) FROM drafts WHERE message_id = ?", (message_id,)
    ).fetchone()
    return int(row[0]) + 1


def process_pending(
    cfg: Config, llm: llm_mod.LLM, limit: int = 10, dry_run: bool = False
) -> ProcessResult:
    result = ProcessResult()
    prompts = {
        "classify": llm_mod.load_prompt(cfg, "mail-classify.md"),
        "extract": llm_mod.load_prompt(cfg, "mail-extract.md"),
        "draft": llm_mod.load_prompt(cfg, "draft-reply.md"),
    }
    style_path = cfg.project_root / "rules" / "reply-style.md"
    style = style_path.read_text(encoding="utf-8") if style_path.exists() else ""

    conn = store.connect(cfg)
    try:
        rows = conn.execute(PENDING_SQL, (limit,)).fetchall()
        for row in rows:
            _process_one(cfg, llm, conn, row, prompts, style, dry_run, result)
        if not dry_run:
            conn.commit()
        return result
    finally:
        conn.close()


def _process_one(  # noqa: PLR0913 - 流水线内部函数，参数即上下文
    cfg: Config,
    llm: llm_mod.LLM,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    prompts: dict[str, str],
    style: str,
    dry_run: bool,
    result: ProcessResult,
) -> None:
    email = from_row(row)
    triage = row["triage"]
    extracted = row["extracted_at"] is not None
    queries = _kb_queries(conn, email.message_id)

    if triage is None:
        try:
            data = llm_mod.parse_json(llm.complete(prompts["classify"], render_email(email)))
        except llm_mod.LLMError as exc:
            result.errors.append(f"分诊失败 {email.message_id}: {exc}")
            return
        if _as_bool(data.get("needs_reply")):
            triage = "needs_reply"
        elif _as_bool(data.get("is_fyi")):
            triage = "fyi"
        else:
            triage = "ignore"
        result.classified += 1
        result.events.append(f"{email.message_id} → {triage}")
        if not dry_run:
            conn.execute(
                "UPDATE messages SET triage = ?, triage_by = 'llm' WHERE message_id = ?",
                (triage, email.message_id),
            )

    if triage not in ("needs_reply", "fyi") or extracted:
        return

    try:
        data = llm_mod.parse_json(llm.complete(prompts["extract"], render_email(email)))
    except llm_mod.LLMError as exc:
        result.errors.append(f"抽取失败 {email.message_id}: {exc}")
        return
    result.extracted += 1
    todos = data.get("todos") or []
    queries = [q for q in (data.get("kb_queries") or []) if isinstance(q, str) and q.strip()]
    if not dry_run:
        for todo in todos:
            title = str((todo or {}).get("title", "")).strip()
            if not title:
                continue
            conn.execute(
                "INSERT INTO todos(message_id, title, deadline, status, created_at) "
                "VALUES (?, ?, ?, 'open', ?)",
                (email.message_id, title, (todo.get("deadline") or None), store.utcnow()),
            )
            result.todos_created += 1
        conn.execute(
            "UPDATE messages SET extracted_at = ?, kb_queries = ? WHERE message_id = ?",
            (
                store.utcnow(),
                json.dumps(queries, ensure_ascii=False),
                email.message_id,
            ),
        )
    result.events.append(f"{email.message_id} 抽取 {len(todos)} 条待办")

    if triage != "needs_reply":
        return
    if not dry_run and _has_draft(conn, email.message_id):
        return

    context, citations = _retrieve(cfg, queries)
    user = (
        f"## 原邮件\n{render_email(email)}\n\n"
        f"## 参考资料\n{context or '（没有相关资料）'}\n\n"
        f"## 写作风格\n{style}\n"
    )
    try:
        data = llm_mod.parse_json(llm.complete(prompts["draft"], user))
    except llm_mod.LLMError as exc:
        result.errors.append(f"起草失败 {email.message_id}: {exc}")
        return
    body = str(data.get("body", "")).strip()
    if not body:
        result.errors.append(f"起草失败 {email.message_id}: 模型返回空草稿")
        return
    subject = str(data.get("subject") or f"Re: {email.subject}").strip()
    result.drafted += 1
    if not dry_run:
        conn.execute(
            """INSERT INTO drafts(message_id, version, to_addr, subject, body,
                                  citations, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'proposed', ?)""",
            (
                email.message_id,
                _next_version(conn, email.message_id),
                email.from_addr,
                subject,
                body,
                json.dumps(citations, ensure_ascii=False),
                store.utcnow(),
            ),
        )
        conn.execute(
            "UPDATE messages SET drafted_at = ? WHERE message_id = ?",
            (store.utcnow(), email.message_id),
        )
    result.events.append(f"{email.message_id} 起草 → {subject}")


def _has_draft(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM drafts WHERE message_id = ? LIMIT 1", (message_id,)
    ).fetchone()
    return row is not None


def _kb_queries(conn: sqlite3.Connection, message_id: str) -> list[str]:
    row = conn.execute(
        "SELECT kb_queries, subject FROM messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    queries: list[str] = []
    if row:
        try:
            stored = json.loads(row["kb_queries"] or "[]")
        except json.JSONDecodeError:
            stored = []
        queries.extend(q for q in stored if isinstance(q, str) and q.strip())
        if not queries and row["subject"]:
            queries.append(row["subject"])
    for todo in conn.execute(
        "SELECT title FROM todos WHERE message_id = ? ORDER BY id LIMIT 2", (message_id,)
    ):
        queries.append(todo["title"])
    return queries
