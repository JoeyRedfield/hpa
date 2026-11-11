"""M5 常驻循环：每 N 秒 拉邮件 → 规则分诊 → 模型流水线，把新事推到手机端。

设计要点（spec §2）：

- 确定性部分（同步、去重、分诊、状态机）不依赖模型；模型挂了只影响"理解"。
- 单个 tick 的失败不会杀死循环；错误进日志并留到下一次。
- 手机端页面每 20 秒刷新，所以这里不需要"推送"也能看到新事；
  配了 `HPA_PUSH_URL`（ntfy / 自建 webhook）时，额外 POST 一条摘要。
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request
from dataclasses import dataclass, field

from . import mail as mail_mod
from . import pipeline, store, triage
from .config import Config

PUSH_ENV = "HPA_PUSH_URL"


@dataclass
class TickResult:
    at: str = ""
    synced: int = 0
    triaged_matched: int = 0
    triaged_unmatched: int = 0
    classified: int = 0
    extracted: int = 0
    drafted: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def has_news(self) -> bool:
        return bool(self.synced or self.drafted or self.triaged_matched)


def notify(url: str, text: str) -> bool:
    """向 webhook/ntfy POST 一条纯文本；失败不抛异常（推送不是主链路）。"""
    if not url:
        return False
    try:
        req = urllib.request.Request(
            url,
            data=text.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10):
            return True
    except Exception as exc:  # noqa: BLE001 - 推送失败不能影响主循环
        print(f"   ⚠️ 推送失败：{exc}", file=sys.stderr)
        return False


def tick(
    cfg: Config,
    mailbox: mail_mod.Mailbox | None = None,
    llm=None,
    folder: str = "INBOX",
    push_url: str = "",
) -> TickResult:
    result = TickResult(at=store.utcnow())

    if mailbox is not None:
        try:
            sync_result = mail_mod.sync(cfg, mailbox, folder=folder)
            result.synced = sync_result.inserted
        except Exception as exc:  # noqa: BLE001 - 网络/IMAP 抖动不该杀死循环
            result.errors.append(f"同步失败：{exc}")

    try:
        triaged = triage.triage_pending(cfg)
        result.triaged_matched = triaged.matched
        result.triaged_unmatched = triaged.unmatched
    except Exception as exc:  # noqa: BLE001
        result.errors.append(f"规则分诊失败：{exc}")

    if llm is not None:
        try:
            processed = pipeline.process_pending(cfg, llm)
            result.classified = processed.classified
            result.extracted = processed.extracted
            result.drafted = processed.drafted
            result.errors.extend(processed.errors)
        except Exception as exc:  # noqa: BLE001
            result.errors.append(f"流水线失败：{exc}")

    summary = (
        f"新邮件 {result.synced} / 规则命中 {result.triaged_matched}"
        f" / 分诊 {result.classified} / 待办 {result.extracted} / 草稿 {result.drafted}"
    )
    if push_url and result.has_news:
        notify(push_url, f"hpa: {summary}（打开手机端查看确认）")

    conn = store.connect(cfg)
    try:
        store.set_meta(conn, "last_tick_at", result.at)
        store.set_meta(conn, "last_tick_summary", summary)
        conn.commit()
    finally:
        conn.close()
    return result


def _build_mailbox():
    try:
        return mail_mod.mailbox_from_env()
    except RuntimeError as exc:
        print(f"   （跳过邮件同步：{exc}）")
        return None


def _build_llm(cfg: Config):
    from .llm import LLMError, from_config

    try:
        return from_config(cfg)
    except LLMError as exc:
        print(f"   （跳过模型流水线：{exc}）")
        return None


def run(
    cfg: Config,
    interval: int = 300,
    once: bool = False,
    max_ticks: int | None = None,
    mailbox=None,
    llm=None,
    folder: str = "INBOX",
    push_url: str = "",
) -> None:
    if mailbox is None:
        mailbox = _build_mailbox()
    if llm is None:
        llm = _build_llm(cfg)
    ticks = 0
    print(f"hpa daemon 启动：每 {interval}s 一轮（{'单轮' if once else 'Ctrl-C 退出'}）")
    while True:
        ticks += 1
        result = tick(cfg, mailbox=mailbox, llm=llm, folder=folder, push_url=push_url)
        print(f"[{result.at}] 同步 {result.synced} / 规则命中 {result.triaged_matched}"
              f" / 分诊 {result.classified} / 草稿 {result.drafted}")
        for err in result.errors:
            print(f"   ⚠️ {err}", file=sys.stderr)
        if once or (max_ticks is not None and ticks >= max_ticks):
            return
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("\n收到中断，退出。")
            return
