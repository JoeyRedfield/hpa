"""命令行入口：hpa index / ask / stats。"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import kb
from .config import Config


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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
