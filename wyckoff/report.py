# -*- coding: utf-8 -*-
"""把分析结果渲染成可读的中文文本报告（CLI 与网页「完整报告」共用）。"""
from __future__ import annotations

LINE = "─" * 68


def _num(value, digits: int = 2, suffix: str = "") -> str:
    if value is None:
        return "--"
    try:
        return f"{float(value):,.{digits}f}{suffix}"
    except (TypeError, ValueError):
        return str(value)


def _money(value: float) -> str:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "--"
    if abs(value) >= 1e12:
        return f"{value/1e12:.2f} 万亿"
    if abs(value) >= 1e8:
        return f"{value/1e8:.2f} 亿"
    if abs(value) >= 1e4:
        return f"{value/1e4:.2f} 万"
    return f"{value:.0f}"


_INDEX_TMAP = {"up": "向上", "down": "向下", "sideways": "横盘"}


def _add_index_analysis(add, title: str, market: dict, phase_key: str,
                        trend_key: str, wy_key: str) -> None:
    """渲染单个指数的威科夫完整结构段落（自上而下分析：先指数后个股）。"""
    mk = market if isinstance(market, dict) else {}
    mphase = mk.get(phase_key) or {}
    mwy = mk.get(wy_key) or {}
    if not mphase.get("phase_cn"):
        return
    add("")
    add(LINE)
    add(f" {title}")
    add(LINE)
    mtrend = mk.get(trend_key) or {}
    add(f" 阶段：{mphase.get('phase_cn', '--')}"
        f"{'·' + mphase['sub_phase_cn'] if mphase.get('sub_phase_cn') else ''}   "
        f"置信度 {_num(mphase.get('confidence'), 0)}%   "
        f"日线趋势 {_INDEX_TMAP.get(mtrend.get('dir'), '--')}")
    mrg = mwy.get("range") or {}
    if mrg.get("valid"):
        add(f" 交易区间：{_num(mrg.get('low'))} ~ {_num(mrg.get('high'))}"
            f"（冰线 {_num(mrg.get('low'))}／溪流 {_num(mrg.get('high'))}，"
            f"{mrg.get('bars')} 根K线）")
    if mwy.get("cause"):
        add(f" 因果量度：{mwy.get('cause')}")
    if mwy.get("volume_text"):
        add(f" 供需：{mwy.get('volume_text')}")
    for level in (mwy.get("levels") or [])[:4]:
        kind = "压力" if level["type"] == "resistance" else "支撑"
        add(f"   [{kind}] {_num(level['price']):>10}  {level['label']}")
    for e in (mwy.get("events") or [])[:4]:
        bull = e.get("bullish")
        flag = "多" if bull else ("空" if bull is False else "中")
        add(f"   {e.get('date', '')}  [{flag}] {e.get('type_cn', '')}  {e.get('desc', '')}")


def render(meta: dict, wyckoff: dict, decision: dict, event_data: dict, volume: dict,
           cause: dict, market: dict | None) -> str:
    out: list[str] = []
    add = out.append

    add(LINE)
    add(f" 威科夫复盘报告 · {meta.get('name','')}（{meta.get('code','')}）")
    add(LINE)
    change = meta.get("change_pct") or 0.0
    arrow = "▲" if change > 0 else ("▼" if change < 0 else "—")
    add(f" 最新价 {_num(meta.get('price'))}   {arrow} {change:+.2f}%   "
        f"（{_num(meta.get('change'),2)}）")
    add(f" 开 {_num(meta.get('open'))}   高 {_num(meta.get('high'))}   "
        f"低 {_num(meta.get('low'))}   昨收 {_num(meta.get('prev_close'))}")
    add(f" 成交额 {_money(meta.get('amount'))}   成交量 {_num(meta.get('volume'),0)} 手   "
        f"换手 {_num(meta.get('turnover'))}%   量比 {_num(meta.get('volume_ratio'))}")
    add(f" 市盈率 {_num(meta.get('pe'))}   市净率 {_num(meta.get('pb'))}   "
        f"总市值 {_money(meta.get('total_cap'))}   振幅 {_num(meta.get('amplitude'))}%")
    add(f" 数据：{meta.get('bars')} 根K线（{meta.get('start_date')} ~ {meta.get('end_date')}）"
        f"，{meta.get('adjust')}，来源 {meta.get('source')}，更新 {meta.get('updated')}"
        f"，分析耗时 {meta.get('elapsed', '--')} 秒")
    mw = meta.get("warnings") or []
    if mw:
        add(" 数据提示：" + "；".join(str(w) for w in mw))

    # ---------- 财务概览 ----------
    fin = meta.get("financials")
    if fin:
        add("")
        add(LINE)
        add(f" 公司财务概览　{fin.get('report_type', '')}（{fin.get('report_date', '')}）"
            f"　来源 {fin.get('source', '东方财富')}")
        add(LINE)
        add(f" ROE(加权) {_num(fin.get('roe'))}%   ROIC {_num(fin.get('roic'))}%   "
            f"毛利率 {_num(fin.get('gross_margin'))}%   净利率 {_num(fin.get('net_margin'))}%   "
            f"资产负债率 {_num(fin.get('debt_ratio'))}%")
        rev_yoy = fin.get("revenue_yoy")
        np_yoy = fin.get("net_profit_yoy")
        rev_yoy_txt = f"{rev_yoy:+.2f}%" if rev_yoy else "--"
        np_yoy_txt = f"{np_yoy:+.2f}%" if np_yoy else "--"
        add(f" 营业收入 {_money(fin.get('revenue'))}（同比 {rev_yoy_txt}）   "
            f"归母净利润 {_money(fin.get('net_profit'))}（同比 {np_yoy_txt}）")
        add(f" 每股收益 {_num(fin.get('eps'))} 元   每股净资产 {_num(fin.get('bps'))} 元   "
            f"扣非EPS {_num(fin.get('deducted_eps'))} 元")
        div_yield = fin.get("dividend_yield")
        div_plan = fin.get("dividend_plan", "")
        div_year = fin.get("dividend_year", "")
        div_txt = f"{div_yield:.2f}%" if div_yield else "--"
        plan_txt = f"{div_plan}（{div_year}年度）" if div_plan else "暂无分红方案"
        add(f" 股息率 {div_txt}   分红方案 {plan_txt}")

    # ---------- 自上而下：先指数（上证→创业板），后个股 ----------
    _mk_top = market if isinstance(market, dict) else {}
    _add_index_analysis(add, "一、大盘威科夫分析（上证指数）", _mk_top, "phase", "trend", "wyckoff")
    _add_index_analysis(add, "二、大盘威科夫分析（创业板指）", _mk_top, "cyb_phase", "cyb_trend", "wyckoff_cyb")

    add("")
    add(LINE)
    add(" 三、威科夫阶段判定（个股）")
    add(LINE)
    add(f" 当前阶段：{wyckoff.get('phase_cn')}   子阶段：{wyckoff.get('sub_phase') or '—'}")
    add(f" 判定置信度：{wyckoff.get('confidence')}%    "
        f"吸筹分 {wyckoff.get('acc_score')} / 派发分 {wyckoff.get('dist_score')}")
    trend = wyckoff.get("trend") or {}
    tmap = {"up": "向上", "down": "向下", "sideways": "横盘"}
    add(f" 趋势结构：日线 {tmap.get(trend.get('daily'),'--')} / "
        f"周线 {tmap.get(trend.get('weekly'),'--')} / 分时 {tmap.get(trend.get('intraday'),'--')}")
    rng = wyckoff.get("range") or {}
    if rng.get("valid"):
        add(f" 交易区间：{_num(rng.get('low'))} ~ {_num(rng.get('high'))}"
            f"（宽度 {_num(rng.get('width_pct'))}%，{rng.get('bars')} 根K线，"
            f"{rng.get('start')} ~ {rng.get('end')}）")
        add(f" 　　　　　上沿触碰 {rng.get('touches_high')} 次 / 下沿触碰 {rng.get('touches_low')} 次，"
            f"区间漂移 {_num(rng.get('drift_pct'))}%，形成前走势 {_num(rng.get('prior_change_pct'))}%")
    else:
        add(" 交易区间：未形成清晰的横向震荡区间（阶段判定可靠性下降）")
    add(f" 因果量度：{cause.get('text')}")
    for reason in (wyckoff.get("reasons") or [])[:6]:
        add(f"   · {reason}")

    add("")
    add(LINE)
    add(" 四、关键价位")
    add(LINE)
    for level in (wyckoff.get("levels") or []):
        kind = "压力" if level["type"] == "resistance" else "支撑"
        add(f" [{kind}] {_num(level['price']):>10}  ({level['distance_pct']:+.2f}%)  "
            f"强度 {level['strength']:>2}  {level['label']}")

    add("")
    add(LINE)
    add(" 五、近期威科夫事件（时间倒序）")
    add(LINE)
    events = list(event_data.get("events") or [])
    if not events:
        add(" 未识别到明确的威科夫事件")
    for event in sorted(events, key=lambda e: -e["index"])[:12]:
        flag = "多" if event["bullish"] else "空"
        add(f" {event['date']}  [{flag}] {event['type_cn']:<14} 价 {_num(event['price']):>10}  "
            f"量比 {event['volume_ratio']:<5} 评分 {event['score']}")
        add(f"            {event['desc']}")

    add("")
    add(LINE)
    add(" 六、量价关系（VSA）")
    add(LINE)
    add(f" 供需：{volume.get('text')}")
    add(f" 努力/结果：{(event_data.get('effort_result') or {}).get('text')}")
    for item in sorted(event_data.get("vsa") or [], key=lambda e: -e["index"])[:8]:
        flag = "多" if item["bullish"] else "空"
        add(f" {item['date']}  [{flag}] {item['type_cn']:<18} {item['desc']}")
    add(f" 主力行为推断：{(event_data.get('composite') or {}).get('text')}")

    add("")
    add(LINE)
    add(" 七、买卖决策")
    add(LINE)
    add(f" 【{decision.get('action_cn')}】 置信度 {decision.get('confidence')}%   "
        f"执行紧迫度：{decision.get('urgency_cn')}")
    add(f" {decision.get('summary')}")
    entry = decision.get("entry") or {}
    zone = entry.get("zone") or [0, 0]
    add(f" 入场区间：{_num(zone[0])} ~ {_num(zone[1])}   理想入场价：{_num(entry.get('ideal'))}")
    stop = decision.get("stop") or {}
    add(f" 止损位：{_num(stop.get('price'))}（{stop.get('pct'):+.1f}%） 依据：{stop.get('basis')}")
    targets = decision.get("targets") or []
    if targets:
        add(" 目标位：")
        for idx, item in enumerate(targets, 1):
            add(f"   目标{idx}：{_num(item['price']):>10}  ({item['pct']:+.1f}%)  {item['basis']}")
    add(f" 风险收益比：{decision.get('risk_reward')}")
    add(f" 仓位建议：{decision.get('position', {}).get('text')}")

    add("")
    add(" 决策依据：")
    for reason in (decision.get("reasons") or [])[:6]:
        add(f"   · {reason}")

    checklist = decision.get("checklist") or []
    if checklist:
        add("")
        add(" 操作检查清单：")
        for item in checklist:
            add(f"   [{'√' if item['pass'] else '×'}] {item['item']}")

    warnings = decision.get("warnings") or []
    if warnings:
        add("")
        add(" 风险提示：")
        for item in warnings:
            add(f"   ! {item}")

    if decision.get("invalidations"):
        add("")
        add(" 判断失效条件：")
        for item in decision["invalidations"]:
            add(f"   × {item}")

    add("")
    add(LINE)
    add(f" 大盘环境：{(market or {}).get('text','未获取')}")
    bg = (wyckoff.get("background") or {}) if isinstance(wyckoff, dict) else {}
    if bg:
        ms = bg.get("market") or {}
        ss = bg.get("stock") or {}
        add(f" 当下背景：大盘【{ms.get('stage_cn', '--')}"
            f"{'·' + ms['sub_phase_cn'] if ms.get('sub_phase_cn') else ''}】／"
            f"个股【{ss.get('stage_cn', '--')}"
            f"{'·' + ss['sub_phase_cn'] if ss.get('sub_phase_cn') else ''}】")
        if bg.get("interaction"):
            add(f" 阶段联动：{bg['interaction']}")
        cyc = bg.get("cycle") or {}
        if cyc:
            add(f" 聪明钱循环：当前【{cyc.get('position_cn', '--')}】—— {cyc.get('next_move', '')}")
    add(" "*1 + (meta.get("disclaimer") or ""))
    add(LINE)
    return "\n".join(out)
