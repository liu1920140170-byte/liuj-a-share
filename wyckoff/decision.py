# -*- coding: utf-8 -*-
"""威科夫评分与买卖决策。

思路：把「阶段 + 事件 + 量价 + 趋势 + 位置 + 大盘」量化成多空评分，
再据此给出可执行的操作方案（动作、入场区间、止损、目标位、仓位、风险收益比、检查清单）。
评分不是预测，而是把威科夫逻辑显式化，方便复盘与纪律执行。
"""
from __future__ import annotations

from typing import Any

from .analysis import Indicators, Range, cause_effect
from .datasource import Bar, Quote

ACTION_CN = {
    "buy": "买入（分批建仓）", "hold": "持有观察", "reduce": "减仓",
    "sell": "卖出/回避", "wait": "观望等待",
}
ACTION_STYLE = {"buy": "bull", "hold": "neutral", "reduce": "bear", "sell": "bear", "wait": "neutral"}

BULL_EVENTS = {"Spring": 14, "Shakeout": 16, "Test": 9, "SOS": 13, "LPS": 11, "BU": 8,
               "SC": 6, "ST": 5, "AR": 3, "PS": 3}
BEAR_EVENTS = {"UT": 12, "UTAD": 16, "SOW": 13, "LPSY": 10, "BC": 12}
BULL_VSA = {"NoSupply": 5, "StoppingVolume": 6, "DemandComingIn": 5, "VolumeDryUp": 4, "Absorption": 2}
BEAR_VSA = {"NoDemand": 5, "SupplyComingIn": 5, "Absorption": 2}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def decide(bars: list[Bar], ind: Indicators, rng: Range, phase: dict, event_data: dict,
           volume: dict, quote: Quote | None, market: dict | None, levels: list[dict],
           fresh: dict | None = None, index_mode: bool = False) -> dict:
    n = len(bars)
    price = bars[-1].close
    events = event_data.get("events", [])
    vsa = event_data.get("vsa", [])
    recent_events = [e for e in events if e["index"] >= max(0, n - 60)]
    recent_vsa = [v for v in vsa if v["index"] >= max(0, n - 30)]

    bull = 0.0
    bear = 0.0
    reasons: list[str] = []
    warnings: list[str] = []

    # 1) 阶段基准分
    phase_name = phase.get("phase")
    sub = phase.get("sub_phase") or ""
    base = {"accumulation": (30, 0), "distribution": (0, 30), "markup": (24, 0),
            "markdown": (0, 24), "unknown": (6, 6)}.get(phase_name, (6, 6))
    bull += base[0]
    bear += base[1]
    if phase_name == "accumulation":
        reasons.append(f"结构上处于{phase['phase_cn']}（{phase.get('sub_phase_cn') or '区间震荡'}），"
                       f"属于逢低关注的区域")
        bull += {"Phase A": 3, "Phase B": 0, "Phase C": 9, "Phase D": 11, "Phase E": 6}.get(sub, 0)
    elif phase_name == "distribution":
        reasons.append(f"结构上处于{phase['phase_cn']}（{phase.get('sub_phase_cn') or '区间震荡'}），"
                       f"属于逢高减仓的区域")
        bear += {"Phase A": 3, "Phase B": 0, "Phase C": 9, "Phase D": 11, "Phase E": 6}.get(sub, 0)
    elif phase_name == "markup":
        reasons.append("价格已走出区间进入上涨（Markup）阶段，趋势方向向上")
        bull += 10 if rng.breakout == "up" else 4
    elif phase_name == "markdown":
        reasons.append("价格处于下跌（Markdown）阶段，趋势方向向下")
        bear += 10 if rng.breakout == "down" else 4

    # 2) 事件分：按「时间衰减 + 是否被后续走势验证」加权
    #    威科夫信号的有效性取决于它之后价格有没有跟上，一个月前的 SOS 不能等同于昨天的 SOS。
    scored_events: list[tuple[float, dict, str]] = []
    for event in recent_events:
        age = (n - 1) - event["index"]
        decay = max(0.28, 1.0 - age / 45.0)
        table = BULL_EVENTS if event["bullish"] else BEAR_EVENTS
        weight = table.get(event["type"], 5) * (0.7 + event["score"] / 200.0) * decay
        note = ""
        # 跟随验证：偏多信号出现后价格反而跌回信号位下方 → 信号失效，反向计分
        if event["bullish"] and event["type"] in ("SOS", "Spring", "Shakeout", "LPS", "BU"):
            if price < event["price"] * 0.97:
                weight *= 0.25
                note = "（价格已回落至该信号位下方，信号未获跟随）"
        if (not event["bullish"]) and event["type"] in ("SOW", "UT", "UTAD", "LPSY", "BC"):
            if price > event["price"] * 1.03:
                weight *= 0.25
                note = "（价格已回升至该信号位上方，看空信号未兑现）"
        if age <= 5:
            note = note or "（最近 5 根K线内的新信号）"
        scored_events.append((weight, event, note))

    for weight, event, note in sorted(scored_events, key=lambda item: -item[0])[:5]:
        if event["bullish"]:
            bull += weight
        else:
            bear += weight
        reasons.append(f"{event['date']} 出现{event['type_cn']}（评分 {event['score']}，"
                       f"距今 {n - 1 - event['index']} 根K线）{note}：{event['desc']}")

    fresh_bull = any(e["bullish"] and (n - 1 - e["index"]) <= 8 for e in events)
    fresh_bear = any((not e["bullish"]) and (n - 1 - e["index"]) <= 8 for e in events)

    for item in sorted(recent_vsa, key=lambda e: -e["score"])[:3]:
        table = BULL_VSA if item["bullish"] else BEAR_VSA
        weight = table.get(item["type"], 3)
        if item["bullish"]:
            bull += weight
        else:
            bear += weight
        reasons.append(f"{item['date']} VSA：{item['type_cn']} —— {item['desc']}")

    # 3) 量价关系
    ratio = volume.get("up_down_vol_ratio", 1.0)
    if ratio >= 1.15:
        bull += 6
    elif ratio and ratio <= 0.87:
        bear += 6

    # 4) 趋势（日线 / 周线）
    daily_trend = phase.get("trend", {}).get("daily", "sideways")
    weekly_trend = phase.get("trend", {}).get("weekly", "sideways")
    bull += {"up": 8, "sideways": 0, "down": -8}.get(daily_trend, 0)
    bear += {"down": 8, "sideways": 0, "up": -8}.get(daily_trend, 0)
    bull += {"up": 6, "sideways": 0, "down": -6}.get(weekly_trend, 0)
    bear += {"down": 6, "sideways": 0, "up": -6}.get(weekly_trend, 0)

    # 5) 位置（区间内相对位置 / 一年分位）
    rel = 0.5
    if rng.high > rng.low:
        rel = (price - rng.low) / (rng.high - rng.low)
    if rng.valid:
        if phase_name in ("accumulation", "markup"):
            if rel <= 0.35:
                bull += 6
                reasons.append(f"股价位于区间下沿附近（区间位置 {rel*100:.0f}%），下方空间有限，"
                               f"是威科夫体系里风险较低的介入区")
            elif rel >= 0.85:
                bear += 4
                warnings.append("股价已接近区间上沿，若不能放量突破，回落测试的概率上升")
    year_window = bars[-min(n, 250):]
    low_52, high_52 = min(b.low for b in year_window), max(b.high for b in year_window)
    if high_52 > low_52:
        pos = (price - low_52) / (high_52 - low_52)
        if pos >= 0.95:
            warnings.append(f"股价处于近一年高位（分位 {pos*100:.0f}%），追高的回撤风险偏大")
        elif pos <= 0.10:
            warnings.append(f"股价处于近一年低位（分位 {pos*100:.0f}%），左侧交易需严格控制仓位")
    gain20 = (price - bars[max(0, n - 21)].close) / bars[max(0, n - 21)].close if n > 21 else 0.0
    if gain20 >= 0.25:
        bear += 5
        warnings.append(f"近20根K线已上涨 {gain20*100:.1f}%，短期涨幅较大，等回踩再介入更稳妥")
    elif gain20 <= -0.20:
        bull += 3

    # 6) 大盘环境
    market_text = "未获取到大盘指数数据"
    if market:
        index_trend = (market.get("trend") or {}).get("dir", "sideways")
        if index_trend == "up":
            bull += 3
        elif index_trend == "down":
            bear += 3
            warnings.append("大盘指数处于弱势，个股即使结构良好也可能被系统性拖累，仓位需打折")
        market_text = market.get("text", market_text)

    # 7) 风险与数据质量
    if quote:
        if quote.name and ("ST" in quote.name.upper()):
            warnings.append("该股为 ST/*ST 股票，退市与流动性风险极高，本工具的常规止损逻辑不再适用")
        if quote.limit_up and price >= quote.limit_up * 0.999:
            warnings.append("当前处于涨停价，实际买入可能无法成交，需等次日或打开涨停")
        if quote.limit_down and price <= quote.limit_down * 1.001:
            warnings.append("当前处于跌停价，流动性风险极大，切勿盲目抄底")
    if rng.valid is False:
        warnings.append("当前未形成清晰的横向交易区间，阶段判定可靠性下降，建议以趋势跟随为主")
    if n < 130:
        warnings.append(f"可分析的K线仅 {n} 根，样本偏少（新股或次新股），结论置信度会自动下调")
    if fresh and fresh.get("suspended"):
        warnings.append("该股最近交易日数据缺失，疑似停牌，价格不具备实时参考意义")

    total = bull + bear
    bull_pct = round(bull / total * 100) if total else 50
    bear_pct = 100 - bull_pct
    net = bull - bear

    # ---------------- 动作映射 ----------------
    if net >= 45:
        action = "buy"
    elif net >= 22:
        action = "buy"
    elif net >= 8:
        action = "wait"
    elif net > -8:
        action = "hold"
    elif net > -25:
        action = "reduce"
    else:
        action = "sell"

    # 结构不明时不给方向性动作：没有清晰结构就没有可执行的威科夫交易
    if phase_name == "unknown" and action in ("buy", "reduce", "sell"):
        action = "wait" if action == "buy" else "hold"
        warnings.append("当前没有形成清晰可辨识的威科夫结构（区间/趋势），各类证据互相矛盾，"
                        "因此只提示观察不给出方向性动作")

    # 交易方向由评分决定（而不是由动作词决定）：观望偏多时给的是「计划买入」的价位
    bullish = net >= 0
    bias_bull = net > 0
    entry_zone, ideal, stop_price, stop_basis, targets, entry_basis = _plan(
        bars, ind, rng, price, bullish, levels, event_data)

    stop_pct = (stop_price - ideal) / ideal * 100 if ideal else 0.0
    if bullish and stop_pct >= -1.0:
        stop_pct = -3.0
        stop_price = ideal * 0.97
        stop_basis = "结构支撑不明确，采用固定 3% 止损"

    target_list: list[dict] = []
    for item in targets:
        if bullish and item["price"] <= ideal:
            continue
        if not bullish and item["price"] >= ideal:
            continue
        pct = (item["price"] - ideal) / ideal * 100 if ideal else 0.0
        target_list.append({"price": round(item["price"], 2), "pct": round(pct, 1),
                            "basis": item["basis"]})
    target_list = target_list[:3]
    # 目标位按距离排序（多头由近到远，空头由近到远），并去掉彼此过于接近的
    target_list.sort(key=lambda item: item["price"])
    if not bullish:
        target_list.reverse()
    dedup: list[dict] = []
    for item in target_list:
        if dedup and abs(item["price"] - dedup[-1]["price"]) / max(ideal, 1e-9) < 0.02:
            continue
        dedup.append(item)
    target_list = dedup[:3]

    risk = abs(ideal - stop_price)
    reward = abs(target_list[0]["price"] - ideal) if target_list else 0.0
    rr = round(reward / risk, 2) if risk else 0.0
    if bullish and rr and rr < 1.5:
        warnings.append(f"到第一目标位的风险收益比仅 {rr:.2f}（低于 1.5），性价比不足，"
                        f"更稳妥的做法是等价格回落到支撑附近再介入")
        if action == "buy" and net < 60:
            action = "wait"
    if (not bullish) and rr and rr < 1.5:
        warnings.append(f"当前位置做空的性价比不足（到第一目标位仅 {rr:.2f} 倍风险），"
                        f"更稳妥的做法是等反弹到压力位再减仓，或不参与做空")
        if action in ("sell", "reduce"):
            action = "hold"

    # 震荡区间中部追价拦截：这是威科夫体系里最常见的亏损来源
    if action == "buy" and rng.valid and 0.42 <= rel <= 0.80 and not fresh_bull:
        warnings.append(f"股价位于区间中部（区间位置 {rel*100:.0f}%），且最近 8 根K线内没有新的偏多信号，"
                        f"此刻追价买在震荡中部性价比偏低；更优做法是等回落至区间下部，"
                        f"或等出现放量突破（SOS）并回踩确认（LPS）")
        action = "wait"
    if action == "buy" and not rng.valid and not fresh_bull and daily_trend != "up":
        warnings.append("当前既无有效横盘区间、也无新的走强信号，且日线趋势不向上，买入缺乏依据")
        action = "wait"

    # ---------------- 检查清单 ----------------
    support_below = [lv for lv in levels if lv["type"] == "support" and lv["price"] < price]
    nearest_support = max(support_below, key=lambda lv: lv["price"]) if support_below else None
    resistance_above = [lv for lv in levels if lv["type"] == "resistance" and lv["price"] > price]
    nearest_resistance = min(resistance_above, key=lambda lv: lv["price"]) if resistance_above else None
    key_events = {e["type"] for e in recent_events}
    checklist = [
        {"item": "处于可识别的交易区间或趋势结构", "pass": bool(rng.valid or phase_name in ("markup", "markdown"))},
        {"item": "出现威科夫关键事件（Spring/Test/SOS 或 UT/SOW）",
         "pass": bool(key_events & {"Spring", "Shakeout", "Test", "SOS", "LPS", "UT", "UTAD", "SOW", "LPSY"})},
        {"item": "近 8 根K线内有新的方向性信号", "pass": bool(fresh_bull if bias_bull else fresh_bear)},
        {"item": "量价配合（上涨量大于下跌量）", "pass": bool(ratio >= 1.05) if bias_bull else bool(ratio <= 0.95)},
        {"item": "日线趋势与操作方向一致", "pass": (daily_trend == "up") if bias_bull else (daily_trend != "up")},
        {"item": "周线趋势不构成反向压制", "pass": (weekly_trend != "down") if bias_bull else (weekly_trend != "up")},
        {"item": "入场位置靠近支撑（多头）/ 压力（空头）",
         "pass": bool(nearest_support and abs(nearest_support["distance_pct"]) <= 6) if bias_bull
         else bool(nearest_resistance and abs(nearest_resistance["distance_pct"]) <= 6)},
        {"item": "止损空间可控（不超过 10%）", "pass": bool(abs(stop_pct) <= 10)},
        {"item": "风险收益比不低于 1.5", "pass": bool(rr >= 1.5)},
        {"item": "大盘环境不构成明显逆风",
         "pass": bool(not market or (market.get("trend") or {}).get("dir") != "down")},
    ]
    fail_count = sum(1 for item in checklist if not item["pass"])

    # 清单一致性校验：多项不通过说明信号互相矛盾，此时不给方向性动作
    if action == "buy" and fail_count >= 4:
        warnings.append(f"操作清单有 {fail_count} 项未通过，信号一致性差，本次买入建议降级为观望")
        action = "wait"
    if action in ("sell", "reduce") and fail_count >= 4:
        warnings.append(f"操作清单有 {fail_count} 项未通过，做空/减仓依据不足，降级为持有观察")
        action = "hold"

    # ---------------- 仓位（按单笔风险预算倒推上限；需在动作降级之后计算） ----------------
    stop_frac = abs(stop_pct) / 100.0
    risk_budget = 0.02  # 单笔交易最大亏损 = 总资金的 2%
    risk_cap = int(min(60, max(5, risk_budget / stop_frac * 100))) if stop_frac else 10
    if index_mode:
        suggest = 0
        position_text = ("指数不可直接交易，本结论用于判断大盘环境与整体风险偏好，"
                         "应体现为对个股总仓位的调节，而不是买卖指数本身")
    elif action == "buy":
        if net >= 60:
            suggest = 50
        elif net >= 45:
            suggest = 35
        else:
            suggest = 20
        if phase_name == "distribution":
            suggest = min(suggest, 10)
        suggest = min(suggest, risk_cap)
        position_text = (f"建议总仓位 {suggest}%：首次建仓 {max(5, suggest // 2)}% 试探，"
                         f"待放量突破区间上沿并回踩不破后加到 {suggest}%。"
                         f"止损幅度 {abs(stop_pct):.1f}%，按单笔亏损不超过总资金 2% 计算，"
                         f"仓位上限为 {risk_cap}%")
    elif action == "wait":
        suggest = 0
        position_text = "暂不建仓（0%），等价格进入入场区间或出现确认信号后再动手"
    elif action == "hold":
        suggest = 0
        position_text = "已持仓者可继续持有并收紧止损，空仓者不新增买入"
    elif action == "reduce":
        suggest = 0
        position_text = "建议减仓至半仓以下，反弹到压力位分批兑现"
    else:
        suggest = 0
        position_text = "建议清仓或空仓回避，等待新的吸筹结构形成"

    # ---------------- 失效条件 ----------------
    if abs(stop_pct) > 12:
        warnings.append(f"按结构位计算的止损幅度达 {abs(stop_pct):.1f}%，单笔风险偏大；"
                        f"若一定要参与，应把该笔交易的风险敞口控制在总资金 1% 以内，"
                        f"或改用 60 分钟级别的更小结构止损")
    invalidations: list[str] = []
    if rng.valid:
        if bullish:
            invalidations.append(f"收盘价跌破 {rng.low:.2f}（区间下沿/冰线）且不能快速收回 → 吸筹结构失效，"
                                 f"按纪律离场")
        else:
            invalidations.append(f"收盘价站上 {rng.high:.2f}（区间上沿/溪流）并放量站稳 → 派发判断失效")
    if stop_price:
        invalidations.append(f"触及止损位 {stop_price:.2f}（{stop_pct:+.1f}%）无条件执行，不加仓摊薄")

    # ---------------- 置信度 ----------------
    data_quality = 92 if n >= 250 else (75 if n >= 150 else 55)
    agreement = min(100, abs(net) * 1.7 + 40)
    pass_ratio = (len(checklist) - fail_count) / len(checklist)
    confidence = int(_clamp(phase.get("confidence", 50) * 0.42 + data_quality * 0.18
                            + agreement * 0.25 + pass_ratio * 100 * 0.15, 20, 88))
    confidence = max(15, confidence - fail_count * 4)
    if rng.valid is False:
        confidence = int(confidence * 0.85)
    if action in ("buy", "sell") and fail_count >= 2:
        confidence = min(confidence, 62)
    if fail_count >= 4:
        confidence = min(confidence, 45)

    if not target_list and bullish:
        warnings.append("上方暂无明显量度目标位，建议以移动止盈（跌破 5 日均线离场）方式持有")

    entry_lo, entry_hi = entry_zone
    if action == "buy":
        near = abs(price - ideal) / ideal <= 0.015 if ideal else False
        urgency = "high" if near and net >= 45 else ("medium" if near else "low")
    elif action in ("reduce", "sell"):
        urgency = "high" if net <= -45 else "medium"
    else:
        urgency = "low"
    urgency_cn = {"high": "当下即可执行", "medium": "可等待更好价位", "low": "以观察为主"}[urgency]

    if action == "buy":
        summary = (f"{ACTION_CN[action]}：在 {entry_lo:.2f}~{entry_hi:.2f} 区间分批介入（理想价 "
                   f"{ideal:.2f}），止损 {stop_price:.2f}（{stop_pct:+.1f}%），"
                   f"第一目标 {target_list[0]['price']:.2f}，风险收益比 {rr:.2f}")
    elif action in ("reduce", "sell"):
        summary = (f"{ACTION_CN[action]}：反弹至 {entry_lo:.2f}~{entry_hi:.2f} 区间分批兑现，"
                   f"若价格反向站上 {stop_price:.2f} 则本轮看空判断作废")
    elif action == "wait":
        if bias_bull:
            summary = (f"观望偏多：暂不追价，等回落到 {entry_lo:.2f}~{entry_hi:.2f} 分批介入"
                       f"（止损 {stop_price:.2f}，第一目标 "
                       f"{target_list[0]['price']:.2f}，风险收益比 {rr:.2f}）")
        else:
            summary = (f"观望偏空：不宜抄底，若反弹至 {entry_lo:.2f}~{entry_hi:.2f} 视为减仓机会，"
                       f"反向站上 {stop_price:.2f} 则空头判断作废")
    else:
        summary = f"{ACTION_CN[action]}：结构未给出明确方向，持仓者以 {stop_price:.2f} 作为保护性止损"

    return {
        "action": action, "action_cn": ACTION_CN[action], "style": ACTION_STYLE[action],
        "confidence": confidence, "urgency": urgency, "urgency_cn": urgency_cn,
        "entry": {"zone": [round(entry_lo, 2), round(entry_hi, 2)], "ideal": round(ideal, 2),
                  "basis": entry_basis},
        "stop": {"price": round(stop_price, 2), "pct": round(stop_pct, 1), "basis": stop_basis},
        "targets": target_list,
        "risk_reward": rr,
        "position": {"suggest_pct": suggest, "text": position_text},
        "reasons": reasons[:8],
        "warnings": warnings[:6],
        "invalidations": invalidations,
        "checklist": checklist,
        "summary": summary,
        "score": {"bull": bull_pct, "bear": bear_pct, "net": round(net, 1)},
        "levels_ref": {"support": nearest_support, "resistance": nearest_resistance},
    }


def _plan(bars: list[Bar], ind: Indicators, rng: Range, price: float, bullish: bool,
          levels: list[dict], event_data: dict) -> tuple[list[float], float, float, str, list[dict]]:
    """给出入场区间、理想入场价、止损、目标位。"""
    n = len(bars)
    heights = max(rng.high - rng.low, price * 0.03)
    events = event_data.get("events", [])
    spring_lows = [e["price"] for e in events if e["type"] in ("Spring", "Shakeout", "SC", "Test")]
    ut_highs = [e["price"] for e in events if e["type"] in ("UT", "UTAD", "BC")]
    recent_low = min(b.low for b in bars[-20:])
    recent_high = max(b.high for b in bars[-20:])

    if bullish:
        breakout_up = bool(rng.valid and rng.breakout == "up")
        rel = (price - rng.low) / heights if (rng.valid and heights > 0) else 0.5
        if breakout_up and price >= rng.high * 0.995:
            lo = max(rng.high * 0.99, price * 0.975)
            hi = price * 1.015
            ideal = max(rng.high * 0.998, price * 0.985)
            entry_basis = "突破后回踩区间上沿（回抽确认位）"
        elif rng.valid and rel <= 0.42:
            lo, hi = price * 0.985, price * 1.015
            ideal = price * 0.995
            entry_basis = "位于区间下部，当前价位可分批建仓"
        elif rng.valid and rel <= 0.78:
            lo = rng.low * 1.01
            hi = rng.low + heights * 0.30
            ideal = rng.low + heights * 0.20
            entry_basis = "当前处于区间中部，等回落到区间下部支撑带再介入"
        elif rng.valid:
            lo = rng.low + heights * 0.25
            hi = rng.low + heights * 0.50
            ideal = rng.low + heights * 0.35
            entry_basis = "位于区间上部，等回落到区间中部偏下再介入"
        else:
            lo, hi = price * 0.975, price * 1.01
            ideal = price * 0.985
            entry_basis = "无有效区间，当前价位轻仓试探"

        # 止损优先挂在「结构位」上（区间下沿 / Spring 低点），而不是最近的噪声低点
        structural: list[tuple[float, str]] = []
        if breakout_up:
            structural.append((rng.high * 0.97, f"跌回区间上沿 {rng.high:.2f} 下方（突破失败）"))
        if rng.valid:
            structural.append((rng.low * 0.985, f"跌破区间下沿（冰线）{rng.low:.2f} 且不能快速收回"))
        for value in spring_lows:
            structural.append((value * 0.985, f"跌破 {value:.2f}（Spring/震仓低点）"))
        structural.append((recent_low * 0.985, f"跌破近20日低点 {recent_low:.2f}"))
        below = [item for item in structural if 0 < item[0] < ideal * 0.995]
        roomy = [item for item in below if item[0] <= ideal * 0.975]
        if roomy:
            stop, stop_basis = max(roomy, key=lambda item: item[0])
        elif below:
            stop, stop_basis = min(below, key=lambda item: item[0])
        else:
            stop, stop_basis = ideal * 0.93, "下方无明确结构支撑，采用固定 7% 止损"
        if (ideal - stop) / ideal < 0.025:
            stop, stop_basis = ideal * 0.96, "结构支撑位距入场过近，改用 4% 固定止损避免被噪声打掉"

        ce = cause_effect(rng, price)
        target_prices: list[dict] = []
        if rng.valid:
            target_prices.append({"price": rng.high, "basis": "区间上沿（供给线），第一压力"})
        target_prices.append({"price": recent_high + heights * 0.5, "basis": "近期高点上方 0.5 倍区间高度（进阶目标）"})
        if ce.get("up_target"):
            target_prices.append({"price": ce["up_target"],
                                  "basis": f"因果量度目标（横盘 {ce.get('cause_bars')} 根，乐观）"})
        target_prices.append({"price": max(b.high for b in bars), "basis": "本轮数据区间最高点"})
        return [lo, hi], ideal, stop, stop_basis, target_prices, entry_basis

    # 空头 / 减仓
    if rng.valid:
        lo, hi = price * 0.99, min(rng.high * 0.995, price * 1.035)
        ideal = price * 1.015
        entry_basis = "反弹到区间中上部减仓"
    else:
        lo, hi = price * 0.99, price * 1.03
        ideal = price * 1.015
        entry_basis = "当前价位分批减仓"
    candidates = [rng.high * 1.03] if rng.valid else []
    candidates += [value * 1.025 for value in ut_highs]
    candidates.append(recent_high * 1.02)
    stop_candidates = [c for c in candidates if c > ideal]
    if stop_candidates:
        roomy_up = [c for c in stop_candidates if c >= ideal * 1.025]
        stop = min(roomy_up) if roomy_up else max(stop_candidates)
    else:
        stop = ideal * 1.07
    stop_basis = "站上区间上沿 / 上冲失败高点即视为空头判断失效"
    if (stop - ideal) / ideal < 0.025:
        stop, stop_basis = ideal * 1.04, "上方结构压力位距入场过近，改用 4% 固定止损"

    ce = cause_effect(rng, price)
    targets = []
    if rng.valid:
        targets.append({"price": rng.low, "basis": "区间下沿（冰线），第一支撑"})
    targets.append({"price": recent_low - heights * 0.5, "basis": "近期低点下方 0.5 倍区间高度（进阶目标）"})
    if ce.get("down_target"):
        targets.append({"price": ce["down_target"], "basis": "因果量度下延目标（乐观）"})
    targets.append({"price": min(b.low for b in bars), "basis": "本轮数据区间最低点"})
    return [lo, hi], ideal, stop, stop_basis, targets, entry_basis
