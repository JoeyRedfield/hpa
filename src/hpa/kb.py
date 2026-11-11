"""笔记库索引与检索（SQLite FTS5 + trigram）。

设计约束见 `.scratch/growing-assistant/spec.md`：

- 索引（`.cache/kb.sqlite3`）可删可重建；游标与草稿等状态不在这里。
- 隐私三档在 **SQL 层** 过滤：`purpose="prompt"` 时只允许 P0，调用方无法绕过。
- 中文 trigram 分词器对 **少于 3 字** 的查询静默返回 0 条（实测），统一走 LIKE 兜底。
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import SKIP_DIRS, SKIP_NAMES, Config, classify_tier

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*\n", re.S)
TAGS_INLINE_RE = re.compile(r"^tags:\s*\[(.*?)\]\s*$", re.M)
TAGS_LIST_RE = re.compile(r"^tags:\s*(.+?)\s*$", re.M)
HEADING_RE = re.compile(r"^#\s+(.+?)\s*$", re.M)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_docs (
    path  TEXT PRIMARY KEY,
    tier  TEXT NOT NULL,
    mtime REAL NOT NULL,
    size  INTEGER NOT NULL,
    title TEXT NOT NULL,
    lines INTEGER NOT NULL DEFAULT 0
);
CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(
    path UNINDEXED,
    tier UNINDEXED,
    line_offset UNINDEXED,
    title,
    body,
    tokenize='trigram'
);
CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

PROMPT_TIERS = ("P0",)
LOCAL_TIERS = ("P0", "P1", "P2")


@dataclass(frozen=True)
class Doc:
    path: str  # 相对 vault 的 posix 路径
    tier: str
    title: str
    tags: tuple[str, ...]
    body: str
    mtime: float
    size: int
    line_offset: int = 0  # 剥离 frontmatter 后，body 第 1 行对应原文第 line_offset+1 行


@dataclass(frozen=True)
class Hit:
    path: str
    title: str
    tier: str
    snippet: str
    line: int
    score: float

    @property
    def citation(self) -> str:
        """原文定位：`path#标题`，能定位到行时再附行号。"""
        if self.line > 0:
            return f"{self.path}:{self.line}"
        return f"{self.path}#{self.title}"


def connect(cfg: Config) -> sqlite3.Connection:
    cfg.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def parse_doc(vault: Path, file: Path, cfg: Config) -> Doc | None:
    try:
        st = file.stat()
    except OSError:
        return None
    if st.st_size > cfg.max_file_bytes:
        return None
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    rel = file.relative_to(vault).as_posix()
    body = text
    tags: tuple[str, ...] = ()
    line_offset = 0
    m = FRONTMATTER_RE.match(text)
    if m:
        tags = _parse_tags(m.group(1))
        body = text[m.end():]
        line_offset = m.group(0).count("\n")

    heading = HEADING_RE.search(body)
    title = heading.group(1).strip() if heading else file.stem
    return Doc(
        path=rel,
        tier=classify_tier(rel),
        title=title,
        tags=tags,
        body=body,
        mtime=st.st_mtime,
        size=st.st_size,
        line_offset=line_offset,
    )


def _parse_tags(frontmatter: str) -> tuple[str, ...]:
    m = TAGS_INLINE_RE.search(frontmatter)
    if m:
        items = [x.strip().strip("'\"") for x in m.group(1).split(",")]
        return tuple(x for x in items if x)
    m = TAGS_LIST_RE.search(frontmatter)
    if m:
        return (m.group(1).strip().strip("'\""),)
    return ()


def iter_docs(cfg: Config) -> Iterator[Doc]:
    vault = cfg.vault
    if not vault.is_dir():
        return
    for root, dirs, files in os.walk(vault):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(files):
            if not name.endswith(".md") or name in SKIP_NAMES:
                continue
            doc = parse_doc(vault, Path(root) / name, cfg)
            if doc is not None:
                yield doc


def build_index(cfg: Config, rebuild: bool = False) -> dict:
    started = time.time()
    conn = connect(cfg)
    try:
        if rebuild:
            conn.execute("DELETE FROM kb_fts")
            conn.execute("DELETE FROM kb_docs")
            conn.commit()

        existing = {
            row["path"]: (row["mtime"], row["size"])
            for row in conn.execute("SELECT path, mtime, size FROM kb_docs")
        }
        added = updated = unchanged = 0
        seen: set[str] = set()

        for doc in iter_docs(cfg):
            seen.add(doc.path)
            prev = existing.get(doc.path)
            if prev and abs(prev[0] - doc.mtime) < 1e-6 and prev[1] == doc.size:
                unchanged += 1
                continue
            if prev is None:
                added += 1
            else:
                updated += 1
            conn.execute("DELETE FROM kb_fts WHERE path = ?", (doc.path,))
            conn.execute(
                "INSERT INTO kb_fts(path, tier, line_offset, title, body) VALUES (?, ?, ?, ?, ?)",
                (doc.path, doc.tier, doc.line_offset, doc.title, doc.body),
            )
            conn.execute(
                """INSERT INTO kb_docs(path, tier, mtime, size, title, lines)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(path) DO UPDATE SET
                     tier=excluded.tier, mtime=excluded.mtime, size=excluded.size,
                     title=excluded.title, lines=excluded.lines""",
                (doc.path, doc.tier, doc.mtime, doc.size, doc.title, doc.body.count("\n") + 1),
            )

        deleted = 0
        for stale in set(existing) - seen:
            conn.execute("DELETE FROM kb_fts WHERE path = ?", (stale,))
            conn.execute("DELETE FROM kb_docs WHERE path = ?", (stale,))
            deleted += 1

        conn.execute(
            "INSERT INTO kb_meta(key, value) VALUES ('last_index_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(time.time()),),
        )
        conn.commit()
        by_tier = {
            row[0]: row[1]
            for row in conn.execute("SELECT tier, COUNT(*) FROM kb_docs GROUP BY tier")
        }
    finally:
        conn.close()

    return {
        "vault": str(cfg.vault),
        "added": added,
        "updated": updated,
        "unchanged": unchanged,
        "deleted": deleted,
        "total": sum(by_tier.values()),
        "by_tier": by_tier,
        "elapsed": round(time.time() - started, 2),
    }


def search(cfg: Config, query: str, purpose: str = "prompt", limit: int = 5) -> list[Hit]:
    """检索笔记库。

    purpose="prompt"：只允许 P0（给 LLM 用，隐私边界在 SQL 层强制）。
    purpose="local" ：允许 P0/P1/P2（本机填表、人工查看）。
    """
    query = query.strip()
    if not query:
        return []
    terms = [t for t in re.split(r"\s+", query) if t]
    if not terms:
        return []

    tiers = PROMPT_TIERS if purpose == "prompt" else LOCAL_TIERS
    tier_sql = "(" + ", ".join("?" for _ in tiers) + ")"

    conn = connect(cfg)
    try:
        if all(len(t) >= 3 for t in terms):
            # trigram 全文检索（≥3 字），标题列加权
            match_expr = " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)
            sql = (
                "SELECT path, tier, line_offset, title, body, "
                "bm25(kb_fts, 0.0, 0.0, 0.0, 5.0, 1.0) AS score "
                f"FROM kb_fts WHERE kb_fts MATCH ? AND tier IN {tier_sql} "
                "ORDER BY score LIMIT ?"
            )
            rows = conn.execute(sql, (match_expr, *tiers, limit * 4)).fetchall()
        else:
            # 短查询（含 2 字中文）走 LIKE 兜底，评分用命中次数
            like_sql = " AND ".join(["body LIKE ?"] * len(terms))
            sql = (
                f"SELECT path, tier, line_offset, title, body, 0.0 AS score FROM kb_fts "
                f"WHERE {like_sql} AND tier IN {tier_sql}"
            )
            rows = conn.execute(sql, (*[f"%{t}%" for t in terms], *tiers)).fetchall()
            rows = sorted(
                rows,
                key=lambda r: -sum(r["body"].lower().count(t.lower()) for t in terms),
            )[: limit * 4]

        hits: list[Hit] = []
        for row in rows:
            snippet, line = _snippet(row["body"], terms, row["line_offset"])
            hits.append(
                Hit(
                    path=row["path"],
                    title=row["title"],
                    tier=row["tier"],
                    snippet=snippet,
                    line=line,
                    score=float(row["score"]),
                )
            )
            if len(hits) >= limit:
                break
        return hits
    finally:
        conn.close()


def _snippet(body: str, terms: list[str], line_offset: int = 0, width: int = 90) -> tuple[str, int]:
    lowered = body.lower()
    idx = -1
    for term in terms:
        i = lowered.find(term.lower())
        if i >= 0 and (idx < 0 or i < idx):
            idx = i
    if idx < 0:
        flat = " ".join(body.split())
        return (flat[:width], 0)
    start = max(0, idx - 30)
    end = min(len(body), idx + width)
    text = " ".join(body[start:end].split())
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(body) else ""
    return (f"{prefix}{text}{suffix}", line_offset + body.count("\n", 0, idx) + 1)


def stats(cfg: Config) -> dict:
    conn = connect(cfg)
    try:
        by_tier = {
            row[0]: row[1]
            for row in conn.execute("SELECT tier, COUNT(*) FROM kb_docs GROUP BY tier")
        }
        row = conn.execute("SELECT value FROM kb_meta WHERE key = 'last_index_at'").fetchone()
        last_index_at = float(row["value"]) if row else None
    finally:
        conn.close()
    db_size = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
    return {
        "db_path": str(cfg.db_path),
        "db_bytes": db_size,
        "total": sum(by_tier.values()),
        "by_tier": by_tier,
        "last_index_at": last_index_at,
    }
