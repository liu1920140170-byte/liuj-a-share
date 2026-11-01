# -*- coding: utf-8 -*-
"""分析编排：取数 → 指标 → 结构 → 事件 → 阶段 → 决策 → 报告。

对外只暴露 analyze()，CLI 与 Web 都调用它，保证两条入口结果完全一致。
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import report as report_mod
from .analysis import (build_indicators, cause_effect, classify_phase, find_range,
                       key_levels, long_trend_of, swings, trend_lines, trend_of,
                       volume_stats)
from .datasource import (DataSourceError, get_bars, get_financials, get_index_quotes,
                         get_intraday, get_quote, is_index, last_sources,
                         rescale_to_current, resolve_input, split_code)
from .decision import decide
from .events import detect
from .http_client import fetch_json
from .intraday import intraday_advice

DISCLAIMER = ("本报告由威科夫量价模型自动生成，仅用于技术分析与复盘学习，"
              "不构成任何投资建议；据此操作，盈亏自负。")

ADJUST_CN = {"qfq": "前复权", "hfq": "后复权", "none": "不复权"}
TREND_CN = {"up": "向上", "down": "向下", "sideways": "横盘"}


def _slice_indicators(ind, offset: int):
    from .analysis import Indicators
    out = Indicators()
    for field in ("ma5", "ma10", "ma20", "ma60", "ma120", "atr", "vol_ma5", "vol_ma20",
                  "rel_vol", "spread", "spread_ratio", "clv", "pct"):
        setattr(out, field, getattr(ind, field)[offset:])
    return out


def _round_list(values, digits: int = 2):
    out = []
    for value in values:
        if value is None:
            out.append(None)
        else:
            out.append(round(float(value), digits))
    return out


def _trade_levels(events: list[dict], rng, close: float, ce: dict) -> dict:
    """威科夫交易价位（买点 / 止损 / 目标）。

    复用 v27 _scan_one 的同一套逻辑，保证 analyze 单股主分析、扫描候选、
    K 线主图三处标注的价格位完全一致。

    - 买点：优先 LPS（最后支撑点回踩不破），否则 Spring/Shakeout 低点之上缩量回踩，
            都没有时用现价逢缩量回调
    - 止损：Spring 低点下沿 0.5% 或区间下沿
    - 目标：因果量度 up_target（横盘时间越久投射越高，封顶 1.2 倍区间高度）
    """
    def _recent(t: str) -> dict | None:
        best, best_i = None, -1
        for e in events:
            if e.get("type") == t and int(e.get("index", -1)) > best_i:
                best, best_i = e, int(e.get("index", -1))
        return best
    lps = _recent("LPS")
    spring = _recent("Spring") or _recent("Shakeout")
    entry = float(lps["price"]) if lps else (float(spring["price"]) if spring else close)
    stop = float(spring["price"]) * 0.995 if spring else (float(rng.low) if rng.valid else 0.0)
    target = float(ce.get("up_target") or 0.0)
    # 进场点类型（书中只有两类有效进场点，p90/p95）：
    # ① SOS/JOC 突破后的低量回踩（LPS）；② 终极震仓/带量 Spring 后的二次测试
    if lps:
        entry_type = "SOS/JOC 突破后的低量回踩（进场点①·LPS）"
    elif spring:
        entry_type = "终极震仓/弹簧效应后的二次测试位（进场点②）"
    else:
        entry_type = "尚无标准进场点（区间左侧，只观察等待右手边确认）"
    return {
        "entry": round(entry, 2) if entry else None,
        "stop": round(stop, 2) if stop > 0 else None,
        "target": round(target, 2) if target > 0 else None,
        "entry_type": entry_type,
    }


def _mk_predict(decision: dict, rng, cause: dict) -> str:
    """书中式可验证预测（第④步）：主路径 + 反例条件，触发什么就改变判断。"""
    net = float((decision.get("score") or {}).get("net") or 0)
    hi = f"{rng.high:.2f}" if (rng and rng.valid) else "--"
    lo = f"{rng.low:.2f}" if (rng and rng.valid) else "--"
    up_tg = cause.get("up_target") if isinstance(cause, dict) else None
    tg = f"{up_tg:.2f}" if isinstance(up_tg, (int, float)) else "--"
    if net >= 15:  # 吸筹/做多侧
        return (f"走势关键在回撤的力度：若缩量回踩冰线 {lo} 附近出现需求（收盘在K线上半部、量比小于0.8倍），"
                f"则吸筹结构得到确认，随后价格向区间上沿 {hi} 及因果量度目标 {tg} 运行；"
                f"若放量跌破 {lo} 且两根内不收回，则吸筹判断失效，转入观望。")
    if net <= -15:  # 派发/做空侧
        return (f"反弹关键看需求能否持续：若缩量反弹（量比逐日下降、收盘留上影线形成 SOT），"
                f"则供应继续压制，随后跌破冰线 {lo} 的概率大增，反弹至压力位只减仓不加仓；"
                f"若放量站上区间上沿 {hi}且回踩不破（LPS），则派发判断失效，转为中性观察。")
    return (f"区间震荡延续，方向选择之前以观望为主：向上放量突破 {hi}（量比大于1.5倍且收盘站稳）"
            f"确认新一轮吸筹完成，向下放量跌破 {lo} 确认供应重新控制市场；"
            f"在两者出现之前，任何方向的突破都可能是诱多或诱空，不做 First Cross 追单。")


def _smart_money_steps(market: dict, long_ctx: dict, phase: dict, decision: dict,
                       volume: dict, effort: dict, composite: dict, vsa: list,
                       cause: dict, disp_a: list, ind_a, tests: dict = None,
                       key_candles: list = None, latest: dict = None, rng=None,
                       trade_levels: dict = None) -> dict:
    """《新威科夫操盘》聪明钱看图顺序的逐级推导链（按书中案例的三段式写法）。

    ① 背景 → ② 价量形态 → ③ 价量形态的性质 → ④ 结论或预测 → ⑤ 措施和行动。
    每一步 = 结论一句话 + 依据明细；依据必须落到具体K线（日期/价格/量比/收盘位置），
    第④步按书中格式给出「结论 / 预测 / 依据」——预测附带可验证的反例条件。
    """
    steps: list[dict] = []

    # 相对强弱：个股近60根涨幅 vs 大盘同期（书中 p201-203 选股与进场前必查项）
    m_chg60 = market.get("chg60") if isinstance(market.get("chg60"), (int, float)) else None
    stock_chg60 = None
    if len(disp_a) >= 2:
        seg60 = disp_a[-60:]
        if seg60[0].close:
            stock_chg60 = (seg60[-1].close / seg60[0].close - 1.0) * 100

    # ---------- ① 背景：大盘 + 个股大级别坐标 ----------
    m_dir = str((market.get("trend") or {}).get("dir") or "sideways")
    m_cn = {"up": "上升趋势", "down": "下降趋势", "sideways": "横盘震荡"}.get(m_dir, "未知")
    lt = long_ctx or {}
    lt_dir_cn = {"up": "长期上升趋势", "down": "长期下降趋势", "sideways": "长期宽幅震荡"}.get(
        str(lt.get("dir") or ""), "长期背景未知")
    pos250 = lt.get("pos250")
    pos_txt = (f"，现价处于近250根高低区间的 {pos250 * 100:.0f}% 分位"
               f"（{'大区间低位' if pos250 <= 0.35 else '大区间高位' if pos250 >= 0.65 else '大区间中部'}）"
               ) if pos250 is not None else ""
    steps.append({
        "no": 1, "title": "背景",
        "verdict": f"大盘{m_cn}，个股{lt_dir_cn}{pos_txt}",
        "points": [
            "理论根据（市场本质交易法，先理论后图形）：反转的理论基础＝供应枯竭＋需求扩大并保持；"
            "下跌中供应枯竭的标志是下跌缩量、低点不再创实质新低，需求扩大的标志是放量收回、高收盘、突破供应线。"
            "以下所有步骤都是在图中找能确认这一理论的价量行为，而非形态匹配",
            "第一步先看大盘：" + str(market.get("text") or "")[:140],
            "个股长期背景（全部已加载K线）：" + str(lt.get("text") or ""),
        ] + ([
            ("相对强弱（与大盘同期对比，书中必查项）：个股近60根 {:.1f}% vs 大盘同期 {:.1f}%——{}（{}）".format(
                stock_chg60, m_chg60,
                "强于大盘，大资金收购的证据之一" if stock_chg60 - m_chg60 > 3 else
                "弱于大盘，反弹质量存疑，进场前需更严格的右手边确认" if stock_chg60 - m_chg60 < -3 else
                "与大盘同步",
                "回调缩量且守住支撑者为优"))
        ] if (stock_chg60 is not None and m_chg60 is not None) else []) + [
            "背景对操作的含义：" + (
                "现价在大区间低位，若此处出现吸筹证据（Spring/放量收回/缩量测试），风险收益比最好——"
                "书中案例全部在恐慌后的低位区进场，不在大区间高位追多" if (pos250 is not None and pos250 <= 0.35) else
                "现价在大区间高位，此处的放量滞涨/上冲回落要首先按派发嫌疑处理，"
                "书中案例的高位行为（BC/UT/SOW）出现即只减仓不加仓" if (pos250 is not None and pos250 >= 0.65) else
                "现价在大区间中部，多空证据都可能出现，按书中要求等区间边缘（上沿/冰线）的行为再做决策，"
                "中部突破多为诱多诱空"),
        ],
    })

    # ---------- ② 价量形态：K线长度 / 成交量高度 / 变化速度（最近5根） ----------
    n5 = min(5, len(disp_a))
    last5 = disp_a[-n5:] if n5 else []
    body_txt = vol_txt = speed_txt = "--"
    if last5:
        atr = (ind_a.atr[-1] if ind_a.atr and ind_a.atr[-1] else 0) or (last5[-1].close * 0.02)
        avg_body = sum(abs(b.close - b.open) for b in last5) / n5
        body_ratio = avg_body / atr if atr else 0
        avg_v5 = sum(b.volume for b in last5) / n5
        tail = disp_a[-25:-n5]
        base_v = (sum(b.volume for b in tail) / len(tail)) if tail else avg_v5
        vol_ratio = (avg_v5 / base_v) if base_v else 1.0
        chg5 = (last5[-1].close / last5[0].open - 1) * 100 if last5[0].open else 0.0
        body_txt = (f"K线明显变长（均幅 {body_ratio:.2f}×ATR，波动放大）" if body_ratio >= 0.8 else
                    f"K线短小（均幅 {body_ratio:.2f}×ATR，波动收敛）" if body_ratio <= 0.45 else
                    f"K线长度正常（均幅 {body_ratio:.2f}×ATR）")
        vol_txt = (f"成交量显著放大（近5日均量为前期均量的 {vol_ratio:.2f} 倍）" if vol_ratio >= 1.5 else
                   f"成交量明显萎缩（近5日均量为前期均量的 {vol_ratio:.2f} 倍）" if vol_ratio <= 0.7 else
                   f"成交量与前期持平（比值 {vol_ratio:.2f}）")
        speed_txt = (f"变化速度：近{n5}根累计 {chg5:+.1f}%（{'急速' if abs(chg5) >= 6 else '温和'}）")
    ph_tr = phase.get("trend") or {}
    ph_trend = str(ph_tr.get("daily") or ph_tr.get("dir") or "") if isinstance(ph_tr, dict) else str(ph_tr)
    ph_trend_cn = {"up": "短线向上", "down": "短线向下", "sideways": "短线横盘"}.get(ph_trend, "短线方向未知")
    ph_tt = phase.get("trend_text") or {}
    ph_trend_line = str(ph_tt.get("daily") or "") if isinstance(ph_tt, dict) else str(ph_tt)
    steps.append({
        "no": 2, "title": "价量形态",
        "verdict": f"{ph_trend_cn}；{body_txt.split('（')[0]}，{vol_txt.split('（')[0]}",
        "points": [
            "近5根K线形态：" + body_txt + "；" + vol_txt + "；" + speed_txt,
            "日线趋势（近60根）：" + (ph_trend_line or "未知"),
            "量能结构：" + str(volume.get("text") or "") +
            (f"（涨跌量比 {volume.get('up_down_vol_ratio'):.2f}）"
             if isinstance(volume.get("up_down_vol_ratio"), (int, float)) else ""),
        ] + ([
            "最新一根K线（" + str(latest.get("date") or "") + "）："
            + (f"涨跌 {latest.get('chg_pct'):+.1f}%，" if isinstance(latest.get("chg_pct"), (int, float)) else "")
            + f"量比 {latest.get('vr')} 倍 —— " + str(latest.get("title") or "")
            + "：" + str(latest.get("text") or "")[:80]
        ] if latest and latest.get("title") else []),
    })

    # ---------- ③ 价量形态的性质：逐根关键K线点评（书中案例式） ----------
    hot = [v for v in (vsa or []) if isinstance(v, dict)][-3:]
    hot_pts = []
    for v in hot:
        desc = str(v.get("desc") or v.get("text") or "")
        hot_pts.append(f"{v.get('date', '')} {v.get('type_cn', '')}：{desc[:70]}")
    effort_txt = str(effort.get("text") or "")
    comp_txt = str(composite.get("text") or "")
    nature = "多空拉锯" if not (effort_txt or comp_txt) else (effort_txt or comp_txt)[:40]
    # 关键K线逐根点评：像书中案例一样，对标记出来的K线逐根给出「行为 → 谁在控制」
    kc_pts = []
    for kc in (key_candles or [])[-3:]:
        if not isinstance(kc, dict):
            continue
        kc_pts.append(f"K线{kc.get('num')}（{kc.get('date')}）{kc.get('title')}：{str(kc.get('text') or '')[:90]}")
    steps.append({
        "no": 3, "title": "价量形态的性质",
        "verdict": f"走势性质：{nature}",
        "points": (["努力与结果：" + effort_txt] if effort_txt else []) +
                  (["主力行为（CM 痕迹）：" + comp_txt] if comp_txt else []) +
                  (["关键K线逐根点评：" + p for p in kc_pts]) +
                  (["最近量价信号：" + p for p in hot_pts]),
    })

    # ---------- ④ 结论或预测：处于什么趋势秩序、接下来最可能怎样 ----------
    conf = phase.get("confidence")
    conf_txt = f"（置信度 {conf:.0f}%）" if isinstance(conf, (int, float)) else ""
    ce_txt = str(cause.get("text") or "")
    et = str((trade_levels or {}).get("entry_type") or "")
    side_ok = bool(et) and ("尚无" not in et)
    steps.append({
        "no": 4, "title": "结论或预测",
        "verdict": "结论：" + str(phase.get("summary") or phase.get("phase_cn") or "结构不明") + conf_txt,
        "points": [
            "结论（当前趋势秩序）：" + "；".join(str(x) for x in (phase.get("reasons") or [])[:3]),
            "左出右进（书中硬规则：「进场切记要在右手边」）："
            + (f"右手边确认已具备——{et}" if side_ok else
               "右手边确认未完成（无 LPS/二次测试）——区间左侧只观察定阶段，不抄底；"
               "等 SOS/JOC 后低量回踩（进场点①）或终极震仓/带量 Spring 后的二次测试（进场点②）出现再进场。"
               "书中强调：在底部没有看到终极震仓和 SOS 之一，不要抄底"),
            "预测（可验证）：书中的预测必须落到可观察的条件上——" + _mk_predict(decision, rng, cause),
        ] + (["因果测算依据：" + ce_txt] if ce_txt else []) + [
            "《新威科夫操盘法》九大检验：买入检验通过 {}/9，卖出检验通过 {}/9（逐项清单见交易决策卡）".format(
                (tests or {}).get("buy_passed", 0), (tests or {}).get("sell_passed", 0)),
        ],
    })

    # ---------- ⑤ 措施和行动：进场、离场、收紧止损 ----------
    act_cn = str(decision.get("action_cn") or decision.get("action") or "--")
    urg = str(decision.get("urgency_cn") or "")
    entry = decision.get("entry") or {}
    stop = decision.get("stop") or {}
    pos = decision.get("position") or {}
    tg = decision.get("targets") or []
    tg_txt = "、".join(str(t.get("price") or t.get("target") or t) for t in tg[:2]) if tg else ""
    steps.append({
        "no": 5, "title": "措施和行动",
        "verdict": f"{act_cn}" + (f"（{urg}）" if urg else "") +
                   f"；理想买点 {entry.get('ideal', '--')}，止损 {stop.get('price', '--')}",
        "points": [
            str(decision.get("summary") or ""),
            "进场点类型（书中仅两类有效进场点：①SOS/JOC 突破后的低量回踩；②终极震仓/带量 Spring 后的二次测试）："
            + (et or "--"),
            f"执行：入场区间 {entry.get('zone', ['--', '--'])}，理想买点 {entry.get('ideal', '--')}；"
            f"止损 {stop.get('price', '--')}（依据：{stop.get('basis', '')}）"
            + (f"；目标 {tg_txt}" if tg_txt else "")
            + (f"；仓位 {pos.get('text', '')}" if pos.get("text") else ""),
            "危机管理（书中交易计划固定第三段，出现任一情况即放弃计划离场）："
            f"① 进场后未出现三高＋量增的确认，反而出现放量阴线；"
            f"② 收盘跌破止损位 {stop.get('price', '--')} 且两根内不收回；"
            f"③ 突破后的回踩出现放量长阴（浮动供应还在，突破失效）",
        ],
    })

    return {"framework": "《新威科夫操盘》聪明钱看图顺序", "steps": steps}


def _nine_tests(events: list, rng, vsa: list, cause: dict, phase: dict,
                market: dict, decision: dict) -> dict:
    """《新威科夫操盘法》案例中反复使用的检验工具，固化为九大买入/卖出检验。

    买入检验：①目的与因果 ②卖出高潮与初现支撑 ③自动反弹后的缩量二次测试
    ④突破小溪（SOS） ⑤回踩获支撑（LPS/Spring） ⑥高点低点抬高
    ⑦供应枯竭 ⑧个股相对强度 ⑨大盘背景配合。卖出检验为镜像。
    每项给出 passed 与 note（依据），失败也说明缺什么证据。
    """
    def last(*types: str):
        best = None
        for e in events:
            if e.get("type") in types:
                best = e
        return best

    have = lambda *ts: last(*ts) is not None
    dates = lambda *ts: "、".join(str(e.get("date") or "") for e in events if e.get("type") in ts and e.get("date"))[:60]

    def kw(*words: str) -> bool:
        tail = " ".join(str(v.get("type_cn") or "") + str(v.get("desc") or "")
                        for v in (vsa or [])[-14:] if isinstance(v, dict))
        return any(w in tail for w in words)

    acc = float(phase.get("acc_score") or 0)
    dist = float(phase.get("dist_score") or 0)
    net = float((decision.get("score") or {}).get("net") or 0.0)
    m_dir = str((market.get("trend") or {}).get("dir") or "sideways")
    tgt_up = float(cause.get("up_target") or 0)
    entry = decision.get("entry") or {}
    stop = decision.get("stop") or {}
    ideal = float(entry.get("ideal") or 0)
    stopp = float(stop.get("price") or 0)
    rr = (tgt_up - ideal) / (ideal - stopp) if ideal > 0 and stopp > 0 and ideal > stopp else 0.0

    lps_p = [float(e["price"]) for e in events if e.get("type") in ("LPS", "BU") and e.get("price")]
    lpsy_p = [float(e["price"]) for e in events if e.get("type") in ("LPSY", "UT") and e.get("price")]
    rng_len = (rng.end_index - rng.start_index) if rng and rng.valid else 0

    buy = [
        {"name": "① 目的与因果：区间为吸筹、上涨目标空间充足",
         "passed": bool(rng.valid and rr >= 2.0),
         "note": (f"横盘区间约 {rng_len} 根K线" if rng.valid else "未确认有效吸筹区间") +
                 (f"，因果量度目标 {tgt_up:.2f}，风险收益比 {rr:.1f}" if tgt_up else "，暂无量度目标")},
        {"name": "② 卖出高潮与初现支撑（SC/PS）",
         "passed": have("SC", "PS", "Spring", "Shakeout"),
         "note": ("出现于 " + dates("SC", "PS", "Spring", "Shakeout")) if have("SC", "PS", "Spring", "Shakeout") else "未发现卖出高潮/初现支撑/震仓事件"},
        {"name": "③ 自动反弹后的缩量二次测试（AR/ST）",
         "passed": have("AR", "ST", "Test"),
         "note": ("AR、ST 出现于 " + dates("AR", "ST", "Test")) if have("AR", "ST", "Test") else "未发现 AR/ST 序列"},
        {"name": "④ 强势信号突破小溪（SOS）",
         "passed": have("SOS"),
         "note": ("最近 SOS：" + dates("SOS")) if have("SOS") else "尚未出现放量突破区间的强势信号"},
        {"name": "⑤ 回踩获支撑（LPS/Spring 收回）",
         "passed": have("LPS", "BU", "Spring"),
         "note": ("低点获支撑：" + dates("LPS", "BU", "Spring")) if have("LPS", "BU", "Spring") else "突破后暂无回踩确认"},
        {"name": "⑥ 高点低点抬高（结构转强）",
         "passed": len(lps_p) >= 2 and lps_p[-1] > lps_p[-2],
         "note": (f"最近两个支撑点 {lps_p[-2]:.2f} → {lps_p[-1]:.2f}，逐级抬高" if len(lps_p) >= 2 else "支撑点不足两个，结构未确认")},
        {"name": "⑦ 供应枯竭（无供给/缩量回落）",
         "passed": kw("无供给", "卖压枯竭", "供应枯竭"),
         "note": "最近量价信号中出现供应枯竭迹象" if kw("无供给", "卖压枯竭", "供应枯竭") else "近期回落仍有供给痕迹或证据不足"},
        {"name": "⑧ 个股相对强度（吸筹分高于派发分）",
         "passed": acc > dist,
         "note": f"吸筹证据 {acc:.0f} 分 / 派发证据 {dist:.0f} 分"},
        {"name": "⑨ 大盘背景配合",
         "passed": m_dir in ("up", "sideways"),
         "note": {"up": "大盘上升趋势", "down": "大盘下降趋势（拖累）", "sideways": "大盘横盘（中性）"}.get(m_dir, "大盘未知")},
    ]

    tg = (decision.get("targets") or [])
    tgt_dn = float(tg[-1].get("price") or 0) if tg else 0
    rr_dn = (ideal - tgt_dn) / (stopp - ideal) if ideal > 0 and stopp > ideal and tgt_dn > 0 else 0.0

    sell = [
        {"name": "① 目的与因果：区间为派发、下跌空间充足",
         "passed": bool(rng.valid and rr_dn >= 2.0),
         "note": (f"横盘区间约 {rng_len} 根K线" if rng.valid else "未确认有效派发区间") +
                 (f"，下方量度目标 {tgt_dn:.2f}，风险收益比 {rr_dn:.1f}" if tgt_dn else "，暂无下方目标")},
        {"name": "② 买入高潮与初现供应（BC/UT）",
         "passed": have("BC", "UT", "UTAD"),
         "note": ("出现于 " + dates("BC", "UT", "UTAD")) if have("BC", "UT", "UTAD") else "未发现买入高潮/上冲回落"},
        {"name": "③ 反弹无力的二次测试（需求枯竭）",
         "passed": have("LPSY", "UT") or kw("无需求", "需求枯竭", "反弹无力"),
         "note": ("LPSY/UT：" + dates("LPSY", "UT")) if have("LPSY", "UT") else ("量价信号显示反弹无力" if kw("无需求", "需求枯竭", "反弹无力") else "未发现需求枯竭证据")},
        {"name": "④ 弱势信号跌破冰线（SOW）",
         "passed": have("SOW"),
         "note": ("最近 SOW：" + dates("SOW")) if have("SOW") else "尚未出现跌破冰线的强势弱势信号"},
        {"name": "⑤ 反弹受阻（LPSY/上冲回落）",
         "passed": have("LPSY", "UTAD"),
         "note": ("受阻证据：" + dates("LPSY", "UTAD")) if have("LPSY", "UTAD") else "暂无反弹受阻确认"},
        {"name": "⑥ 高点低点降低（结构转弱）",
         "passed": len(lpsy_p) >= 2 and lpsy_p[-1] < lpsy_p[-2],
         "note": (f"最近两个反弹高点 {lpsy_p[-2]:.2f} → {lpsy_p[-1]:.2f}，逐级降低" if len(lpsy_p) >= 2 else "反弹高点不足两个，结构未确认")},
        {"name": "⑦ 需求枯竭（无需求/缩量反弹）",
         "passed": kw("无需求", "需求枯竭", "反弹无力"),
         "note": "最近量价信号显示需求枯竭" if kw("无需求", "需求枯竭", "反弹无力") else "近期反弹仍有需求痕迹或证据不足"},
        {"name": "⑧ 个股相对强度（派发分高于吸筹分）",
         "passed": dist > acc,
         "note": f"派发证据 {dist:.0f} 分 / 吸筹证据 {acc:.0f} 分"},
        {"name": "⑨ 大盘背景配合",
         "passed": m_dir == "down",
         "note": {"down": "大盘下降趋势（助推下跌）", "up": "大盘上升趋势（不利看空）", "sideways": "大盘横盘（中性）"}.get(m_dir, "大盘未知")},
    ]

    return {
        "buy": buy, "sell": sell,
        "buy_passed": sum(1 for t in buy if t["passed"]),
        "sell_passed": sum(1 for t in sell if t["passed"]),
    }


_PT_META = {
    "sym_triangle":  ("收敛三角形", "SYM △", "neutral"),
    "asc_triangle":  ("上升三角形", "ASC △", "bull"),
    "desc_triangle": ("下降三角形", "DESC △", "bear"),
    "rising_wedge":  ("上升楔形", "R.WEDGE", "bear"),
    "falling_wedge": ("下降楔形", "F.WEDGE", "bull"),
    "rectangle":     ("矩形整理", "BOX", "neutral"),
    "bull_flag":     ("上升旗形", "BULL FLAG", "bull"),
    "bear_flag":     ("下降旗形", "BEAR FLAG", "bear"),
}


def _candle_patterns(bars: list, lookback: int = 60) -> list:
    """多根K线组合形态（2-3根经典组合：吞没/启明星/红三兵/镊子线/孕线等）。

    扫描最近 lookback 根，覆盖近期走势中的组合信号；每项给出
    {label:英文缩写(图面用), name_cn, date, index, indexes:[参与K线], bullish, note:决策含义}。
    坐标为传入 bars 的局部下标，由调用方映射；按时间升序返回。
    """
    n = len(bars)
    if n < 10:
        return []
    o = [float(b.open) for b in bars]
    c = [float(b.close) for b in bars]
    h = [float(b.high) for b in bars]
    l = [float(b.low) for b in bars]
    rng_ = [max(h[i] - l[i], 1e-9) for i in range(n)]
    body = [abs(c[i] - o[i]) for i in range(n)]
    trs = [h[0] - l[0]] + [max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
                           for i in range(1, n)]
    atr = [sum(trs[max(0, i - 13): i + 1]) / len(trs[max(0, i - 13): i + 1]) for i in range(n)]

    out = []
    start = max(2, n - lookback)
    for i in range(start, n):
        d = getattr(bars[i], "date", "")
        a = atr[i] or 1e-9
        p, q = i - 1, i - 2
        bull_i, bear_i = c[i] > o[i], c[i] < o[i]
        bull_p, bear_p = c[p] > o[p], c[p] < o[p]
        # ① 阳包阴 / 阴包阳（吞没）
        if bear_p and bull_i and o[i] <= c[p] and c[i] > o[p] and body[i] >= max(body[p], 0.35 * a):
            out.append(dict(label="B.ENG", name_cn="阳包阴（看涨吞没）", date=d, index=i,
                            indexes=[p, i], bullish=True,
                            note="多头长阳完全反包前一根阴线，买方夺回主动；次日不破包线低点可短线跟进，收盘跌回阴线低点则失效"))
        if bull_p and bear_i and o[i] >= c[p] and c[i] < o[p] and body[i] >= max(body[p], 0.35 * a):
            out.append(dict(label="S.ENG", name_cn="阴包阳（看跌吞没）", date=d, index=i,
                            indexes=[p, i], bullish=False,
                            note="空头长阴完全反包前一根阳线，卖方夺回主动；次日不收复包线高点应减仓，收盘收回阳线高点则失效"))
        # ② 启明星 / 黄昏星（三根）
        if body[q] >= 0.55 * a and body[p] <= 0.4 * body[q]:
            mid_q = (o[q] + c[q]) / 2
            if c[q] < o[q] and bull_i and c[i] > mid_q and l[p] <= min(l[q], l[i]) * 1.003:
                out.append(dict(label="M.STAR", name_cn="启明星（底部反转）", date=d, index=i,
                                indexes=[q, p, i], bullish=True,
                                note="急跌—缩量止跌—长阳收复半分位，经典底部反转三连；回踩星线低点不破可低吸，跌破即失败"))
            if c[q] > o[q] and bear_i and c[i] < mid_q and h[p] >= max(h[q], h[i]) * 0.997:
                out.append(dict(label="E.STAR", name_cn="黄昏星（顶部反转）", date=d, index=i,
                                indexes=[q, p, i], bullish=False,
                                note="急涨—缩量滞涨—长阴跌破半分位，经典顶部反转三连；反抽星线高点不过应离场，收复即失败"))
        # ③ 刺透线 / 乌云盖顶
        if bear_p and body[p] >= 0.55 * a and bull_i and o[i] <= c[p] and (o[p] + c[p]) / 2 < c[i] < o[p]:
            out.append(dict(label="PIER", name_cn="刺透线（看涨）", date=d, index=i,
                            indexes=[p, i], bullish=True,
                            note="低开长阳刺入前阴实体过半，下跌动能衰竭；站稳刺入位之上看反弹，跌破前低则失败"))
        if bull_p and body[p] >= 0.55 * a and bear_i and o[i] >= c[p] and o[p] > c[i] > (o[p] + c[p]) / 2:
            out.append(dict(label="DCC", name_cn="乌云盖顶（看跌）", date=d, index=i,
                            indexes=[p, i], bullish=False,
                            note="高开长阴切入前阳实体过半，上攻动能衰竭；跌破切线低点减仓，收复前高则失效"))
        # ④ 红三兵 / 三只乌鸦
        if (bull_i and c[q] > o[q] and c[p] > o[p] and c[i] > c[p] > c[q]
                and all(body[k] >= 0.45 * rng_[k] for k in (q, p, i))
                and o[q] < o[p] <= c[q] and o[p] < o[i] <= c[p]):
            out.append(dict(label="3SOLD", name_cn="红三兵（强势推进）", date=d, index=i,
                            indexes=[q, p, i], bullish=True,
                            note="连续三根实体阳线逐级抬升，需求持续压倒供给；持股者持有，回踩第二根阳线低点不破可加仓，放量长上影再减"))
        if (bear_i and c[q] < o[q] and c[p] < o[p] and c[i] < c[p] < c[q]
                and all(body[k] >= 0.45 * rng_[k] for k in (q, p, i))
                and o[q] > o[p] >= c[q] and o[p] > o[i] >= c[p]):
            out.append(dict(label="3CROW", name_cn="三只乌鸦（弱势推进）", date=d, index=i,
                            indexes=[q, p, i], bullish=False,
                            note="连续三根实体阴线逐级下压，供给持续压制需求；空仓者不抄底，反抽第二根阴线高点不过继续规避"))
        # ⑤ 双针探底 / 平顶镊子
        if abs(l[i] - l[p]) <= 0.2 * a and bear_p and bull_i and (min(o[i], c[i]) - l[i]) >= 0.4 * rng_[i]:
            out.append(dict(label="TWZ-B", name_cn="双针探底（支撑确认）", date=d, index=i,
                            indexes=[p, i], bullish=True,
                            note="两根K线低点几乎相同且收出下影，下方支撑被反复测试有效；守住双针低点看反弹，收盘跌破低点支撑失效"))
        if abs(h[i] - h[p]) <= 0.2 * a and bull_p and bear_i and (h[i] - max(o[i], c[i])) >= 0.4 * rng_[i]:
            out.append(dict(label="TWZ-T", name_cn="平顶镊子（压力受阻）", date=d, index=i,
                            indexes=[p, i], bullish=False,
                            note="两根K线高点几乎相同且收出上影，上方压力被反复确认；突破双针高点看涨，否则逢高减仓"))
        # ⑥ 孕线（蓄势变盘）
        if (body[p] >= 0.6 * a and body[i] <= 0.5 * body[p]
                and min(o[i], c[i]) > min(o[p], c[p]) and max(o[i], c[i]) < max(o[p], c[p])):
            out.append(dict(label="HRM", name_cn="孕线（缩量蓄势）", date=d, index=i,
                            indexes=[p, i], bullish=bear_p,
                            note="大实体后收出被完全包含的小实体，多空暂时休整、方向待选择；向上放量突破大实体高点跟进，向下跌破大实体低点离场"))

    # 同类形态相邻重复时只保留最后一处（避免连续多日同信号刷屏）
    filtered = []
    for it in out:
        if filtered and filtered[-1]["label"] == it["label"] and it["index"] - filtered[-1]["index"] <= 2:
            filtered[-1] = it
        else:
            filtered.append(it)
    return filtered[-24:]


def _chart_patterns(bars: list) -> list:
    """识别当前仍在运行（或刚突破）的经典K线整理形态。

    取最近窗口内 2-3 个摆动高/低点做最小二乘拟合，得到上/下边界线，
    按两线斜率组合判定：收敛三角形、上升/下降三角形、上升/下降楔形、
    矩形、上升/下降旗形。返回边界坐标与书中式说明，供K线图绘制与时间线解读。
    坐标空间为传入 bars 的局部下标，由调用方做偏移映射。
    """
    n = len(bars)
    if n < 45:
        return []
    win = 130
    s0 = max(0, n - win)
    sub = bars[s0:]
    m = len(sub)
    sw = swings(sub, 3)
    highs = sorted((p for p in sw if p["kind"] == "high"), key=lambda p: p["index"])
    lows = sorted((p for p in sw if p["kind"] == "low"), key=lambda p: p["index"])
    if len(highs) < 2 or len(lows) < 2:
        return []
    hu, ll = highs[-3:], lows[-3:]

    def fit(pts):
        q = len(pts)
        sx = sum(p["index"] for p in pts)
        sy = sum(p["price"] for p in pts)
        sxx = sum(p["index"] * p["index"] for p in pts)
        sxy = sum(p["index"] * p["price"] for p in pts)
        den = q * sxx - sx * sx
        if not den:
            return 0.0, 0.0
        a = (q * sxy - sx * sy) / den
        return a, (sy - a * sx) / q

    au, bu = fit(hu)
    al, bl = fit(ll)
    i0 = min(hu[0]["index"], ll[0]["index"])
    i1 = m - 1
    span = i1 - i0
    if span < 25:  # 形态至少覆盖25根K线，否则只是噪音
        return []
    mid0 = ((au * i0 + bu) + (al * i0 + bl)) / 2
    if mid0 <= 0:
        return []
    ku, kl = au / mid0 * 100, al / mid0 * 100   # 每根K线的斜率（%），消除价格量纲
    THR = 0.05
    up_e, lo_e = au * i1 + bu, al * i1 + bl
    if up_e <= lo_e:                              # 边界已交叉，形态早已失效
        return []
    close = sub[-1].close
    pos = (close - lo_e) / (up_e - lo_e) if up_e > lo_e else 0.5
    if pos < -1.0 or pos > 1.35:                 # 跌破下沿过深（>1倍通道宽）才丢弃，刚破位仍标注
        return []
    conv = al > au                               # 数学收敛：下沿斜率大于上沿
    apex = (bu - bl) / (al - au) if abs(al - au) > 1e-9 else 1e9
    flat_u, flat_l = abs(ku) <= THR, abs(kl) <= THR
    typ = None
    if ku < -THR and kl > THR and conv:
        typ = "sym_triangle"
    elif flat_u and kl > THR:
        typ = "asc_triangle"
    elif ku < -THR and flat_l:
        typ = "desc_triangle"
    elif ku > THR and kl > THR and conv:
        typ = "rising_wedge"
    elif ku < -THR and kl < -THR and conv:
        typ = "falling_wedge"
    elif flat_u and flat_l:
        typ = "rectangle"
    else:
        pre_i = max(0, i0 - 30)
        pre_close = sub[pre_i].close
        if abs(ku - kl) < 0.04 and ku < -THR and sub[i0].close >= pre_close * 1.08:
            typ = "bull_flag"    # 急涨后小幅向下平行整理
        elif abs(ku - kl) < 0.04 and ku > THR and sub[i0].close <= pre_close * 0.92:
            typ = "bear_flag"    # 急跌后小幅向上平行整理
    if not typ:
        return []
    # 三角形/楔形要求收敛点在未来且不过远；矩形/旗形不要求
    if typ.endswith("triangle") or typ.endswith("wedge"):
        if apex <= i1 or apex - i1 > 120:
            return []

    name_cn, label, bias = _PT_META[typ]
    breakout = "up" if close > up_e * 1.006 else ("down" if close < lo_e * 0.994 else None)
    if breakout == "up" and pos > 1.20:
        return []
    if breakout == "down" and pos < -1.0:
        return []
    pos_txt = ("已贴近/突破上沿" if pos >= 0.9 else
               "已贴近/跌破下沿" if pos <= 0.1 else
               f"位于形态内部 {pos * 100:.0f}% 分位（0=下沿，1=上沿）")
    up_s, lo_s = f"{up_e:.2f}", f"{lo_e:.2f}"
    notes = {
        "sym_triangle": f"高点逐步降低、低点逐步抬高，两条边界不断收敛——多空力量在收敛末端必将选择方向。{pos_txt}。操作纪律：放量突破上沿 {up_s}（量比大于1.5倍）后回踩不破再跟进；放量跌破下沿 {lo_s} 则形态失败，转空观望。收敛三角形本身中性，方向由突破确认。",
        "asc_triangle": f"水平上沿 {up_s} 反复受压，但低点不断抬高——每次回落都有资金在更高位承接，属偏多形态。{pos_txt}。放量突破水平上沿 {up_s} 确认涨势；若反而放量跌破下沿 {lo_s}，形态失效。",
        "desc_triangle": f"水平下沿 {lo_s} 反复获得支撑，但高点不断降低——反弹力度一次比一次弱，属偏空形态。{pos_txt}。放量跌破水平下沿 {lo_s} 确认跌势；若放量站上上沿 {up_s} 则失效。",
        "rising_wedge": f"高点低点同步抬高，但下沿斜率更陡、两线收敛——上涨在收敛中耗尽力量，是看跌形态（尤其出现在一轮上涨之后）。{pos_txt}。纪律：不在楔形内追多；放量跌破下沿 {lo_s} 常引发快速回落。",
        "falling_wedge": f"高点低点同步下移，但两线收敛、抛压逐步衰竭，是看涨形态（尤其出现在一轮下跌之后）。{pos_txt}。放量突破上沿 {up_s} 为反转确认信号；继续放量跌破 {lo_s} 则形态失败。",
        "rectangle": f"价格在 {lo_s}～{up_s} 的水平箱体内往返，多空暂时均衡。{pos_txt}。箱体无方向，突破方向决定性质：放量向上突破 {up_s} 跟进，放量向下跌破 {lo_s} 离场；箱体内沿边操作、不追中间。",
        "bull_flag": f"一轮急涨之后出现窄幅、略微向下倾斜的平行整理（旗面）——上涨途中的多头蓄势，通常向上延续。{pos_txt}。放量突破旗面上沿 {up_s} 可跟进；跌回旗面下沿 {lo_s} 下方则蓄势失败。",
        "bear_flag": f"一轮急跌之后出现窄幅、略微向上倾斜的平行整理（旗面）——下跌途中的空头蓄势，通常向下延续。{pos_txt}。放量跌破旗面下沿 {lo_s} 续跌；站回上沿 {up_s} 上方则形态失效。",
    }
    if breakout:
        notes[typ] += f"　最新收盘已{'向上突破' if breakout == 'up' else '向下跌破'}边界，需随后K线回抽确认（回抽不破边界＝有效突破，可顺势；快速收回形态内＝假突破）。"
    return [{
        "type": typ, "name_cn": name_cn, "label": label, "bias": bias,
        "start_index": i0 + s0, "end_index": i1 + s0,
        "start_date": sub[i0].date, "end_date": sub[i1].date,
        "upper": [[i0 + s0, round(au * i0 + bu, 2)], [i1 + s0, round(up_e, 2)]],
        "lower": [[i0 + s0, round(al * i0 + bl, 2)], [i1 + s0, round(lo_e, 2)]],
        "upper_price": round(up_e, 2), "lower_price": round(lo_e, 2),
        "breakout": breakout, "note": notes[typ],
    }]


def _minor_trends(bars: list, win: int = 260, keep: int = 4) -> list:
    """次级趋势段（小趋势）：识别「高点与低点同步抬高/降低」的波段趋势并给出趋势线。

    以摆动点(k=3)构造交替锯齿枢轴（zigzag），同一枢轴链上高点与低点同时
    单调延伸（上升：逐个抬高的低点+逐个抬高的高点；下降：镜像），链长
    ≥4个枢轴（≥2个完整摆动）才构成一段次级趋势。上升趋势段沿段内低点
    最小二乘拟合支撑线，下降趋势段沿段内高点拟合压力线；最后一段若贴着
    最新K线（趋势仍在运行）则延伸到最新K线。保留最近 keep 段，过滤
    振幅<2% 的噪音链。坐标为传入 bars 的局部下标，由调用方做偏移映射。
    """
    n = len(bars)
    if n < 60:
        return []
    s0 = max(0, n - win)
    sub = bars[s0:]
    m = len(sub)
    sw = swings(sub, 3)
    if len(sw) < 4:
        return []
    # 交替锯齿枢轴：同类枢轴保留更极端者，异类直接追加
    piv: list[dict] = []
    for p in sw:
        if not piv or piv[-1]["kind"] != p["kind"]:
            piv.append(dict(p))
        elif (p["kind"] == "high" and p["price"] >= piv[-1]["price"]) or \
             (p["kind"] == "low" and p["price"] <= piv[-1]["price"]):
            piv[-1] = dict(p)
    if len(piv) < 4:
        return []

    def mono(seq: list[dict], d: int) -> bool:
        vals = [p["price"] for p in seq]
        return all((b > a) if d == 1 else (b < a) for a, b in zip(vals, vals[1:]))

    # 扫描趋势链：从枢轴 i 出发，同向枢轴（相隔2个）持续更极端则延伸
    runs: list[tuple[int, int, int]] = []   # (起枢轴, 止枢轴, 方向 +1升/-1降)
    i = 0
    P = len(piv)
    while i < P - 3:
        d = 1 if piv[i + 2]["price"] > piv[i]["price"] else \
            (-1 if piv[i + 2]["price"] < piv[i]["price"] else 0)
        j = i
        if d:
            while j + 2 < P and ((d == 1 and piv[j + 2]["price"] > piv[j]["price"]) or
                                 (d == -1 and piv[j + 2]["price"] < piv[j]["price"])):
                j += 2
        if j - i >= 3:
            hs = [p for p in piv[i:j + 1] if p["kind"] == "high"]
            ls = [p for p in piv[i:j + 1] if p["kind"] == "low"]
            # 趋势要求高、低两类枢轴同步单调（排除扩張/收敛等只单边抬升的链）
            if len(hs) >= 2 and len(ls) >= 2 and mono(hs, d) and mono(ls, d):
                runs.append((i, j, d))
                i = j            # 反转枢轴作为下一段的候选起点
                continue
        i += 1
    if not runs:
        return []
    last_end = runs[-1][1]

    def fit(pts):
        q = len(pts)
        sx = sum(p["index"] for p in pts)
        sy = sum(p["price"] for p in pts)
        sxx = sum(p["index"] * p["index"] for p in pts)
        sxy = sum(p["index"] * p["price"] for p in pts)
        den = q * sxx - sx * sx
        if not den:
            return 0.0, 0.0
        a = (q * sxy - sx * sy) / den
        return a, (sy - a * sx) / q

    out: list[dict] = []
    for (i0, j0, d) in runs[-keep:]:
        anchors = [p for p in piv[i0:j0 + 1]
                   if p["kind"] == ("low" if d == 1 else "high")]
        if len(anchors) < 2:
            continue
        a, b = fit(anchors)
        end_i = piv[j0]["index"]
        # 最后一段且末枢轴贴近最新K线 → 趋势仍在运行，延伸到最新K线
        if (i0, j0, d) == runs[-1] and (m - 1 - end_i) <= 6:
            end_i = m - 1
        if end_i - anchors[0]["index"] < 8:   # 过短只是局部噪音
            continue
        p0 = a * anchors[0]["index"] + b
        p1 = a * end_i + b
        if d == 1:
            ext0 = min(p["price"] for p in piv[i0:j0 + 1])
            ext1 = max(p["price"] for p in piv[i0:j0 + 1])
        else:
            ext0 = max(p["price"] for p in piv[i0:j0 + 1])
            ext1 = min(p["price"] for p in piv[i0:j0 + 1])
        chg = (ext1 / ext0 - 1) * 100 if ext0 else 0.0
        if abs(chg) < 2.0:                    # 振幅过小的链没有趋势意义
            continue
        lo_s, hi_s = f"{min(ext0, ext1):.2f}", f"{max(ext0, ext1):.2f}"
        if d == 1:
            note = (f"次级上升趋势：低点自 {lo_s} 起逐级抬高（段内振幅 +{chg:.1f}%），"
                    f"回调低点不断上移＝需求控制局面。沿线（TL-UP）为动态支撑，"
                    f"缩量回踩不破可顺势低吸；放量收盘跌破趋势线则趋势转弱，减仓观望。")
        else:
            note = (f"次级下降趋势：高点自 {hi_s} 起逐级降低（段内振幅 {chg:.1f}%），"
                    f"反弹高点不断下移＝供给持续压制。沿线（TL-DN）为动态压力，"
                    f"反弹到线附近缩量滞涨宜减仓；放量收盘升破趋势线则下跌动能衰竭，转观察。")
        out.append({
            "kind": "up" if d == 1 else "dn",
            "label": "TL-UP" if d == 1 else "TL-DN",
            "name_cn": "上升趋势段" if d == 1 else "下降趋势段",
            "start_index": anchors[0]["index"] + s0,
            "end_index": end_i + s0,
            "start_date": sub[anchors[0]["index"]].date,
            "end_date": sub[end_i].date,
            "line": [[anchors[0]["index"] + s0, round(p0, 2)], [end_i + s0, round(p1, 2)]],
            "chg_pct": round(chg, 1),
            "note": note,
        })
    return out


def _key_candles(disp_a: list, ind_a) -> list:
    """《新威科夫操盘法》案例式关键K线解读。

    逐根扫描最近40根，用「量比 × 波动幅度 × 收盘位置」识别书中反复出现的关键行为：
    高量窄幅（努力无结果，书中B案例）、创新高但收盘背离（C案例）、回落中首次放量（D案例）、
    缩量推进（需求不足/供应枯竭）、放量趋势K线（努力有结果）。
    每根给出编号/量比/行为判定/决策含义，编号与K线图上的圆圈标注一一对应。
    """
    out: list[dict] = []
    n = len(disp_a)
    if n < 5:
        return out
    start = max(0, n - 40)
    atrs = getattr(ind_a, "atr", []) or []

    def vbase(k: int) -> float:
        seg = disp_a[max(0, k - 20):k]
        return (sum(float(x.volume or 0) for x in seg) / len(seg)) if seg else 0.0

    tested_src = set()  # 同一弹簧/需求柱只报第一次测试（书：首次低量测试即完成证明，其后是跟随）
    for i in range(start, n):
        b = disp_a[i]
        vol = float(b.volume or 0)
        if vol <= 0:
            continue
        base_v = vbase(i)
        if base_v <= 0:
            continue
        vr = vol / base_v
        body = abs(b.close - b.open)
        spread = max(b.high - b.low, 1e-9)
        atr = float(atrs[i] or 0) or spread
        w = i - 60 if i - 60 >= 0 else 0
        seg_hi = max(x.high for x in disp_a[w:i + 1])
        seg_lo = min(x.low for x in disp_a[w:i + 1])
        pos = (b.close - seg_lo) / max(seg_hi - seg_lo, 1e-9)
        up_bar = b.close >= b.open
        close_pos = (b.close - b.low) / spread
        title = None
        text = ""
        tone = "flat"

        # ⓪ 共性一：对前期弹簧/垂直需求柱的二次测试——「只有低量短K测试才证明供应耗尽」
        if i >= start + 1:
            for j in range(max(start, i - 3), i):
                pj = disp_a[j]
                vrj = float(pj.volume or 0) / max(vbase(j), 1e-9)
                pj_spread = max(pj.high - pj.low, 1e-9)
                pj_low8 = min(x.low for x in disp_a[max(0, j - 8):j]) if j > start else None
                is_spring = pj_low8 is not None and pj.low <= pj_low8 and (pj.close - pj.low) / pj_spread > 0.55
                pj_atr = float(atrs[j] or 0) or pj_spread
                is_pole = pj.close > pj.open and abs(pj.close - pj.open) >= 1.2 * pj_atr and vrj >= 1.5
                if not (is_spring or is_pole):
                    continue
                if j in tested_src:
                    continue
                level = pj.low
                pullback = b.low <= level + 1.2 * atr and (b.close < b.open or b.close < disp_a[i - 1].close)
                if not pullback:
                    continue
                kind = "弹簧低点" if is_spring else "需求柱底部"
                if vr < 0.8 and spread <= 0.75 * atr:
                    title, tone = "低量短K二次测试（供应耗尽证明）", "up"
                    text = (f"回踩 {str(getattr(pj, 'date', '') or '')} {kind} {level:.2f} 时缩量（{vr:.1f} 倍）且K线短小——"
                            f"测试量缩于高潮量——「测试应该是大幅减小的K线和成交量并保持高支撑，"
                            f"这使熊市终止的理论根据成立」（书中 p50），此处即书中的进场参考点")
                elif vr >= 1.15:
                    title, tone = "回踩带量（浮动供应仍在）", "down"
                    text = f"回踩 {kind} {level:.2f} 时成交量 {vr:.1f} 倍——浮动供应未耗尽：「第一次测试成交量扩大，我们不能进场，需要等待进一步的二次测试，直到确认供应已经耗尽」（书中 p53）"
                if title:
                    tested_src.add(j)
                    break

        # ⓪b 共性二：停止行为（下跌幅度缩小但成交量放大 → 需求进入、市场刹车）
        if title is None and i >= start + 2:
            drop_prev = disp_a[i - 2].close - disp_a[i - 1].close
            drop_now = disp_a[i - 1].close - b.close
            vr_prev = float(disp_a[i - 1].volume or 0) / max(vbase(i - 1), 1e-9)
            if drop_prev > 0 and 0 < drop_now < drop_prev * 0.8 and vr >= 1.25 and vr > vr_prev:
                title, tone = "停止行为（需求进入刹车）", "up"
                text = (f"下跌幅度缩小（{drop_prev:.2f} → {drop_now:.2f}）但成交量反而放大（{vr:.1f} 倍 > 前一日 {vr_prev:.1f} 倍）——"
                        f"这是需求进入的结果，下跌动能衰竭，关注是否止跌企稳")

        # ⓪c 共性三：SOT 上推减弱（涨幅缩小 + 低量/上影 → 需求不足将回落）
        if title is None and i >= start + 2:
            gain_prev = disp_a[i - 1].close - disp_a[i - 2].close
            gain_now = b.close - disp_a[i - 1].close
            if gain_prev > 0 and 0 < gain_now < gain_prev * 0.75 and (vr < 0.85 or close_pos < 0.62):
                title, tone = "上推减弱 SOT（需求不足）", "down"
                text = (f"上涨幅度缩小（{gain_prev:.2f} → {gain_now:.2f}）"
                        + ("且缩量" if vr < 0.85 else "且留有上影线（收盘位置偏低）")
                        + "——需求不足以延续涨势，预计价格回落")

        if vr >= 1.6 and body <= 0.45 * spread and body <= 0.6 * atr:
            # 高量窄幅：努力无结果（书中B案例的隐蔽危险行为）
            if pos >= 0.62:
                title, tone = "高量窄幅（努力无结果）", "down"
                text = (f"成交量 {vr:.1f} 倍于均量但波幅仅 {body / atr:.2f}×ATR。"
                        + ("长上影冲高回落：公众买单被专家接走——高位派发嫌疑，继续观察出货是否再现" if close_pos < 0.55
                           else "高位大换手而价格推不动：卖单有人接，留意顶部形成"))
            elif pos <= 0.38:
                title, tone = "高量窄幅（卖压被吸收）", "up"
                text = f"低位放量 {vr:.1f} 倍但跌幅收不住——卖单被专家吸走（吸筹痕迹），观察是否不再创新低"
        if title is None and i > w + 5 and b.high >= seg_hi - 1e-9 and close_pos < 0.4 and not up_bar:
            title, tone = "创新高但收盘疲弱", "down"
            text = f"盘中创新高但收在K线下三分之一——强势表象下供应进场（弱势信号），次日需求跟不上则警惕回落"
        if title is None and i > w + 5 and b.low <= seg_lo + 1e-9 and close_pos > 0.6 and up_bar:
            title, tone = "创新低但强力收回", "up"
            text = "盘中创新低后收在K线上三分之一——低位出现需求，Spring 式收回，供应可能枯竭"
        if title is None and i >= start + 2:
            p1, p2 = disp_a[i - 1], disp_a[i - 2]
            down_seq = p1.close < p1.open and p2.close < p2.open
            up_seq = p1.close > p1.open and p2.close > p2.open
            if down_seq and not up_bar and vr >= 1.4:
                title, tone = "回落中首次放量", "down"
                text = f"连续回落中第一次放量（{vr:.1f} 倍）——供应大于需求的明确表示，迅速套住追高的公众，回调可能加深"
            elif up_seq and up_bar and vr >= 1.4:
                title, tone = "反弹中首次放量", "up"
                text = f"连续上涨中第一次放量（{vr:.1f} 倍）——需求进场确认，突破有效或行情加速"
        if title is None and i >= start + 3:
            v1, v2, v3 = (float(disp_a[j].volume or 0) for j in (i - 2, i - 1, i))
            rising = b.close > disp_a[i - 1].close > disp_a[i - 2].close
            falling = b.close < disp_a[i - 1].close < disp_a[i - 2].close
            if rising and v1 > v2 > v3 and vr < 0.9:
                title, tone = "连续缩量上涨", "down"
                text = "价升量缩——需求不足，涨势难以持续，警惕无量诱多"
            elif falling and v1 > v2 > v3 and vr < 0.9:
                title, tone = "连续缩量阴跌", "up"
                text = "价跌量缩——供应逐步枯竭，卖压趋于消失，关注下方支撑对测试的反应"
        if title is None and up_bar and body >= 1.2 * atr and vr >= 1.5:
            # 共性四：垂直需求柱——底部即支撑，回踩测试质量决定进场点
            title, tone = "垂直需求柱（底部=支撑）", "up"
            text = (f"放量长阳（{vr:.1f} 倍、实体 {body / atr:.2f}×ATR）——需求完全控制局面。"
                    f"需求柱底部 {b.low:.2f} 为支撑，再次回踩时必须出现低量短K线测试，即是书中的进场点")
        if title is None and vr >= 1.8 and body >= 0.85 * atr:
            if up_bar:
                title, tone = "放量大阳（努力有结果）", "up"
                text = f"成交量 {vr:.1f} 倍且实体 {body / atr:.2f}×ATR——需求完全控制局面，趋势K线"
            else:
                title, tone = "放量大阴（努力有结果）", "down"
                text = f"成交量 {vr:.1f} 倍且实体 {body / atr:.2f}×ATR——供应完全控制局面，趋势K线"

        if title:
            out.append({
                "index": i, "num": len(out) + 1,
                "date": str(getattr(b, "date", "") or ""), "vr": round(vr, 1),
                "title": title, "text": text, "tone": tone,
            })
    return out[-10:]


def _next_watch(rng, tl: dict) -> str:
    """下一观察点：区间上下沿与止损的行为要求（书中「测试必须低量」原则）。"""
    parts = []
    if rng and rng.valid:
        parts.append(f"区间上沿 {rng.high:.2f}（再次测试需放量确认，缩量则警惕上冲回落）")
        parts.append(f"区间下沿/冰线 {rng.low:.2f}（回踩必须缩量，放量跌破离场）")
    if tl and tl.get("stop"):
        parts.append(f"决策止损 {tl['stop']:.2f}")
    return "；".join(parts)


def _shakeout_hint(disp_a: list, ind_a, event_data: dict, rng) -> dict | None:
    """震仓（Shakeout / 震仓洗盘）提示与书中策略。

    震仓 = 主力借跌破关键支撑清理浮筹：快速跌破区间下沿/冰线后收回，或 Spring/震仓事件。
    书中结论：震仓是洗盘而非反转——出现后等缩量测试（Test/LPS）确认再介入，
    止损挂在震仓低点下方；放量跌破震仓低点不收回则判断错误，必须离场。
    """
    n = len(disp_a)
    if n < 10:
        return None
    evs = event_data.get("events", [])
    disp_dates = {str(getattr(b, "date", "") or "") for b in disp_a}
    ev = None
    for e in evs:
        if e.get("type") in ("Spring", "Shakeout") and str(e.get("date") or "") in disp_dates:
            ev = e  # 取窗口内最新的一个
    kind = date_s = low_px = vr = None
    if ev:
        kind = str(ev.get("type") or "")
        date_s = str(ev.get("date") or "")
        low_px = float(ev.get("price") or 0) or None
        vr = float(ev.get("volume_ratio") or 0) or None
    else:
        # 行为层检测：最近25根内跌破区间下沿/冰线（或近20根低点）后 2 根内收回，且至今守住
        start = max(0, n - 25)
        for k in range(start, n - 1):
            b = disp_a[k]
            level = float(rng.low) if (rng and rng.valid) else min(x.low for x in disp_a[max(0, k - 20):k])
            if b.low >= level:
                continue
            for m in range(k + 1, min(k + 3, n)):
                if disp_a[m].close > level and all(x.close > level for x in disp_a[m:n]):
                    kind = "破位收回"
                    date_s = str(getattr(b, "date", "") or "")
                    low_px = float(b.low)
                    vr = None
                    break
            if kind:
                break
    if not kind:
        return None
    low_txt = f"{low_px:.2f}" if low_px else "--"
    vr_txt = f"（量比 {vr:.1f} 倍）" if vr else ""
    text = (f"{date_s} 出现{kind}{vr_txt}——价格快速跌破关键支撑 {low_txt} 后收回上方，"
            f"属主力震仓洗盘：浮筹被清理、供应做最后释放，不是趋势反转。"
            f"此后行情通常更健康，需观察后续是否出现缩量测试。")
    strategy = (f"① 切勿在震仓低点附近恐慌卖出，震仓是低风险买入区而非卖出信号；"
                f"② 等随后的缩量测试（Test/LPS）确认供应枯竭后再分批介入；"
                f"③ 止损挂在震仓低点 {low_txt} 下方，放量跌破不收回即认错离场；"
                f"④ 震仓后放量收复冰线则属强势确认，可加仓。")
    return {"date": date_s, "kind": kind, "low": round(low_px, 2) if low_px else None,
            "vr": vr, "text": text, "strategy": strategy}


def _latest_candle_read(disp_a: list, ind_a, kc_raw: list, rng, tl: dict) -> dict:
    """最新一根K线的书中方法速读。

    若最新K线已触发关键行为则复用其判定；否则给出中性速读
    （量比×波幅×收盘位置×区间位置 → 行为含义），并附下一观察点。
    """
    n = len(disp_a)
    if n < 5:
        return {}
    b = disp_a[-1]
    seg = disp_a[max(0, n - 20):n - 1]
    base_v = (sum(float(x.volume or 0) for x in seg) / len(seg)) if seg else 0.0
    vr = (float(b.volume or 0) / base_v) if base_v > 0 else 0.0
    atrs = getattr(ind_a, "atr", []) or []
    atr = float(atrs[-1] or 0) or max(b.high - b.low, 1e-9)
    spread = max(b.high - b.low, 1e-9)
    body = abs(b.close - b.open)
    close_pos = (b.close - b.low) / spread
    w = max(0, n - 60)
    seg_hi = max(x.high for x in disp_a[w:])
    seg_lo = min(x.low for x in disp_a[w:])
    pos60 = (b.close - seg_lo) / max(seg_hi - seg_lo, 1e-9)
    prev_c = float(disp_a[-2].close or 0)
    chg = (b.close / prev_c - 1) * 100 if prev_c else 0.0
    up_bar = b.close >= b.open

    for kc in kc_raw:
        if kc.get("index") == n - 1:
            out = dict(kc)
            out["is_key"] = True
            out["chg_pct"] = round(chg, 2)
            out["next_watch"] = _next_watch(rng, tl)
            return out

    tone = "up" if (up_bar and close_pos > 0.6) else ("down" if (not up_bar and close_pos < 0.4) else "flat")
    vol_txt = "放量" if vr >= 1.4 else ("缩量" if vr <= 0.75 else "量能持平")
    pos_txt = "高" if pos60 >= 0.62 else ("低" if pos60 <= 0.38 else "中")
    cp_txt = "收盘偏强（上三分之一）" if close_pos >= 0.6 else ("收盘偏弱（下三分之一）" if close_pos <= 0.4 else "收盘居中")
    text = (f"{'阳线' if up_bar else '阴线'} {chg:+.1f}%，{vol_txt}（{vr:.1f} 倍），实体 {body / atr:.2f}×ATR，{cp_txt}；"
            f"处于近60根区间{pos_txt}位。"
            + {"up": "多头占优但未触发书中关键行为，正常持有观察。",
               "down": "空头占优但未触发书中关键行为，暂不单独作为决策依据。",
               "flat": "多空拉锯的过渡K线，等待方向确认。"}.get(tone, ""))
    return {"index": n - 1, "num": 0, "date": str(getattr(b, "date", "") or ""), "vr": round(vr, 1),
            "title": "最新K线速读", "text": text, "tone": tone,
            "is_key": False, "chg_pct": round(chg, 2), "next_watch": _next_watch(rng, tl)}


def _index_wyckoff(code: str) -> dict:
    """单个指数的威科夫完整结构：趋势 + 阶段 + 区间/关键位/事件/供需/因果。

    阶段判定必须放在大趋势下看：长趋势背景取 750 根长历史
    （年线 MA250、250 根高低分位、500 根斜率），结构与事件分析仍用最近 160 根。
    """
    bars_full = get_bars(code, bars=750, period="day", adjust="qfq")
    bars = bars_full[-160:] if len(bars_full) > 160 else bars_full
    ind = build_indicators(bars)
    trend = trend_of(bars, ind, 60)
    rng = find_range(bars)
    ev = detect(bars, ind, rng)
    vol = volume_stats(bars, ind)
    # 指数自身的威科夫阶段（market 参数传中性值，避免用指数趋势给指数自身循环加分）
    long_ctx = long_trend_of(bars_full, build_indicators(bars_full))
    phase = classify_phase(bars, ind, rng, ev["events"], ev["vsa"],
                           {"text": "", "indices": [], "trend": {"dir": "sideways"}},
                           long_ctx=long_ctx)
    # 相对强弱基础数据：大盘近60根涨跌幅（与个股同期对比，书中 p201-203 必查项）
    seg = bars[-60:] if len(bars) >= 60 else bars
    chg60 = (seg[-1].close / seg[0].close - 1.0) * 100 if len(seg) >= 2 else None
    return {
        "trend": trend,
        "phase": phase,
        "chg60": round(chg60, 2) if chg60 is not None else None,
        "wyckoff": {
            "long_trend": long_ctx,
            "range": rng.to_dict(),
            "levels": key_levels(bars, ind, rng, ev["events"])[:6],
            "events": [{"date": e["date"], "type_cn": e["type_cn"], "bullish": e["bullish"],
                        "price": e["price"], "desc": e["desc"]}
                       for e in sorted(ev["events"], key=lambda x: -x["index"])[:6]],
            "volume_text": vol.get("text", ""),
            "cause": cause_effect(rng, bars[-1].close).get("text", ""),
            "trend_text": trend.get("text", ""),
        },
    }


_NO_INDEX = {"dir": "sideways", "strength": 0, "text": "未获取到指数K线"}


def _market_context() -> dict:
    """大盘环境（自上而下）：三大指数快照 + 上证指数、创业板指的威科夫完整结构。"""
    indices = get_index_quotes()
    parts = [f"{item['name']} {item['price']:.2f}（{item['change_pct']:+.2f}%）" for item in indices]
    sh: dict = {"trend": dict(_NO_INDEX), "phase": None, "wyckoff": {}}
    cyb: dict = {"trend": dict(_NO_INDEX), "phase": None, "wyckoff": {}}
    try:
        sh = _index_wyckoff("sh000001")
    except Exception:  # noqa: BLE001
        pass
    try:
        cyb = _index_wyckoff("sz399006")
    except Exception:  # noqa: BLE001
        pass
    text = ("；".join(parts) if parts else "未获取到大盘指数数据")
    text += f"。上证指数趋势：{TREND_CN.get(sh['trend']['dir'], sh['trend']['dir'])}（{sh['trend']['text']}）"
    if sh.get("phase"):
        text += f"。上证指数威科夫阶段：{sh['phase'].get('phase_cn', '未知')}"
    text += f"。创业板指趋势：{TREND_CN.get(cyb['trend']['dir'], cyb['trend']['dir'])}（{cyb['trend']['text']}）"
    if cyb.get("phase"):
        text += f"。创业板指威科夫阶段：{cyb['phase'].get('phase_cn', '未知')}"
    return {"text": text, "indices": indices,
            "trend": sh["trend"], "phase": sh["phase"], "wyckoff": sh["wyckoff"],
            "chg60": sh.get("chg60"),
            "cyb_trend": cyb["trend"], "cyb_phase": cyb["phase"], "wyckoff_cyb": cyb["wyckoff"]}


INDEX_CHART_CODES = {"sh000001": "上证指数", "sz399006": "创业板指"}
INDEX_CHART_PERIODS = ("day", "week", "month", "year")


def _aggregate_year_bars(rows: list) -> list:
    """月线按自然年聚合：开=首日开盘，收=末日收盘，高/低/量聚合（date 为年份字符串）。

    返回 SimpleNamespace（与 Bar 同样支持 .date/.open 属性访问），保证输出组装逻辑一致。
    """
    from types import SimpleNamespace
    out: list = []
    cur: SimpleNamespace | None = None
    for r in rows:
        y = str(r.date)[:4]
        if cur is None or str(cur.date)[:4] != y:
            if cur is not None:
                out.append(cur)
            cur = SimpleNamespace(date=y, open=r.open, close=r.close,
                                  high=r.high, low=r.low, volume=r.volume)
        else:
            cur.close = r.close
            cur.high = max(cur.high, r.high)
            cur.low = min(cur.low, r.low)
            cur.volume += r.volume
    if cur is not None:
        out.append(cur)
    return out


def _key_lines(rows, period: str = "day") -> dict:
    """任意周期的关键线：供给线 / 需求线 + 交易区间（溪流 Creek / 冰线 Ice）。

    K线根数少的周期（周/月/年线）按根数自适应放宽摆动点与区间窗口，
    保证高周期图同样能画出这几条重要线；数据不足时返回空标注。
    """
    n = len(rows)
    if n < 6:
        return {"trend_lines": [], "range": {"valid": False}}
    k = 3 if n >= 40 else 2
    tls: list[dict] = []
    for kind in ("supply", "demand"):
        t = trend_lines(rows, k).get(kind)
        if t:
            tls.append({"kind": kind, **t})
    if n >= 60:
        min_look, max_look = 18, 140
    else:
        min_look = max(4, min(18, n // 3))
        max_look = max(min_look + 2, n - 2)
    rng = find_range(rows, max_look=max_look, min_look=min_look)
    rd = rng.to_dict()
    if not rd.get("valid") or not rd.get("high") or not rd.get("low"):
        # 高周期常不满足「强区间」判定：改用最近一段的最高/最低作为溪流/冰线的参考位。
        # 窗口按周期给定（周线≈半年、月线≈2年、年线≈8年），避免退化成全历史极值
        # （如年线出现 1990 年 105 点这种对当下没有支撑/压力意义的线）。
        win_len = {"week": 24, "month": 24, "year": 8}.get(period, max(6, n // 3))
        win = rows[-max(6, min(n, win_len)):]
        hi = max(b.high for b in win)
        lo = min(b.low for b in win)
        if hi > 0 and lo > 0 and hi > lo:
            rd = {"valid": False, "high": round(hi, 2), "low": round(lo, 2),
                  "start_index": n - len(win), "end_index": n - 1,
                  "start": str(getattr(win[0], "date", "")),
                  "end": str(getattr(win[-1], "date", ""))}
        else:
            rd = {"valid": False}
    return {"trend_lines": tls, "range": rd}


def index_chart(code: str, bars: int = 160, period: str = "day") -> dict:
    """大盘指数K线图数据：K线 + 威科夫事件 + 供给/需求线 + 交易区间（供前端绘制大盘图，样式与个股图一致）。

    period=day 时附带威科夫事件标注；week/month/year 不标注事件，但同样计算
    「重要的线」——供给线 / 需求线 / 区间上下沿（溪流 Creek、冰线 Ice），
    按该周期自身的K线自适应计算。
    period=year 时取月线并按自然年聚合。
    """
    code = (code or "").strip().lower()
    if code not in INDEX_CHART_CODES:
        raise ValueError(f"仅支持大盘指数代码：{'/'.join(sorted(INDEX_CHART_CODES))}")
    period = period if period in INDEX_CHART_PERIODS else "day"
    n_default = {"day": 160, "week": 200, "month": 240, "year": 3000}
    # K线数量与前端「K线数量」选择器同步（500→500根，9999→全部），未传时用各周期默认值
    n = int(max(30, min(9999, bars or n_default[period])))
    fetch_period = "month" if period == "year" else period
    rows = get_bars(code, bars=n, period=fetch_period, adjust="qfq")
    if period == "year":
        rows = _aggregate_year_bars(rows)
    if len(rows) < 5:
        raise DataSourceError(f"{INDEX_CHART_CODES[code]}K线数据不足（仅 {len(rows)} 根）")
    out = {
        "ok": True, "code": code, "name": INDEX_CHART_CODES[code], "period": period,
        "range": {"valid": False}, "trend_lines": [], "events": [],
        "dates": [r.date for r in rows],
        "open": [r.open for r in rows],
        "high": [r.high for r in rows],
        "low": [r.low for r in rows],
        "close": [r.close for r in rows],
        "volume": [r.volume for r in rows],
    }
    if period == "day":
        ind = build_indicators(rows)
        rng = find_range(rows)
        ev = detect(rows, ind, rng)
        tls = []
        for kind in ("supply", "demand"):
            t = trend_lines(rows).get(kind)
            if t:
                tls.append({"kind": kind, **t})
        out["range"] = rng.to_dict()
        out["trend_lines"] = tls
        out["events"] = sorted(ev["events"], key=lambda x: x["index"])
    else:
        # 周/月/年线同样画出重要的线（供给/需求线、区间上下沿），按本周期K线计算
        keys = _key_lines(rows, period)
        out["range"] = keys["range"]
        out["trend_lines"] = keys["trend_lines"]
    return out


STAGE_CN = {"accumulation": "吸筹", "distribution": "派发", "markup": "上涨", "markdown": "下跌",
            "unknown": "结构不明"}


def _stage_interaction(stock: str, mk: str, index_mode: bool) -> str:
    """威科夫大盘-个股阶段联动结论：个股大阶段 × 大盘大阶段 → 当下应对。"""
    mk_up = mk in ("accumulation", "markup")
    if mk == "unknown":
        head = "大盘阶段结构不清晰，以个股自身结构为主"
    elif mk_up:
        head = f"大盘处于{STAGE_CN[mk]}阶段（背景偏多）"
    else:
        head = f"大盘处于{STAGE_CN[mk]}阶段（背景偏空）"

    if stock == "accumulation":
        body = ("个股同步吸筹：与大盘共振蓄势，出现 Spring/SOS 等信号时可信度高，可按计划分批布局"
                if mk_up else
                "大盘走弱而个股独立吸筹：属相对强势标的，但逆势阶段仓位应打折，等大盘企稳共振再放大动作")
    elif stock == "markup":
        body = ("趋势共振向上：以持股为主，回踩需求线/冰线不破可加仓，跌破冰线按纪律离场"
                if mk_up else
                "个股强于大盘（相对强度高）：可持有，但大盘派发会压制上行空间，收紧止盈、不追高")
    elif stock == "distribution":
        body = ("大盘尚可而个股已开始派发：资金正在撤离该标的，逢冲高减仓，勿因大盘走强而留恋"
                if mk_up else
                "大盘派发叠加个股派发：双重供给压力，逢反弹坚决减仓，不抄底")
    elif stock == "markdown":
        body = ("个股仍在下跌而大盘已转强：超跌反弹可关注，但需等个股自身出现止跌吸筹结构再动手"
                if mk_up else
                "大盘与个股同步走弱：系统性下跌环境，空仓回避为主，勿接飞刀")
    else:
        body = "个股结构不清晰：先观察，等形成可辨识的吸筹/派发结构再制定计划"
    if index_mode:
        body += "（当前分析对象为指数：该结论用于判断整体环境与仓位总闸，而非买卖指数本身）"
    return f"{head}；{body}"


def _background(market: dict, stock_phase: dict, index_mode: bool) -> dict:
    """当下背景：大盘与个股所处的威科夫大阶段 + 阶段联动结论。"""
    m = (market or {}).get("phase") or {}
    s = stock_phase or {}
    m_stage = str(m.get("phase") or "unknown")
    s_stage = str(s.get("phase") or "unknown")
    m_trend = ((market or {}).get("trend") or {}).get("dir", "sideways")
    return {
        "market": {"stage": m_stage,
                   "stage_cn": m.get("phase_cn") or STAGE_CN.get(m_stage, "结构不明"),
                   "sub_phase_cn": m.get("sub_phase_cn", "") if m_stage != "unknown" else "",
                   "summary": m.get("summary", ""), "trend": m_trend,
                   "confidence": m.get("confidence", 0)},
        "stock": {"stage": s_stage,
                  "stage_cn": s.get("phase_cn") or STAGE_CN.get(s_stage, "结构不明"),
                  "sub_phase_cn": s.get("sub_phase_cn", "") if s_stage != "unknown" else "",
                  "summary": s.get("summary", ""), "confidence": s.get("confidence", 0)},
        "interaction": _stage_interaction(s_stage, m_stage, index_mode),
        "cycle": _smart_money_cycle(s),
    }


# 聪明钱四阶段循环（吸筹→拉升→派发→下跌）：每个位置对应的「聪明钱下一步动作」预判
CYCLE_NEXT = {
    ("accumulation", "A"): "恐慌抛售刚结束（SC→AR→ST），聪明钱开始初步承接：下一步是确认供给枯竭，进入震荡吸筹",
    ("accumulation", "B"): "聪明钱在区间内耐心吸筹、高抛低吸：下一步是在区间下部制造震仓（Spring）或直接试探溪流",
    ("accumulation", "C"): "震仓/缩量测试已现，聪明钱确认供给枯竭：下一步是等待需求入场，放量突破溪流（SOS）",
    ("accumulation", "D"): "聪明钱已基本建仓完毕（SOS 与 LPS 结构成立）：下一步是推升价格离开区间，进入上涨阶段",
    ("accumulation", "E"): "价格已离开吸筹区间，建仓完成：聪明钱转入持有并顺势拉升",
    ("markup", ""): "聪明钱持有主仓，逢回调（回踩需求线/突破位）继续承接：下一步是延续趋势，直至高位出现放量滞涨、二次上冲失败（UTAD）等派发迹象",
    ("distribution", "A"): "上涨动能衰竭，聪明钱开始在高位派发（BC→AR→ST）：下一步是制造二次上冲（UTAD）假突破吸引买盘",
    ("distribution", "B"): "聪明钱在区间顶部反复出货：下一步是以上冲诱惑（UT/UTAD）诱多后，向下测试冰线",
    ("distribution", "C"): "二次上冲失败（UTAD）已现，出货接近尾声：下一步是放量跌破冰线（SOW），进入下跌",
    ("distribution", "D"): "派发末期，多数筹码已出：下一步是破位砸盘，下跌趋势展开",
    ("distribution", "E"): "价格已离开派发区间，出货完成：聪明钱转入观望，等待新的吸筹背景",
    ("markdown", ""): "出货完毕进入下跌：反弹至压力位（LPSY）是聪明钱继续出货的机会，下一步是延续下跌，直至重新出现吸筹背景",
    ("unknown", ""): "结构不清晰，聪明钱意图不明：以观察为主，等待区间或趋势形成后再判断其动作",
}


def _smart_money_cycle(stock_phase: dict) -> dict:
    """个股在聪明钱四阶段循环（吸筹→拉升→派发→下跌）中的位置 + 下一步动作预判。"""
    stage = str(stock_phase.get("phase") or "unknown")
    sub = str(stock_phase.get("sub_phase") or "")   # 如 "Phase B"
    letter = sub.replace("Phase", "").strip()[:1] if sub else ""
    key = (stage, letter if stage in ("accumulation", "distribution") and letter in "ABCDE" else "")
    next_move = CYCLE_NEXT.get(key) or CYCLE_NEXT.get((stage, "")) or CYCLE_NEXT[("unknown", "")]
    position_cn = STAGE_CN.get(stage, "结构不明")
    if key[1]:
        position_cn += f"·{key[1]} 阶段"
    return {"position_cn": position_cn, "stage": stage, "sub_phase": sub, "next_move": next_move}


def _position_plan(decision: dict, rng) -> list:
    """聪明钱开仓顺序：区间下部初始仓 → SOS 突破确认加仓 → 回踩 LPS 补仓。"""
    act = str(decision.get("action") or "")
    net = float(((decision.get("score") or {}).get("net")) or 0)
    total = int((decision.get("position") or {}).get("suggest_pct") or 0)
    if act not in ("buy", "wait") or net < 0 or total <= 0:
        return []
    entry = decision.get("entry") or {}
    zone = entry.get("zone") or []
    lo, hi = (zone[0], zone[1]) if len(zone) == 2 else (0, 0)
    steps = [{"seq": 1, "name": "初始仓（试探）", "pct": max(5, total // 2),
              "trigger": (f"价格进入入场区间 {lo:.2f}~{hi:.2f} 并出现止跌企稳（缩量回踩、停止行为 Test）"
                          if lo else "价格出现止跌企稳信号（缩量回踩、停止行为）"),
              "note": "先用不超过一半仓位试探——聪明钱从不一次建满，留给确认后加仓"}]
    if rng.valid and rng.high > 0:
        p2 = max(5, int(total * 0.7))
        steps.append({"seq": 2, "name": "突破确认加仓（SOS）", "pct": p2,
                      "trigger": f"放量突破溪流/区间上沿 {rng.high:.2f}（量能明显放大、收盘站上），加到 {p2}%",
                      "note": "缩量突破视为假突破（UT），不加仓"})
        steps.append({"seq": 3, "name": "回踩补仓（LPS）", "pct": total,
                      "trigger": f"突破后回踩 {rng.high:.2f} 不破且缩量企稳，补足至计划仓位 {total}%",
                      "note": "回踩放量下跌=突破失败，取消补仓并按纪律离场"})
    else:
        p2 = max(5, int(total * 0.7))
        steps.append({"seq": 2, "name": "趋势确认加仓", "pct": p2,
                      "trigger": "站上短期压力/均线并放量，加仓至 70% 以内",
                      "note": "无区间结构时以量价确认代替结构位确认"})
    return steps


def analyze(code: str, bars: int = 300, adjust: str = "qfq", with_intraday: bool = True) -> dict:
    """输入股票代码，输出完整威科夫分析结果。

    为避免网络抖动时无限等待，整次取数有一个时间预算（默认 45 秒，可用环境变量
    WYCKOFF_BUDGET 调整）：核心的实时快照与日K线一定取，周线/分时/大盘在预算不足时跳过，
    并在报告里注明。
    """
    started = time.time()
    budget = float(os.environ.get("WYCKOFF_BUDGET", "45") or 45)

    def time_left() -> float:
        return budget - (time.time() - started)

    adjust = adjust if adjust in ADJUST_CN else "qfq"
    # bars = K线展示窗口（用户可要求全部历史）；威科夫结构分析固定在最近 analyze_win 根上
    bars = int(max(120, min(9999, bars)))
    symbol, resolved_name, resolved_via = resolve_input(code)
    market_prefix, code6 = split_code(symbol)
    index_mode = is_index(symbol)
    data_warnings: list[str] = []
    if resolved_via == "search" and resolved_name:
        data_warnings.append(f"已按名称「{resolved_name}」解析为 {symbol}")

    # ---------- 实时快照 ----------
    quote = None
    try:
        quote = get_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        data_warnings.append(f"实时快照获取失败（{exc}），以下价格以最后一根K线为准")
    if quote and index_mode:
        # 指数没有涨跌停/市盈率概念：源头清零，避免下游误报「处于涨停价」等个股提示
        quote.limit_up = quote.limit_down = 0.0
        quote.pe = quote.pb = quote.turnover = 0.0

    # ---------- 日线（多取 140 根用于均线预热，再截取展示窗口；至少 640 根保证大趋势背景） ----------
    fetch_n = max(bars + 140, 640)
    rows = get_bars(symbol, bars=fetch_n, period="day", adjust=adjust)
    adjust_label = ADJUST_CN[adjust]
    if any(bar.close <= 0 or bar.low <= 0 for bar in rows):
        # 后复权量纲与现价差数倍，按最近前复权收盘等比缩放，保证与 quote/报告同口径
        anchor = rows[-1].close if (rows and rows[-1].close > 0) else 0.0
        rows = get_bars(symbol, bars=fetch_n, period="day", adjust="hfq")
        rows = rescale_to_current(rows, anchor)
        adjust_label = "后复权（已换算为现价量纲）"
        data_warnings.append("前复权在长周期出现非正值（多为高分红个股），"
                             "已改用后复权并换算为现价量纲")
    if len(rows) < 60:
        data_warnings.append(f"仅有 {len(rows)} 根日K线，样本过少，结论仅供参考")

    full_ind = build_indicators(rows)
    offset = max(0, len(rows) - bars)
    disp = rows[offset:]
    ind = _slice_indicators(full_ind, offset)

    # 威科夫结构/事件只分析最近 analyze_win 根（超长历史仅用于图表浏览）。
    # 事件与区间的索引在输出前平移 a_off，对齐全量 disp 坐标系。
    analyze_win = min(bars, 700)
    a_off = max(0, len(disp) - analyze_win)
    disp_a = disp[a_off:]
    ind_a = _slice_indicators(ind, a_off)

    # ---------- 结构 / 事件 / 量价 ----------
    rng = find_range(disp_a)
    event_data = detect(disp_a, ind_a, rng)
    volume = volume_stats(disp_a, ind_a)
    levels = key_levels(disp_a, ind_a, rng, event_data["events"])
    tlines = trend_lines(disp_a)
    cause = cause_effect(rng, disp_a[-1].close)

    # ---------- 趋势（日 / 周 / 分时） ----------
    daily_trend = trend_of(disp_a, ind_a, 60)
    weekly_dir, weekly_note = "sideways", "周线数据不足"
    if time_left() > 6:
        try:
            week_rows = get_bars(symbol, bars=160, period="week", adjust=adjust)
            if len(week_rows) >= 30:
                week_ind = build_indicators(week_rows)
                week_trend = trend_of(week_rows, week_ind, 30)
                weekly_dir, weekly_note = week_trend["dir"], week_trend["text"]
        except Exception as exc:  # noqa: BLE001
            weekly_note = f"周线获取失败：{exc}"
    else:
        weekly_note = "取数超时，已跳过周线（可用 WYCKOFF_BUDGET 放宽时间预算）"
        data_warnings.append("取数耗时过长，周线趋势未纳入判断（结论置信度已相应下调）")

    intraday_dir, intraday_note = "sideways", "分时数据未启用"
    minute_rows: list = []
    if with_intraday and time_left() > 8:
        try:
            minute_rows = get_intraday(symbol, bars=96, period="m5")
            if len(minute_rows) >= 12:
                minute_ind = build_indicators(minute_rows)
                minute_trend = trend_of(minute_rows, minute_ind, min(48, len(minute_rows)))
                intraday_dir = minute_trend["dir"]
                intraday_note = (f"{minute_rows[0].date} ~ {minute_rows[-1].date} 的 5 分钟级别"
                                 f"{TREND_CN[intraday_dir]}（{minute_trend['text']}）")
        except Exception as exc:  # noqa: BLE001
            intraday_note = f"分时获取失败：{exc}"
    elif with_intraday:
        intraday_note = "取数超时，已跳过分时"
        data_warnings.append("取数耗时过长，分时（当日实时走势）未纳入判断")

    if time_left() > 4:
        market = _market_context()
    else:
        market = {"text": "取数超时，已跳过指数数据", "indices": [], "trend": {"dir": "sideways"}}

    # ---------- 公司财务指标 ----------
    financials = None
    if not index_mode and time_left() > 4:
        try:
            financials = get_financials(symbol)
        except Exception as exc:  # noqa: BLE001
            data_warnings.append(f"财务指标获取失败（{exc}）")
    elif index_mode:
        pass  # 指数无财务指标
    elif time_left() <= 4:
        data_warnings.append("取数耗时过长，公司财务指标未获取")

    # ---------- 阶段判定 ----------
    # 大趋势背景用全部拉取的日线（≥640 根：年线 + 250根分位 + 500根斜率），而非仅展示窗口
    long_ctx = long_trend_of(rows, full_ind)
    phase = classify_phase(disp_a, ind_a, rng, event_data["events"], event_data["vsa"], market,
                           long_ctx=long_ctx)
    phase["trend"] = {"daily": daily_trend["dir"], "weekly": weekly_dir, "intraday": intraday_dir}
    phase["trend_text"] = {
        "daily": daily_trend["text"], "weekly": weekly_note, "intraday": intraday_note,
    }

    # ---------- 当下背景（大盘与个股的威科夫大阶段联动） ----------
    background = _background(market, phase, index_mode)

    # ---------- 停牌 / 数据新鲜度 ----------
    fresh = {"suspended": False}
    last_date = disp[-1].date
    if quote and quote.updated:
        quote_date = quote.updated[:10]
        if quote_date and quote_date != last_date and disp[-1].volume <= 0:
            fresh["suspended"] = True
    if disp[-1].volume <= 0:
        fresh["suspended"] = True

    # ---------- 决策 ----------
    decision = decide(disp_a, ind_a, rng, phase, event_data, volume, quote, market, levels, fresh,
                      index_mode=index_mode)
    # 聪明钱开仓顺序（初始仓→SOS加仓→LPS补仓）挂在决策结果上
    decision["position_plan"] = _position_plan(decision, rng)

    # ---------- 当日作战建议（盘中实时） ----------
    # 趋势线先平移到 disp 坐标系（与前端绘制一致），供 kline 图与当日建议共用
    tlines_out = [
        {"kind": kind, "a_index": t["a_index"] + a_off, "a_price": t["a_price"],
         "a_date": t["a_date"], "b_index": t["b_index"] + a_off, "b_price": t["b_price"],
         "b_date": t["b_date"]}
        for kind in ("supply", "demand") if (t := tlines.get(kind))
    ]
    daily_pack = {
        "date": disp[-1].date,
        "open": [b.open for b in disp], "high": [b.high for b in disp],
        "low": [b.low for b in disp], "close": [b.close for b in disp],
        "volume": [b.volume for b in disp], "adjust": adjust_label,
    }
    try:
        intraday = intraday_advice(minute_rows, quote, rng.to_dict(), tlines_out, decision,
                                   daily=daily_pack, index_mode=index_mode)
    except Exception as exc:  # noqa: BLE001
        intraday = {"ok": False, "error": f"当日建议生成失败：{type(exc).__name__}: {exc}"}

    # ---------- meta ----------
    if quote:
        name, source, updated = quote.name, quote.source, quote.updated
        price, prev_close = quote.price, quote.prev_close
        open_, high, low = quote.open, quote.high, quote.low
        change, change_pct = quote.change, quote.change_pct
        amplitude, vol, amount = quote.amplitude, quote.volume, quote.amount
        turnover, vol_ratio = quote.turnover, quote.volume_ratio
        pe, pb = quote.pe, quote.pb
        total_cap, float_cap = quote.total_cap, quote.float_cap
        limit_up, limit_down = quote.limit_up, quote.limit_down
    else:
        name, source, updated = code6, "K线数据", last_date
        price = disp[-1].close
        prev_close = disp[-2].close if len(disp) > 1 else disp[-1].open
        open_, high, low = disp[-1].open, disp[-1].high, disp[-1].low
        change = price - prev_close
        change_pct = (change / prev_close * 100) if prev_close else 0.0
        amplitude = (high - low) / prev_close * 100 if prev_close else 0.0
        vol, amount = disp[-1].volume, disp[-1].amount
        turnover = vol_ratio = pe = pb = total_cap = float_cap = limit_up = limit_down = 0.0

    if index_mode:
        # 指数没有涨跌停、市盈率等个股指标，避免出现无意义的提示
        limit_up = limit_down = 0.0
        pe = pb = turnover = 0.0
        data_warnings.append("这是指数，不是可交易个股：结论用于判断大盘环境，不能直接买卖")

    src_log = last_sources()
    source_text = f"日线 {src_log.get('bars', '未知')}／快照 {src_log.get('quote', '未知')}"
    if financials:
        source_text += f"／财务 {src_log.get('financials', '东方财富')}"
    meta = {
        "symbol": symbol,
        "code": code6,
        "name": name or code6,
        "market": market_prefix.upper(),
        "updated": updated,
        "price": round(price, 2), "prev_close": round(prev_close, 2), "open": round(open_, 2),
        "high": round(high, 2), "low": round(low, 2),
        "change": round(change, 2), "change_pct": round(change_pct, 2),
        "amplitude": round(amplitude, 2), "volume": round(vol, 0), "amount": round(amount, 0),
        "turnover": round(turnover, 2), "volume_ratio": round(vol_ratio, 2),
        "pe": round(pe, 2), "pb": round(pb, 2),
        "total_cap": round(total_cap, 0), "float_cap": round(float_cap, 0),
        "limit_up": round(limit_up, 2), "limit_down": round(limit_down, 2),
        "bars": len(disp), "start_date": disp[0].date, "end_date": disp[-1].date,
        "adjust": adjust_label, "source": source_text,
        "warnings": data_warnings, "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": round(time.time() - started, 1),
        "disclaimer": DISCLAIMER,
        "financials": financials,
    }

    # ---------- 震仓提示与策略（书中：震仓是洗盘而非反转） ----------
    shk = _shakeout_hint(disp_a, ind_a, event_data, rng)
    if shk:
        data_warnings.append("震仓提示：" + shk["text"] + "　策略：" + shk["strategy"])

    # ---------- 图表数据 ----------
    kc_raw = _key_candles(disp_a, ind_a)
    pt_raw = _chart_patterns(disp_a)
    cp_raw = _candle_patterns(disp_a)
    tr_raw = _minor_trends(disp_a)
    tl = _trade_levels(event_data["events"], rng, disp_a[-1].close, cause)
    kline = {
        "dates": [bar.date for bar in disp],
        "open": _round_list([bar.open for bar in disp]),
        "high": _round_list([bar.high for bar in disp]),
        "low": _round_list([bar.low for bar in disp]),
        "close": _round_list([bar.close for bar in disp]),
        "volume": _round_list([bar.volume for bar in disp], 0),
        "ma5": _round_list(ind.ma5), "ma10": _round_list(ind.ma10),
        "ma20": _round_list(ind.ma20), "ma60": _round_list(ind.ma60),
        "ma120": _round_list(ind.ma120),
        "events": [{"index": e["index"] + a_off, "date": e["date"], "type_cn": e["type_cn"],
                    "type": e["type"], "price": e["price"], "bullish": e["bullish"]}
                   for e in event_data["events"]],
        "range": ({"high": round(rng.high, 2), "low": round(rng.low, 2),
                   "mid": round(rng.mid, 2), "start_index": rng.start_index + a_off,
                   "end_index": rng.end_index + a_off, "breakout": rng.breakout,
                   "prior_change_pct": round(rng.prior_change_pct * 100, 2)} if rng.valid else {}),
        "trend_lines": tlines_out,
        "trade_levels": tl,
        # 关键K线编号：_key_candles 在 disp_a（分析窗）空间内计数，统一加 a_off 映射回 kline 展示空间
        "key_candles": [{**kc, "index": kc["index"] + a_off} for kc in kc_raw],
        # 经典K线形态（三角形/楔形/矩形/旗形）：坐标同样映射到展示空间
        "patterns": [{
            **pt,
            "start_index": pt["start_index"] + a_off, "end_index": pt["end_index"] + a_off,
            "upper": [[ix + a_off, py] for ix, py in pt["upper"]],
            "lower": [[ix + a_off, py] for ix, py in pt["lower"]],
        } for pt in pt_raw],
        # 多根K线组合形态（吞没/启明星/红三兵等）：坐标同样映射到展示空间
        "candle_patterns": [{
            **cp,
            "index": cp["index"] + a_off,
            "indexes": [ix + a_off for ix in cp["indexes"]],
        } for cp in cp_raw],
        # 次级趋势段（小趋势，TL-UP/TL-DN）：坐标同样映射到展示空间
        "trends": [{
            **tr,
            "start_index": tr["start_index"] + a_off, "end_index": tr["end_index"] + a_off,
            "line": [[ix + a_off, py] for ix, py in tr["line"]],
        } for tr in tr_raw],
        "latest_read": _latest_candle_read(disp_a, ind_a, kc_raw, rng, tl),
    }

    net = decision.get("score", {}).get("net", 0.0)
    strength = int(max(0, min(100, abs(net) * 1.9)))
    wyckoff = {
        "phase": phase["phase"], "phase_cn": phase["phase_cn"],
        "sub_phase": phase["sub_phase"], "sub_phase_cn": phase.get("sub_phase_cn", ""),
        "long_trend": long_ctx,
        "confidence": phase["confidence"], "summary": phase["summary"],
        "long_context": phase.get("long_context", ""),
        "acc_score": phase["acc_score"], "dist_score": phase["dist_score"],
        "reasons": phase["reasons"],
        "trend": phase["trend"], "trend_text": phase["trend_text"],
        "range": rng.to_dict(),
        "cause_effect": cause,
        "levels": levels,
        "events": event_data["events"],
        "vsa": event_data["vsa"],
        "volume_analysis": volume,
        "effort_result": event_data["effort_result"],
        "composite": event_data["composite"],
        "counts": event_data["counts"],
        "market_context": {"text": market["text"], "indices": market["indices"],
                           "trend": market["trend"], "phase": market.get("phase"),
                           "wyckoff": market.get("wyckoff", {}),
                           "cyb_trend": market.get("cyb_trend", {"dir": "sideways"}),
                           "cyb_phase": market.get("cyb_phase"),
                           "wyckoff_cyb": market.get("wyckoff_cyb", {})},
        "background": background,
        "score": {"bull": strength if net > 0 else 0,
                  "bear": strength if net < 0 else 0,
                  "neutral": 100 - strength},
        "trade_levels": _trade_levels(event_data["events"], rng, disp_a[-1].close, cause),
    }

    # ---------- 聪明钱看盘顺序（《新威科夫操盘》）+ 九大检验（《新威科夫操盘法》） ----------
    nine_tests = _nine_tests(event_data["events"], rng, event_data["vsa"], cause,
                             phase, market, decision)
    wyckoff["nine_tests"] = nine_tests

    smart_flow = _smart_money_steps(
        market, long_ctx, phase, decision, volume,
        event_data["effort_result"], event_data["composite"], event_data["vsa"],
        cause, disp_a, ind_a, nine_tests,
        key_candles=kc_raw, latest=kline.get("latest_read"), rng=rng,
        trade_levels=tl)

    # 交易计划第三段：危机管理（书中第五章案例固定三段式，前端决策卡渲染）
    if isinstance(decision, dict):
        _stop_px = (decision.get("stop") or {}).get("price", "--")
        decision["crisis"] = ("危机管理（出现任一情况即放弃计划离场）："
                              "① 进场后未出现三高＋量增的确认，反而出现放量阴线；"
                              f"② 收盘跌破止损位 {_stop_px} 且两根内不收回；"
                              "③ 突破后的回踩出现放量长阴（浮动供应还在，突破失效）")

    payload = {
        "ok": True, "error": "",
        "meta": meta,
        "wyckoff": wyckoff,
        "decision": decision,
        "smart_flow": smart_flow,
        "intraday": intraday,
        "kline": kline,
        "shakeout": shk,
        "disclaimer": DISCLAIMER,
    }
    payload["report_text"] = report_mod.render(meta, wyckoff, decision, event_data, volume, cause, market)
    return payload


# --------------------------------------------------------------------------- #
# 吸筹机会扫描：沪主板 + 深主板 + 创业板全市场威科夫筛选（后台线程 + 磁盘缓存）
# --------------------------------------------------------------------------- #
EM_CLIST_SCAN = ("https://push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
                 "&fltt=2&invt=2&fid=f12&fs={fs}"
                 "&fields=f12,f13,f14,f2,f3,f8,f9,f20,f100,f105,f115")
SCAN_FS = "m:1+t:2,m:0+t:6,m:0+t:80"          # 沪A主板 + 深A主板 + 创业板
# 备用（主源）：东财 datacenter 业绩报表（与 push2 不同主机，push2 被风控时仍可用）
EM_FIN_SCAN = ("https://datacenter-web.eastmoney.com/api/data/v1/get?reportName=RPT_LICO_FN_CPD"
               "&columns=SECURITY_CODE%2CSECURITY_NAME_ABBR%2CSECUCODE%2CTRADE_MARKET%2C"
               "PARENT_NETPROFIT%2CBOARD_NAME&filter=(REPORTDATE%3D%27{rdate}%27)"
               "&pageSize=500&pageNumber={page}&sortColumns=SECURITY_CODE&sortTypes=1")
SCAN_TTL = 2 * 3600                            # 结果新鲜度：交易日内 2 小时
SCAN_WORKERS = 6                               # 并发取数线程（防数据源风控）
SCAN_CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "screener_cache.json")
SCAN_CRITERIA = ("候选口径（威科夫·仅「完成吸筹」）：吸筹D/E·SOS/LPS确认（建仓完成）｜吸筹C·震仓/弹簧测试完成"
                 "（供给枯竭）。已剔除 ST/退市股；仅保留所在行业板块近 2 周涨幅≥2% 且主力资金净流入为正的个股"
                 "（板块数据缺失者保留并标注「--」）。买卖策略按威科夫结构位给出：LPS 回踩买入/区间突破加仓/"
                 "跌破结构止损/因果量度定目标。评分仅供参考，不构成投资建议。")

_scan_state: dict = {"running": False, "done": 0, "total": 0, "hits": 0,
                     "stage": "", "error": "", "started": 0.0, "finished": 0.0}
_scan_lock = threading.Lock()
_scan_cache: dict | None = None
_scan_cache_ts: float = 0.0
_last_auto_ts: float = 0.0            # 上次自动触发全量扫描的时刻（防轮询风暴）


def _scan_load_cache() -> dict | None:
    """读取磁盘缓存（进程内镜像一次，之后直接用内存映像）。"""
    global _scan_cache, _scan_cache_ts
    if _scan_cache is not None:
        return _scan_cache
    try:
        with open(SCAN_CACHE_FILE, "r", encoding="utf-8") as fh:
            _scan_cache = json.load(fh)
            _scan_cache_ts = float(_scan_cache.get("generated_ts") or 0)
    except Exception:  # noqa: BLE001
        _scan_cache = None
    return _scan_cache


def _scan_report_period() -> str:
    """最近一个「已到披露截止日」的财报期末日（Q1→4-30，Q2→8-31，Q3→10-31，Q4→次年4-30）。"""
    today = _dt.date.today()
    deadlines = [(_dt.date(today.year, 4, 30), f"{today.year}-03-31"),
                 (_dt.date(today.year, 8, 31), f"{today.year}-06-30"),
                 (_dt.date(today.year, 10, 31), f"{today.year}-09-30"),
                 (_dt.date(today.year + 1, 4, 30), f"{today.year}-12-31")]
    best = deadlines[0][1]
    for dl, period in deadlines:
        if today >= dl:
            best = period
    return best


def _scan_universe_datacenter() -> list[dict]:
    """主源：东财 datacenter 业绩报表（最新已披露报告期）。

    返回代码/名称/行业/净利润；现价、涨跌幅、PE、市值由命中后 get_quote 补全。
    仅保留沪主板（60）/深主板（00）/创业板（30）前缀的 A 股。
    """
    rdate = _scan_report_period()
    out: list[dict] = []
    page, pages = 1, 1
    while page <= 40:
        j = fetch_json(EM_FIN_SCAN.format(rdate=rdate, page=page),
                       referer="https://data.eastmoney.com/", retries=2)
        result = (j or {}).get("result") or {}
        rows = result.get("data") or []
        pages = int(result.get("pages") or 0)
        for r in rows:
            code = str(r.get("SECURITY_CODE") or "")
            if not code or code[0] not in ("0", "3", "6"):
                continue
            name0 = str(r.get("SECURITY_NAME_ABBR") or "")
            if "ST" in name0.upper() or "退" in name0:
                continue                       # ST / 退市股直接剔除
            if code.startswith("60"):
                symbol = "sh" + code
            elif code.startswith("00"):
                symbol = "sz" + code
            elif code.startswith("30"):
                symbol = "sz" + code
            else:
                continue                       # 科创板 688 / B股 / 北交所等排除
            profit = r.get("PARENT_NETPROFIT")
            out.append({
                "code": code, "symbol": symbol,
                "name": name0 or code,
                "industry": str(r.get("BOARD_NAME") or "--"),
                "price": None, "pct": 0.0,
                "pe": None,
                "profit": float(profit) if isinstance(profit, (int, float)) else None,
                "cap": None,
            })
        if not rows or page >= pages:
            break
        page += 1
    if not out:
        raise DataSourceError("业绩报表列表为空（datacenter 接口异常），请稍后重试")
    return out


def _scan_universe() -> tuple[list[dict], str]:
    """候选池列表：主源 datacenter 业绩报表；失败回退 push2 clist（带现价/PE，但常被风控）。"""
    try:
        return _scan_universe_datacenter(), "datacenter"
    except Exception:  # noqa: BLE001
        return _scan_universe_clist(), "clist"


def _scan_universe_clist() -> list[dict]:
    """备用列表源：东财 push2 clist 全列表（含现价/涨跌幅/PE/净利润/总市值）。

    f2 无报价（退市/长期停牌）的标的直接跳过。
    """
    out: list[dict] = []
    page, total = 1, 0
    while page <= 70:
        j = fetch_json(EM_CLIST_SCAN.format(page=page, fs=SCAN_FS),
                       referer="https://quote.eastmoney.com/", retries=2, timeout=6)
        d = (j or {}).get("data") or {}
        rows = d.get("diff") or []
        total = int(d.get("total") or 0)
        for r in rows:
            code = str(r.get("f12") or "")
            price = r.get("f2")
            name0 = str(r.get("f14") or "")
            if len(code) != 6 or not isinstance(price, (int, float)):
                continue
            if "ST" in name0.upper() or "退" in name0:
                continue                       # ST / 退市股直接剔除
            out.append({
                "code": code,
                "symbol": ("sh" if r.get("f13") == 1 else "sz") + code,
                "name": name0 or code,
                "industry": str(r.get("f100") or "--"),
                "price": float(price),
                "pct": float(r.get("f3")) if isinstance(r.get("f3"), (int, float)) else 0.0,
                "pe": float(r.get("f115")) if isinstance(r.get("f115"), (int, float)) else None,
                "profit": float(r.get("f105")) if isinstance(r.get("f105"), (int, float)) else None,
                "cap": float(r.get("f20")) if isinstance(r.get("f20"), (int, float)) else None,
            })
        if not rows or (total and page * 100 >= total):
            break
        page += 1
    if not out:
        raise DataSourceError("股票列表获取失败（东财接口风控或网络异常），请稍后重试")
    return out


def _scan_recent(events: list[dict], types: tuple, within: int, n: int) -> dict | None:
    """最近 within 根K线内、指定类型中离当前最近的一个事件。"""
    best = None
    for e in events:
        if e.get("type") in types:
            age = n - 1 - int(e.get("index", -10 ** 9))
            if age <= within and (best is None or int(e["index"]) > int(best["index"])):
                best = e
    return best


# ----------------------- 板块热度：行业板块近 2 周异动 + 主力资金关注 ----------------------- #
EM_SUGGEST_BOARD = ("https://searchapi.eastmoney.com/api/suggest/get?input={q}"
                    "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=8")
EM_BK_FFLOW = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
               "?secid=90.{bk}&fields1=f1,f2,f3,f7&fields2=f51,f52,f62,f63&lmt=0")
_BK_MAP: dict[str, str] = {}                       # 行业名 -> BK 代码（进程内缓存）
_BOARD_FLOW: dict[str, tuple[float, dict | None]] = {}   # BK -> (ts, 2周指标)，缓存 2 小时
BOARD_HOT_MIN_PCT = 2.0                            # 近2周板块涨幅阈值（异动）


def _board_bk_code(industry: str) -> str | None:
    """行业名 -> 东财行业板块 BK 代码（searchapi 搜索，进程内缓存）。

    「银行Ⅱ」等罗马数字后缀、「酿酒行业」等「行业」后缀会导致搜索不到板块，
    依次尝试原名与去后缀名。
    """
    base = re.sub(r"[ⅠⅡⅢⅣ]$", "", (industry or "").strip())
    if not base or base == "--":
        return None
    for q in dict.fromkeys([base, re.sub(r"行业$", "", base)]):
        if not q:
            continue
        if q in _BK_MAP:
            return _BK_MAP[q]
        try:
            d = fetch_json(EM_SUGGEST_BOARD.format(q=urllib.parse.quote(q)), encoding="utf-8")
            for it in ((d or {}).get("QuotationCodeTable") or {}).get("Data") or []:
                code = str(it.get("Code") or "")
                if code.startswith("BK"):
                    _BK_MAP[q] = code
                    return code
        except Exception:  # noqa: BLE001
            pass
    return None


def _board_flow_2w(bk: str) -> dict | None:
    """行业板块近 2 周（约 10 个交易日）主力净流入（亿）与累计涨幅（%）。"""
    try:
        j = fetch_json(EM_BK_FFLOW.format(bk=bk),
                       referer="https://data.eastmoney.com/", retries=1, timeout=8)
        kl = ((j or {}).get("data") or {}).get("klines") or []
        rows = [r.split(",") for r in kl[-11:]]    # 近 11 根：10 日涨幅需 11 个收盘价
        if len(rows) < 6:
            return None
        net = 0.0
        for r in rows[1:]:
            try:
                net += float(r[1])
            except (ValueError, IndexError):
                pass
        closes = []
        for r in rows:
            try:
                closes.append(float(r[2]))
            except (ValueError, IndexError):
                pass
        pct10 = (closes[-1] / closes[0] - 1) * 100.0 if len(closes) >= 2 and closes[0] else 0.0
        return {"net": round(net / 1e8, 2), "pct": round(pct10, 2)}
    except Exception:  # noqa: BLE001
        return None


def _board_flow_cached(industry: str) -> dict | None:
    bk = _board_bk_code(industry)
    if not bk:
        return None
    hit = _BOARD_FLOW.get(bk)
    if hit and time.time() - hit[0] <= 2 * 3600:
        return hit[1]
    m = _board_flow_2w(bk)
    _BOARD_FLOW[bk] = (time.time(), m)
    return m


def _board_hot(m: dict | None) -> bool:
    """板块异动+资金关注：近2周涨幅≥阈值 且 主力净流入为正。"""
    return bool(m) and m["net"] > 0 and m["pct"] >= BOARD_HOT_MIN_PCT


def _scan_one(item: dict) -> dict | None:
    """单股威科夫筛选：仅命中「完成吸筹」（吸筹C震仓测试完成 / 吸筹D·E·SOS/LPS确认）返回候选行，否则 None。"""
    try:
        rows = get_bars(item["symbol"], bars=320, period="day", adjust="qfq")
        if any(b.close <= 0 or b.low <= 0 for b in rows):
            # 同 analyze：切后复权后按当前价格量纲等比换算，否则候选行现价会放大数倍
            anchor = rows[-1].close if (rows and rows[-1].close > 0) else 0.0
            rows = get_bars(item["symbol"], bars=320, period="day", adjust="hfq")
            rows = rescale_to_current(rows, anchor)
        n = len(rows)
        if n < 120 or rows[-1].volume <= 0:
            return None
        ind = build_indicators(rows)
        rng = find_range(rows)
        ev = detect(rows, ind, rng)
        events = ev["events"]
        long_ctx = long_trend_of(rows, ind)
        phase = classify_phase(rows, ind, rng, events, ev["vsa"],
                               {"text": "", "indices": [], "trend": {"dir": "sideways"}},
                               long_ctx=long_ctx)
        stage = str(phase.get("phase") or "unknown")
        sub = str(phase.get("sub_phase") or "")
        letter = sub.replace("Phase", "").strip()[:1]
        close = rows[-1].close
        ma120 = ind.ma120[-1] if ind.ma120 and ind.ma120[-1] else None
        # datacenter 列表源不带现价：用最后一根K线兜底
        prev_close = rows[-2].close if n > 1 else close
        if item.get("price") is None:
            item["price"] = close
            item["pct"] = round((close - prev_close) / prev_close * 100.0, 2) if prev_close else 0.0

        sos = _scan_recent(events, ("SOS",), 25, n)
        lps = _scan_recent(events, ("LPS",), 25, n)
        spring = _scan_recent(events, ("Spring", "Shakeout", "Test"), 40, n)
        brk_idx = rng.breakout_index if (rng.valid and rng.breakout == "up"
                                         and rng.breakout_index is not None) else None
        brk_recent = brk_idx is not None and (n - 1 - brk_idx) <= 20

        score, signals = 0, []
        if stage == "accumulation":
            if letter in ("D", "E") and (sos or lps):
                score = 46 if letter == "D" else 50
                signals.append(f"吸筹{letter}·SOS/LPS确认（建仓完成）")
            elif letter == "C" and spring:
                score = 34
                signals.append("吸筹C·震仓/测试完成（供给枯竭）")
            else:
                return None        # 仅保留「完成吸筹」核心档（C/D/E 且有事件确认）
        else:
            return None            # 拉升/派发/下跌/结构不明一律排除：只要「完成吸筹」

        if sos:
            score += 6
            signals.append(f"SOS {sos['date']}")
        if lps:
            score += 6
            signals.append(f"LPS {lps['date']}")
        if spring and stage == "accumulation":
            signals.append(f"{spring['type_cn']} {spring['date']}")
        if brk_recent:
            score += 4
        if ma120 and close > ma120:
            score += 6
            signals.append("站上MA120")
        if sos is not None:
            i = int(sos["index"])
            if 0 <= i < n and ind.rel_vol[i] and ind.rel_vol[i] >= 1.5:
                score += 5
                signals.append(f"突破放量{ind.rel_vol[i]}倍")
        if str(long_ctx.get("dir")) == "up":
            score += 4

        # 买卖策略（威科夫结构位：LPS 回踩买 / 区间突破加仓 / 跌破结构止损 / 因果量度定目标）
        ce = cause_effect(rng, close)
        tl = _trade_levels(events, rng, close, ce)
        entry = tl["entry"] or 0.0
        stop = tl["stop"] or 0.0
        target = tl["target"] or 0.0
        if lps and entry:
            buy_txt = f"回踩LPS {entry:.2f} 不破分批买入"
        elif spring and entry:
            buy_txt = f"弹簧低点 {entry:.2f} 之上缩量回踩即分批买入"
        else:
            buy_txt = f"现价 {close:.2f} 逢缩量回调分批买入"
        strategy = buy_txt
        if rng.valid:
            strategy += f"；放量突破 {round(rng.high, 2):.2f} 加仓"
        if stop > 0:
            strategy += f"；收盘跌破 {stop:.2f} 止损"
        if target > 0:
            strategy += f"；目标 {target:.2f}（因果量度）"

        return {**item,
                "stage": stage, "stage_cn": str(phase.get("phase_cn") or stage), "sub": sub,
                "score": int(max(0, min(100, score))), "signals": signals[:4],
                "strategy": strategy,
                "buy_price": entry or None, "stop_price": stop or None,
                "target_price": target or None, "date": rows[-1].date}
    except Exception:  # noqa: BLE001
        return None


def _scan_worker(limit: int | None = None) -> None:
    global _scan_cache, _scan_cache_ts
    try:
        _scan_state["stage"] = "获取沪深主板+创业板股票列表"
        universe, src = _scan_universe()
        if limit:
            universe = universe[:limit]
        _scan_state["total"] = len(universe)
        _scan_state["done"] = 0
        _scan_state["stage"] = f"逐股威科夫结构分析（列表源：{src}）"
        hits: list[dict] = []
        done = 0
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futures = [pool.submit(_scan_one, it) for it in universe]
            for fu in as_completed(futures):
                try:
                    r = fu.result()
                except Exception:  # noqa: BLE001
                    r = None
                done += 1
                if r:
                    hits.append(r)
                if done % 10 == 0 or done == len(universe):
                    _scan_state["done"] = done
                    _scan_state["hits"] = len(hits)
        hits.sort(key=lambda x: -x.get("score", 0))
        # 板块热度核查：仅保留「所在行业板块近2周异动(涨幅≥2%)且主力净流入为正」的个股；
        # 板块数据缺失（BK 映射/接口失败）的保留并标注，避免数据源故障静默清空列表。
        _scan_state["stage"] = "板块近2周资金流与异动核查"
        inds = sorted({str(h_.get("industry") or "") for h_ in hits} - {""})
        board_m: dict[str, dict | None] = {}
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            futs = {pool.submit(_board_flow_cached, ind): ind for ind in inds}
            for fu, ind in futs.items():
                try:
                    board_m[ind] = fu.result()
                except Exception:  # noqa: BLE001
                    board_m[ind] = None
        kept: list[dict] = []
        for h_ in hits:
            ind = str(h_.get("industry") or "")
            m = board_m.get(ind)
            if m is None:
                h_["sector_net"] = None
                h_["sector_pct"] = None
                h_["sector_hot"] = False
                h_["sector_missing"] = True
                kept.append(h_)
                continue
            h_["sector_net"] = m["net"]
            h_["sector_pct"] = m["pct"]
            h_["sector_hot"] = True
            h_["sector_missing"] = False
            if _board_hot(m):
                kept.append(h_)
        hits = kept
        # 命中股补全实时快照（PE-TTM/总市值等；最多 150 只，防拖慢整体扫描）
        _scan_state["stage"] = "补全命中股实时快照"
        for h_ in hits[:150]:
            try:
                q = get_quote(h_["symbol"])
                if h_.get("price") is None:
                    if q.price:
                        h_["price"] = q.price
                    if q.change_pct:
                        h_["pct"] = q.change_pct
                if h_.get("pe") is None and q.pe:
                    h_["pe"] = q.pe
                if h_.get("cap") is None and q.total_cap:
                    h_["cap"] = q.total_cap
            except Exception:  # noqa: BLE001
                pass
        if limit:
            _scan_state["stage"] = "冒烟扫描完成（不写入缓存）"
            _scan_state["preview"] = hits
        else:
            _scan_state["preview"] = []
            payload = {"generated_ts": time.time(),
                       "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "universe": len(universe), "count": len(hits), "items": hits,
                       "elapsed": round(time.time() - _scan_state["started"], 1)}
            tmp = SCAN_CACHE_FILE + ".tmp"
            last_err: Exception | None = None
            for attempt in range(3):        # 杀软/同步盘可能短暂锁文件，重试兜底
                try:
                    with open(tmp, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh, ensure_ascii=False)
                    os.replace(tmp, SCAN_CACHE_FILE)
                    last_err = None
                    break
                except OSError as wexc:
                    last_err = wexc
                    time.sleep(0.5 * (attempt + 1))
            if last_err is not None:
                raise last_err
            _scan_cache, _scan_cache_ts = payload, payload["generated_ts"]
            _scan_state["stage"] = "扫描完成"
        _scan_state["hits"] = len(hits)
        _scan_state["error"] = ""
        _scan_state["running"] = False
        _scan_state["finished"] = time.time()
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()          # 后台线程异常必须落到服务日志，否则无迹可查
        _scan_state["running"] = False
        _scan_state["finished"] = time.time()
        _scan_state["stage"] = "扫描失败"
        _scan_state["error"] = f"{type(exc).__name__}: {exc}"


def _scan_status() -> dict:
    st = {k: _scan_state[k] for k in ("running", "done", "total", "hits", "stage", "error",
                                      "started", "finished")}
    if _scan_state.get("preview"):
        st["preview"] = _scan_state["preview"]
    return st


def screener_scan(limit: int | None = None) -> dict:
    """启动一次后台扫描（正在运行则直接返回进度）。limit 仅用于冒烟验证，不写缓存。"""
    with _scan_lock:
        if _scan_state["running"]:
            return {"ok": True, **_scan_status()}
        _scan_state.update({"running": True, "done": 0, "total": 0, "hits": 0, "preview": [],
                            "stage": "准备启动", "error": "", "started": time.time(),
                            "finished": 0.0})
        t = threading.Thread(target=_scan_worker, kwargs={"limit": limit}, daemon=True)
    t.start()
    return {"ok": True, "running": True}


def _in_trading_window() -> bool:
    """是否处于 A股交易日盘中窗口（周一至周五 09:00-15:30 本地时间）。

    用于「交易日每 2 小时自动更新」的定时刷新判定：只在交易时段内
    拉起全量扫描，避免非交易时段的空扫浪费请求配额。
    """
    import datetime as _dt
    now = _dt.datetime.now()
    if now.weekday() >= 5:      # 周末
        return False
    hm = now.hour * 60 + now.minute
    return 9 * 60 <= hm <= 15 * 60 + 30


def screener_results() -> dict:
    """当前扫描结果 + 进度；从未扫描过或交易日缓存超 2 小时自动触发后台全量扫描。

    自动触发冷却：缓存缺失（如历史写盘失败）时 10 分钟、交易日过期刷新
    30 分钟，避免轮询流量反复拉起 20 分钟级的全量扫描。
    """
    global _last_auto_ts
    with _scan_lock:
        running = _scan_state["running"]
    cache = _scan_load_cache()
    now = time.time()
    cache_ts = float((cache or {}).get("generated_ts") or 0)
    need_scan = cache is None or (cache_ts and now - cache_ts > SCAN_TTL and _in_trading_window())
    if need_scan and not running and now - _last_auto_ts > (1800 if cache else 600):
        _last_auto_ts = now
        screener_scan()
        with _scan_lock:
            running = _scan_state["running"]
    items = (cache or {}).get("items") or []
    stale = bool(cache) and (now - cache_ts > SCAN_TTL)
    return {"ok": True, "criteria": SCAN_CRITERIA,
            "generated_at": (cache or {}).get("generated_at", ""),
            "universe": (cache or {}).get("universe", 0),
            "elapsed": (cache or {}).get("elapsed", 0),
            "stale": stale, "count": len(items), "items": items,
            "scan": _scan_status()}
