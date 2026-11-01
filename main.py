#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""命令行入口：输入股票代码，输出威科夫复盘结论。

用法示例：
    python main.py 600519               # 贵州茅台，日线 300 根
    python main.py 300750 --bars 500    # 宁德时代，500 根K线
    python main.py sh000001             # 上证指数
    python main.py 600519 --json        # 输出 JSON（便于二次开发）
    python main.py 600519 --loop 60     # 每 60 秒刷新一次（盘中盯盘）
    python main.py --web                # 启动本地网页版
    python main.py 600519 --http-only   # 网络受限（只放行 http）时使用
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台默认是 GBK 代码页，报告里有 ─ ▲ √ 等符号，极端情况下会抛
# UnicodeEncodeError 让程序崩溃。这里只把错误处理改成替换，
# 不强制改编码 —— 跟随终端编码才能在各种终端（cmd / Trae 集成终端）里都正常显示中文。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001 - 老版本 Python 或非文本流时忽略
        pass

from wyckoff.api import analyze  # noqa: E402
from wyckoff.datasource import DataSourceError  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="威科夫理论 A 股实时复盘工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("code", nargs="?", help="股票代码，如 600519 / sh600519 / 300750 / sz000001")
    parser.add_argument("--bars", type=int, default=300, help="分析用的日K线根数（默认 300）")
    parser.add_argument("--adjust", default="qfq", choices=["qfq", "hfq", "none"],
                        help="复权方式：qfq 前复权（默认）/ hfq 后复权 / none 不复权")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是文本报告")
    parser.add_argument("--no-intraday", action="store_true", help="不获取分时数据（更快）")
    parser.add_argument("--http-only", action="store_true", help="强制只使用 http 接口（受限网络）")
    parser.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                        help="循环刷新，间隔秒数（0 表示只跑一次）")
    parser.add_argument("--web", action="store_true", help="启动本地网页版")
    parser.add_argument("--port", type=int, default=8765, help="网页版端口（默认 8765）")
    return parser


def run_once(args) -> int:
    if not args.code:
        print("错误：请提供股票代码，例如 python main.py 600519", file=sys.stderr)
        return 2
    try:
        result = analyze(args.code, bars=args.bars, adjust=args.adjust,
                         with_intraday=not args.no_intraday)
    except (DataSourceError, ValueError) as exc:
        print(f"分析失败：{exc}", file=sys.stderr)
        print("排查建议：1) 检查代码是否正确；2) 试试 --http-only；3) 确认网络可访问行情接口。",
              file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result["report_text"])
    return 0


def main() -> int:
    args = build_parser().parse_args()
    if args.http_only:
        os.environ["WYCKOFF_FORCE_HTTP"] = "1"

    if args.web or not args.code:
        # 没给代码时默认进网页版，并自动打开浏览器
        from server import serve
        return serve(port=args.port, open_browser=True)

    if args.loop and args.loop > 0:
        try:
            while True:
                os.system("cls" if os.name == "nt" else "clear")
                run_once(args)
                print(f"\n（每 {args.loop} 秒自动刷新，按 Ctrl+C 退出）")
                time.sleep(args.loop)
        except KeyboardInterrupt:
            print("\n已退出。")
            return 0
    return run_once(args)


if __name__ == "__main__":
    raise SystemExit(main())
