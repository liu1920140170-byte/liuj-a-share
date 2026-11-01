"""W-M 模型：威科夫-均线共振自上而下选股模型。

四层结构（与用户给定框架一一对应）：
  第一层 宏观环境：三大指数均线位置/20日线拐头 → 安全/震荡/危险区 + 主线板块共振
  第二层 量化海选：均线金叉与 20 日线拐头 + 量能基础 + 流动性（成交额/市值）
  第三层 威科夫精选：UT 排雷 → 左侧吸筹区间定性 → A(SOS/LPS) / B(Spring) 买点
  第四层 交易执行：买入区间、止损（MA20 / 结构 / 弹簧）、止盈（5/10 日线、UT、背离）

对外只暴露 wm_analyze(code)，Web 路由 /api/wm 调用。
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from .analysis import (build_indicators, cause_effect, classify_phase, find_range,
                       long_trend_of, swings, trend_of)
from .datasource import (DataSourceError, get_bars, get_index_quotes, get_intraday,
                         get_quote, is_index, rescale_to_current, resolve_input,
                         split_code, _secid)
from .events import detect
from .http_client import fetch_json, fetch_text

INDEX_LIST = [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]

EM_BOARD_TOP = ("https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=20&po=1&np=1"
                "&fltt=2&invt=2&fid=f3&fs=m:90+t:2&fields=f12,f14,f3,f62,f104,f105")
EM_STOCK_PROFILE = ("https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
                    "&fields=f57,f58,f116,f117,f127,f128")
EM_F10_SURVEY = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax?code={secu}"
EM_BK_SUGGEST = ("https://searchapi.eastmoney.com/api/suggest/get?input={q}"
                 "&type=14&token=D43BF722C8E33BDC906FB84D85E326E8&count=8")
SINA_INDUSTRY = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
# 行业板块「近 N 日涨幅榜前 10 次数」：同花顺行业分类（881xxx，纯行业、不含概念板块 885xxx）
THS_LIST = "http://q.10jqka.com.cn/thshy/"
THS_KLINE = "http://d.10jqka.com.cn/v6/line/48_{code}/01/last.js"
THS_REF = "http://q.10jqka.com.cn/"
# 降级口径：腾讯申万二级行业（pt 代码，124 个行业板块，同样不含概念板块）
TX_BOARD_KIND = "hy2"                       # hy=申万一级(31) / hy2=申万二级(124)
TX_BOARD_RANK = ("https://proxy.finance.qq.com/cgi/cgi-bin/rank/pt/getRank?board_type={bt}"
                 "&sort_type=price&direct=down&offset={off}&count={cnt}")
TX_BOARD_KLINE = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
                  "?param={code},day,,,{n},qfq")
HIST_DAYS = 7
HIST_TTL = 600.0
_THS_LIST_CACHE: tuple[float, list[dict]] = (0.0, [])
_THS_SNAP_CACHE: tuple[float, dict] = (0.0, {})
_TX_LIST_CACHE: dict[str, tuple[float, list[dict]]] = {}
_TX_SNAP_CACHE: dict[str, tuple[float, dict]] = {}

SINA_BOARD_CACHE: tuple[float, list[dict]] = (0.0, [])
_BK_CACHE: dict[str, str] = {}
_IND_CACHE: dict[str, str] = {}

ZONE_CN = {"safe": "安全区", "range": "震荡区", "danger": "危险区"}
ZONE_CAP = {"safe": "70%-100%", "range": "≤30%", "danger": "0%（空仓休息）"}


# --------------------------------------------------------------------------- #
# 通用小工具
# --------------------------------------------------------------------------- #
def _sma_at(values: list, i: int) -> float | None:
    if i < 0 or i >= len(values):
        return None
    v = values[i]
    return float(v) if v is not None else None


def _cross_index(short: list, long_: list, n: int, lookback: int = 12) -> int | None:
    """最近一次「短均线上穿长均线」的位置（只回看 lookback 根）。"""
    last = None
    for i in range(max(1, n - lookback), n):
        sp, lp = _sma_at(short, i - 1), _sma_at(long_, i - 1)
        sc, lc = _sma_at(short, i), _sma_at(long_, i)
        if None in (sp, lp, sc, lc):
            continue
        if sp <= lp and sc > lc:
            last = i
    return last


def _ema(values: list[float], n: int) -> list[float]:
    k = 2.0 / (n + 1)
    out: list[float] = []
    prev = None
    for v in values:
        prev = v if prev is None else v * k + prev * (1 - k)
        out.append(prev)
    return out


def _rsi(closes: list[float], n: int = 14) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        ch = closes[i] - closes[i - 1]
        gains += max(ch, 0.0)
        losses += max(-ch, 0.0)
    avg_g, avg_l = gains / n, losses / n
    out[n] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    for i in range(n + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        avg_g = (avg_g * (n - 1) + max(ch, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-ch, 0.0)) / n
        out[i] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1 + avg_g / avg_l)
    return out


def _macd(closes: list[float]) -> tuple[list[float], list[float], list[float]]:
    dif = [a - b for a, b in zip(_ema(closes, 12), _ema(closes, 26))]
    dea = _ema(dif, 9)
    hist = [(a - b) * 2 for a, b in zip(dif, dea)]
    return dif, dea, hist


# --------------------------------------------------------------------------- #
# 第一层：宏观环境
# --------------------------------------------------------------------------- #
def _index_gate(symbol: str, name: str) -> dict:
    bars = get_bars(symbol, bars=180, period="day", adjust="qfq")
    if len(bars) < 65:
        raise DataSourceError(f"{name}K线不足（{len(bars)} 根）")
    ind = build_indicators(bars)
    close = bars[-1].close
    ma5 = ind.ma5[-1]
    ma20 = ind.ma20[-1]
    ma60 = ind.ma60[-1]
    prev20 = ind.ma20[-2]
    ma20_5ago = ind.ma20[-6]
    above20 = bool(ma20 and close > ma20)
    above60 = bool(ma60 and close > ma60)
    up20 = bool(ma20 and prev20 and ma20 > prev20)
    # 危险区：60日线之下 + 均线空头排列（MA20 < MA60 且 MA5 更低）
    bear_align = bool(ma20 and ma60 and ma5 and ma20 < ma60 and ma5 < ma20)
    if not above60 and bear_align:
        zone = "danger"
    elif above20 and above60 and up20:
        zone = "safe"
    else:
        zone = "range"
    slope5 = ((ma20 / ma20_5ago - 1) * 100) if (ma20 and ma20_5ago) else None
    return {
        "symbol": symbol, "name": name, "price": round(close, 2),
        "change_pct": round((close / bars[-2].close - 1) * 100, 2) if len(bars) > 1 else 0.0,
        "ma5": round(ma5, 2) if ma5 else None, "ma20": round(ma20, 2) if ma20 else None,
        "ma60": round(ma60, 2) if ma60 else None,
        "above20": above20, "above60": above60, "up20": up20,
        "ma20_slope5": round(slope5, 2) if slope5 is not None else None,
        "zone": zone, "zone_cn": ZONE_CN[zone],
        "position_word": {"safe": "指数在多条均线上方且20日线向上",
                          "range": "指数在20日线附近缠绕",
                          "danger": "指数在60日线下方且均线空头排列"}[zone],
    }


def _macro_gate() -> dict:
    rows, errs = [], []
    for symbol, name in INDEX_LIST:
        try:
            rows.append(_index_gate(symbol, name))
        except Exception as exc:  # noqa: BLE001
            errs.append(f"{name}：{exc}")
    if not rows:
        raise DataSourceError("三大指数K线均获取失败：" + "；".join(errs))
    safe_n = sum(1 for r in rows if r["zone"] == "safe")
    danger_n = sum(1 for r in rows if r["zone"] == "danger")
    sh_danger = next((r["zone"] == "danger" for r in rows if r["symbol"] == "sh000001"), False)
    if danger_n >= 2 or (sh_danger and safe_n == 0):
        zone = "danger"
    elif safe_n >= 2 and danger_n == 0:
        zone = "safe"
    else:
        zone = "range"
    detail = "；".join(f"{r['name']}：{r['zone_cn']}（现价 {r['price']}，MA20 {r['ma20']}，"
                       f"MA60 {r['ma60']}，20日线{'向上' if r['up20'] else '未向上'}）" for r in rows)
    advice = {
        "safe": "大盘处于安全区：覆巢风险低，可按计划把仓位放大到 70%-100%，顺势持股。",
        "range": "大盘处于震荡区：指数在20日线附近反复缠绕，只做轻仓试错，总仓不超过 30%。",
        "danger": "大盘处于危险区：指数在60日线下方且均线空头排列，空仓休息，放弃一切选股。",
    }[zone]
    return {"zone": zone, "zone_cn": ZONE_CN[zone], "indices": rows,
            "position_cap": ZONE_CAP[zone], "advice": advice, "text": detail,
            "errors": errs}


def _bk_code(industry: str) -> str | None:
    """行业名 → 东财行业板块 BK 代码（先精确、再去罗马数字/行业后缀）。"""
    base = (industry or "").strip()
    if not base or base == "--":
        return None
    cands = [base, re.sub(r"[ⅠⅡⅢⅣ]$", "", base), re.sub(r"行业$", "", base)]
    for q in dict.fromkeys(c for c in cands if c):
        if q in _BK_CACHE:
            return _BK_CACHE[q]
        try:
            data = fetch_json(EM_BK_SUGGEST.format(q=urllib.parse.quote(q)), encoding="utf-8")
            for item in ((data or {}).get("QuotationCodeTable") or {}).get("Data") or []:
                code = str(item.get("Code") or "")
                if code.startswith("BK") and item.get("Classify") == "BK":
                    _BK_CACHE[q] = code
                    return code
        except Exception:  # noqa: BLE001
            continue
    return None


def _industry_of(symbol: str) -> str:
    """股票所属行业：东财行情 f127 优先，失败回退 F10 公司概况 EM2016（取最后一级）。"""
    if symbol in _IND_CACHE:
        return _IND_CACHE[symbol]
    industry = ""
    try:
        prof = fetch_json(EM_STOCK_PROFILE.format(secid=_secid(symbol)),
                          referer="https://quote.eastmoney.com/", retries=1, timeout=8)
        industry = str(((prof or {}).get("data") or {}).get("f127") or "").strip()
    except Exception:  # noqa: BLE001
        industry = ""
    if not industry:
        try:
            market, code = split_code(symbol)
            survey = fetch_json(EM_F10_SURVEY.format(secu=f"{market.upper()}{code}"),
                                referer="https://emweb.securities.eastmoney.com/", retries=1, timeout=8)
            rows = (survey or {}).get("jbzl") or []
            if rows:
                chain = str(rows[0].get("EM2016") or "")
                industry = chain.split("-")[-1].strip() if chain else ""
        except Exception:  # noqa: BLE001
            industry = ""
    if industry:
        _IND_CACHE[symbol] = industry
    return industry


def _tx_board_list(kind: str = TX_BOARD_KIND) -> list[dict]:
    """腾讯行业板块清单（kind: hy=申万一级 31 个 / hy2=申万二级 124 个，均为行业、不含概念），
    带今日涨跌幅与 5 分钟缓存。count 上限 100，需按 offset 翻页。"""
    ts, rows = _TX_LIST_CACHE.get(kind, (0.0, []))
    if rows and time.time() - ts < 300:
        return rows
    out: list[dict] = []
    off = 0
    while off < 400:
        j = fetch_json(TX_BOARD_RANK.format(bt=kind, off=off, cnt=100),
                       referer="http://gu.qq.com/", retries=1, timeout=10)
        data = (j or {}).get("data") or {}
        chunk = data.get("rank_list") or []
        if not chunk:
            break
        for r in chunk:
            code, name = str(r.get("code") or ""), str(r.get("name") or "")
            if not code or not name:
                continue
            try:
                pct = round(float(r.get("zdf")), 2)
            except (TypeError, ValueError):
                pct = 0.0
            out.append({"code": code, "name": name, "pct": pct, "net": None})
        off += len(chunk)
        try:
            total = int(data.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if not total or off >= total:
            break
    out.sort(key=lambda x: -x["pct"])
    _TX_LIST_CACHE[kind] = (time.time(), out)
    return out


def _ths_board_list() -> list[dict]:
    """同花顺行业板块清单（q.10jqka.com.cn/thshy 静态页，881xxx 共 90 个）。

    同花顺的行业指数是 881xxx（申万口径的细分行业二次归类，纯行业），
    概念板块是 885xxx，本函数只取 881xxx，天然排除概念板块。
    """
    global _THS_LIST_CACHE
    ts, rows = _THS_LIST_CACHE
    if rows and time.time() - ts < HIST_TTL:
        return rows
    text = fetch_text(THS_LIST, encoding="gbk", retries=1, timeout=10,
                      referer=THS_REF, headers={"Referer": THS_REF})
    out: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r'thshy/detail/code/(881\d{3})/"[^>]*>([^<]{1,20})</a>', text):
        code, name = m.group(1), m.group(2).strip()
        if not name or code in seen:
            continue
        seen.add(code)
        out.append({"code": code, "name": name})
    if len(out) < 60:      # 只解析到半截就别缓存，下次重取
        return []
    _THS_LIST_CACHE = (time.time(), out)
    return out


def _ths_pct_series(code: str, n: int) -> dict[str, float]:
    """同花顺行业指数近 n 根日线的日涨跌幅。

    行格式：日期,开,高,低,收,量,额（取第 5 列收盘价自算，index 0 不含涨跌幅字段）。
    """
    text = fetch_text(THS_KLINE.format(code=code), encoding="utf-8", retries=1, timeout=8,
                      referer=THS_REF, headers={"Referer": THS_REF})
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    payload = json.loads(text[start:end + 1])
    rows = payload.get("data") or ""
    out: dict[str, float] = {}
    prev = 0.0
    for row in rows.split(";"):
        p = row.split(",")
        if len(p) < 5:
            continue
        try:
            date, close = str(p[0])[:4] + "-" + str(p[0])[4:6] + "-" + str(p[0])[6:8], float(p[4])
        except (IndexError, TypeError, ValueError):
            continue
        if prev > 0 and close > 0:
            out[date] = (close / prev - 1) * 100
        prev = close
    if len(out) > n:
        out = dict(sorted(out.items())[-n:])
    return out


def _board_snapshot() -> dict:
    """今日行业板块涨幅榜前 10 + 近 7 日上榜次数（同花顺行业分类优先）。

    返回 {"ok","provider","source","boards","days","dates","top","by_code","top_by_date"}
    top 每行 {code,name,pct,net,hit7}
    """
    for name, fn in (("ths", _ths_snapshot), ("tx", _tx_snapshot)):
        try:
            snap = fn()
            if snap.get("ok"):
                return snap
        except Exception:  # noqa: BLE001
            continue
    # 最后兜底：新浪 49 个粗分类（无历史排名，仅保证有榜可看）
    top = _sina_board_top10()
    return {"ok": False, "provider": "sina" if top else "none",
            "source": "新浪行业（降级口径）" if top else "",
            "boards": 0, "days": 0, "dates": [], "top": top,
            "by_code": {}, "top_by_date": {}}


def _sina_board_top10() -> list[dict]:
    """新浪行业板块今日涨幅前 10（49 个粗分类，降级兜底）。"""
    global SINA_BOARD_CACHE
    ts, rows = SINA_BOARD_CACHE
    if rows and time.time() - ts < 300:
        return rows
    try:
        text = fetch_text(SINA_INDUSTRY, encoding="gbk", referer="https://finance.sina.com.cn/",
                          retries=1, timeout=10)
        data = json.loads(text.split("=", 1)[1].strip().rstrip(";"))
        out = []
        for v in data.values():
            p = str(v).split(",")
            if len(p) < 6:
                continue
            try:
                out.append({"code": p[0], "name": p[1], "pct": round(float(p[5]), 2), "net": None})
            except (TypeError, ValueError):
                continue
        out.sort(key=lambda x: -x["pct"])
        out = out[:10]
        SINA_BOARD_CACHE = (time.time(), out)
        return out
    except Exception:  # noqa: BLE001
        return []


# --------------------------------------------------------------------------- #
# 行业板块近 N 个交易日「涨幅榜前 10 次数」
# 各板块日线收盘价自算涨跌幅 → 按日排序取前 10 → 统计每个板块上榜天数
# --------------------------------------------------------------------------- #
def _tx_pct_series(code: str, n: int) -> dict[str, float]:
    """腾讯申万二级行业指数近 n 根日线的日涨跌幅（row[0]=日期、row[2]=收盘）。"""
    j = fetch_json(TX_BOARD_KLINE.format(code=code, n=n), referer="http://gu.qq.com/",
                   retries=1, timeout=8)
    rows = (((j or {}).get("data") or {}).get(code) or {}).get("day") or []
    out: dict[str, float] = {}
    prev = 0.0
    for r in rows:
        try:
            date, close = str(r[0])[:10], float(r[2])
        except (IndexError, TypeError, ValueError):
            continue
        if prev > 0 and close > 0:
            out[date] = (close / prev - 1) * 100
        prev = close
    return out


def _fetch_series(boards: list[dict], series_fn, need: int, workers: int = 6) -> dict[str, dict]:
    """并发拉一批板块的日涨跌幅序列（单个失败不影响整体）。"""
    series: dict[str, dict[str, float]] = {}

    def worker(b: dict) -> tuple[str, dict]:
        try:
            return b["code"], series_fn(b["code"], need)
        except Exception:  # noqa: BLE001
            return b["code"], {}

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for code, pct in pool.map(worker, boards):
            if pct:
                series[code] = pct
    return series


def _snapshot_from_series(boards: list[dict], series: dict[str, dict[str, float]],
                          days: int, provider: str, source: str) -> dict:
    """用同一批板块日线算出「最近交易日涨幅榜前 10」与「近 days 日上榜次数」。"""
    dates = sorted({d for p in series.values() for d in p})
    # 只保留大多数板块都有数据的交易日（停牌/缺数据板块当天不参与排名）
    dates = [d for d in dates
             if sum(1 for p in series.values() if d in p) >= max(3, int(len(boards) * 0.6))]
    dates = dates[-days:]
    rec = {b["code"]: {"code": b["code"], "name": b["name"], "count": 0, "ranks": []}
           for b in boards}
    snap = {"ok": bool(dates), "provider": provider, "source": source,
            "boards": len(boards), "days": len(dates), "dates": dates,
            "top": [], "by_code": rec, "top_by_date": {}}
    if not dates:
        return snap
    top_by_date: dict[str, list[dict]] = {}
    for d in dates:
        items = [(code, series[code][d]) for code in series if d in series[code] and code in rec]
        items.sort(key=lambda x: -x[1])
        chunk = items[:10]
        top_by_date[d] = [{"code": c, "name": rec[c]["name"], "pct": round(p, 2), "net": None}
                          for c, p in chunk]
        for i, (c, _p) in enumerate(chunk, 1):
            rec[c]["count"] += 1
            rec[c]["ranks"].append({"date": d, "rank": i})
    top = [dict(t) for t in top_by_date[dates[-1]]]
    for row in top:
        row["hit7"] = rec[row["code"]]["count"]
    snap["top"] = top
    snap["top_by_date"] = top_by_date
    return snap


def _ths_snapshot(days: int = HIST_DAYS) -> dict:
    """同花顺行业（881xxx，90 个）最近交易日榜单 + 近 days 日上榜次数（10 分钟缓存）。"""
    global _THS_SNAP_CACHE
    ts, snap = _THS_SNAP_CACHE
    if snap and time.time() - ts < HIST_TTL:
        return snap
    boards = _ths_board_list()
    if not boards:
        return {"ok": False, "provider": "ths", "source": "同花顺行业", "boards": 0,
                "days": 0, "dates": [], "top": [], "by_code": {}, "top_by_date": {}}
    need = days + 3   # 多取几根，保证最早一天也算得出涨跌幅
    series = _fetch_series(boards, _ths_pct_series, need, workers=8)
    snap = _snapshot_from_series(boards, series, days, "ths", "同花顺行业")
    _THS_SNAP_CACHE = (time.time(), snap)
    return snap


def _tx_snapshot(days: int = HIST_DAYS) -> dict:
    """降级口径：腾讯申万二级行业（124 个，不含概念板块）榜单 + 近 days 日上榜次数。"""
    ts, snap = _TX_SNAP_CACHE.get(TX_BOARD_KIND, (0.0, {}))
    if snap and time.time() - ts < HIST_TTL:
        return snap
    boards = _tx_board_list(TX_BOARD_KIND)
    if not boards:
        return {"ok": False, "provider": "tx", "source": "腾讯·申万二级行业", "boards": 0,
                "days": 0, "dates": [], "top": [], "by_code": {}, "top_by_date": {}}
    need = days + 3
    series = _fetch_series(boards, _tx_pct_series, need, workers=6)
    snap = _snapshot_from_series(boards, series, days, "tx", "腾讯·申万二级行业（降级口径）")
    _TX_SNAP_CACHE[TX_BOARD_KIND] = (time.time(), snap)
    return snap


def _norm_ind(name: str) -> str:
    base = (name or "").strip().split("-")[-1]
    return re.sub(r"[ⅠⅡⅢⅣ]$|行业$|板块$", "", base).strip()


# 东财细分行业 → 新浪 49 个粗分类的近似归属（仅在东财板块榜不可用时用于共振匹配）
_SINA_ALIAS = [
    (("白酒", "啤酒", "其他酒类", "葡萄酒", "软饮料"), ("酿酒行业", "食品行业")),
    (("食品", "调味", "乳品", "休闲", "烘焙", "预制"), ("食品行业",)),
    (("半导体", "元件", "光学", "消费电子", "电子化学", "面板"), ("电子器件", "电子信息")),
    (("软件", "计算机", "互联网", "通信", "云"), ("电子信息",)),
    (("医疗器械",), ("医疗器械",)),
    (("医药", "制药", "生物制品", "中药", "医疗服务"), ("生物制药",)),
    (("银行", "证券", "保险", "多元金融", "金融"), ("金融行业",)),
    (("房地产", "装修"), ("房地产",)),
    (("汽车", "零部件", "乘用车", "商用车"), ("汽车制造", "摩托车")),
    (("电力", "电网", "核电"), ("电力行业",)),
    (("燃气", "水务", "环保"), ("供水供气", "环保行业")),
    (("煤炭",), ("煤炭行业",)),
    (("钢铁", "特钢"), ("钢铁行业",)),
    (("有色", "金属", "黄金", "稀土"), ("有色金属",)),
    (("化学", "化工", "农药", "化肥"), ("化工行业", "农药化肥")),
    (("化纤", "纤维"), ("化纤行业",)),
    (("塑料", "橡胶"), ("塑料制品",)),
    (("机械", "设备", "自动化", "仪器"), ("机械行业", "仪器仪表")),
    (("家电", "厨卫"), ("家电行业",)),
    (("传媒", "影视", "游戏", "广告", "出版", "文化"), ("传媒娱乐",)),
    (("百货", "商业", "贸易", "零售", "电商"), ("商业百货", "物资外贸")),
    (("旅游", "酒店", "餐饮", "景区"), ("酒店旅游",)),
    (("建筑", "建材", "工程"), ("建筑建材", "水泥行业")),
    (("交通", "物流", "港口", "航运", "航空", "机场", "铁路", "公路"), ("交通运输", "公路桥梁")),
    (("农林牧渔", "种植", "养殖", "饲料", "农业", "渔业", "林业"), ("农林牧渔",)),
    (("纺织", "面料"), ("纺织行业",)),
    (("服装", "鞋", "服饰"), ("服装鞋类",)),
    (("造纸", "包装", "印刷"), ("造纸行业", "印刷包装")),
    (("石油", "油气", "油服"), ("石油行业",)),
    (("军工", "航天", "航空装备", "兵装", "船舶"), ("飞机制造", "船舶制造")),
    (("电池", "光伏", "风电", "储能", "电气", "电源"), ("发电设备", "电器行业")),
    (("玻璃",), ("玻璃行业",)),
    (("陶瓷",), ("陶瓷行业",)),
    (("水泥",), ("水泥行业", "建筑建材")),
    (("综合",), ("综合行业",)),
]


def _coarse_names(industry: str) -> list[str]:
    """细分行业名 → 新浪粗分类候选名（无命中返回空列表）。"""
    text = industry or ""
    out: list[str] = []
    for keys, names in _SINA_ALIAS:
        if any(k in text for k in keys):
            out.extend(n for n in names if n not in out)
    return out


def _sector_resonance(industry: str) -> dict:
    snap = _board_snapshot()
    top, source, provider = snap["top"], snap["source"], snap["provider"]
    top = [dict(t) for t in top]        # 榜单来自缓存，先复制再挂历史统计字段
    by_code = snap.get("by_code") or {}
    for row in top:
        rec = by_code.get(str(row.get("code") or ""))
        row["hit7"] = rec["count"] if rec else None
    bk = _bk_code(industry) if industry else None
    base = _norm_ind(industry)
    coarse = _coarse_names(industry)
    rank, via = None, ""
    for i, item in enumerate(top, 1):
        if (bk and item["code"] == bk) or (base and _norm_ind(item["name"]) == base):
            rank, via = i, "精确"
            break
    if rank is None and base:
        for i, item in enumerate(top, 1):
            name = _norm_ind(item["name"])
            if name and (base in name or name in base):
                rank, via = i, "近似"
                break
    if rank is None and coarse:
        for i, item in enumerate(top, 1):
            if _norm_ind(item["name"]) in coarse:
                rank, via = i, "粗分类降级"
                break
    hit_name = top[rank - 1]["name"] if rank else ""
    days_n = snap.get("days") or 0
    hit7 = top[rank - 1]["hit7"] if rank else None
    if rank is None and base and by_code:
        # 未进前 10 时，按「板块名完全相同」找它自己的板块记录（不做模糊映射）
        own = next((r for r in by_code.values() if _norm_ind(str(r.get("name") or "")) == base), None)
        if own:
            hit7 = own["count"]
    persist = ""
    if rank and hit7 is not None and days_n:
        persist = (f"；近 {days_n} 个交易日 {hit7} 次进前 10"
                   + ("（持续性主线）" if hit7 >= 3 else "（偶发上榜，非持续主线）"))
    elif not rank and hit7 is not None and days_n:
        persist = f"；不过近 {days_n} 个交易日该板块 {hit7} 次进前 10"
    if snap.get("ok"):
        boards_n = snap.get("boards") or 0
        hist_note = (f"「近 {days_n} 个交易日进前 10 次数」与上方榜单同一口径："
                     f"{snap.get('source') or ''} {boards_n} 个行业板块（不含概念板块），"
                     f"按板块日线涨跌幅排名（{snap['dates'][0]} ~ {snap['dates'][-1]}）")
    elif provider in ("ths", "tx"):
        hist_note = "近 7 个交易日行业板块排名历史获取失败，上榜次数暂无法统计"
    else:
        hist_note = ("当前榜单为新浪行业粗分类降级口径，无对应历史排名数据，"
                     "上榜次数显示「—」")
    return {
        "industry": industry or "",
        "bk": bk or "",
        "top": top,
        "source": source,
        "provider": provider,
        "hist": {"ok": bool(snap.get("ok")), "source": snap.get("source") or "",
                 "boards": snap.get("boards") or 0,
                 "days": days_n, "dates": snap.get("dates") or []},
        "hist_note": hist_note,
        "rank": rank,
        "resonant": rank is not None,
        "match": via,
        "text": (f"所属行业「{base or industry}」对应今日涨幅榜第 {rank} 名「{hit_name}」"
                 + (f"（{via}匹配）" if via else "") + "，有主线板块支撑" + persist
                 if rank else
                 f"所属行业「{base or industry or '未知'}」未进入今日行业涨幅榜前 10，"
                 "缺少主线板块共振" + persist
                 ) if top else "行业板块涨幅榜获取失败，板块共振暂无法判定",
    }


# --------------------------------------------------------------------------- #
# 第二层：量化海选
# --------------------------------------------------------------------------- #
def _screen_layer(bars: list, ind, quote, n: int, period: str = "day") -> dict:
    daily = period == "day"
    unit = "日" if daily else "根"
    close = bars[-1].close
    prev_close = bars[-2].close if n > 1 else close
    ma5, ma10, ma20, ma60 = ind.ma5[-1], ind.ma10[-1], ind.ma20[-1], ind.ma60[-1]
    ma20_prev = ind.ma20[-2] if n > 1 else ma20
    vol, vma5, vma20 = bars[-1].volume, ind.vol_ma5[-1], ind.vol_ma20[-1]

    crosses = []
    for short, long_, a, b in ((ind.ma5, ind.ma10, 5, 10),
                               (ind.ma5, ind.ma20, 5, 20),
                               (ind.ma5, ind.ma60, 5, 60)):
        label = f"{a}日线上穿{b}日线" if daily else f"MA{a} 上穿 MA{b}（本周期）"
        ix = _cross_index(short, long_, n, lookback=12)
        days = (n - 1 - ix) if ix is not None else None
        crosses.append({"label": label, "index": ix, "date": bars[ix].date if ix is not None else "",
                        "days": days, "today": days == 0,
                        "recent5": days is not None and days <= 5})
    any_cross5 = any(c["recent5"] for c in crosses)
    up20 = bool(ma20 and ma20_prev and ma20 > ma20_prev)

    amount = 0.0
    if quote is not None and quote.amount > 0:
        amount = quote.amount
    elif bars[-1].amount > 0:
        amount = bars[-1].amount
    else:
        amount = close * vol * 100.0     # 成交量单位为手（100 股），估算成交额
    total_cap = quote.total_cap if quote is not None else 0.0
    cap_ok = total_cap >= 3e9 if total_cap else None

    ma_pass = bool(any_cross5 and up20)
    vol_pass = bool(vol and vma5 and vma20 and vol > vma5 > vma20)
    amt_ok = amount >= 1e8
    liq_pass = bool(amt_ok and cap_ok is not False)

    def row(key, label, ok, detail, group, counted=True):
        return {"key": key, "label": label, "pass": bool(ok),
                "state": "pass" if ok else "fail", "detail": detail,
                "group": group, "counted": counted}

    conds = []
    for i, c in enumerate(crosses, 1):
        if c["index"] is None:
            detail = "近 12 根K线内未出现金叉"
        elif c["today"]:
            detail = f"{'今日' if daily else '本根'}金叉（{c['date']}）"
        else:
            detail = f"{c['date']} 金叉，{c['days']} 个交易{unit}前" if daily else f"{c['date']} 金叉，{c['days']} 根K线前"
        # 三条金叉为「任一满足」组，单条仅作参考展示，不单独计入失败项
        conds.append(row(f"cross{i}", c["label"], c["recent5"], detail, "MA_ITEM", counted=False))
    cross_any_label = ("近5日内任一均线金叉（5上穿10/20/60）" if daily
                       else "近 5 根K线内任一均线金叉（MA5 上穿 MA10/20/60）")
    conds.append(row("cross_any", cross_any_label, any_cross5,
                     f"近5{unit}金叉 {sum(1 for c in crosses if c['recent5'])}/3 条；"
                     f"{'今日' if daily else '本根'}金叉 {sum(1 for c in crosses if c['today'])}/3 条", "MA"))
    conds.append(row("ma20up", "MA20 拐头向上（今日 > 昨日）" if daily else "MA20 拐头向上（本周期 > 上一周期）", up20,
                     (f"MA20 今日 {ma20:.2f} ／ 昨日 {ma20_prev:.2f}" if daily
                      else f"MA20 本周期 {ma20:.2f} ／ 上周期 {ma20_prev:.2f}")
                     if ma20 and ma20_prev else "MA20 数据不足",
                     "MA"))
    conds.append(row("vol", "今日成交量 > 5日均量 > 20日均量" if daily else "本周期成交量 > MA5量 > MA20量", vol_pass,
                     (f"今日 {vol:,.0f} 手 ／ MA5量 {vma5:,.0f} ／ MA20量 {vma20:,.0f}" if daily
                      else f"本周期 {vol:,.0f} 手 ／ MA5量 {vma5:,.0f} ／ MA20量 {vma20:,.0f}"),
                     "VOL"))
    conds.append(row("amount", "日成交额 > 1 亿元", amt_ok,
                     f"成交额约 {amount / 1e8:.2f} 亿元", "LIQ"))
    conds.append(row("cap", "总市值 > 30 亿元", cap_ok is True,
                     (f"总市值 {total_cap / 1e8:.1f} 亿元" if total_cap
                      else "总市值数据缺失（行情快照未返回）"), "LIQ"))
    aux_alignment = bool(ma5 and ma10 and ma20 and ma60 and ma5 > ma10 > ma20 > ma60)
    conds.append({"key": "align", "label": "辅助：均线多头排列（MA5>MA10>MA20>MA60）",
                  "pass": aux_alignment, "state": "info", "counted": False,
                  "detail": "多头排列" if aux_alignment else "尚未形成完全多头排列", "group": "AUX"})

    fails = [c["label"] for c in conds if c["counted"] and not c["pass"]]
    passed = ma_pass and vol_pass and liq_pass
    return {
        "pass": passed, "ma_pass": ma_pass, "vol_pass": vol_pass, "liq_pass": liq_pass,
        "conditions": conds,
        "crosses": crosses,
        "ma": {"ma5": round(ma5, 2) if ma5 else None, "ma10": round(ma10, 2) if ma10 else None,
               "ma20": round(ma20, 2) if ma20 else None, "ma60": round(ma60, 2) if ma60 else None,
               "up20": up20, "alignment": aux_alignment},
        "volume": {"today": round(vol, 0), "ma5": round(vma5, 0) if vma5 else None,
                   "ma20": round(vma20, 0) if vma20 else None},
        "liquidity": {"amount": round(amount, 0), "total_cap": round(total_cap, 0)},
        "fails": fails,
        "text": ("海选通过：均线形态、量能、流动性三项硬条件全部满足"
                 if passed else "海选未通过：未满足 " + "、".join(fails)),
    }


# --------------------------------------------------------------------------- #
# 第三层：威科夫精选
# --------------------------------------------------------------------------- #
def _ut_check(bars: list, ind, events: list, long_ctx: dict, cross_ix: int | None) -> dict:
    n = len(bars)
    price = bars[-1].close
    pos250 = long_ctx.get("pos250")
    reasons, risk = [], False

    recent_uts = [e for e in events if e.get("type") in ("UT", "UTAD") and n - 1 - e["index"] <= 15]
    if recent_uts:
        risk = True
        e = max(recent_uts, key=lambda x: x["index"])
        reasons.append(f"最近 15 根内出现 {e['type_cn']}（{e['date']}，价格 {e['price']}）：上冲被压回，"
                       "属主力拉高出货的典型动作")

    idx = cross_ix if cross_ix is not None else n - 1
    b = bars[idx]
    body = abs(b.close - b.open)
    rng = max(b.high - b.low, 1e-9)
    upper = b.high - max(b.open, b.close)
    upper_ratio = upper / rng
    vol60 = [x.volume for x in bars[max(0, idx - 60):idx]] or [b.volume]
    huge = (ind.rel_vol[idx] or 0) >= 2.0 or b.volume >= max(vol60) * 0.98
    long_upper = upper_ratio >= 0.5 and upper >= max(body * 1.2, rng * 0.25)
    failed = b.close < b.high * 0.97
    limit_up = (b.close / bars[idx - 1].close - 1) * 100 >= 9.7 if idx > 0 else False
    if huge and long_upper and failed and not limit_up:
        risk = True
        reasons.append(f"上穿均线那根K线（{b.date}）放量（量比 {ind.rel_vol[idx]:.2f}）且留长上影"
                       f"（上影占振幅 {upper_ratio * 100:.0f}%），收盘未站稳高点")
    if pos250 is not None and pos250 >= 0.75 and huge and long_upper:
        risk = True
        reasons.append(f"该放量长上影出现在近250根高位（{pos250 * 100:.0f}%分位），高位巨量长上影按 UT 处理")
    if long_ctx.get("dir") == "down" and huge and long_upper and b.close < (ind.ma20[idx] or price):
        risk = True
        reasons.append("长期下跌趋势中的放量长上影（死猫跳特征），反弹未站上20日线，直接淘汰")
    if not reasons:
        reasons.append("未见放量长上影 / 高位 UT 结构，排雷通过")
    return {"risk": risk, "pass": not risk, "reasons": reasons}


def _range_check(bars: list, ind, rng, events: list, long_ctx: dict) -> dict:
    n = len(bars)
    price = bars[-1].close
    valid = bool(rng.valid and rng.bars >= 20)
    details, healthy = [], False
    ratio = None
    if valid:
        s, e = rng.start_index, min(rng.end_index + 1, n)
        up_vol = sum(bars[i].volume for i in range(s, e) if ind.pct[i] > 0)
        dn_vol = sum(bars[i].volume for i in range(s, e) if ind.pct[i] < 0)
        ratio = up_vol / dn_vol if dn_vol else 2.0
        sc = [x for x in events if x.get("type") in ("SC", "ST", "AR", "PS") and s <= x["index"] <= e]
        spring = [x for x in events if x.get("type") in ("Spring", "Shakeout", "Test") and s <= x["index"] <= e]
        healthy = ratio >= 1.05 and (bool(sc) or bool(spring))
        details.append(f"左侧横盘 {rng.bars} 根K线（{rng.start_date} ~ {rng.end_date}），"
                       f"区间 {rng.low:.2f}~{rng.high:.2f}，宽度 {rng.width_pct * 100:.1f}%")
        details.append(f"横盘区间前累计{'下跌' if rng.prior_change_pct < 0 else '上涨'} "
                       f"{abs(rng.prior_change_pct) * 100:.1f}%"
                       + ("，符合吸筹背景" if rng.prior_change_pct <= -0.08 else ""))
        details.append(f"区间内涨时量/跌时量 = {ratio:.2f}"
                       + ("，涨放量跌缩量（供需健康）" if ratio >= 1.05
                          else "，量价配合一般" if ratio >= 0.9 else "，跌时放量（供给占优）"))
        if sc:
            details.append("区间内出现 " + "、".join(sorted({x["type_cn"] for x in sc})) + "，有恐慌抛售与承接")
        if spring:
            details.append("区间内出现 " + "、".join(sorted({x["type_cn"] for x in spring})) + "，有洗盘/缩量测试")
    else:
        # 没有有效横盘：判断是否为 V 型直接反转
        tail = bars[-40:]
        low_i = min(range(len(tail)), key=lambda i: tail[i].low)
        rise = (price / tail[low_i].low - 1) * 100 if tail[low_i].low else 0.0
        if rise >= 15 and low_i >= len(tail) - 15:
            details.append(f"近 40 根内自低点 {tail[low_i].low:.2f} 直接拉升 {rise:.1f}%，无像样横盘，属 V 型反转，放弃")
        else:
            details.append(f"未识别到 ≥20 根的有效横盘区间（当前区间 {getattr(rng, 'bars', 0)} 根），左侧吸筹结构不成立，放弃")
    return {
        "ok": valid, "healthy": healthy, "bars": rng.bars,
        "high": round(rng.high, 2), "low": round(rng.low, 2),
        "start": rng.start_date, "end": rng.end_date,
        "prior_change_pct": round(rng.prior_change_pct * 100, 2),
        "up_down_vol_ratio": round(ratio, 2) if ratio is not None else None,
        "breakout": rng.breakout, "breakout_level": round(rng.breakout_level, 2),
        "breakout_index": rng.breakout_index,
        "details": details,
    }


def _buy_point(bars: list, ind, rng, events: list, screen: dict, range_info: dict,
               period: str = "day") -> dict:
    daily = period == "day"
    n = len(bars)
    price = bars[-1].close
    ma5, ma20 = ind.ma5[-1], ind.ma20[-1]
    vol, vma5, vma20 = bars[-1].volume, ind.vol_ma5[-1], ind.vol_ma20[-1]
    ma5_name = "5日线" if daily else "MA5"
    ma20_name = "20日线" if daily else "MA20"
    sos = [e for e in events if e.get("type") == "SOS" and n - 1 - e["index"] <= 25]
    lps = [e for e in events if e.get("type") in ("LPS", "BU") and n - 1 - e["index"] <= 25]
    spring = [e for e in events if e.get("type") in ("Spring", "Shakeout", "Test") and n - 1 - e["index"] <= 30]
    breakout_up = bool(rng.valid and rng.breakout == "up"
                       and rng.breakout_index is not None and n - 1 - rng.breakout_index <= 30)
    near_ma20 = bool(ma20 and abs(price / ma20 - 1) <= 0.03)
    shrink = bool(vol and vma5 and vma20 and vol < vma5 * 0.85 and vol < vma20 * 0.90)
    ma5_leading = bool(ma5 and ma20 and ind.ma10[-1] and ind.ma60[-1]
                       and ma5 > ind.ma10[-1] and ma5 > ma20 and ma5 > ind.ma60[-1])
    up20 = screen["ma"]["up20"]

    if (sos or breakout_up or lps) and ma5_leading:
        if near_ma20 and shrink:
            return {"type": "A", "label": "情况 A：最佳狙击（SOS 突破 → 缩量回踩 MA20，LPS 买点）",
                    "ready": True, "price": round(ma20, 2),
                    "reasons": [f"SOS/突破已成立（{'、'.join(e['date'] for e in sos) or '区间向上突破'}），"
                                + f"{ma5_name}位于所有均线上方",
                                f"现价 {price:.2f} 回踩至 MA20 {ma20:.2f} 附近（偏离 {abs(price / ma20 - 1) * 100:.1f}%）",
                                f"回踩缩量（{'今日' if daily else '本周期'}量 {vol:,.0f} 手 < MA5量 {vma5:,.0f}×0.85），供给枯竭"]}
        return {"type": "A", "label": "情况 A：SOS 已突破，等待缩量回踩 MA20（不追高）",
                "ready": False, "price": round(ma20, 2) if ma20 else None,
                "reasons": [f"突破/SOS 已出现且 {ma5_name}领先所有均线，但现价未回到 MA20 附近或未缩量",
                            f"耐心等回踩 MA20 {ma20:.2f} 附近出现缩量小实体K线再买" if ma20 else ""]}
    if spring and up20 and near_ma20:
        return {"type": "B", "label": f"情况 B：左侧潜伏（Spring 收回 + {ma20_name}拐头，MA20 附近建仓）",
                "ready": True, "price": round(ma20, 2),
                "reasons": [f"近期出现 {'、'.join(sorted({e['type_cn'] for e in spring}))}，跌破支撑后迅速收回",
                            f"MA20 已拐头向上（{ind.ma20[-2]:.2f} → {ma20:.2f}）",
                            f"现价 {price:.2f} 在 MA20 {ma20:.2f} 附近，可分批潜伏"]}
    if spring and not up20:
        return {"type": "B", "label": "情况 B 前置：Spring 已现，但 MA20 尚未拐头向上",
                "ready": False, "price": round(ma20, 2) if ma20 else None,
                "reasons": [f"Spring/测试已出现，但{ma20_name}尚未拐头，买点未确认，继续观察"]}
    return {"type": "", "label": "暂无标准买点：既非 SOS 后 LPS 回踩，也非 Spring + MA20 拐头",
            "ready": False, "price": None,
            "reasons": [f"等待价格走出「突破 → 缩量回踩 MA20」或「Spring 收回 + {ma20_name}拐头」再行动"]}


# --------------------------------------------------------------------------- #
# 第四层：交易执行
# --------------------------------------------------------------------------- #
def _divergence(bars: list, ind) -> dict:
    n = len(bars)
    closes = [b.close for b in bars]
    rsi = _rsi(closes)
    dif, dea, hist = _macd(closes)
    sw = [p for p in swings(bars, 2) if p["kind"] == "high" and n - 1 - p["index"] <= 70][-3:]
    out = {"rsi_div": False, "macd_div": False, "lower_high": False, "text": []}
    if len(sw) >= 2:
        a, b = sw[-2], sw[-1]
        if b["price"] > a["price"]:
            if rsi[b["index"]] is not None and rsi[a["index"]] is not None and rsi[b["index"]] < rsi[a["index"]] - 1:
                out["rsi_div"] = True
                out["text"].append(f"价格创新高（{a['date']} {a['price']:.2f} → {b['date']} {b['price']:.2f}）"
                                   f"但 RSI 走低（{rsi[a['index']]:.1f} → {rsi[b['index']]:.1f}），RSI 顶背离")
            if hist[b["index"]] < hist[a["index"]]:
                out["macd_div"] = True
                out["text"].append(f"价格创新高但 MACD 柱体走弱（{hist[a['index']]:.3f} → {hist[b['index']]:.3f}），MACD 顶背离")
        elif b["price"] < a["price"]:
            out["lower_high"] = True
            out["text"].append(f"高点降低（{a['date']} {a['price']:.2f} → {b['date']} {b['price']:.2f}），趋势转弱")
    if not out["text"]:
        out["text"].append("暂未出现明显的高点降低或 MACD/RSI 顶背离")
    return out


def _plan(bars: list, ind, rng, events: list, buy: dict, long_ctx: dict, screen: dict,
          range_info: dict, period: str = "day") -> dict:
    daily = period == "day"
    n = len(bars)
    price = bars[-1].close
    ma5, ma10, ma20 = ind.ma5[-1], ind.ma10[-1], ind.ma20[-1]
    ce = cause_effect(rng, price)
    spring = [e for e in events if e.get("type") in ("Spring", "Shakeout")]
    spring_low = min((e["price"] for e in spring), default=None)
    brk_low = bars[rng.breakout_index].low if (rng.valid and rng.breakout_index is not None
                                               and rng.breakout_index < n) else None

    cands: list[tuple[float, str]] = []
    if rng.valid:
        cands.append((rng.low * 0.995, f"横盘区间下沿 {rng.low:.2f}"))
    if brk_low:
        cands.append((brk_low * 0.995, f"标志性突破阳线底部 {brk_low:.2f}"))
    if spring_low:
        cands.append((spring_low * 0.995, f"Spring 低点 {spring_low:.2f}"))
    cands = [(p, b) for p, b in cands if p and 0 < p < price]
    struct_stop = max(cands, key=lambda x: x[0]) if cands else None
    ma_stop = round(ma20, 2) if ma20 else None

    highs = [b.high for b in bars[-250:]]
    prior_high = max(highs) if highs else None
    targets = []
    if ce.get("up_target"):
        targets.append({"price": ce["up_target"], "basis": "因果量度目标（区间高度×系数）"})
    if prior_high and prior_high > price * 1.02:
        targets.append({"price": round(prior_high, 2), "basis": "近250根前高压力"})
    if not targets:
        targets.append({"price": round(price * 1.15, 2), "basis": "无有效区间/前高，按 +15% 参考"})
    targets.sort(key=lambda x: x["price"])

    buy_zone = None
    # 计划买入价（回踩 MA20）作为风险收益比的基准；无买点时以现价计
    entry_ref = buy["price"] if (buy.get("type") and buy.get("price")) else price
    if buy.get("type") and buy.get("price"):
        base = buy["price"]
        buy_zone = [round(base * 0.99, 2), round(base * 1.015, 2)]

    stop_price = struct_stop[0] if struct_stop else (ma_stop or round(price * 0.92, 2))
    stop_basis = struct_stop[1] if struct_stop else "结构位缺失，暂按 MA20"
    divergence = _divergence(bars, ind)
    recent_ut = [e for e in events if e.get("type") in ("UT", "UTAD") and n - 1 - e["index"] <= 10]
    stagnant = bool(ind.rel_vol[-1] >= 1.8 and ind.clv[-1] <= 0.35 and ind.spread_ratio[-1] >= 1.4)

    exit_rules = [
        ("移动止盈：股价沿 5日线/10日线上行不破不走；收盘跌破5日线减仓，跌破10日线清仓" if daily
         else "移动止盈：股价沿 MA5/MA10 上行不破不走；跌破 MA5 减仓，跌破 MA10 清仓"),
        "威科夫离场：高位放巨量滞涨/长上影（UT/UTAD）出现 → 无条件清仓",
        "背离离场：出现高点降低或 MACD/RSI 顶背离 → 无条件清仓",
    ]
    if recent_ut:
        exit_rules.append(f"当前已出现 {'、'.join(sorted({e['type_cn'] for e in recent_ut}))}，按纪律应减仓/清仓")
    if stagnant:
        exit_rules.append("最新一根为放量滞涨（高量比但收盘位置差），警惕派发")
    if divergence["rsi_div"] or divergence["macd_div"] or divergence["lower_high"]:
        exit_rules.extend(divergence["text"])

    return {
        "buy_zone": buy_zone,
        "buy_ideal": buy["price"],
        "entry_ref": round(entry_ref, 2),
        "no_chase": ("不在大阳线拉升、5日线刚金叉当天追高；只在缩量小阴小阳回踩 MA20 附近逢低吸纳"
                     if daily else
                     "不在放量大阳线、MA5 刚金叉那根K线追高；只在缩量小实体回踩 MA20 附近逢低吸纳"),
        "stop": {
            "price": round(stop_price, 2),
            "basis": stop_basis,
            "ma20": ma_stop,
            "ma20_rule": ("收盘跌破 MA20 且 2-3 个交易日不收回 → 无条件止损" if ma_stop else "MA20 数据不足"),
            "structure": round(struct_stop[0], 2) if struct_stop else None,
            "structure_basis": struct_stop[1] if struct_stop else "",
            "spring": round(spring_low * 0.995, 2) if spring_low else None,
        },
        "take_profit": {
            "trailing": "沿 5日线/10日线持有，不破不走",
            "targets": targets,
            "exit_rules": exit_rules,
        },
        "divergence": divergence,
        "risk_pct": round((entry_ref - stop_price) / entry_ref * 100, 2) if entry_ref else None,
        "rr": round((targets[0]["price"] - entry_ref) / (entry_ref - stop_price), 2)
        if targets and entry_ref > stop_price else None,
    }


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
PERIOD_CN = {"day": "日线", "week": "周线", "month": "月线", "m60": "60分钟", "m30": "30分钟"}
PERIODS = tuple(PERIOD_CN.keys())


def wm_analyze(code: str, bars: int = 320, period: str = "day") -> dict:
    started = time.time()
    period = period if period in PERIOD_CN else "day"
    period_cn = PERIOD_CN[period]
    minute = period in ("m60", "m30")
    symbol, resolved_name, via = resolve_input(code)
    if is_index(symbol):
        raise ValueError("W-M 模型用于个股选股；指数请使用「大盘分析」区块")
    market, code6 = split_code(symbol)
    warnings: list[str] = []
    if via == "search" and resolved_name:
        warnings.append(f"已按名称「{resolved_name}」解析为 {symbol}")
    if minute:
        warnings.append(f"分钟周期数据源通常仅返回近 {period_cn} 约 320 根K线（约 2~4 个月），"
                        "样本较短，结论以短线节奏为主")

    quote = None
    try:
        quote = get_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"实时快照获取失败（{exc}），价格以最后一根K线为准")

    if minute:
        rows = get_intraday(symbol, bars=max(320, min(1000, bars or 1000)), period=period)
        adjust_cn = "分钟线（前复权口径，源内已复权）"
    else:
        need = int(max(180, min(700, bars)))
        rows = get_bars(symbol, bars=need, period=period, adjust="qfq")
        if any(b.close <= 0 or b.low <= 0 for b in rows):
            # 后复权量纲与现价差数倍，按最近前复权收盘等比缩放后再判定与报价
            anchor = rows[-1].close if (rows and rows[-1].close > 0) else 0.0
            rows = get_bars(symbol, bars=need, period=period, adjust="hfq")
            rows = rescale_to_current(rows, anchor)
            warnings.append("前复权在长周期出现非正值，已改用后复权并换算为现价量纲")
        adjust_cn = "前复权"
    n = len(rows)
    if n < 60:
        warnings.append(f"仅有 {n} 根{period_cn}K线，样本不足，W-M 结论仅供观察")
    ind = build_indicators(rows)
    rng = find_range(rows)
    ev = detect(rows, ind, rng)
    events = ev["events"]
    long_ctx = long_trend_of(rows, ind)
    phase = classify_phase(rows, ind, rng, events, ev["vsa"],
                           {"text": "", "indices": [], "trend": {"dir": "sideways"}},
                           long_ctx=long_ctx)

    industry = _industry_of(symbol)
    if not industry:
        warnings.append("行业归属获取失败，板块共振未纳入")

    macro = _macro_gate()
    sector = _sector_resonance(industry)
    macro["sector"] = sector

    screen = _screen_layer(rows, ind, quote, n, period)
    cross_ix = max((c["index"] for c in screen["crosses"] if c["index"] is not None), default=None)
    ut = _ut_check(rows, ind, events, long_ctx, cross_ix)
    range_info = _range_check(rows, ind, rng, events, long_ctx)
    buy = _buy_point(rows, ind, rng, events, screen, range_info, period)
    plan = _plan(rows, ind, rng, events, buy, long_ctx, screen, range_info, period)

    # ---------------- 综合结论与仓位 ----------------
    zone = macro["zone"]
    if ut["risk"]:
        verdict, verdict_cn = "eliminate", "淘汰（UT 风险）"
    elif not range_info["ok"]:
        verdict, verdict_cn = "eliminate", "放弃（无吸筹区间）"
    elif zone == "danger":
        verdict, verdict_cn = "blocked", "禁止开仓（宏观危险区）"
    elif screen["pass"] and buy["ready"]:
        verdict, verdict_cn = "execute", f"可执行（情况 {buy['type']}）"
    elif screen["pass"] and buy["type"]:
        verdict, verdict_cn = "watch", "观察（等买点确认）"
    elif not screen["pass"]:
        verdict, verdict_cn = "watch", "观察（海选未通过）"
    else:
        verdict, verdict_cn = "watch", "观察（暂无标准买点）"

    single_cap = {"safe": 30, "range": 10, "danger": 0}[zone]
    first = {"A": min(single_cap, 15), "B": min(single_cap, 8)}.get(buy["type"], 0)
    position = {
        "account_cap": macro["position_cap"],
        "single_max_pct": single_cap if verdict in ("execute",) else 0,
        "first_entry_pct": first if verdict == "execute" else 0,
        "text": (f"总仓上限 {macro['position_cap']}；本票单票上限 {single_cap}%，"
                 f"首次建仓建议 {first}%，其余等突破/回踩确认后分批加"
                 if verdict == "execute" else
                 f"当前结论为「{verdict_cn}」，不建仓（宏观总仓上限 {macro['position_cap']}）"),
    }
    score = (40 if zone == "safe" else 20 if zone == "range" else 0)
    score += 25 if screen["pass"] else 0
    score += {"A": 25, "B": 20}.get(buy["type"], 8 if buy["type"] else 0)
    score += 10 if range_info["healthy"] else 0
    score = int(min(100, score))

    price = quote.price if (quote and quote.price > 0) else rows[-1].close
    change_pct = quote.change_pct if quote else (
        (rows[-1].close / rows[-2].close - 1) * 100 if n > 1 else 0.0)
    summary_text = (
        f"【{period_cn}】① 宏观：{macro['zone_cn']}（日线口径），总仓上限 {macro['position_cap']}；"
        f"行业「{industry or '未知'}」{'位列涨幅榜前10（共振）' if sector['resonant'] else '未进前10（无板块共振）'}。"
        f"② 海选：{'通过' if screen['pass'] else '未通过（' + '、'.join(screen['fails']) + '）'}。"
        f"③ 威科夫：{'UT 风险，淘汰' if ut['risk'] else ('吸筹区间成立' if range_info['ok'] else '无吸筹区间，放弃')}；"
        f"{buy['label']}。④ 执行：{('回踩 ' + str(buy['price']) + ' 附近分批买，止损 ' + str(plan['stop']['price'])) if verdict == 'execute' else '暂不扣动扳机'}。"
    )

    return {
        "ok": True, "error": "",
        "meta": {
            "symbol": symbol, "code": code6, "name": (quote.name if quote and quote.name else code6),
            "market": market.upper(), "price": round(price, 2), "change_pct": round(change_pct, 2),
            "industry": industry, "updated": (quote.updated if quote else rows[-1].date),
            "total_cap": round(quote.total_cap, 0) if quote else 0,
            "amount": round(quote.amount, 0) if quote else 0,
            "bars": n, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - started, 1), "warnings": warnings,
            "adjust": adjust_cn, "period": period, "period_cn": period_cn,
            "period_last": rows[-1].date,
        },
        "macro": macro,
        "screen": screen,
        "wyckoff": {
            "stage": phase.get("phase"), "stage_cn": phase.get("phase_cn"),
            "sub_phase": phase.get("sub_phase"), "confidence": phase.get("confidence"),
            "long_trend": long_ctx, "ut": ut, "range": range_info, "buy": buy,
            "events": [{"date": e["date"], "type_cn": e["type_cn"], "type": e["type"],
                        "price": e["price"], "bullish": e["bullish"]}
                       for e in sorted(events, key=lambda x: -x["index"])[:8]],
        },
        "plan": plan,
        "summary": {"verdict": verdict, "verdict_cn": verdict_cn, "score": score,
                    "position": position, "text": summary_text},
        "disclaimer": ("本模型由威科夫量价结构 + 均线共振规则自动生成，仅用于技术分析与复盘学习，"
                       "不构成任何投资建议；据此操作，盈亏自负。"),
    }
