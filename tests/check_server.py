# -*- coding: utf-8 -*-
"""端到端验证：通过 HTTP 访问本地服务，检查页面与接口是否正常。

    python tests/check_server.py [端口]
"""
import json
import sys
import time
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
BASE = f"http://127.0.0.1:{PORT}"
failures: list[str] = []


def get(path: str, timeout: float = 180.0):
    request = urllib.request.Request(BASE + path, headers={"Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def main() -> int:
    # 1. 健康检查
    try:
        status, body = get("/api/health", 15)
        data = json.loads(body)
        print(f"[health] {status} ok={data.get('ok')}")
        if not data.get("ok"):
            failures.append("health 未返回 ok")
    except Exception as exc:  # noqa: BLE001
        print(f"[health] 失败：{exc}")
        failures.append(f"服务未启动或无法访问：{exc}")
        print("\n".join(f"  × {f}" for f in failures))
        return 1

    # 2. 首页（含前端 bug 修复标记）
    status, body = get("/", 15)
    page = body.decode("utf-8", "replace")
    print(f"[index] {status} {len(body)/1024:.1f} KB")
    for token, label in [("isArr", "obj() 修复"), ("/api/analyze", "接口地址"),
                         ("<canvas", "K线画布")]:
        ok = token in page
        print(f"   {'√' if ok else '×'} 页面包含{label}")
        if not ok:
            failures.append(f"首页缺少 {label}")

    # 3. 真实分析接口
    started = time.time()
    status, body = get("/api/analyze?code=600519&bars=250")
    payload = json.loads(body.decode("utf-8"))
    elapsed = time.time() - started
    if not payload.get("ok"):
        failures.append(f"analyze 返回失败：{payload.get('error')}")
        print(f"[analyze] 失败：{payload.get('error')}")
    else:
        meta, wk, dec = payload["meta"], payload["wyckoff"], payload["decision"]
        print(f"[analyze] {status} 用时 {elapsed:.1f}s  {meta['name']}（{meta['code']}）"
              f"  {meta['price']}  {meta['change_pct']:+.2f}%")
        print(f"   阶段={wk['phase_cn']} / {wk['sub_phase']}  置信度={wk['confidence']}%"
              f"  动作={dec['action_cn']}")
        print(f"   入场={dec['entry']['zone']}  止损={dec['stop']['price']}({dec['stop']['pct']}%)"
              f"  风险收益比={dec['risk_reward']}  事件={len(wk['events'])} 个")
        print(f"   K线={len(payload['kline']['dates'])} 根  MA20 末尾={payload['kline']['ma20'][-1]}"
              f"  报告={len(payload['report_text'])} 字")
        # 关键字段存在性
        for key in ("report_text", "disclaimer"):
            if not payload.get(key):
                failures.append(f"缺少 {key}")
        if dec["action"] in ("buy", "sell") and dec["risk_reward"] < 1.5:
            failures.append("给出了买卖动作但风险收益比低于 1.5（纪律失效）")
        if dec["action"] in ("buy", "sell") and wk["phase"] == "unknown":
            failures.append("结构不明却给出了方向性动作（纪律失效）")
    print()
    if failures:
        print(f"发现 {len(failures)} 个问题：")
        for item in failures:
            print("  ×", item)
        return 1
    print("端到端验证通过：页面、接口、字段与决策纪律均正常。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
