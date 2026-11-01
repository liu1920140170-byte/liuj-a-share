# -*- coding: utf-8 -*-
"""指标与结构分析：均线 / ATR / 相对量 / 摆动点 / 交易区间 / 趋势结构 / 阶段判定 / 关键价位。

威科夫理论的核心是「结构 + 量价」，所以这一层的任务是把 K 线翻译成：
    1. 当前是否处于横盘震荡区间（Cause/原因），区间上下沿在哪；
    2. 大级别趋势（日线、周线）是多是空；
    3. 现在处在威科夫九大阶段的哪一步（A~E 子阶段）；
    4. 关键支撑阻力位（含冰线 Ice、溪流 Creek 这类威科夫叫法）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .datasource import Bar


# --------------------------------------------------------------------------- #
# 基础指标
# --------------------------------------------------------------------------- #
def sma(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if n <= 0:
        return out
    total = 0.0
    for i, value in enumerate(values):
        total += value
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def atr(bars: list[Bar], n: int = 14) -> list[float]:
    """平均真实波幅（Wilder 平滑）。"""
    out: list[float] = []
    prev_close = None
    value = 0.0
    for i, bar in enumerate(bars):
        tr = bar.high - bar.low
        if prev_close is not None:
            tr = max(tr, abs(bar.high - prev_close), abs(bar.low - prev_close))
        if i == 0:
            value = tr
        else:
            value = (value * (n - 1) + tr) / n
        out.append(value)
        prev_close = bar.close
    return out


def linreg_slope(values: list[float]) -> float:
    """最小二乘斜率（每根 K 线的价格变化）。"""
    n = len(values)
    if n < 2:
        return 0.0
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((i - mean_x) * (v - mean_y) for i, v in enumerate(values))
    den = sum((i - mean_x) ** 2 for i in range(n))
    return num / den if den else 0.0


@dataclass
class Indicators:
    ma5: list = field(default_factory=list)
    ma10: list = field(default_factory=list)
    ma20: list = field(default_factory=list)
    ma60: list = field(default_factory=list)
    ma120: list = field(default_factory=list)
    atr: list = field(default_factory=list)
    vol_ma5: list = field(default_factory=list)
    vol_ma20: list = field(default_factory=list)
    rel_vol: list = field(default_factory=list)     # 相对 20 日均量
    spread: list = field(default_factory=list)      # 当日振幅（元）
    spread_ratio: list = field(default_factory=list)  # 振幅 / ATR
    clv: list = field(default_factory=list)         # 收盘位置 (c-l)/(h-l)
    pct: list = field(default_factory=list)         # 涨跌幅 %


def build_indicators(bars: list[Bar]) -> Indicators:
    n = len(bars)
    closes = [b.close for b in bars]
    vols = [b.volume for b in bars]
    ind = Indicators()
    ind.ma5 = sma(closes, 5)
    ind.ma10 = sma(closes, 10)
    ind.ma20 = sma(closes, 20)
    ind.ma60 = sma(closes, 60)
    ind.ma120 = sma(closes, 120)
    ind.atr = atr(bars, 14)
    ind.vol_ma5 = sma(vols, 5)
    ind.vol_ma20 = sma(vols, 20)
    ind.rel_vol = []
    ind.spread = []
    ind.spread_ratio = []
    ind.clv = []
    ind.pct = []
    for i, bar in enumerate(bars):
        base = ind.vol_ma20[i] if i < len(ind.vol_ma20) else None
        prev_base = ind.vol_ma20[i - 1] if i >= 1 else None
        ref = prev_base or base
        ind.rel_vol.append(round(bar.volume / ref, 2) if ref else 1.0)
        spread = max(bar.high - bar.low, 1e-9)
        ind.spread.append(spread)
        ind.spread_ratio.append(round(spread / ind.atr[i], 2) if ind.atr[i] else 1.0)
        ind.clv.append(round((bar.close - bar.low) / spread, 3))
        prev_close = closes[i - 1] if i else bar.open
        ind.pct.append(round((bar.close - prev_close) / prev_close * 100.0, 2) if prev_close else 0.0)
    return ind


def swings(bars: list[Bar], k: int = 3) -> list[dict]:
    """摆动高低点（分形法）。"""
    out: list[dict] = []
    for i in range(k, len(bars) - k):
        window = bars[i - k:i + k + 1]
        if bars[i].high >= max(b.high for b in window):
            out.append({"index": i, "price": bars[i].high, "kind": "high", "date": bars[i].date})
        elif bars[i].low <= min(b.low for b in window):
            out.append({"index": i, "price": bars[i].low, "kind": "low", "date": bars[i].date})
    return out


def trend_lines(bars: list[Bar], k: int = 3) -> dict:
    """供给线 / 需求线：连接摆动高低点的一对趋势线。

    供给线连接两个摆动高点（上方供给压力），需求线连接两个摆动低点（下方
    需求支撑）。有效性规则（威科夫图表基本功）：连线不得「切穿」K线——
    供给线全程位于高点之上、需求线全程位于低点之下（含右锚之后延伸到
    当前，保证线对当下仍有意义）。右锚优先取最近的摆动点，左锚在有效
    前提下取尽可能远的，使线覆盖足够长的行情。
    返回 {"supply": {...}|None, "demand": {...}|None}。
    """
    sw = swings(bars, k)
    highs = [p for p in sw if p["kind"] == "high"]
    lows = [p for p in sw if p["kind"] == "low"]
    n = len(bars)

    def build(points: list[dict], side: str) -> dict | None:
        min_span = max(10, n // 12)   # 太短的连线只是局部噪音
        for j in range(len(points) - 1, 0, -1):      # 右锚：最近的摆动点优先
            b = points[j]
            for i in range(j):                       # 左锚：越远越有代表性
                a = points[i]
                span = b["index"] - a["index"]
                if span < min_span:
                    continue
                slope = (b["price"] - a["price"]) / span
                ok = True
                for m in range(a["index"] + 1, n):
                    if m >= b["index"]:              # 右锚之后的延伸段容差放宽，
                        tol = 1.006 if side == "high" else 0.994   # 允许小幅回抽/反抽
                    else:                            # 锚点之间要求严格贴合
                        tol = 1.0035 if side == "high" else 0.9965
                    v = a["price"] + slope * (m - a["index"])
                    if side == "high" and bars[m].high > v * tol:
                        ok = False
                        break
                    if side == "low" and bars[m].low < v * tol:
                        ok = False
                        break
                if ok:
                    return {"a_index": a["index"], "a_price": round(a["price"], 2),
                            "a_date": a["date"], "b_index": b["index"],
                            "b_price": round(b["price"], 2), "b_date": b["date"]}
        return None

    return {"supply": build(highs, "high"), "demand": build(lows, "low")}


# --------------------------------------------------------------------------- #
# 交易区间（Trading Range）
# --------------------------------------------------------------------------- #
@dataclass
class Range:
    valid: bool = False
    high: float = 0.0
    low: float = 0.0
    mid: float = 0.0
    width_pct: float = 0.0
    bars: int = 0
    start_index: int = 0
    end_index: int = 0
    start_date: str = ""
    end_date: str = ""
    touches_high: int = 0
    touches_low: int = 0
    drift_pct: float = 0.0
    prior_change_pct: float = 0.0
    breakout: str = ""        # up / down / ""
    breakout_level: float = 0.0
    breakout_index: int | None = None
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "valid": self.valid, "high": round(self.high, 2), "low": round(self.low, 2),
            "mid": round(self.mid, 2), "width_pct": round(self.width_pct * 100, 2),
            "bars": self.bars, "start": self.start_date, "end": self.end_date,
            "start_index": self.start_index, "end_index": self.end_index,
            "touches_high": self.touches_high, "touches_low": self.touches_low,
            "drift_pct": round(self.drift_pct * 100, 2),
            "prior_change_pct": round(self.prior_change_pct * 100, 2),
            "breakout": self.breakout, "breakout_level": round(self.breakout_level, 2),
        }


def find_range(bars: list[Bar], max_look: int = 140, min_look: int = 18) -> Range:
    """寻找「当前」交易区间：最近一段横向震荡的价格箱体。

    做法：枚举若干「结束位置 × 长度」窗口，对每个窗口检查
        横向度（回归斜率小）、箱体宽度适中、上下沿被反复触碰、收盘价集中在箱体中部，
    再综合打分取最优。
    """
    n = len(bars)
    rng = Range()
    if n < min_look + 5:
        return rng
    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    best: Range | None = None
    for end in range(n, max(n - 7, min_look + 4) - 1, -1):
        for length in range(min(max_look, end), min_look - 1, -2):
            start = end - length
            win_h = max(highs[start:end])
            win_l = min(lows[start:end])
            if win_l <= 0:
                continue
            mid = (win_h + win_l) / 2.0
            width = (win_h - win_l) / mid
            if width > 0.40 or width < 0.035:
                continue
            slope = linreg_slope(closes[start:end])
            drift = slope * length / mid
            if abs(drift) > 0.20:
                continue
            touch_hi = sum(1 for i in range(start, end) if highs[i] >= win_h * 0.98)
            touch_lo = sum(1 for i in range(start, end) if lows[i] <= win_l * 1.02)
            if touch_hi < 2 or touch_lo < 2:
                continue
            band = sum(1 for i in range(start, end)
                       if win_l + 0.2 * (win_h - win_l) <= closes[i] <= win_h - 0.2 * (win_h - win_l))
            if band / length < 0.35:
                continue
            score = (min(touch_hi, 6) * 4 + min(touch_lo, 6) * 4
                     + min(length, 120) / 120.0 * 30
                     + (1 - abs(drift) / 0.20) * 15
                     - max(0.0, width - 0.30) * 120
                     + max(0, 6 - (n - end)))
            if width <= 0.22:
                score += 5
            if best is None or score > best.score:
                candidate = Range(
                    valid=True, high=win_h, low=win_l, mid=mid, width_pct=width,
                    bars=length, start_index=start, end_index=end - 1,
                    start_date=bars[start].date, end_date=bars[end - 1].date,
                    touches_high=touch_hi, touches_low=touch_lo, drift_pct=drift, score=score,
                )
                best = candidate

    if best is None:
        # 兜底：用最近 60 根的高低点作为「弱区间」，不参与强判定
        start = max(0, n - 60)
        win_h = max(highs[start:])
        win_l = min(lows[start:])
        mid = (win_h + win_l) / 2.0
        rng.high, rng.low, rng.mid = win_h, win_l, mid
        rng.width_pct = (win_h - win_l) / mid if mid else 0.0
        rng.bars = n - start
        rng.start_index, rng.end_index = start, n - 1
        rng.start_date, rng.end_date = bars[start].date, bars[-1].date
        rng.valid = False
        return rng

    rng = best
    # 区间形成前的走势（决定吸筹还是派发的基本背景）
    prior_start = max(0, rng.start_index - 40)
    if closes[prior_start]:
        rng.prior_change_pct = (closes[rng.start_index] - closes[prior_start]) / closes[prior_start]
    # 是否已经突破区间
    if closes[-1] > rng.high * 1.01 and rng.end_index < n - 1:
        rng.breakout, rng.breakout_level = "up", rng.high
        rng.breakout_index = next((i for i in range(rng.end_index + 1, n)
                                   if closes[i] > rng.high * 1.01), None)
    elif closes[-1] < rng.low * 0.99 and rng.end_index < n - 1:
        rng.breakout, rng.breakout_level = "down", rng.low
        rng.breakout_index = next((i for i in range(rng.end_index + 1, n)
                                   if closes[i] < rng.low * 0.99), None)
    return rng


# --------------------------------------------------------------------------- #
# 趋势结构
# --------------------------------------------------------------------------- #
def trend_of(bars: list[Bar], ind: Indicators, span: int = 60) -> dict:
    """返回 {'dir': up/down/sideways, 'strength': 0-100, 'text': ...}"""
    if len(bars) < 20:
        return {"dir": "sideways", "strength": 0, "text": "数据不足"}
    closes = [b.close for b in bars]
    tail = closes[-min(span, len(closes)):]
    slope = linreg_slope(tail)
    atr_now = ind.atr[-1] or (sum(tail) / len(tail) * 0.02)
    normalized = slope * len(tail) / atr_now  # 以 ATR 为单位的整体位移
    ma20 = ind.ma20[-1] or closes[-1]
    ma60 = ind.ma60[-1] or closes[-1]
    ma_bull = ma20 > ma60
    if normalized > 1.2:
        direction, strength = "up", min(95, 45 + normalized * 12)
        text = f"近{len(tail)}根K线累计上行约 {normalized:.1f} 倍ATR"
    elif normalized < -1.2:
        direction, strength = "down", min(95, 45 + abs(normalized) * 12)
        text = f"近{len(tail)}根K线累计下行约 {abs(normalized):.1f} 倍ATR"
    else:
        direction, strength = "sideways", max(20, 45 - abs(normalized) * 12)
        text = f"近{len(tail)}根K线横向震荡（{normalized:+.1f} 倍ATR）"
    if (direction == "up") != ma_bull and abs(normalized) < 2.5:
        text += "；均线排列与斜率略有分歧"
    return {"dir": direction, "strength": round(strength), "text": text}


def long_trend_of(bars: list[Bar], ind: Indicators) -> dict:
    """基于全部已加载K线判定长期趋势背景（主要摆动结构 + MA120 + 长窗口斜率）。

    为阶段判定提供「背景」：长期下降趋势后的震荡更可能是吸筹，
    长期上升趋势后的高位震荡更可能是派发。
    """
    n = len(bars)
    if n < 40:
        return {"dir": "sideways", "strength": 0, "bars": n,
                "text": f"仅{n}根K线，数量不足，暂无法评估长期趋势"}
    closes = [b.close for b in bars]
    price = closes[-1]

    # ① 大级别摆动结构（k=5 取主要摆动点，比较相邻同类型摆动点的高低关系）
    sw = swings(bars, 5)[-10:]
    hh = hl = lh = ll = 0
    for a, b in zip(sw, sw[1:]):
        if a["kind"] == "high" and b["kind"] == "high":
            if b["price"] > a["price"]:
                hh += 1
            else:
                lh += 1
        elif a["kind"] == "low" and b["kind"] == "low":
            if b["price"] > a["price"]:
                hl += 1
            else:
                ll += 1
    up_struct, dn_struct = hh + hl, lh + ll

    # ② MA120 位置关系 + 长窗口线性斜率（以 ATR 归一；超长历史时斜率看最近 500 根）
    ma120 = ind.ma120[-1] if ind.ma120 and ind.ma120[-1] else None
    tail_n = min(n, 500)
    slope = linreg_slope(closes[-tail_n:]) * tail_n / (ind.atr[-1] or price * 0.02)

    # ③ 大级别坐标系：MA250（年线）+ 近250根高低区间分位——阶段判定必须放在大趋势坐标下看
    ma250 = sum(closes[-250:]) / 250 if n >= 250 else None
    pos250 = None
    if n >= 60:
        hi250 = max(b.high for b in bars[-250:])
        lo250 = min(b.low for b in bars[-250:])
        if hi250 > lo250:
            pos250 = round((price - lo250) / (hi250 - lo250), 3)

    score = 0
    parts: list[str] = []
    if up_struct - dn_struct >= 2:
        score += 1
        parts.append(f"主要摆动高低点抬高（向上{up_struct}次 vs 向下{dn_struct}次）")
    elif dn_struct - up_struct >= 2:
        score -= 1
        parts.append(f"主要摆动高低点降低（向下{dn_struct}次 vs 向上{up_struct}次）")
    else:
        parts.append("主要摆动高低点互现（宽幅震荡结构）")
    if ma120:
        if price > ma120:
            score += 1
            parts.append("价格站上 MA120")
        else:
            score -= 1
            parts.append("价格位于 MA120 下方")
    if ma250:
        if price > ma250:
            score += 1
            parts.append("价格站上 MA250（年线）")
        else:
            score -= 1
            parts.append("价格位于 MA250（年线）下方")
    if pos250 is not None:
        if pos250 <= 0.35:
            parts.append(f"现价处于近250根高低区间的{pos250 * 100:.0f}%分位（大区间低位）")
        elif pos250 >= 0.65:
            parts.append(f"现价处于近250根高低区间的{pos250 * 100:.0f}%分位（大区间高位）")
        else:
            parts.append(f"现价处于近250根高低区间的{pos250 * 100:.0f}%分位（大区间中部）")
    slope_label = f"近{tail_n}根" if tail_n < n else "全程"
    if slope > 1.0:
        score += 1
        parts.append(f"{slope_label}斜率向上（{slope:+.1f} 倍ATR）")
    elif slope < -1.0:
        score -= 1
        parts.append(f"{slope_label}斜率向下（{slope:+.1f} 倍ATR）")
    else:
        parts.append(f"{slope_label}斜率持平（{slope:+.1f} 倍ATR）")

    if score >= 2:
        direction, strength = "up", min(92, 55 + score * 12)
    elif score <= -2:
        direction, strength = "down", min(92, 55 + abs(score) * 12)
    else:
        direction, strength = "sideways", 40
    cn = {"up": "长期上升趋势", "down": "长期下降趋势", "sideways": "长期宽幅震荡"}[direction]
    return {"dir": direction, "strength": round(strength), "bars": n, "pos250": pos250,
            "text": f"全部{n}根K线判定为{cn}（" + "；".join(parts) + "）"}


# --------------------------------------------------------------------------- #
# 阶段判定
# --------------------------------------------------------------------------- #
PHASE_CN = {
    "accumulation": "吸筹（Accumulation）",
    "distribution": "派发（Distribution）",
    "markup": "上涨（Markup）",
    "markdown": "下跌（Markdown）",
    "unknown": "结构不明（Unknown）",
}

SUB_PHASE_CN = {
    "Phase A": "A 阶段：下跌/上涨停止",
    "Phase B": "B 阶段：建仓/派发（震荡蓄势）",
    "Phase C": "C 阶段：测试（Spring / UTAD 震仓）",
    "Phase D": "D 阶段：区间内走强/走弱（SOS / SOW）",
    "Phase E": "E 阶段：突破，走出趋势",
    "": "",
}


def classify_phase(bars: list[Bar], ind: Indicators, rng: Range, events: list[dict],
                   vsa: list[dict], market: dict | None = None,
                   long_ctx: dict | None = None) -> dict:
    """综合区间结构、事件与量价，判定当前威科夫阶段与子阶段。"""
    closes = [b.close for b in bars]
    price = closes[-1]
    recent_events = [e for e in events if e["index"] >= max(0, len(bars) - 90)]
    types = {e["type"] for e in recent_events}
    vsa_types = {v["type"] for v in vsa if v["index"] >= max(0, len(bars) - 60)}
    # 最近 35 根K线内的信号才算「正在发生」，过期信号只作为背景
    hot_types = {e["type"] for e in events if e["index"] >= max(0, len(bars) - 35)}

    up_vol = sum(b.volume for i, b in enumerate(bars[-60:]) if ind.pct[len(bars) - 60 + i] > 0)
    down_vol = sum(b.volume for i, b in enumerate(bars[-60:]) if ind.pct[len(bars) - 60 + i] < 0)
    ratio = (up_vol / down_vol) if down_vol else 2.0

    acc = 0.0
    dist = 0.0
    reasons: list[str] = []

    prior = rng.prior_change_pct if rng.valid else 0.0
    if rng.valid and prior <= -0.12:
        acc += 18
        reasons.append(f"区间形成前累计下跌 {abs(prior)*100:.1f}%，具备吸筹的「背景」条件")
    elif rng.valid and prior >= 0.12:
        dist += 18
        reasons.append(f"区间形成前累计上涨 {prior*100:.1f}%，具备派发的「背景」条件")

    if rng.valid:
        rel = (price - rng.low) / (rng.high - rng.low) if rng.high > rng.low else 0.5
        if rel <= 0.4:
            acc += 8
            reasons.append(f"股价处于区间下部（{rel*100:.0f}% 位置），下方空间相对有限")
        elif rel >= 0.6:
            dist += 8
            reasons.append(f"股价处于区间上部（{rel*100:.0f}% 位置），上方压力渐重")

    if ratio >= 1.15:
        acc += 10
        reasons.append(f"近60根K线上涨成交量/下跌成交量 = {ratio:.2f}，需求占优")
    elif ratio <= 0.87:
        dist += 10
        reasons.append(f"近60根K线上涨成交量/下跌成交量 = {ratio:.2f}，供给占优")

    bull_events = {"Spring", "Shakeout", "SOS", "LPS", "Test", "SC", "ST", "AR", "PS", "BU"}
    bear_events = {"UT", "UTAD", "SOW", "LPSY", "BC"}
    if types & {"Spring", "Shakeout"}:
        acc += 18
        reasons.append("出现 Spring / 震仓（假破位后快速收回），是吸筹 C 阶段的典型动作")
    if "Test" in types:
        acc += 10
        reasons.append("Spring 之后出现缩量测试（Test），抛压枯竭")
    if types & {"SOS"}:
        acc += 12
        reasons.append("出现强势信号 SOS（放量宽幅阳线突破近端压力）")
    if types & {"LPS", "BU"}:
        acc += 10
        reasons.append("出现 LPS / 回抽确认，回踩不破关键支撑")
    if "SC" in types:
        acc += 8
        reasons.append("出现抛售高潮 SC，下跌动能被大量承接")
    if types & {"UT", "UTAD"}:
        dist += 18
        reasons.append("出现上冲失败 UT / UTAD（冲高后收回区间内），是派发 C 阶段的典型动作")
    if "SOW" in types:
        dist += 12
        reasons.append("出现弱势信号 SOW（放量宽幅阴线跌破近端支撑）")
    if "LPSY" in types:
        dist += 10
        reasons.append("出现 LPSY（反弹无力、高点降低），供给持续压制")
    if "BC" in types:
        dist += 8
        reasons.append("出现买入高潮 BC，追高动能被大量派发")

    if "NoSupply" in vsa_types:
        acc += 6
        reasons.append("VSA：出现「无供给」缩量小阴，抛压枯竭")
    if "NoDemand" in vsa_types:
        dist += 6
        reasons.append("VSA：出现「无需求」缩量小阳，买盘不足")
    if "StoppingVolume" in vsa_types or "DemandComingIn" in vsa_types:
        acc += 5
    if "SupplyComingIn" in vsa_types:
        dist += 5

    # 最新 35 根K线内的信号额外加权：威科夫看的是「正在发生什么」，而不是三个月前发生过什么
    hot_bull = hot_types & {"Spring", "Shakeout", "Test", "SOS", "LPS", "BU", "SC", "ST"}
    hot_bear = hot_types & {"UT", "UTAD", "SOW", "LPSY", "BC"}
    if hot_bull:
        acc += len(hot_bull) * 5
        reasons.append(f"最近 35 根K线内出现偏多信号：{'、'.join(sorted(hot_bull))}")
    if hot_bear:
        dist += len(hot_bear) * 5
        reasons.append(f"最近 35 根K线内出现偏空信号：{'、'.join(sorted(hot_bear))}")

    ma20 = ind.ma20[-1] or price
    ma60 = ind.ma60[-1] or price
    if ma20 > ma60:
        acc += 6
    elif ma20 < ma60:
        dist += 6

    if market:
        idx_trend = (market.get("trend") or {}).get("dir")
        if idx_trend == "up":
            acc += 4
        elif idx_trend == "down":
            dist += 4

    # 全部K线的长期趋势背景：长期下降后的区间偏向吸筹，长期上升后的区间偏向派发。
    # 用户要求：阶段判定必须放到大趋势背景下看，大背景权重不低于局部信号
    long_dir = str((long_ctx or {}).get("dir") or "")
    if long_dir == "down":
        acc += 10
        reasons.append("大趋势背景（全部K线）为下降趋势" +
                       ("，区间更可能是吸筹而非派发" if rng.valid else ""))
    elif long_dir == "up":
        dist += 10
        reasons.append("大趋势背景（全部K线）为上升趋势" +
                       ("，区间更可能是派发而非吸筹" if rng.valid else ""))

    # 大级别坐标锚定：现价在近250根高低区间的位置，低位背景利多吸筹定性、高位背景利空派发定性
    pos250 = (long_ctx or {}).get("pos250")
    if pos250 is not None:
        if pos250 <= 0.35 and long_dir in ("down", "sideways"):
            acc += 8
            reasons.append(f"现价处于近250根高低区间{pos250 * 100:.0f}%分位（大区间低位），"
                           "大级别背景支持吸筹/底部定性")
        elif pos250 >= 0.65 and long_dir in ("up", "sideways"):
            dist += 8
            reasons.append(f"现价处于近250根高低区间{pos250 * 100:.0f}%分位（大区间高位），"
                           "大级别背景支持派发/顶部定性")

    # 决定阶段
    trend = trend_of(bars, ind, 60)
    sub_types = hot_types if hot_types else types
    if rng.valid:
        if rng.breakout == "up" and (acc >= dist) and (rng.end_index is None or rng.end_index >= len(bars) - 25):
            phase = "markup"
            sub = "Phase E"
        elif rng.breakout == "down" and (dist > acc):
            phase = "markdown"
            sub = "Phase E"
        elif acc - dist >= 8 and trend["dir"] == "down" and (acc - dist) < 18:
            # 区间内偏多证据与日线下降趋势冲突，不硬给吸筹结论
            phase = "unknown"
            sub = "Phase B"
            reasons.append("区间内虽有一些偏多迹象，但日线仍处于下降趋势且证据强度不足，"
                           "暂按「结构不明」处理，等方向确认后再定性")
        elif dist - acc >= 8 and trend["dir"] == "up" and (dist - acc) < 18:
            phase = "unknown"
            sub = "Phase B"
            reasons.append("区间内偏空迹象与日线上升趋势冲突，暂按「结构不明」处理")
        elif acc - dist >= 8:
            phase = "accumulation"
            if sub_types & {"Spring", "Shakeout", "Test"}:
                sub = "Phase C"
            elif sub_types & {"SOS", "LPS", "BU"}:
                sub = "Phase D"
            elif sub_types & {"SC", "AR", "ST", "PS"}:
                sub = "Phase A"
            else:
                sub = "Phase B"
        elif dist - acc >= 8:
            phase = "distribution"
            if sub_types & {"UT", "UTAD"}:
                sub = "Phase C"
            elif sub_types & {"SOW", "LPSY"}:
                sub = "Phase D"
            elif sub_types & {"BC"}:
                sub = "Phase A"
            else:
                sub = "Phase B"
        else:
            phase = "unknown"
            sub = "Phase B"
    else:
        if trend["dir"] == "up":
            phase, sub = "markup", "Phase E"
        elif trend["dir"] == "down":
            phase, sub = "markdown", "Phase E"
        else:
            phase, sub = "unknown", ""

    bias = "bull" if acc > dist else ("bear" if dist > acc else "neutral")
    confidence = 42 + min(20, abs(acc - dist) * 0.55)
    if rng.valid:
        confidence += 8
    if len(bars) >= 250:
        confidence += 5
    if len(bars) < 130:
        confidence -= 12
    if phase == "unknown":
        confidence -= 8
    confidence = int(max(15, min(88, round(confidence))))

    if not reasons:
        reasons.append("尚未观察到明确的威科夫事件，结构信息有限，建议继续跟踪")

    label = PHASE_CN.get(phase, phase)
    long_cn = {"up": "长期上升趋势", "down": "长期下降趋势", "sideways": "长期宽幅震荡"}.get(long_dir, "")
    base_summary = f"当前判定为{label}{('，' + SUB_PHASE_CN.get(sub, '')) if sub else ''}"
    summary = (f"置于{long_cn}的大背景下，{base_summary}") if long_cn else base_summary
    return {
        "phase": phase, "phase_cn": label, "sub_phase": sub,
        "sub_phase_cn": SUB_PHASE_CN.get(sub, ""),
        "confidence": confidence, "bias": bias,
        "acc_score": round(acc, 1), "dist_score": round(dist, 1),
        "reasons": reasons[:9],
        "summary": summary,
        "long_context": long_cn,
    }


# --------------------------------------------------------------------------- #
# 关键价位 / 因果量度
# --------------------------------------------------------------------------- #
def key_levels(bars: list[Bar], ind: Indicators, rng: Range, events: list[dict]) -> list[dict]:
    price = bars[-1].close
    levels: list[dict] = []

    def add(kind: str, value: float, label: str, strength: int):
        if value and value > 0:
            levels.append({
                "type": kind, "price": round(value, 2), "label": label,
                "strength": int(max(10, min(95, strength))),
                "distance_pct": round((value - price) / price * 100, 2),
            })

    if rng.high and rng.low:
        add("resistance", rng.high, "区间上沿（溪流 Creek / 派发区上沿）",
            50 + rng.touches_high * 6 + (10 if rng.valid else 0))
        add("support", rng.low, "区间下沿 / 冰线（Ice）",
            50 + rng.touches_low * 6 + (10 if rng.valid else 0))

    for event in events[-14:]:
        if event["type"] in ("Spring", "Shakeout", "SC", "Test"):
            add("support", event["price"] * 0.995, f"{event['type_cn']}低点", 55 + event["score"] * 0.3)
        elif event["type"] in ("UT", "UTAD", "BC"):
            add("resistance", event["price"] * 1.005, f"{event['type_cn']}高点", 55 + event["score"] * 0.3)

    sw = swings(bars, 3)
    for item in sw[-10:]:
        kind = "resistance" if item["kind"] == "high" else "support"
        label = "近期摆动高点" if kind == "resistance" else "近期摆动低点"
        add(kind, item["price"], label, 40)

    for name, series in (("MA20", ind.ma20), ("MA60", ind.ma60), ("MA120", ind.ma120)):
        value = series[-1] if series else None
        if value:
            kind = "support" if value < price else "resistance"
            add(kind, value, f"{name} 均线（动态{'支撑' if kind == 'support' else '压力'}）", 45)

    w = bars[-min(len(bars), 250):]
    add("resistance", max(b.high for b in w), "近一年最高点", 65)
    add("support", min(b.low for b in w), "近一年最低点", 65)

    # 合并过于接近的价位
    levels.sort(key=lambda x: x["price"])
    merged: list[dict] = []
    for item in levels:
        if merged and abs(item["price"] - merged[-1]["price"]) / price < 0.012:
            if item["strength"] > merged[-1]["strength"]:
                merged[-1] = item
            continue
        merged.append(item)
    merged.sort(key=lambda x: -x["strength"])
    return merged[:10]


def cause_effect(rng: Range, price: float) -> dict:
    """因果量度：横盘（原因）越久越大，突破后的目标（结果）越高。"""
    if not rng.valid or rng.high <= rng.low:
        return {"text": "尚未形成有效横盘区间，无法进行因果量度", "target": 0.0,
                "targets": [], "cause_bars": rng.bars}
    height = rng.high - rng.low
    factor = min(1.2, 0.8 + min(1.2, rng.bars / 100.0))  # 横盘越久量度目标越大（上限 1.2 倍区间高度）
    up_target = rng.high + height * factor
    down_target = rng.low - height * factor
    base = rng.high if price >= rng.mid else rng.low
    target = up_target if price >= rng.mid else down_target
    text = (f"横盘蓄势 {rng.bars} 根K线（区间 {rng.low:.2f}~{rng.high:.2f}，高度 {height:.2f}），"
            f"按因果量度系数 {factor:.2f} 计算：向上目标 {up_target:.2f}，向下目标 {down_target:.2f}")
    return {"text": text, "target": round(target, 2), "cause_bars": rng.bars,
            "up_target": round(up_target, 2), "down_target": round(down_target, 2),
            "height": round(height, 2), "factor": round(factor, 2), "base": round(base, 2)}


def volume_stats(bars: list[Bar], ind: Indicators, span: int = 60) -> dict:
    start = max(0, len(bars) - span)
    up_vol = sum(bars[i].volume for i in range(start, len(bars)) if ind.pct[i] > 0)
    down_vol = sum(bars[i].volume for i in range(start, len(bars)) if ind.pct[i] < 0)
    ratio = (up_vol / down_vol) if down_vol else 0.0
    if ratio >= 1.25:
        text = f"近{span}根K线上涨量/下跌量 = {ratio:.2f}，需求明显大于供给（偏多）"
    elif ratio >= 1.05:
        text = f"近{span}根K线上涨量/下跌量 = {ratio:.2f}，需求略占优"
    elif ratio >= 0.85:
        text = f"近{span}根K线上涨量/下跌量 = {ratio:.2f}，供需基本均衡"
    elif ratio > 0:
        text = f"近{span}根K线上涨量/下跌量 = {ratio:.2f}，供给大于需求（偏空）"
    else:
        text = "成交量数据不足，无法评估供需"
    return {"text": text, "up_down_vol_ratio": round(ratio, 2),
            "up_vol": round(up_vol, 1), "down_vol": round(down_vol, 1)}
