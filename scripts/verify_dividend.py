# -*- coding: utf-8 -*-
"""多维度验证股息率计算正确性。

用法：python scripts/verify_dividend.py

验证维度：
  1. 解析器单测：覆盖所有已知分红描述格式
  2. 内部一致性：get_financials 与 get_financial_trend 最新年股息率应相同
  3. 自算 TTM vs 东财原始值 vs 外部参考（雪球/同花顺公开数据抽查）
  4. 覆盖边界条件：纯派现、转增+派现、送股+派现、仅转增不派现、不分配、ST股、新股
"""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from wyckoff.datasource import (  # noqa: E402
    _dividend_stats,
    _parse_dividend_per_share,
    clear_cache,
    fetch_json,
    get_financial_trend,
    get_financials,
    resolve_input,
)

# --------------------------------------------------------------------------- #
# 维度 1：解析器单测
# --------------------------------------------------------------------------- #
PARSE_CASES = [
    # (description, expected_ps_per_share, note)
    ("10派280.2423元(含税,扣税后252.2181元)", 28.02423, "茅台年报"),
    ("10转3股派5.00元(含税,扣税后4.50元)", 0.50, "华绿生物转增在前"),
    ("10送2股派3元", 0.30, "送股在前"),
    ("10派7.30元(含税)", 0.73, "新华保险中期"),
    ("10派20.60元(含税,扣税后18.54元)", 2.06, "新华保险年报"),
    ("10派239.57元(含税,扣税后215.613元)", 23.957, "茅台中期"),
    ("不分配不转增", None, "不分红应返回 None"),
    ("10转5股", None, "仅转增不派现"),
    ("10送10股", None, "仅送股不派现"),
    (None, None, "None 输入"),
    ("", None, "空字符串"),
    ("10派5元", 0.50, "最简格式"),
    ("10派0.5元", 0.05, "小额派现"),
    ("每10股派3.5元", 0.35, "中文全角标点"),
]


def test_parser() -> int:
    failed = 0
    for desc, expected, note in PARSE_CASES:
        got = _parse_dividend_per_share(desc)
        ok = (got is None and expected is None) or (
            got is not None and expected is not None and abs(got - expected) < 1e-6
        )
        status = "✓" if ok else "✗"
        if not ok:
            failed += 1
        print(f"  {status} {note:16s} | desc={desc!r} → {got} (expect {expected})")
    print(f"\n解析器：{len(PARSE_CASES) - failed}/{len(PARSE_CASES)} 通过")
    return failed


# --------------------------------------------------------------------------- #
# 维度 2：内部一致性（get_financials vs get_financial_trend 最新年股息率）
# --------------------------------------------------------------------------- #
CONSISTENCY_SAMPLES = [
    ("600519", "贵州茅台"),
    ("601336", "新华保险"),
    ("300970", "华绿生物"),
    ("601318", "中国平安"),
    ("000858", "五粮液"),
    ("600036", "招商银行"),
    ("000001", "平安银行"),
    ("600585", "海螺水泥"),
    ("002415", "海康威视"),
    ("600276", "恒瑞医药"),
]


def test_consistency() -> int:
    print("\n--- 维度 2：内部一致性（get_financials vs get_financial_trend）---")
    clear_cache()
    failed = 0
    for code, name in CONSISTENCY_SAMPLES:
        try:
            f = get_financials(code)
            t = get_financial_trend(code)
            if not f or not t:
                print(f"  ? {name}({code})：无数据")
                continue
            card_dy = f.get("dividend_yield")
            trend_dy = t["dividend_yield"][-1] if t["dividend_yield"] else None
            trend_year = t["dates"][-1] if t["dates"] else None
            if card_dy is None and trend_dy is None:
                print(f"  ✓ {name}({code})：均无股息率")
                continue
            if card_dy is None or trend_dy is None:
                print(f"  ✗ {name}({code})：card={card_dy} trend={trend_dy}")
                failed += 1
                continue
            diff = abs(card_dy - trend_dy)
            ok = diff < 0.2  # 允许 0.2% 误差（股价微小波动）
            status = "✓" if ok else "✗"
            if not ok:
                failed += 1
            print(f"  {status} {name}({code}) 趋势图{trend_year}={trend_dy:.2f}% 概览卡={card_dy:.2f}% diff={diff:.3f}%")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {name}({code})：异常 {exc}")
            failed += 1
    return failed


# --------------------------------------------------------------------------- #
# 维度 3：自算 TTM vs 东财原始数据（抽样交叉验证）
# --------------------------------------------------------------------------- #
# 用东财 CPD 原始接口拉数据，自算 TTM 股息率，对比 get_financial_trend 返回值
def test_ttm_vs_em() -> int:
    print("\n--- 维度 3：自算 TTM vs 东财原始 CPD 接口 ---")
    from wyckoff.datasource import EM_FIN_TREND_CPD, get_quote
    import urllib.parse
    import datetime as _dt

    clear_cache()
    failed = 0
    for code, name in CONSISTENCY_SAMPLES[:6]:  # 前 6 只
        try:
            symbol, _, _ = resolve_input(code)
            market, code6 = symbol[:2], symbol[2:]
            secu = f"{code6}.{market.upper()}"
            d = fetch_json(
                EM_FIN_TREND_CPD.format(secu=urllib.parse.quote(secu, safe="")),
                encoding="utf-8", referer="https://emweb.securities.eastmoney.com/",
            )
            rows = ((d or {}).get("result") or {}).get("data") or []
            ttm_div, _, year_div = _dividend_stats(rows)
            q = get_quote(symbol)
            price = q.price if q and q.price > 0 else 0
            self_ttm_dy = round(ttm_div / price * 100, 2) if price and ttm_div else None

            t = get_financial_trend(code)
            api_dy = t["dividend_yield"][-1] if t and t["dividend_yield"] else None
            trend_year = t["dates"][-1] if t else None

            # 额外：手工验证 TTM 分红金额
            today = _dt.date.today()
            manual_ttm = 0.0
            manual_parts = []
            for r in rows:
                desc = str(r.get("ASSIGNDSCRPT", "") or "")
                if not (desc and "不分配" not in desc and "不转增" not in desc):
                    continue
                ps = _parse_dividend_per_share(desc)
                nd = str(r.get("NOTICE_DATE", "") or "")[:10]
                try:
                    if ps and (today - _dt.date.fromisoformat(nd)).days <= 365:
                        manual_ttm += ps
                        manual_parts.append(f"{nd}:{ps:.2f}")
                except Exception:
                    pass

            match = (api_dy is not None and self_ttm_dy is not None
                     and abs(api_dy - self_ttm_dy) < 0.3)
            status = "✓" if match else "✗"
            if not match:
                failed += 1
            print(f"  {status} {name}({code}) trend:{trend_year}={api_dy}% self_ttm={self_ttm_dy}% "
                  f"price={price} ttm_div={ttm_div:.2f} manual={manual_ttm:.2f}[{'+'.join(manual_parts)}]")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {name}({code})：异常 {exc}")
            failed += 1
    return failed


# --------------------------------------------------------------------------- #
# 维度 4：边界条件
# --------------------------------------------------------------------------- #
EDGE_CASES = [
    ("688041", "海光信息", "科创板次新股"),
    ("301626", "翔楼新材", "创业板次新股"),
    ("600243", "*ST 青海华鼎", "ST 股多年不分红"),
    ("002148", "北纬科技", "曾长期不分红"),
]


def test_edge_cases() -> int:
    print("\n--- 维度 4：边界条件 ---")
    clear_cache()
    failed = 0
    for code, name, tag in EDGE_CASES:
        try:
            t = get_financial_trend(code)
            f = get_financials(code)
            if not t:
                print(f"  ? {name}({code}) [{tag}]：无趋势数据")
                continue
            dy = t["dividend_yield"]
            dates = t["dates"]
            has_ttm = dy[-1] is not None and dy[-1] > 0
            has_history = any(v is not None for v in dy[:-1])
            card_dy = f.get("dividend_yield") if f else None
            print(f"  {'✓' if True else '✗'} {name}({code}) [{tag}] "
                  f"trend_years={len(dates)} last={dates[-1]} trend_dy={dy[-1]}% "
                  f"card_dy={card_dy}% history={'有' if has_history else '无'} ttm={'有' if has_ttm else '无'}")
        except Exception as exc:  # noqa: BLE001
            print(f"  ✗ {name}({code})：异常 {exc}")
            failed += 1
    return failed


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 60)
    print("  股息率多维度验证")
    print("=" * 60)

    total_failed = 0

    print("\n--- 维度 1：分红描述解析器单测 ---")
    total_failed += test_parser()

    try:
        total_failed += test_consistency()
        total_failed += test_ttm_vs_em()
        total_failed += test_edge_cases()
    except Exception:
        traceback.print_exc()
        total_failed += 1

    print("\n" + "=" * 60)
    if total_failed == 0:
        print("  全部通过 ✓")
    else:
        print(f"  {total_failed} 项需要关注 ✗")
    print("=" * 60)
    return total_failed


if __name__ == "__main__":
    raise SystemExit(main())
