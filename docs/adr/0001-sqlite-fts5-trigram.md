# ADR 0001：检索层用 SQLite FTS5 trigram，venv 用系统 Python

- 日期：2026-11-11
- 状态：已采纳

## 背景

M1 要做「个人数据库」：对 `~/Documents/notes`（约 680 篇 Markdown）做增量索引，支持中文检索，并且**返回原文定位**（文件 + 行号）。约束：

- 不引入外部服务或第三方依赖（目前 `pyproject.toml` 的 `dependencies = []`）。
- 隐私三档（P0/P1/P2）必须在检索层强制过滤，不能靠调用方自觉。
- 索引必须可删可重建，不承载任何状态。

## 决定

1. **用 Python 标准库 `sqlite3` + FTS5 `trigram` 分词器**做全文索引，索引文件 `.cache/kb.sqlite3`（git 忽略）。中文无需外部分词器，且能按子串匹配。
2. **venv 使用系统 `/opt/homebrew/bin/python3`（3.14.6，sqlite 3.53.4）**，而不是 uv 下载的 CPython 3.12.11——后者自带的 sqlite 3.49.1 **没有编译 FTS5**，建表直接报 `sqlite3.OperationalError: no such module: fts5`。
3. **少于 3 字的查询走 `LIKE` 兜底**：trigram 对 2 字中文查询会**静默返回 0 条且不报错**（实测 `MATCH '验收'` → 空），这是分词器行为，不是 bug，因此不能只看 MATCH 的结果判断「没找到」。
4. **tier 过滤写进 SQL 的 `WHERE tier IN (...)`**：`purpose="prompt"` 只放 P0，`purpose="local"` 放 P0/P1/P2。调用方拿不到未过滤的结果集。
5. **行号 = frontmatter 行数 + body 内偏移**：索引时存 `line_offset`（UNINDEXED 列），`Hit.citation` 输出 `path:line`，保证能跳回原文。

## 后果

- 好处：零依赖、可重建、中文子串检索可用、隐私边界在数据库层。
- 代价：trigram 索引比源文件大不少（13MB 笔记 → 约 46MB 索引），本机可接受；短查询是 `LIKE` 扫描，只在 P0 全集上做全表匹配，目前 680 篇量级毫秒级返回。
- 若日后换成外部分词器或向量检索，本 ADR 的 2、3 条仍需保留其教训（FTS5 未编译、trigram 短查询静默空结果）。
