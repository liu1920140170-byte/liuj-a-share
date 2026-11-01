# -*- coding: utf-8 -*-
"""威科夫事件与 VSA 量价信号识别。

覆盖的事件（威科夫体系标准命名）：
    吸筹侧：PS 初次支撑、SC 抛售高潮、AR 自动反弹、ST 二次测试、Spring 弹簧、
            Test 缩量测试、Shakeout 震仓、SOS 强势信号、LPS 最后支撑点、BU 回抽确认
    派发侧：BC 买入高潮、UT 上冲失败、UTAD 派发后上冲失败、SOW 弱势信号、LPSY 最后供给点
    VSA  ：无供给、无需求、停止量、需求进场、供给进场、放量滞涨（吸收）、轻松移动、量能枯竭

每个信号都带：日期、价格、量比（相对20日均量）、评分（0-100）、多空方向、中文解释。
"""
from __future__ import annotations

from typing import Any

from .analysis import Indicators, Range, sma
from .datasource import Bar

EVENT_CN = {
    "PS": "初次支撑(PS)", "SC": "抛售高潮(SC)", "AR": "自动反弹(AR)", "ST": "二次测试(ST)",
    "Spring": "弹簧效应(Spring)", "Shakeout": "震仓(Shakeout)", "Test": "缩量测试(Test)",
    "SOS": "强势信号(SOS)", "LPS": "最后支撑点(LPS)", "BU": "回抽确认(BU)",
    "BC": "买入高潮(BC)", "UT": "上冲失败(UT)", "UTAD": "派发后上冲失败(UTAD)",
    "SOW": "弱势信号(SOW)", "LPSY": "最后供给点(LPSY)",
}

VSA_CN = {
    "NoSupply": "无供给(No Supply)", "NoDemand": "无需求(No Demand)",
    "StoppingVolume": "停止量(Stopping Volume)", "DemandComingIn": "需求进场",
    "SupplyComingIn": "供给进场", "Absorption": "放量滞涨/吸收(Absorption)",
    "EasyMove": "轻松移动(无阻力)", "VolumeDryUp": "量能枯竭(Volume Dry-up)",
}


def _prior_drop(bars: list[Bar], i: int, look: int = 30) -> float:
    start = max(0, i - look)
    peak = max(b.high for b in bars[start:i + 1])
    return (peak - bars[i].close) / bars[i].close if bars[i].close else 0.0


def _prior_rise(bars: list[Bar], i: int, look: int = 30) -> float:
    start = max(0, i - look)
    trough = min(b.low for b in bars[start:i + 1])
    return (bars[i].close - trough) / bars[i].close if bars[i].close else 0.0


def _is_local_low(bars: list[Bar], i: int, span: int = 5) -> bool:
    start, end = max(0, i - span), min(len(bars), i + span + 1)
    return bars[i].low <= min(b.low for b in bars[start:end]) + 1e-9


def _is_local_high(bars: list[Bar], i: int, span: int = 5) -> bool:
    start, end = max(0, i - span), min(len(bars), i + span + 1)
    return bars[i].high >= max(b.high for b in bars[start:end]) - 1e-9


def detect(bars: list[Bar], ind: Indicators, rng: Range) -> dict:
    n = len(bars)
    if n < 30:
        return {"events": [], "vsa": [], "effort_result": {"text": "数据不足"},
                "composite": {"text": "数据不足"}, "counts": {}}

    events: dict[tuple[int, str], dict] = {}
    vsa: dict[tuple[int, str], dict] = {}

    def add_event(i: int, type_: str, price: float, score: float, bullish: bool, desc: str):
        if i < 0 or i >= n:
            return
        # 同类型 3 根内只保留评分最高的一个
        for (j, t) in list(events.keys()):
            if t == type_ and abs(j - i) <= 3:
                if events[(j, t)]["score"] >= score:
                    return
                events.pop((j, t), None)
        events[(i, type_)] = {
            "index": i, "date": bars[i].date, "type": type_, "type_cn": EVENT_CN.get(type_, type_),
            "price": round(price, 2), "volume_ratio": ind.rel_vol[i],
            "score": int(max(10, min(98, score))), "bullish": bullish, "desc": desc,
        }

    def add_vsa(i: int, type_: str, score: float, bullish: bool, desc: str):
        if i < 0 or i >= n:
            return
        for (j, t) in list(vsa.keys()):
            if t == type_ and abs(j - i) <= 2:
                if vsa[(j, t)]["score"] >= score:
                    return
                vsa.pop((j, t), None)
        vsa[(i, type_)] = {
            "index": i, "date": bars[i].date, "type": type_, "type_cn": VSA_CN.get(type_, type_),
            "price": round(bars[i].close, 2), "volume_ratio": ind.rel_vol[i],
            "score": int(max(10, min(95, score))), "bullish": bullish, "desc": desc,
        }

    scan_start = max(20, n - 180)
    dist_context = bool(rng.valid and rng.prior_change_pct >= 0.12)
    acc_context = bool(rng.valid and rng.prior_change_pct <= -0.12)

    # ---------------- 第一遍：独立事件 ----------------
    for i in range(scan_start, n):
        bar, prev = bars[i], bars[i - 1]
        clv, sr, rv = ind.clv[i], ind.spread_ratio[i], ind.rel_vol[i]
        drop, rise = _prior_drop(bars, i), _prior_rise(bars, i)
        near_high = bar.close >= max(b.close for b in bars[max(0, i - 10):i]) * 1.001
        near_low = bar.close <= min(b.close for b in bars[max(0, i - 10):i]) * 0.999

        # 抛售高潮：跌势末端，宽幅放巨量，收盘明显脱离最低点
        if drop >= 0.08 and sr >= 1.5 and rv >= 1.8 and clv >= 0.30:
            add_event(i, "SC", bar.low, 70 + min(15, rv * 3),
                      True, f"下跌 {drop*100:.1f}% 后出现宽幅放量长下影（量比 {rv}），"
                            f"收盘回到当日振幅上 {clv*100:.0f}% 位置，说明有大资金承接恐慌抛盘")

        # 买入高潮：涨势末端，宽幅放巨量但收盘位置不佳
        if rise >= 0.10 and sr >= 1.5 and rv >= 1.8 and clv <= 0.60:
            add_event(i, "BC", bar.high, 70 + min(15, rv * 3),
                      False, f"上涨 {rise*100:.1f}% 后出现宽幅放量（量比 {rv}）但收盘仅位于振幅 "
                             f"{clv*100:.0f}% 位置，追高盘被大量派发")

        # 初次支撑：跌势中第一次出现放量下影
        if drop >= 0.08 and rv >= 1.3 and clv >= 0.5 and _is_local_low(bars, i, 5) and rv < 1.8:
            add_event(i, "PS", bar.low, 48, True,
                      f"下跌途中首次出现放量止跌（量比 {rv}，收盘位于振幅上 {clv*100:.0f}%），"
                      f"可视为初步支撑")

        # 弱势信号：宽幅阴线放量跌破近端低点
        if sr >= 1.2 and clv <= 0.30 and rv >= 1.3 and near_low:
            add_event(i, "SOW", bar.close, 68 + min(10, (1 - clv) * 8), False,
                      f"放量宽幅阴线（量比 {rv}，振幅 {sr} 倍ATR）收在最低附近并跌破近10日低点，"
                      f"供给明显涌出")

        # 强势信号：宽幅阳线放量突破近端高点
        if sr >= 1.3 and clv >= 0.65 and rv >= 1.3 and near_high:
            add_event(i, "SOS", bar.close, 68 + min(10, clv * 8), True,
                      f"放量宽幅阳线（量比 {rv}，振幅 {sr} 倍ATR）收在最高附近并突破近10日高点，"
                      f"需求主动吃掉供给")

        # 上冲失败：突破区间上沿后收回区间内
        if rng.valid and bar.high > rng.high * 1.004 and bar.close < rng.high * 0.997 and rv >= 1.1:
            label = "UTAD" if dist_context else "UT"
            score = 76 if label == "UTAD" else 70
            add_event(i, label, bar.high, score, False,
                      f"价格上冲突破区间上沿 {rng.high:.2f} 后收回区间内（收于 {bar.close:.2f}，"
                      f"收盘位置仅 {clv*100:.0f}%），量比 {rv}，属"
                      f"{'派发后上冲失败' if label == 'UTAD' else '上冲失败'}的诱多动作")

        # 弹簧效应 / 震仓：跌破区间下沿后迅速收回
        if rng.valid and bar.low < rng.low * 0.996 and prev.close > rng.low * 0.985:
            recovered = bar.close > rng.low * 0.995 or (i + 1 < n and bars[i + 1].close > rng.low)
            if recovered and _is_local_low(bars, i, 6):
                kind = "Shakeout" if rv >= 1.8 else "Spring"
                score = 80 if kind == "Shakeout" else 74
                add_event(i, kind, bar.low, score, True,
                          f"盘中跌破区间下沿 {rng.low:.2f} 至 {bar.low:.2f} 后收盘回到 "
                          f"{bar.close:.2f}（{'放量' if rv >= 1.8 else '缩量'}，量比 {rv}），"
                          f"属于典型的诱空吸筹（{'震仓' if kind == 'Shakeout' else '弹簧'}）")

    # ---------------- 第二遍：依赖前置事件的信号 ----------------
    sc_list = sorted([e for e in events.values() if e["type"] == "SC"], key=lambda e: e["index"])
    if sc_list:
        sc = sc_list[-1]
        s = sc["index"]
        window_end = min(n - 1, s + 15)
        if window_end > s + 2:
            ar_i = max(range(s + 2, window_end + 1), key=lambda j: bars[j].high)
            add_event(ar_i, "AR", bars[ar_i].high, 56, True,
                      f"抛售高潮后自动反弹至 {bars[ar_i].high:.2f}，构成区间上沿的雏形"
                      f"（与低点 {sc['price']:.2f} 共同框定交易区间）")
            for j in range(ar_i + 1, n):
                if bars[j].low <= sc["price"] * 1.04 and ind.rel_vol[j] <= max(0.9, sc["volume_ratio"] * 0.75) \
                        and ind.spread_ratio[j] <= 1.15:
                    add_event(j, "ST", bars[j].low, 58, True,
                              f"回踩至抛售高潮低点附近（{bars[j].low:.2f}）而量能萎缩至 {ind.rel_vol[j]}，"
                              f"说明该位置抛压明显减轻")
                    break

    spring_list = sorted([e for e in events.values() if e["type"] in ("Spring", "Shakeout")],
                         key=lambda e: e["index"])
    if spring_list:
        sp = spring_list[-1]
        s = sp["index"]
        for j in range(s + 2, min(n, s + 22)):
            if bars[j].low <= sp["price"] * 1.02 and ind.rel_vol[j] <= max(0.75, sp["volume_ratio"] * 0.7) \
                    and ind.clv[j] >= 0.35 and ind.spread_ratio[j] <= 1.1:
                add_event(j, "Test", bars[j].low, 66, True,
                          f"再次回踩弹簧低点附近（{bars[j].low:.2f}）时量比仅 {ind.rel_vol[j]}、"
                          f"振幅收窄，是无供给的缩量测试，抛压枯竭")

    sos_list = sorted([e for e in events.values() if e["type"] == "SOS"], key=lambda e: e["index"])
    if sos_list:
        sos = sos_list[-1]
        s = sos["index"]
        for j in range(s + 1, min(n, s + 16)):
            floor = rng.mid * 0.98 if rng.valid else bars[s].low
            if bars[j].low > bars[s].low and bars[j].low >= floor \
                    and ind.rel_vol[j] <= max(0.85, sos["volume_ratio"] * 0.8) and ind.clv[j] >= 0.40:
                add_event(j, "LPS", bars[j].low, 68, True,
                          f"强势信号后缩量回踩（量比 {ind.rel_vol[j]}）但低点抬高且守住 "
                          f"{max(floor, bars[s].low):.2f}，属最后支撑点 LPS，是相对安全的入场结构")
                break

    sow_list = sorted([e for e in events.values() if e["type"] == "SOW"], key=lambda e: e["index"])
    if sow_list:
        sow = sow_list[-1]
        s = sow["index"]
        for j in range(s + 1, min(n, s + 16)):
            if bars[j].high < bars[s].high and ind.rel_vol[j] <= max(0.85, sow["volume_ratio"] * 0.8) \
                    and ind.clv[j] <= 0.55:
                add_event(j, "LPSY", bars[j].high, 66, False,
                          f"弱势信号后的反弹高点降低（{bars[j].high:.2f} < {bars[s].high:.2f}）"
                          f"且量能萎缩至 {ind.rel_vol[j]}，属最后供给点 LPSY，反弹即卖压")
                break

    if rng.valid and rng.breakout == "up" and rng.breakout_index is not None:
        s = rng.breakout_index
        for j in range(s + 1, min(n, s + 20)):
            if bars[j].low <= rng.high * 1.02 and ind.rel_vol[j] <= 0.85 and ind.clv[j] >= 0.40:
                add_event(j, "BU", bars[j].low, 64, True,
                          f"突破区间上沿 {rng.high:.2f} 后回抽至 {bars[j].low:.2f} 且缩量（量比 "
                          f"{ind.rel_vol[j]}），突破有效性得到确认")
                break

    # ---------------- 第三遍：VSA 量价信号 ----------------
    vol_ma20 = ind.vol_ma20
    for i in range(scan_start, n):
        bar, prev = bars[i], bars[i - 1]
        clv, sr, rv = ind.clv[i], ind.spread_ratio[i], ind.rel_vol[i]
        down_bar = bar.close < prev.close
        up_bar = bar.close > prev.close
        in_lower = (not rng.valid) or bar.close <= rng.low + (rng.high - rng.low) * 0.5

        if down_bar and rv <= 0.70 and sr <= 0.85 and clv >= 0.50:
            add_vsa(i, "NoSupply", 55, True,
                    f"缩量小阴（量比 {rv}，振幅仅 {sr} 倍ATR），收盘仍在上半部，说明下跌没有供给")
        if up_bar and rv <= 0.70 and sr <= 0.85 and clv <= 0.45:
            add_vsa(i, "NoDemand", 55, False,
                    f"缩量小阳（量比 {rv}，振幅仅 {sr} 倍ATR），收盘在下半部，上涨缺乏需求")
        if down_bar and rv >= 1.8 and clv >= 0.40 and _prior_drop(bars, i, 30) >= 0.05:
            add_vsa(i, "StoppingVolume", 58, True,
                    f"下跌中放出 {rv} 倍大量但收盘拉回振幅上 {clv*100:.0f}% 位置，"
                    f"是典型的停止量（大资金承接）")
        if up_bar and rv >= 1.5 and clv <= 0.35:
            add_vsa(i, "SupplyComingIn", 58, False,
                    f"放量阳线（量比 {rv}）却收在振幅下 {clv*100:.0f}% 位置，上方供给沉重")
        if down_bar and rv >= 1.5 and clv >= 0.70:
            add_vsa(i, "DemandComingIn", 56, True,
                    f"放量阴线（量比 {rv}）但收盘位于振幅上 {clv*100:.0f}%，下跌被主动承接")
        if rv >= 1.6 and sr <= 0.80:
            bull = clv >= 0.5
            add_vsa(i, "Absorption", 52, bull,
                    f"放出 {rv} 倍大量但振幅仅 {sr} 倍ATR（放量滞涨/滞跌），说明有大单在吸收"
                    f"{'抛压（偏多）' if bull else '买盘（偏空）'}")
        if rv <= 0.75 and sr >= 1.30:
            bull = bar.close >= prev.close
            add_vsa(i, "EasyMove", 48, bull,
                    f"仅 {rv} 倍量就走出了 {sr} 倍ATR 的振幅，说明{'上方无阻力' if bull else '下方无支撑'}"
                    f"（轻松移动）")
        if i >= 2 and all(ind.rel_vol[k] <= 0.62 for k in (i - 2, i - 1, i)) and in_lower:
            add_vsa(i, "VolumeDryUp", 54, True,
                    "区间下部连续三根缩量（量能枯竭），卖压接近耗尽")

    # ---------------- 整理输出 ----------------
    def pack(store: dict, limit: int) -> list[dict]:
        items = sorted(store.values(), key=lambda e: e["index"])
        # 去重：连续同向同类簇，保留评分最高者
        result: list[dict] = []
        for item in items:
            if result and item["index"] - result[-1]["index"] <= 2 and item["type"] == result[-1]["type"]:
                if item["score"] > result[-1]["score"]:
                    result[-1] = item
                continue
            result.append(item)
        if len(result) > limit:
            # 保留最近的若干 + 评分最高的若干
            recent = result[-limit:]
            top = sorted(result, key=lambda e: -e["score"])[:limit]
            merged = {id(x): x for x in recent + top}
            result = sorted(merged.values(), key=lambda e: e["index"])
        return result

    event_list = pack(events, 16)
    vsa_list = pack(vsa, 12)

    # ---------------- 努力与结果 ----------------
    recent = range(max(0, n - 30), n)
    absorb = [i for i in recent if ind.rel_vol[i] >= 1.5 and ind.spread_ratio[i] <= 0.85]
    easy = [i for i in recent if ind.rel_vol[i] <= 0.8 and ind.spread_ratio[i] >= 1.3]
    last = n - 1
    if ind.rel_vol[last] >= 1.5 and ind.spread_ratio[last] >= 1.3:
        effort = (f"最后一根K线放量 {ind.rel_vol[last]} 倍且振幅达 {ind.spread_ratio[last]} 倍ATR，"
                  f"努力与结果一致，{'多方' if ind.pct[last] >= 0 else '空方'}在主动推动")
    elif absorb:
        effort = (f"近30根K线中有 {len(absorb)} 根出现「大量小振幅」（放量滞涨/滞跌），"
                  f"说明有资金在区间内默默吸收筹码；这种吸收一旦结束，往往伴随方向性突破")
    elif easy:
        effort = (f"近30根K线中有 {len(easy)} 根出现「小量宽幅」（轻松移动），"
                  f"说明某一方向几乎没有对手盘，趋势延续概率较高")
    else:
        effort = "近30根K线量价配合中性，暂无明显的努力/结果背离"

    # ---------------- 主力行为推断 ----------------
    chain = [e for e in event_list if e["index"] >= n - 90][-6:]
    if chain:
        seq = " → ".join(e["type_cn"] for e in chain)
        bull = sum(1 for e in chain if e["bullish"])
        bear = len(chain) - bull
        if bull > bear:
            tone = ("近期事件以吸筹/走强为主，主力行为更倾向于「洗盘后吸筹再拉升」"
                    "——若后续回踩不破关键支撑，可视为低吸窗口")
        elif bear > bull:
            tone = ("近期事件以派发/走弱为主，主力行为更倾向于「拉高派发后打压」"
                    "——反弹到压力位附近应优先考虑减仓")
        else:
            tone = "近期多空事件交织，主力处于观望或双边试探阶段，方向未定，宜等信号确认"
        composite = {"text": f"事件链条：{seq}。{tone}", "chain": [e["type_cn"] for e in chain]}
    else:
        composite = {"text": "近90根K线内未观察到清晰的威科夫事件链条，主力痕迹不明显", "chain": []}

    counts: dict[str, int] = {}
    for item in event_list:
        counts[item["type_cn"]] = counts.get(item["type_cn"], 0) + 1

    return {"events": event_list, "vsa": vsa_list, "effort_result": {"text": effort},
            "composite": composite, "counts": counts}
