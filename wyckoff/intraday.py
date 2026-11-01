# -*- coding: utf-8 -*-
"""当日盘中作战建议：实时快照 + 5 分钟K + 日线关键位 → 及时操作提示。

与 decision.py 的日线级决策互补：日线决策回答「这笔交易该不该做、怎么做」，
当日作战建议回答「此刻盘中价格走到了哪个关键位、量能在说什么、现在该不该动手」。

所有输入都是纯数据（Quote/Bar/dict），api.analyze() 与 server 的轻量轮询接口共用
同一实现，保证首次渲染与定时轮询两条入口的结论口径完全一致。
"""
from __future__ import annotations

import datetime as _dt

from .datasource import Bar, Quote

SESSION_CN = {"trading": "盘中", "closed": "已收盘", "pre": "未开盘", "holiday": "非交易日"}
TREND_CN = {"up": "向上", "down": "向下", "sideways": "横盘"}
SHAPE_CN = {"up_strong": "高开高走", "down_strong": "低开低走",
            "fade_high": "冲高回落", "recover_low": "探底回升", "range": "日内震荡"}


def _session_now() -> str:
    """按本地时间（即北京时间）推断交易时段。"""
    now = _dt.datetime.now()
    if now.weekday() >= 5:
        return "holiday"
    hm = now.hour * 60 + now.minute
    if hm < 9 * 60 + 30:
        return "pre"
    if hm >= 15 * 60:
        return "closed"
    return "trading"   # 含 11:30~13:00 午间休市（数据冻结，仍按盘中口径提示）


def _tline_end(t: dict, n: int):
    """供给/需求趋势线延伸到最后一根日K处的投影价（与前端绘制口径一致）。"""
    try:
        ai = float(t.get("a_index"))
        bi = float(t.get("b_index"))
        ap = float(t.get("a_price"))
        bp = float(t.get("b_price"))
    except (TypeError, ValueError):
        return None
    if bi <= ai:
        return None
    return bp + (bp - ap) / (bi - ai) * ((n - 1) - bi)


def intraday_advice(minute_rows: list[Bar], quote: Quote | None, rng: dict,
                    tlines: list[dict], decision: dict, daily: dict | None = None,
                    index_mode: bool = False) -> dict:
    """生成当日作战建议。

    minute_rows : 5 分钟K（可为空，自动降级为仅实时快照 + 日线关键位）
    quote       : 实时快照（可为 None）
    rng         : 日线交易区间（Range.to_dict()，valid=False 时冰线/溪流不参与触发）
    tlines      : 与 kline.trend_lines 同构的列表（supply/demand，含 a_/b_ 锚点）
    decision    : 日线决策结果（entry.zone / stop.price / action_cn 用于触发对照）
    daily       : {"date","open","high","low","close","volume","adjust"} 日线序列
    index_mode  : 指数模式（无涨跌停/个股仓位概念）
    """
    daily = daily or {}
    closes = [c for c in (daily.get("close") or []) if c]
    vols = daily.get("volume") or []
    day_date = str(daily.get("date") or "")
    # 后复权价格与实时价不同尺度，价格关键位触发无意义，自动降级为仅量能特征
    hfq_mode = str(daily.get("adjust", "")).startswith("后复权")

    bars = [b for b in (minute_rows or []) if b is not None and b.close > 0]
    session = _session_now()
    now = _dt.datetime.now()
    lunch_break = now.weekday() < 5 and 11 * 60 + 30 <= now.hour * 60 + now.minute < 13 * 60
    session_date = ""
    if quote and quote.updated:
        session_date = quote.updated[:10]
    elif bars:
        session_date = bars[-1].date[:10]
    elif day_date:
        session_date = day_date

    # ---------- 现价（实时快照优先，退化到最后一根 5 分钟K / 日线） ----------
    price = prev_close = t_open = t_high = t_low = change_pct = 0.0
    if quote and quote.price > 0:
        price, prev_close = quote.price, quote.prev_close
        t_open, t_high, t_low = quote.open, quote.high, quote.low
        change_pct = quote.change_pct
    elif bars:
        price = bars[-1].close
        t_high = max(b.high for b in bars)
        t_low = min(b.low for b in bars)
        if session_date and bars[0].date[:10] == session_date:
            t_open = bars[0].open
    elif closes:
        price = closes[-1]
    if price <= 0:
        return {"ok": False, "error": "暂无有效实时价格，无法生成当日作战建议",
                "session": session, "session_cn": SESSION_CN.get(session, session)}

    today_in_daily = bool(day_date and session_date and day_date == session_date)
    if not prev_close and len(closes) >= 2:
        prev_close = closes[-2] if today_in_daily else closes[-1]
    if not change_pct and prev_close:
        change_pct = (price - prev_close) / prev_close * 100

    # ---------- 当日量能：已成交量 / 过去5日同时段折算 ----------
    today_bars = [b for b in bars if b.date[:10] == session_date] if session_date else []
    n_today = len(today_bars)
    today_vol = sum(b.volume for b in today_bars)
    frac = min(1.0, n_today / 48.0)   # A股全天 48 根 5 分钟K
    past5 = [v for v in (vols[-6:-1] if today_in_daily else vols[-5:]) if v and v > 0]
    vol_ratio_now = 0.0
    vol_basis = ""
    if n_today and past5 and frac > 0.05:
        expected = (sum(past5) / len(past5)) * frac
        if expected > 0:
            vol_ratio_now = today_vol / expected
            vol_basis = f"按已走完 {n_today}/48 根5分钟K折算"
    elif quote and quote.volume_ratio > 0:
        vol_ratio_now = quote.volume_ratio
        vol_basis = "取自实时快照量比"

    # ---------- 日内量价特征 ----------
    up_vol = sum(b.volume for b in today_bars if b.close > b.open)
    down_vol = sum(b.volume for b in today_bars if b.close < b.open)
    ud_ratio = (up_vol / down_vol) if down_vol > 0 else (2.0 if up_vol > 0 else 1.0)

    l30_chg = l30_vol_x = 0.0
    l30_dir = "sideways"
    if n_today >= 6:
        l30 = today_bars[-6:]
        if l30[0].open > 0:
            l30_chg = (l30[-1].close - l30[0].open) / l30[0].open * 100
        l30_dir = "up" if l30_chg >= 0.15 else ("down" if l30_chg <= -0.15 else "sideways")
        avg_vol = today_vol / n_today if n_today else 0.0
        if avg_vol > 0:
            l30_vol_x = sum(b.volume for b in l30) / (avg_vol * len(l30))

    # 开盘第一波（9:30~10:00 前 6 根5分钟K）：聪明钱开盘意图的第一现场
    fw_chg = fw_vol_x = 0.0
    fw_dir = "sideways"
    fw_text = ""
    if n_today >= 2:
        fw = today_bars[:6]
        if fw[0].open > 0:
            fw_chg = (fw[-1].close - fw[0].open) / fw[0].open * 100
        fw_dir = "up" if fw_chg >= 0.3 else ("down" if fw_chg <= -0.3 else "sideways")
        avg_vol = today_vol / n_today if n_today else 0.0
        if avg_vol > 0:
            fw_vol_x = sum(b.volume for b in fw) / (avg_vol * len(fw))
        if fw_vol_x >= 1.3:
            fw_text = {"up": "放量上攻——聪明钱开盘主动吸纳/试盘，关注能否守住开盘价",
                       "down": "放量下压——开盘主动抛售或向下震仓，观察关键位能否快速收回",
                       "sideways": "放量对倒整理——多空争夺，等方向确认"}[fw_dir]
        elif 0 < fw_vol_x <= 0.7:
            fw_text = {"up": "缩量上推——需求不足，反弹持续性存疑",
                       "down": "缩量下探——抛压不重，若收回开盘价上方属健康回踩",
                       "sideways": "缩量整理——聪明钱未参与，观望其开盘意图"}[fw_dir]
        else:
            fw_text = "量能中性，开盘意图尚不明朗"

    shape = "range"
    pull = 0.0
    if t_open > 0 and prev_close > 0 and t_high > 0 and t_low > 0:
        move = (price - t_open) / t_open * 100
        pull = (t_high - price) / t_high * 100
        bounce = (price - t_low) / t_low * 100
        if move >= 0.8 and price >= t_high * 0.995:
            shape = "up_strong"
        elif move <= -0.8 and price <= t_low * 1.005:
            shape = "down_strong"
        elif pull >= 1.2:
            shape = "fade_high"
        elif bounce >= 1.2:
            shape = "recover_low"

    as_of = bars[-1].date[11:16] if bars and len(bars[-1].date) >= 16 else ""
    if quote and quote.updated and len(quote.updated) >= 16:
        as_of = quote.updated[11:16]

    # ---------- 关键位与触发清单 ----------
    watch: list[dict] = []
    actions: list[tuple[int, str, str]] = []   # (优先级, 风格, 文案)

    def add_watch(name: str, level: float, side: str):
        if level and level > 0:
            d = (price - level) / level * 100
            watch.append({"name": name, "price": round(level, 2),
                          "distance_pct": round(d, 2), "side": side})

    valid_rng = bool(rng.get("valid")) and not hfq_mode
    ice = float(rng.get("low") or 0) if valid_rng else 0.0
    creek = float(rng.get("high") or 0) if valid_rng else 0.0
    supply_end = demand_end = 0.0
    if not hfq_mode:
        for t in (tlines or []):
            if not isinstance(t, dict):
                continue
            p = _tline_end(t, len(closes))
            if p is None:
                continue
            if t.get("kind") == "supply":
                supply_end = p
            elif t.get("kind") == "demand":
                demand_end = p

    dec = decision or {}
    stop = float(((dec.get("stop") or {}).get("price")) or 0) if not hfq_mode else 0.0
    net = float(((dec.get("score") or {}).get("net")) or 0)
    bullish_plan = net >= 0   # 多头计划的止损在现价下方；空头计划的失效线在现价上方
    entry_zone = (dec.get("entry") or {}).get("zone") or []
    entry_lo = float(entry_zone[0] or 0) if len(entry_zone) == 2 and not hfq_mode else 0.0
    entry_hi = float(entry_zone[1] or 0) if len(entry_zone) == 2 and not hfq_mode else 0.0
    action_cn = str(dec.get("action_cn") or "")

    # 1) 冰线：威科夫体系里最重要的「无条件纪律位」
    if ice > 0:
        add_watch("冰线（区间下沿）", ice, "below")
        if price < ice:
            actions.append((0, "bear", f"现价 {price:.2f} 已跌破冰线 {ice:.2f}：吸筹/支撑结构失效信号，"
                                       f"按威科夫纪律应先离场，等重新收回冰线上方再评估"))
        elif (price - ice) / ice * 100 <= 0.8:
            actions.append((1, "neutral", f"现价距冰线 {ice:.2f} 不足 1%：若 5 分钟K收盘确认跌破，"
                                          f"按纪律减仓离场，不抱侥幸"))
    # 2) 决策止损位（多头）／看空失效线（空头）
    if stop > 0:
        add_watch("决策止损位" if bullish_plan else "看空失效线", stop, "below")
        if bullish_plan and price <= stop:
            actions.append((0, "bear", f"已触及决策止损位 {stop:.2f}：无条件执行止损纪律，不摊薄、不加仓"))
        elif (not bullish_plan) and price >= stop:
            actions.append((0, "bull", f"现价已站上空头计划的失效线 {stop:.2f}：看空判断作废，"
                                       f"停止减仓/回避动作，等回踩企稳后再评估"))
    # 3) 溪流（区间上沿）：JOC / UT 的分水岭
    if creek > 0:
        add_watch("溪流（区间上沿）", creek, "above")
        if price > creek:
            if vol_ratio_now >= 1.3:
                actions.append((1, "bull", f"放量站上溪流 {creek:.2f}（日内量比 {vol_ratio_now:.1f}）："
                                           f"符合 JOC（跃过溪流）特征，勿追高，等回踩不破（LPS）再分批介入"))
            else:
                actions.append((2, "neutral", f"已站上溪流 {creek:.2f} 但量能不足（日内量比 {vol_ratio_now:.1f}）："
                                              f"警惕上冲失败（UT），回踩确认前不追价加仓"))
        elif (creek - price) / creek * 100 <= 0.8:
            actions.append((2, "neutral", f"现价正在测试溪流/区间上沿 {creek:.2f}：看量能——"
                                          f"放量突破视为 JOC，缩量触碰大概率假突破"))
    # 4) 供给线 / 需求线投影位（±1% 视为触及）
    if supply_end > 0:
        add_watch("供给线投影", supply_end, "above")
        if abs((supply_end - price) / supply_end * 100) <= 1.0:
            actions.append((2, "bear", f"触及供给线投影位 {supply_end:.2f}：反弹遇阻概率大，"
                                       f"持仓者可逢冲高分批减仓；放量有效突破后再回补"))
    if demand_end > 0:
        add_watch("需求线投影", demand_end, "below")
        if abs((price - demand_end) / demand_end * 100) <= 1.0:
            if 0 < vol_ratio_now <= 0.9:
                actions.append((2, "bull", f"回踩需求线 {demand_end:.2f} 且量能收缩（日内量比 {vol_ratio_now:.1f}）："
                                           f"若止跌企稳，属于低风险的 LPS 回踩介入机会"))
            else:
                actions.append((2, "neutral", f"回踩需求线 {demand_end:.2f} 但量能未缩："
                                              f"等企稳信号（缩量 + 5分钟K止跌）再动手"))
    # 5) 量价背离（努力与结果）
    if vol_ratio_now >= 1.8:
        if shape == "fade_high" and change_pct < 1.0:
            actions.append((1, "bear", f"放量冲高回落（日内量比 {vol_ratio_now:.1f}，距日内高点回落 {pull:.1f}%）："
                                       f"高位放量滞涨是供给涌出信号，勿追高，持仓者提高警惕"))
        elif change_pct <= -2.0:
            actions.append((1, "bear", f"放量下跌（日内量比 {vol_ratio_now:.1f}）：供给占主导，不要接下跌的飞刀"))
    elif 0 < vol_ratio_now <= 0.7 and change_pct < 0:
        actions.append((3, "bull", f"缩量回调（日内量比 {vol_ratio_now:.1f}）：抛压不重，属于健康回调，"
                                   f"关注需求线/冰线能否守住"))
    # 6) 进入日线决策的入场区间
    if entry_lo > 0 and entry_hi > 0 and entry_lo <= price <= entry_hi:
        tail = f"，{'止损位' if bullish_plan else '失效线'} {stop:.2f}" if stop > 0 else ""
        if str(dec.get("action") or "") == "buy":
            actions.append((3, "bull", f"现价已进入日线决策的入场区间 {entry_lo:.2f}~{entry_hi:.2f}"
                                       f"（决策：{action_cn or '待定'}），可按计划分批执行{tail}"))
        else:
            actions.append((3, "neutral", f"现价处于日线决策的参考区间 {entry_lo:.2f}~{entry_hi:.2f}"
                                          f"（决策：{action_cn or '待定'}）：区间内有企稳信号（如缩量回踩）"
                                          f"再按计划行动{tail}"))
    # 7) 尾盘提醒
    if session == "trading" and not lunch_break and now.hour == 14 and now.minute >= 40:
        actions.append((4, "neutral", "已临近收盘（14:40 后）：除非关键位刚触发，新开仓建议等次日走势确认，"
                                      "避免尾盘情绪单"))

    actions.sort(key=lambda a: a[0])
    seen: set[str] = set()
    act_list: list[dict] = []
    for _, style, text in actions:
        key = text[:18]
        if key in seen:
            continue
        seen.add(key)
        act_list.append({"style": style, "text": text})
    act_list = act_list[:5]

    # ---------- 汇总 ----------
    if act_list:
        advice, style = act_list[0]["text"], act_list[0]["style"]
    else:
        style = "neutral"
        if session == "trading":
            refs = [f"冰线 {ice:.2f}" if ice > 0 else "", f"溪流 {creek:.2f}" if creek > 0 else ""]
            refs = [r for r in refs if r]
            advice = ("盘中暂未触发关键位：现价距" + "、".join(refs) + "均有安全距离，"
                      if refs else "盘中暂未触发关键位，")
            advice += f"维持日线决策「{action_cn or '持有观察'}」"
        else:
            advice = f"当前{SESSION_CN.get(session, session)}：以下为最近交易日的盘中快照，开盘后将自动更新"

    today_text = (f"{SHAPE_CN[shape]}，现价 {change_pct:+.2f}%"
                  + (f"，日内量比 {vol_ratio_now:.1f}" if vol_ratio_now else ""))
    if n_today >= 6:
        today_text += f"，最近30分钟{TREND_CN[l30_dir]}（{l30_chg:+.2f}%）"
    if up_vol + down_vol > 0:
        today_text += f"，日内上攻量/下跌量 {ud_ratio:.2f}"
    if fw_text:
        today_text += f"；开盘第一波（9:30~10:00）{fw_text}"

    notes: list[str] = []
    if hfq_mode:
        notes.append("当前为后复权口径，价格关键位与实时价不同尺度，本面板仅提供量能特征")
    if bars and not today_bars:
        notes.append(f"5分钟K暂无当日数据（最后数据 {bars[-1].date[:10]}）")
    elif not bars:
        notes.append("5分钟K不可用，量能特征可能不完整")
    if not quote:
        notes.append("实时快照不可用，价格以最后一根5分钟K/日K为准")
    if lunch_break:
        notes.append("午间休市，行情暂停更新")

    return {
        "ok": True, "error": "",
        "session": session, "session_cn": SESSION_CN.get(session, session),
        "as_of": as_of, "price": round(price, 2), "change_pct": round(change_pct, 2),
        "today": {"shape": shape, "shape_cn": SHAPE_CN[shape],
                  "open": round(t_open, 2) if t_open else None,
                  "high": round(t_high, 2) if t_high else None,
                  "low": round(t_low, 2) if t_low else None,
                  "vol_ratio_now": round(vol_ratio_now, 2), "vol_basis": vol_basis,
                  "updown_ratio": round(ud_ratio, 2),
                  "last30_dir": l30_dir, "last30_chg": round(l30_chg, 2),
                  "last30_vol_x": round(l30_vol_x, 2),
                  "first_wave": {"dir": fw_dir, "chg": round(fw_chg, 2),
                                 "vol_x": round(fw_vol_x, 2), "text": fw_text},
                  "text": today_text},
        "watch": watch,
        "actions": act_list,
        "advice": advice, "style": style,
        "note": "；".join(notes),
        "index_mode": bool(index_mode),
    }
