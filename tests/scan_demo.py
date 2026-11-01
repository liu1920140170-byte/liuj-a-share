# -*- coding: utf-8 -*-
"""批量抽检：对不同走势类型的股票跑一遍，检查阶段/决策是否合理、有无异常。

    python tests/scan_demo.py
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.api import analyze  # noqa: E402

CODES = [
    ("sh600519", "贵州茅台"),
    ("sz300750", "宁德时代"),
    ("sh601318", "中国平安"),
    ("sz000001", "平安银行"),
    ("sh600030", "中信证券"),
    ("sz002594", "比亚迪"),
    ("sh688981", "中芯国际"),
    ("sh000001", "上证指数"),
]

print(f"{'代码':<10}{'名称':<10}{'阶段':<14}{'子阶段':<9}{'置信':>4} {'动作':<10}"
      f"{'入场':<16}{'止损%':>7}{'RR':>6}{'清单×':>6}{'事件':>5}{'耗时':>7}")
print("-" * 118)
for symbol, label in CODES:
    started = time.time()
    try:
        result = analyze(symbol, bars=300, with_intraday=True)
    except Exception as exc:  # noqa: BLE001
        print(f"{symbol:<10}{label:<10}失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        continue
    meta, wk, dec = result["meta"], result["wyckoff"], result["decision"]
    entry = dec["entry"]
    fails = sum(1 for item in dec["checklist"] if not item["pass"])
    print(f"{meta['code']:<10}{meta['name'][:8]:<10}{wk['phase_cn'][:12]:<14}"
          f"{(wk['sub_phase'] or '-'):<9}{wk['confidence']:>4} {dec['action_cn'][:8]:<10}"
          f"{entry['zone'][0]:>7.2f}~{entry['zone'][1]:<8.2f}{dec['stop']['pct']:>7.1f}"
          f"{dec['risk_reward']:>6.2f}{fails:>6}{len(wk['events']):>5}{time.time()-started:>6.1f}s")
    if dec["warnings"]:
        for warning in dec["warnings"][:2]:
            print(f"{'':>10}  ! {warning[:100]}")
print("-" * 118)
