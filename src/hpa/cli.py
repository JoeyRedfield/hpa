"""命令行入口：hpa index / ask / stats / mail。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import kb, mail, pipeline, rules, store, triage
from .config import Config
from .llm import LLMError, from_config as llm_from_config


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return "从未"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def _cmd_index(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    if args.vault:
        cfg = Config(vault=Path(args.vault).expanduser(), project_root=cfg.project_root)
    if not cfg.vault.is_dir():
        print(f"❌ 笔记库不存在：{cfg.vault}", file=sys.stderr)
        return 2
    res = kb.build_index(cfg, rebuild=args.rebuild)
    print(f"扫描 {res['vault']}")
    print(
        f"新增 {res['added']} / 更新 {res['updated']} / 未变 {res['unchanged']}"
        f" / 删除 {res['deleted']}（{res['elapsed']}s）"
    )
    tiers = " ".join(f"{k}:{v}" for k, v in sorted(res["by_tier"].items()))
    print(f"索引共 {res['total']} 篇（{tiers}）")
    return 0


def _cmd_ask(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    query = " ".join(args.query)
    hits = kb.search(cfg, query, purpose=args.purpose, limit=args.limit)
    if not hits:
        print(f"没有命中「{query}」")
        if args.purpose == "prompt":
            print("提示：P1/P2 内容默认不参与检索，加 --purpose local 可查全部。")
        return 1
    print(f"命中 {len(hits)} 篇（purpose={args.purpose}）：")
    for i, hit in enumerate(hits, 1):
        print(f"\n{i}. {hit.path}#{hit.title}  [{hit.tier}]")
        print(f"   → {hit.citation}")
        print(f"   {hit.snippet}")
    return 0


def _cmd_stats(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    st = kb.stats(cfg)
    tiers = " ".join(f"{k}:{v}" for k, v in sorted(st["by_tier"].items()))
    print(f"索引文件 : {st['db_path']}（{st['db_bytes'] / 1024:.0f} KB）")
    print(f"文档总数 : {st['total']} 篇（{tiers}）")
    print(f"上次索引 : {_fmt_time(st['last_index_at'])}")
    return 0


def _cmd_mail_sync(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    try:
        box = mail.mailbox_from_env()
    except RuntimeError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    try:
        res = mail.sync(cfg, box, folder=args.folder)
    except Exception as exc:  # 网络/认证错误：给一行明确原因，别刷栈
        print(f"❌ 同步失败（{args.folder}）：{exc}", file=sys.stderr)
        return 2
    print(f"邮箱 {args.folder}：拉取 {res.fetched} / 新增 {res.inserted} / 重复 {res.duplicates}")
    if res.reset_cursor:
        print("⚠️ UIDVALIDITY 变化，游标已重置并全量重扫（Message-ID 去重兜底）")
    if res.last_uid:
        print(f"游标推进到 UID {res.last_uid}（UIDVALIDITY {res.uidvalidity}）")
    return 0


def _cmd_mail_list(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    conn = store.connect(cfg)
    try:
        rows = conn.execute(
            "SELECT received_at, from_addr, subject, triage FROM messages "
            "ORDER BY received_at DESC, message_id LIMIT ?",
            (args.limit,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        print("还没有同步过邮件。先运行：bin/hpa mail sync")
        return 0
    for row in rows:
        flag = row["triage"] or "待分类"
        print(f"[{flag}] {row['received_at'] or '?'}  {row['from_addr']}")
        print(f"    {row['subject']}")
    return 0


def _cmd_mail_triage(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    try:
        res = triage.triage_pending(cfg, limit=args.limit, dry_run=args.dry_run)
    except rules.RuleSyntaxError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    verb = "预演" if args.dry_run else "已定型"
    print(f"扫描 {res.scanned} 封：{verb} {res.matched} / 未命中 {res.unmatched}")
    if res.by_action:
        detail = " ".join(f"{k}:{v}" for k, v in sorted(res.by_action.items()))
        print(f"按规则：{detail}")
    if res.todos_created:
        print(f"新建待办 {res.todos_created} 条")
    if res.unmatched:
        print("未命中的留给模型判断：运行 bin/hpa process 继续流水线")
    return 0


def _cmd_process(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    try:
        llm = llm_from_config(cfg)
    except LLMError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    try:
        res = pipeline.process_pending(cfg, llm, limit=args.limit, dry_run=args.dry_run)
    except LLMError as exc:
        print(f"❌ 流水线中止：{exc}", file=sys.stderr)
        return 2
    verb = "预演" if args.dry_run else "完成"
    print(
        f"{verb}：分诊 {res.classified} / 抽取 {res.extracted}"
        f" / 起草 {res.drafted} / 新待办 {res.todos_created}"
    )
    for event in res.events:
        print(f"   · {event}")
    for err in res.errors:
        print(f"   ⚠️ {err}", file=sys.stderr)
    if res.drafted and not args.dry_run:
        print("草稿已进确认队列：等手机端确认后才会发送（M3b 提供页面）。")
    return 0


def _cmd_rules_check(args: argparse.Namespace) -> int:
    cfg = Config.load(Path(args.config) if args.config else None)
    path = cfg.project_root / "rules" / "mail-triage.md"
    try:
        loaded = rules.load_rules(cfg)
    except rules.RuleSyntaxError as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2
    print(f"✅ {path}：{len(loaded)} 条规则")
    for rule in loaded:
        print(f"   L{rule.line_no}: {rule.action} ← {rule.raw}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hpa", description="会成长的个人助手")
    parser.add_argument("--config", help="config.toml 路径（默认仓库根）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_index = sub.add_parser("index", help="增量索引笔记库")
    p_index.add_argument("--vault", help="覆盖笔记库路径")
    p_index.add_argument("--rebuild", action="store_true", help="丢弃旧索引重建")
    p_index.set_defaults(func=_cmd_index)

    p_ask = sub.add_parser("ask", help="检索笔记库，给出原文定位")
    p_ask.add_argument("query", nargs="+", help="查询词（空格分隔多词，AND 关系）")
    p_ask.add_argument("-k", "--limit", type=int, default=5, help="返回条数，默认 5")
    p_ask.add_argument(
        "--purpose",
        choices=["prompt", "local"],
        default="prompt",
        help="prompt=只检索 P0（给模型用）；local=允许 P1/P2（本机查看）",
    )
    p_ask.set_defaults(func=_cmd_ask)

    p_stats = sub.add_parser("stats", help="索引统计")
    p_stats.set_defaults(func=_cmd_stats)

    p_mail = sub.add_parser("mail", help="邮件同步与查看")
    mail_sub = p_mail.add_subparsers(dest="mail_cmd", required=True)

    p_sync = mail_sub.add_parser("sync", help="增量拉取邮件（幂等）")
    p_sync.add_argument("--folder", default="INBOX", help="IMAP 文件夹，默认 INBOX")
    p_sync.set_defaults(func=_cmd_mail_sync)

    p_list = mail_sub.add_parser("list", help="列出已同步的邮件")
    p_list.add_argument("-n", "--limit", type=int, default=20, help="条数，默认 20")
    p_list.set_defaults(func=_cmd_mail_list)

    p_triage = mail_sub.add_parser("triage", help="按规则分诊（规则优先，未命中留给模型）")
    p_triage.add_argument("-n", "--limit", type=int, default=50, help="单次处理上限，默认 50")
    p_triage.add_argument("--dry-run", action="store_true", help="只看结果，不写库")
    p_triage.set_defaults(func=_cmd_mail_triage)

    p_process = sub.add_parser(
        "process", help="流水线：模型分诊 → 抽取待办 → 检索资料 → 起草回复（不发送）"
    )
    p_process.add_argument("-n", "--limit", type=int, default=10, help="单次处理上限，默认 10")
    p_process.add_argument("--dry-run", action="store_true", help="走一遍但不写库、不排队")
    p_process.set_defaults(func=_cmd_process)

    p_rules = sub.add_parser("rules", help="检查规则文件语法")
    rules_sub = p_rules.add_subparsers(dest="rules_cmd", required=True)
    p_check = rules_sub.add_parser("check", help="解析 rules/mail-triage.md")
    p_check.set_defaults(func=_cmd_rules_check)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
