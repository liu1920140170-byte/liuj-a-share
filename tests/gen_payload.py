# -*- coding: utf-8 -*-
"""生成一份真实的分析 JSON，供前端渲染测试使用。

    python tests/gen_payload.py [代码]
输出：tests/_payload.json
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff.api import analyze  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_payload.json")


def main() -> int:
    code = sys.argv[1] if len(sys.argv) > 1 else "600519"
    payload = analyze(code, bars=250, with_intraday=True)
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    print(f"已生成 {OUT}")
    print(f"  {payload['meta']['name']}（{payload['meta']['code']}）"
          f" 阶段={payload['wyckoff']['phase_cn']} 动作={payload['decision']['action_cn']}"
          f" K线={len(payload['kline']['dates'])} 根 事件={len(payload['wyckoff']['events'])} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
