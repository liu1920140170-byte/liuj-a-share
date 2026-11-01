# -*- coding: utf-8 -*-
"""接口契约测试：确保 web/index.html 读取的字段与后端真实 JSON 完全对得上。

背景：本项目的界面是纯前端单文件，若字段名漂移（后端改名、前端没跟上），
页面只会显示「--」而不报错，很容易漏掉。这个脚本用真实行情跑一次 analyze()，
逐项校验「文档化的字段路径」在后端存在、且前端源码里确实引用了同名键。

    python tests/check_contract.py
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.api import analyze  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UI = os.path.join(ROOT, "web", "index.html")

# (JSON 路径, 前端源码中应出现的键名) —— 与交付给前端开发时的接口说明一致
CONTRACT = [
    ("meta.code", "code"), ("meta.name", "name"), ("meta.market", "market"),
    ("meta.updated", "updated"), ("meta.price", "price"), ("meta.prev_close", "prev_close"),
    ("meta.open", "open"), ("meta.high", "high"), ("meta.low", "low"),
    ("meta.change", "change"), ("meta.change_pct", "change_pct"), ("meta.amplitude", "amplitude"),
    ("meta.volume", "volume"), ("meta.amount", "amount"), ("meta.turnover", "turnover"),
    ("meta.pe", "pe"), ("meta.pb", "pb"), ("meta.total_cap", "total_cap"),
    ("meta.float_cap", "float_cap"), ("meta.limit_up", "limit_up"), ("meta.limit_down", "limit_down"),
    ("meta.bars", "bars"), ("meta.start_date", "start_date"), ("meta.end_date", "end_date"),
    ("meta.adjust", "adjust"), ("meta.source", "source"),
    ("wyckoff.phase", "phase"), ("wyckoff.phase_cn", "phase_cn"), ("wyckoff.sub_phase", "sub_phase"),
    ("wyckoff.confidence", "confidence"), ("wyckoff.summary", "summary"),
    ("wyckoff.trend.daily", "daily"), ("wyckoff.trend.weekly", "weekly"),
    ("wyckoff.trend.intraday", "intraday"),
    ("wyckoff.range.high", "high"), ("wyckoff.range.low", "low"), ("wyckoff.range.valid", "valid"),
    ("wyckoff.range.start", "start"), ("wyckoff.range.end", "end"),
    ("wyckoff.range.bars", "bars"), ("wyckoff.range.width_pct", "width_pct"),
    ("wyckoff.cause_effect.text", "text"), ("wyckoff.cause_effect.target", "target"),
    ("wyckoff.levels", "levels"), ("wyckoff.events", "events"), ("wyckoff.vsa", "vsa"),
    ("wyckoff.volume_analysis.text", "text"), ("wyckoff.effort_result.text", "text"),
    ("wyckoff.composite.text", "text"), ("wyckoff.market_context.text", "text"),
    ("wyckoff.score.bull", "bull"), ("wyckoff.score.bear", "bear"), ("wyckoff.score.neutral", "neutral"),
    ("decision.action", "action"), ("decision.action_cn", "action_cn"),
    ("decision.confidence", "confidence"), ("decision.urgency_cn", "urgency_cn"),
    ("decision.entry.zone", "zone"), ("decision.entry.ideal", "ideal"),
    ("decision.stop.price", "price"), ("decision.stop.pct", "pct"), ("decision.stop.basis", "basis"),
    ("decision.targets", "targets"), ("decision.risk_reward", "risk_reward"),
    ("decision.position.suggest_pct", "suggest_pct"), ("decision.position.text", "text"),
    ("decision.reasons", "reasons"), ("decision.warnings", "warnings"),
    ("decision.invalidations", "invalidations"), ("decision.checklist", "checklist"),
    ("decision.summary", "summary"),
    ("kline.dates", "dates"), ("kline.open", "open"), ("kline.high", "high"),
    ("kline.low", "low"), ("kline.close", "close"), ("kline.volume", "volume"),
    ("kline.ma5", "ma5"), ("kline.ma10", "ma10"), ("kline.ma20", "ma20"),
    ("kline.ma60", "ma60"),
    ("kline.events", "events"), ("kline.range", "range"),
    ("report_text", "report_text"), ("disclaimer", "disclaimer"),
]

# 只要求后端提供、界面暂未绘制的字段（便于二次开发）
BACKEND_ONLY = ["kline.ma120"]

ELEMENT_FIELDS = {
    "wyckoff.events": ["date", "type_cn", "price", "volume_ratio", "score", "bullish", "desc", "index"],
    "wyckoff.vsa": ["date", "type_cn", "bullish", "desc", "index", "price", "volume_ratio"],
    "wyckoff.levels": ["type", "price", "label", "strength", "distance_pct"],
    "decision.targets": ["price", "pct", "basis"],
    "decision.checklist": ["item", "pass"],
    "kline.events": ["index", "date", "type_cn", "price", "bullish"],
}


def get_path(data, path: str):
    node = data
    for part in path.split("."):
        if isinstance(node, list):
            if not node:
                return None
            node = node[0]
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    print(f"用真实数据跑一次 analyze({code}) …")
    payload = analyze(code, bars=300, with_intraday=False)
    ui = open(UI, encoding="utf-8").read()

    failures: list[str] = []
    checked = 0
    for path, ui_token in CONTRACT:
        value = get_path(payload, path)
        if value is None:
            failures.append(f"[后端缺失] {path}")
            continue
        if not re.search(r"\b%s\b" % re.escape(ui_token), ui):
            failures.append(f"[前端未引用] {path} → 期望界面源码出现键名 '{ui_token}'")
        checked += 1

    for path in BACKEND_ONLY:
        if get_path(payload, path) is None:
            failures.append(f"[后端缺失] {path}")
        checked += 1

    for path, fields in ELEMENT_FIELDS.items():
        items = get_path(payload, path)
        if not isinstance(items, list) or not items:
            failures.append(f"[后端空数组] {path}（无法校验元素字段）")
            continue
        sample = items[0]
        for field in fields:
            if field not in sample:
                failures.append(f"[元素字段缺失] {path}[].{field}")
            if not re.search(r"\b%s\b" % re.escape(field), ui):
                failures.append(f"[前端未引用] {path}[].{field}")

    # 反向检查：JSON 是否可序列化、数值里有没有 NaN/Infinity（前端 JSON.parse 会炸）
    text = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if "NaN" in text or "Infinity" in text:
        failures.append("[非法数值] JSON 中存在 NaN/Infinity")
    print(f"JSON 体积：{len(text)/1024:.1f} KB，字段校验 {checked} 项（含元素字段共 "
          f"{checked + sum(len(v) for v in ELEMENT_FIELDS.values())} 项）")

    if failures:
        print(f"\n发现 {len(failures)} 个问题：")
        for item in failures:
            print("  ×", item)
        return 1
    print("\n全部通过：前端读取的字段与后端 JSON 一致，无 NaN/Infinity。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
