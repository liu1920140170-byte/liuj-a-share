# -*- coding: utf-8 -*-
"""行情数据源：东方财富为主源，腾讯 / 新浪为备源，任一可用即可出结果。

统一约定（上层不必再关心各接口差异）：
    * 价格单位：元
    * 成交量单位：手
    * 成交额单位：元
    * 代码统一为带市场前缀的小写形式，如 sh600519 / sz300750 / bj430047
"""
from __future__ import annotations

import datetime as _dt
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

from .http_client import DataSourceError, fetch_json, fetch_text

EM_QUOTE = ("https://push2.eastmoney.com/api/qt/stock/get?secid={secid}"
            "&fields=f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f57,f58,f60,f86,"
            "f116,f117,f162,f167,f168,f169,f170,f171")
EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid={secid}"
            "&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
            "&klt={klt}&fqt={fqt}&beg={beg}&end=20500101")
EM_SUGGEST = ("https://searchapi.eastmoney.com/api/suggest/get?input={q}&type=14"
              "&token=D43BF722C8E33BDC906FB84D85E326E8&count=8")

TX_KLINE = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},{period},{start},{end},{n},{fq}"
TX_NEWKLINE = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
               "?param={sym},{period},{start},{end},{n},{fq}")
TX_MKLINE = "http://ifzq.gtimg.cn/appstock/app/kline/mkline?param={sym},{period},,{n}"
TX_QUOTE = "http://qt.gtimg.cn/q={syms}"
TX_SEARCH = "http://smartbox.gtimg.cn/s3/?q={q}&t=all"
SINA_QUOTE = "http://hq.sinajs.cn/list={syms}"
SINA_MKLINE = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               "CN_MarketData.getKLineData?symbol={sym}&scale={scale}&ma=no&datalen={n}")

INDEXES = [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]

# 东财 datacenter 财务接口
EM_FIN_CPD = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
              "reportName=RPT_LICO_FN_CPD&columns=ALL"
              "&filter=(SECUCODE=%22{secu}%22)&pageSize=4&pageNumber=1"
              "&sortColumns=NOTICE_DATE&sortTypes=-1")
EM_FIN_MAIN = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
               "reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL"
               "&filter=(SECUCODE=%22{secu}%22)&pageSize=4&pageNumber=1"
               "&sortColumns=REPORT_DATE&sortTypes=-1")
EM_FIN_TREND_MAIN = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                     "reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL"
                     "&filter=(SECUCODE=%22{secu}%22)&pageSize=60&pageNumber=1"
                     "&sortColumns=REPORT_DATE&sortTypes=-1")
EM_FIN_TREND_CPD = ("https://datacenter.eastmoney.com/securities/api/data/v1/get?"
                    "reportName=RPT_LICO_FN_CPD&columns=ALL"
                    "&filter=(SECUCODE=%22{secu}%22)&pageSize=60&pageNumber=1"
                    "&sortColumns=NOTICE_DATE&sortTypes=-1")
# 大盘风险指标：GDP 年度报表 + 北交所全部标的列表（求总市值）
EM_GDP = ("https://datacenter-web.eastmoney.com/api/data/v1/get?"
          "reportName=RPT_ECONOMY_GDP&columns=ALL&pageSize=14&pageNumber=1"
          "&sortColumns=REPORT_DATE&sortTypes=-1")
EM_CLIST = ("https://push2.eastmoney.com/api/qt/clist/get?pn={page}&pz=100&po=1&np=1"
            "&fltt=2&invt=2&fid=f12&fs={fs}&fields=f12,f20")

_CACHE: dict[str, tuple[float, Any]] = {}
_SOURCE_LOG: dict[str, str] = {}


def last_sources() -> dict[str, str]:
    """记录最近一次实际取数成功的数据源，便于在报告里如实标注。"""
    return dict(_SOURCE_LOG)


def _cache_get(key: str, ttl: float):
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] <= ttl:
        return hit[1]
    return None


def _cache_put(key: str, value):
    _CACHE[key] = (time.time(), value)
    if len(_CACHE) > 400:
        oldest = sorted(_CACHE.items(), key=lambda kv: kv[1][0])[:100]
        for k, _ in oldest:
            _CACHE.pop(k, None)
    return value


def clear_cache():
    _CACHE.clear()


# --------------------------------------------------------------------------- #
# 代码识别
# --------------------------------------------------------------------------- #
def normalize_code(raw: str) -> str:
    """把用户输入的各种写法归一成 sh600519 / sz300750 / bj430047 / bj920185。

    支持：600519、sh600519、SH600519、600519.SH、600519 贵州茅台、'  sz000001 '
    """
    if raw is None:
        raise ValueError("请提供股票代码")
    text = str(raw).strip().lower().replace(" ", "")
    if not text:
        raise ValueError("请提供股票代码")

    explicit = None
    m = re.match(r"^(sh|sz|bj)(\d{6})$", text)
    if m:
        explicit, text = m.group(1), m.group(2)
    else:
        m = re.match(r"^(\d{6})\.(sh|sz|bj)$", text)
        if m:
            text, explicit = m.group(1), m.group(2)
        else:
            m = re.search(r"(\d{6})", text)
            if not m:
                raise ValueError(f"无法识别代码：{raw!r}（示例：600519 / sh600519 / 300750）")
            text = m.group(1)

    if explicit:
        return explicit + text

    head = text[0]
    if text.startswith("920"):
        market = "bj"   # 北交所 2024 年起启用 920 新代码段（9 开头但非沪 B 900xxx）
    elif head in ("6", "9"):
        market = "sh"
    elif head in ("0", "2", "3"):
        market = "sz"
    elif head in ("4", "8"):
        market = "bj"
    elif head == "5":
        market = "sh"
    else:
        market = "sz"
    return market + text


def split_code(symbol: str) -> tuple[str, str]:
    """返回 (市场前缀, 6位代码)。"""
    symbol = normalize_code(symbol)
    return symbol[:2], symbol[2:]


def is_index(symbol: str) -> bool:
    symbol = normalize_code(symbol)
    return symbol in {c for c, _ in INDEXES} or symbol.startswith("sz399") or symbol in ("sh000300", "sh000905", "sh000016")


def _secid(symbol: str) -> str:
    market, code = split_code(symbol)
    return ("1." if market == "sh" else "0.") + code


def _resolve_bj_new_code(symbol: str) -> str:
    """北交所老代码（83/87/43 开头）已切换为 920 新段，腾讯/新浪只认新代码。

    用东财搜索接口（searchapi 独立域名，不受 push2 行情风控影响）解析正式代码，
    结果缓存 7 天（代码切换是一次性事件）。解析失败返回原 symbol 让后续源自行报错。
    """
    market, code = split_code(symbol)
    if market != "bj" or code.startswith("92"):
        return symbol
    cached = _cache_get(f"bjmap:{code}", 86400 * 7)
    if cached:
        return cached
    try:
        data = fetch_json(EM_SUGGEST.format(q=code), encoding="utf-8")
        for item in ((data or {}).get("QuotationCodeTable") or {}).get("Data") or []:
            new_code = str(item.get("Code", ""))
            if re.fullmatch(r"\d{6}", new_code) and new_code.startswith("92"):
                return _cache_put(f"bjmap:{code}", "bj" + new_code)
    except Exception:  # noqa: BLE001
        pass
    return symbol


def _f(value, scale: float = 1.0, default: float = 0.0) -> float:
    if value is None or value == "-" or value == "":
        return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if abs(num) > 1e15:
        return default
    return num / scale if scale != 1.0 else num


def _parse_dividend_per_share(desc: str | None) -> float | None:
    """从分红描述（如 '10派280.2423元' / '10转3股派5.00元'）解析每股现金分红。

    东财 ASSIGNDSCRPT 恒为每 10 股口径，转增/送股可能出现在"派"之前，
    故直接匹配"派"后的金额再除以 10。
    """
    if not desc:
        return None
    m = re.search(r"派\s*([\d.]+)", str(desc))
    if m:
        try:
            return float(m.group(1)) / 10.0  # 每10股派X元 → 每股 X/10
        except ValueError:
            pass
    return None


def _dividend_stats(rows: list[dict]) -> tuple[float, dict[str, float], dict[str, float]]:
    """从东财 CPD 行提取分红统计。

    返回 (ttm_div, year_zxgxl, year_div)：
      * ttm_div   ：过去 12 个月（按 NOTICE_DATE）每股现金分红合计，用于实时 TTM 股息率
      * year_zxgxl：各 PAYYEAR 的东财 ZXGXL（取该年最新一条，通常是年报行，非求和），
                    东财每条 ZXGXL 基于该次除权日价格，分母不同不可相加
      * year_div  ：各 PAYYEAR 的每股分红合计
    """
    today = _dt.date.today()
    ttm_div = 0.0
    year_zxgxl: dict[str, float] = {}
    year_div: dict[str, float] = {}
    for r in rows:
        year = str(r.get("PAYYEAR", "") or "")
        desc = str(r.get("ASSIGNDSCRPT", "") or "")
        if not (desc and "不分配" not in desc and "不转增" not in desc):
            continue
        ps = _parse_dividend_per_share(desc)
        if ps and year:
            year_div[year] = year_div.get(year, 0.0) + ps
            nd = str(r.get("NOTICE_DATE", "") or "")[:10]
            try:
                if (today - _dt.date.fromisoformat(nd)).days <= 365:
                    ttm_div += ps
            except Exception:  # noqa: BLE001
                pass
        zxgxl = _f(r.get("ZXGXL"), default=0.0)
        # 东财返回按 NOTICE_DATE 倒序，第一条即该年最新（通常是年报），
        # 只取第一条，因为不同除权日的 ZXGXL 分母不同不可相加
        if zxgxl and year and year not in year_zxgxl:
            year_zxgxl[year] = zxgxl
    return ttm_div, year_zxgxl, year_div


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0   # 手
    amount: float = 0.0   # 元（部分源不提供，为 0）

    def as_list(self) -> list[float]:
        return [self.open, self.high, self.low, self.close, self.volume]


@dataclass
class Quote:
    symbol: str = ""
    code: str = ""
    name: str = ""
    market: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    change: float = 0.0
    change_pct: float = 0.0
    amplitude: float = 0.0
    volume: float = 0.0        # 手
    amount: float = 0.0        # 元
    turnover: float = 0.0      # %
    volume_ratio: float = 0.0  # 量比
    pe: float = 0.0
    pb: float = 0.0
    total_cap: float = 0.0     # 元
    float_cap: float = 0.0     # 元
    limit_up: float = 0.0
    limit_down: float = 0.0
    updated: str = ""
    source: str = ""

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "code": self.code, "name": self.name, "market": self.market,
            "price": self.price, "prev_close": self.prev_close, "open": self.open,
            "high": self.high, "low": self.low, "change": self.change, "change_pct": self.change_pct,
            "amplitude": self.amplitude, "volume": self.volume, "amount": self.amount,
            "turnover": self.turnover, "volume_ratio": self.volume_ratio, "pe": self.pe, "pb": self.pb,
            "total_cap": self.total_cap, "float_cap": self.float_cap,
            "limit_up": self.limit_up, "limit_down": self.limit_down,
            "updated": self.updated, "source": self.source,
        }


# --------------------------------------------------------------------------- #
# 实时快照
# --------------------------------------------------------------------------- #
def _quote_eastmoney(symbol: str) -> Quote:
    data = fetch_json(EM_QUOTE.format(secid=_secid(symbol)), encoding="utf-8",
                      referer="https://quote.eastmoney.com/")
    node = (data or {}).get("data") or {}
    if not node.get("f57"):
        raise DataSourceError("eastmoney quote empty")
    market, code = split_code(symbol)
    scale = 10 ** int(node.get("decimal", 2) or 2)
    ts = node.get("f86")
    updated = ""
    if ts:
        try:
            updated = _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:  # noqa: BLE001
            updated = ""
    return Quote(
        symbol=market + code, code=code, name=str(node.get("f58", "")).strip(), market=market.upper(),
        price=_f(node.get("f43"), scale), prev_close=_f(node.get("f60"), scale),
        open=_f(node.get("f46"), scale), high=_f(node.get("f44"), scale), low=_f(node.get("f45"), scale),
        change=_f(node.get("f169"), scale), change_pct=_f(node.get("f170"), 100.0),
        amplitude=_f(node.get("f171"), 100.0), volume=_f(node.get("f47"), 1.0),
        amount=_f(node.get("f48"), 1.0), turnover=_f(node.get("f168"), 100.0),
        volume_ratio=_f(node.get("f50"), 100.0), pe=_f(node.get("f162"), 100.0), pb=_f(node.get("f167"), 100.0),
        total_cap=_f(node.get("f116"), 1.0), float_cap=_f(node.get("f117"), 1.0),
        limit_up=_f(node.get("f51"), scale), limit_down=_f(node.get("f52"), scale),
        updated=updated, source="东方财富",
    )


def _parse_tencent_quote_line(line: str, symbol: str) -> Quote | None:
    if '="' not in line:
        return None
    body = line.split('="', 1)[1].rstrip('";\n ')
    parts = body.split("~")
    if len(parts) < 46:
        return None
    market, code = split_code(symbol)
    name = parts[1].strip()
    price = _f(parts[3])
    prev = _f(parts[4])

    def gi(idx: int) -> str:
        return parts[idx] if idx < len(parts) else ""

    updated = ""
    raw_ts = gi(30)
    if len(raw_ts) >= 14 and raw_ts.isdigit():
        updated = (f"{raw_ts[0:4]}-{raw_ts[4:6]}-{raw_ts[6:8]} "
                   f"{raw_ts[8:10]}:{raw_ts[10:12]}:{raw_ts[12:14]}")
    return Quote(
        symbol=market + code, code=code, name=name, market=market.upper(),
        price=price, prev_close=prev, open=_f(gi(5)),
        high=_f(gi(33)), low=_f(gi(34)), change=_f(gi(31)), change_pct=_f(gi(32)),
        amplitude=_f(gi(43)), volume=_f(gi(6)), amount=_f(gi(37)) * 10000.0,
        turnover=_f(gi(38)), volume_ratio=_f(gi(49)),
        pe=_f(gi(52)) or _f(gi(39)), pb=_f(gi(46)),
        total_cap=_f(gi(45)) * 1e8, float_cap=_f(gi(44)) * 1e8,
        limit_up=_f(gi(47)), limit_down=_f(gi(48)),
        updated=updated, source="腾讯财经",
    )


def _quote_tencent(symbol: str) -> Quote:
    text = fetch_text(TX_QUOTE.format(syms=symbol), encoding="gbk", referer="http://gu.qq.com/")
    for line in text.splitlines():
        quote = _parse_tencent_quote_line(line, symbol)
        if quote and quote.price > 0:
            return quote
    raise DataSourceError("tencent quote empty")


def _quote_sina(symbol: str) -> Quote:
    text = fetch_text(SINA_QUOTE.format(syms=symbol), encoding="gbk",
                      referer="https://finance.sina.com.cn")
    m = re.search(r'="([^"]*)"', text)
    if not m or not m.group(1):
        raise DataSourceError("sina quote empty")
    parts = m.group(1).split(",")
    if len(parts) < 32:
        raise DataSourceError("sina quote malformed")
    market, code = split_code(symbol)
    price = _f(parts[3])
    prev = _f(parts[2])
    if symbol in {c for c, _ in INDEXES}:
        volume = _f(parts[8]) / 100.0
    else:
        volume = _f(parts[8]) / 100.0
    change = price - prev
    return Quote(
        symbol=market + code, code=code, name=parts[0].strip(), market=market.upper(),
        price=price, prev_close=prev, open=_f(parts[1]), high=_f(parts[4]), low=_f(parts[5]),
        change=change, change_pct=(change / prev * 100.0) if prev else 0.0,
        amplitude=((_f(parts[4]) - _f(parts[5])) / prev * 100.0) if prev else 0.0,
        volume=volume, amount=_f(parts[9]),
        updated=f"{parts[30]} {parts[31]}", source="新浪财经",
    )


def get_quote(symbol: str) -> Quote:
    """实时快照，多源依次尝试。"""
    symbol = _resolve_bj_new_code(normalize_code(symbol))
    cached = _cache_get("quote:" + symbol, 20)
    if cached:
        return cached
    errors = []
    for label, fn in (("东方财富", _quote_eastmoney), ("腾讯财经", _quote_tencent),
                      ("新浪财经", _quote_sina)):
        try:
            quote = fn(symbol)
            if quote and quote.price > 0:
                if not quote.change_pct and quote.prev_close:
                    quote.change_pct = (quote.price - quote.prev_close) / quote.prev_close * 100.0
                if not quote.change:
                    quote.change = quote.price - quote.prev_close
                _SOURCE_LOG["quote"] = label
                return _cache_put("quote:" + symbol, quote)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {exc}")
    raise DataSourceError("实时行情获取失败 -> " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# K 线
# --------------------------------------------------------------------------- #
def _bars_eastmoney(symbol: str, klt: int, fqt: int, bars: int) -> list[Bar]:
    beg = (_dt.date.today() - _dt.timedelta(days=int(bars * 1.65) + 40)).strftime("%Y%m%d")
    if beg < "19901219":  # A股最早交易日；beg 推算过远（如取全量历史）会被源站直接断连
        beg = "19901219"
    data = fetch_json(EM_KLINE.format(secid=_secid(symbol), klt=klt, fqt=fqt, beg=beg),
                      encoding="utf-8", referer="https://quote.eastmoney.com/")
    node = (data or {}).get("data") or {}
    rows = node.get("klines") or []
    out: list[Bar] = []
    for row in rows:
        cells = str(row).split(",")
        if len(cells) < 6:
            continue
        out.append(Bar(
            date=cells[0], open=_f(cells[1]), close=_f(cells[2]), high=_f(cells[3]), low=_f(cells[4]),
            volume=_f(cells[5]), amount=_f(cells[6]) if len(cells) > 6 else 0.0,
        ))
    if len(out) < max(30, bars * 0.5):
        # 复权因子导致的历史价格可能为负，或区间不够：退化为取全量再截取
        data = fetch_json(EM_KLINE.format(secid=_secid(symbol), klt=klt, fqt=fqt, beg=0),
                          encoding="utf-8", referer="https://quote.eastmoney.com/")
        rows = ((data or {}).get("data") or {}).get("klines") or []
        out = []
        for row in rows:
            cells = str(row).split(",")
            if len(cells) < 6:
                continue
            out.append(Bar(date=cells[0], open=_f(cells[1]), close=_f(cells[2]), high=_f(cells[3]),
                           low=_f(cells[4]), volume=_f(cells[5]),
                           amount=_f(cells[6]) if len(cells) > 6 else 0.0))
    return out[-bars:] if bars and len(out) > bars else out


_TX_PERIOD = {101: "day", 102: "week", 103: "month"}
_TX_FQ = {0: "", 1: "qfq", 2: "hfq"}


def _bars_tencent(symbol: str, klt: int, fqt: int, bars: int) -> list[Bar]:
    """腾讯日/周/月 K 线。单次请求上限约 640 根，bars 更大时按 end 日期循环向前翻页。"""
    period = _TX_PERIOD.get(klt, "day")
    fq = _TX_FQ.get(fqt, "qfq")
    request_n = max(int(bars), 60)

    def _parse(rows) -> list[Bar]:
        out: list[Bar] = []
        for row in rows or []:
            if not isinstance(row, (list, tuple)) or len(row) < 6:
                continue
            try:
                out.append(Bar(date=str(row[0])[:10], open=float(row[1]), close=float(row[2]),
                               high=float(row[3]), low=float(row[4]), volume=float(row[5])))
            except (TypeError, ValueError):
                continue
        return out

    def _rows_of(node) -> list | None:
        for key in (f"{fq}{period}", period, f"{period}{fq}"):
            value = (node or {}).get(key)
            if isinstance(value, list) and value:
                return value
        for _key, value in (node or {}).items():
            if isinstance(value, list) and value and isinstance(value[0], (list, tuple)) and len(value[0]) >= 6:
                return value
        return None

    def _fetch(end: str, n: int) -> list[Bar]:
        rows = None
        try:
            url = TX_KLINE.format(sym=symbol, period=period, start="", end=end, n=n, fq=fq)
            data = fetch_json(url, encoding="utf-8", referer="http://gu.qq.com/")
            rows = _rows_of(((data or {}).get("data") or {}).get(symbol))
        except Exception:  # noqa: BLE001
            rows = None            # 老域名 web.ifzq 被网关拒（HTTP 501）时不直接判死
        # 老域名不可用 / 北交所(920 段)只回最新 1 根：改走自选股 newfqkline（https，单次约 640 根）
        if rows is None or len(rows) < 10:
            try:
                url2 = TX_NEWKLINE.format(sym=symbol, period=period, start="", end=end, n=n, fq=fq)
                data2 = fetch_json(url2, encoding="utf-8", referer="http://gu.qq.com/")
                rows2 = _rows_of(((data2 or {}).get("data") or {}).get(symbol))
                if rows2:
                    rows = rows2
            except Exception:  # noqa: BLE001
                if rows is None:
                    raise
        return _parse(rows)

    out: list[Bar] = []
    end = ""
    prev_first = ""
    for _ in range(40):                    # 640 根/次 × 40 次，足够覆盖 A 股全历史
        want = min(640, request_n - len(out))
        n = max(want, 60)                  # count 过小接口可能异常，多拉后裁剪
        chunk = _fetch(end, n)
        if not chunk:
            break
        first = chunk[0].date
        if prev_first and first >= prev_first:
            break                          # 翻页无进展（已到上市首日/源站忽略 end）→ 终止
        prev_first = first
        have = {b.date for b in out}
        out = [b for b in chunk if b.date not in have] + out   # chunk 位于已取区间之前
        if len(out) >= request_n:
            break
        try:
            end = (_dt.date.fromisoformat(first) - _dt.timedelta(days=1)).strftime("%Y-%m-%d")
        except ValueError:
            break
        # 注意：不做「短页提前终止」——源站偶发短页会导致全量历史被截断（曾出现周线
        # 只取到 2011 年），继续向前翻页直到取空或无进展，翻页次数由 40 次上限兜底。
    if not out:
        raise DataSourceError("tencent kline empty")
    out.sort(key=lambda b: b.date)
    return out[-request_n:] if len(out) > request_n else out


def get_bars(symbol: str, bars: int = 300, period: str = "day", adjust: str = "qfq") -> list[Bar]:
    """日/周/月 K 线。period: day|week|month；adjust: qfq(前复权)|hfq|none。"""
    symbol = _resolve_bj_new_code(normalize_code(symbol))
    klt = {"day": 101, "week": 102, "month": 103}[period]
    fqt = {"qfq": 1, "hfq": 2, "none": 0}[adjust]
    key = f"bars:{symbol}:{period}:{adjust}:{bars}"
    cached = _cache_get(key, 120)
    if cached:
        return cached
    errors = []
    sources = [("东方财富", _bars_eastmoney), ("腾讯财经", _bars_tencent)]
    if fqt == 1:
        # 新浪只有日K（scale=240、不复权）：周期为周/月时必须先由日K聚合，
        # 否则会把日线当成周线/月线返回（之前周线/月线图表退化成日线的根因）
        sources.append(("新浪财经", lambda s, k, f, b: _bars_sina_period(s, period, b)))
    for label, fn in sources:
        try:
            rows = fn(symbol, klt, fqt, bars)
            if len(rows) >= 30:
                _SOURCE_LOG["bars"] = label
                _SOURCE_LOG[f"bars:{period}"] = label
                return _cache_put(key, rows)
            errors.append(f"{fn.__name__}: only {len(rows)} bars")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {exc}")
    raise DataSourceError(
        f"未能获取 {symbol} 的{period}K线。可能原因：代码不存在或已退市；"
        f"数据源限流（请等几分钟重试）。"
        f"[{period} K线获取失败 -> " + " | ".join(errors) + "]"
    )


def rescale_to_current(rows: list[Bar], anchor: float | None) -> list[Bar]:
    """把后复权序列等比缩放回当前报价量纲（末根收盘对齐 anchor）。

    后复权（hfq）的绝对值以首个交易日为基准，与现价/前复权的量纲不同（高分红
    个股可相差数倍，如茅台约 6.9 倍），会让图上的区间与关键线和复盘报告里的
    现价、区间数字对不上。按 k = anchor / 末根收盘 对整段 OHLC 等比缩放即可，
    形态与相对幅度完全不变；volume/amount 是真实成交数据，不做缩放。
    返回新对象，避免污染 get_bars 的缓存。
    """
    if not rows or not anchor or anchor <= 0:
        return rows
    last = rows[-1].close
    if not last or last <= 0:
        return rows
    k = anchor / last
    if abs(k - 1.0) < 1e-6:
        return rows
    return [Bar(date=r.date, open=round(r.open * k, 4), high=round(r.high * k, 4),
                low=round(r.low * k, 4), close=round(r.close * k, 4),
                volume=r.volume, amount=r.amount) for r in rows]


def _bars_tencent_mkline(symbol: str, period: str, bars: int) -> list[Bar]:
    data = fetch_json(TX_MKLINE.format(sym=symbol, period=period, n=bars),
                      encoding="utf-8", referer="http://gu.qq.com/")
    node = ((data or {}).get("data") or {}).get(symbol) or {}
    rows = node.get(period)
    if not isinstance(rows, list) or not rows:
        raise DataSourceError("tencent mkline empty")
    out: list[Bar] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        stamp = str(row[0])
        if len(stamp) == 12:  # 202610090935
            date = f"{stamp[0:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[8:10]}:{stamp[10:12]}"
        else:
            date = stamp
        try:
            out.append(Bar(date=date, open=float(row[1]), close=float(row[2]),
                           high=float(row[3]), low=float(row[4]), volume=float(row[5])))
        except (TypeError, ValueError):
            continue
    return out[-bars:]


def _bars_sina_mkline(symbol: str, scale: int, bars: int) -> list[Bar]:
    data = fetch_json(SINA_MKLINE.format(sym=symbol, scale=scale, n=bars),
                      encoding="utf-8", referer="https://finance.sina.com.cn")
    if not isinstance(data, list):
        raise DataSourceError("sina mkline malformed")
    out: list[Bar] = []
    for row in data:
        try:
            out.append(Bar(date=str(row.get("day", "")), open=float(row["open"]), high=float(row["high"]),
                           low=float(row["low"]), close=float(row["close"]),
                           volume=float(row["volume"]) / 100.0))
        except (KeyError, TypeError, ValueError):
            continue
    return out[-bars:]


def _bars_sina_daily(symbol: str, bars: int) -> list[Bar]:
    """新浪日K（scale=240 在 A 股等价日线，不复权），东财/腾讯全败时的兜底。"""
    out = _bars_sina_mkline(symbol, 240, min(bars + 60, 1023))
    if not out:
        raise DataSourceError("sina daily empty")
    return out[-bars:]


def _agg_daily(rows: list[Bar], period: str, bars: int) -> list[Bar]:
    """把日K按自然周/月聚合（开=首根开、收=末根收、高/低/量/额聚合，日期取该组末根）。"""
    out: list[Bar] = []
    key = None
    for b in rows:
        try:
            day = _dt.date.fromisoformat(str(b.date)[:10])
        except ValueError:
            continue
        k = day.isocalendar()[:2] if period == "week" else (day.year, day.month)
        last = out[-1] if out else None
        if last is not None and k == key:
            last.date = str(b.date)[:10]
            last.high = max(last.high, b.high)
            last.low = min(last.low, b.low) if last.low > 0 and b.low > 0 else max(last.low, b.low)
            last.close = b.close
            last.volume += b.volume
            last.amount += b.amount
        else:
            out.append(Bar(date=str(b.date)[:10], open=b.open, high=b.high, low=b.low,
                           close=b.close, volume=b.volume, amount=b.amount))
            key = k
    return out[-bars:] if bars and len(out) > bars else out


def _bars_sina_period(symbol: str, period: str, bars: int) -> list[Bar]:
    """新浪兜底（仅日K、不复权）：周/月线由日K聚合而来，禁止把日线当周/月线用。"""
    if period == "day":
        return _bars_sina_daily(symbol, bars)
    need_days = min(1023, max(120, bars * (6 if period == "week" else 24)))
    daily = _bars_sina_daily(symbol, need_days)
    return _agg_daily(daily, period, bars)


def _bars_eastmoney_mkline(symbol: str, klt: int, bars: int) -> list[Bar]:
    data = fetch_json(EM_KLINE.format(secid=_secid(symbol), klt=klt, fqt=1, beg=0),
                      encoding="utf-8", referer="https://quote.eastmoney.com/")
    rows = ((data or {}).get("data") or {}).get("klines") or []
    out: list[Bar] = []
    for row in rows:
        cells = str(row).split(",")
        if len(cells) < 6:
            continue
        out.append(Bar(date=cells[0], open=_f(cells[1]), close=_f(cells[2]), high=_f(cells[3]),
                       low=_f(cells[4]), volume=_f(cells[5])))
    if not out:
        raise DataSourceError("eastmoney mkline empty")
    return out[-bars:]


def get_intraday(symbol: str, bars: int = 240, period: str = "m5") -> list[Bar]:
    """分钟 K 线（默认 5 分钟）。用于当日/近几日实时走势复盘。"""
    symbol = _resolve_bj_new_code(normalize_code(symbol))
    key = f"mk:{symbol}:{period}:{bars}"
    cached = _cache_get(key, 60)
    if cached:
        return cached
    em_klt = {"m1": 1, "m5": 5, "m15": 15, "m30": 30, "m60": 60}[period]
    sina_scale = {"m1": 5, "m5": 5, "m15": 15, "m30": 30, "m60": 60}[period]
    errors = []
    for label, fn, args in (("东方财富", _bars_eastmoney_mkline, (symbol, em_klt, bars)),
                            ("腾讯财经", _bars_tencent_mkline, (symbol, period, bars)),
                            ("新浪财经", _bars_sina_mkline, (symbol, sina_scale, bars))):
        try:
            rows = fn(*args)
            if len(rows) >= 12:
                _SOURCE_LOG["intraday"] = label
                return _cache_put(key, rows)
            errors.append(f"{fn.__name__}: only {len(rows)} bars")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {exc}")
    raise DataSourceError("分时数据获取失败 -> " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# 大盘环境 / 代码搜索
# --------------------------------------------------------------------------- #
def get_index_quotes() -> list[dict]:
    cached = _cache_get("indexes", 60)
    if cached:
        return cached
    out: list[dict] = []
    try:
        text = fetch_text(TX_QUOTE.format(syms=",".join(c for c, _ in INDEXES)),
                          encoding="gbk", referer="http://gu.qq.com/")
        lines = {ln.split("=")[0].replace("v_", "").strip(): ln for ln in text.splitlines() if "=" in ln}
        for symbol, label in INDEXES:
            line = lines.get(symbol)
            if not line:
                continue
            quote = _parse_tencent_quote_line(line, symbol)
            if quote:
                out.append({"symbol": symbol, "name": label, "price": quote.price,
                            "change_pct": quote.change_pct})
    except Exception:  # noqa: BLE001
        out = []
    if not out:
        for symbol, label in INDEXES:
            try:
                quote = _quote_sina(symbol)
                out.append({"symbol": symbol, "name": label, "price": quote.price,
                            "change_pct": quote.change_pct})
            except Exception:  # noqa: BLE001
                continue
    return _cache_put("indexes", out) if out else out


def _mv_all_a():
    """沪深京全A总市值（亿元）：东财 clist 全市场逐页求和（同花顺全A口径，不含B股）。

    每页 pz=100（该接口实际上限），全市场约 5400+ 家需 ~55 页；成功值缓存 4 小时，
    另存最近成功值兜底（48 小时）。返回 (亿元值, "live"/"lkg") 或 (None, None)。
    东财 push2 存在风控性拒连，属预期内降级链路。
    """
    cached = _cache_get("mv_all_a", 14400)
    if cached is not None:
        return cached, "live"
    fs = "m:1+t:2,m:1+t:23,m:0+t:6,m:0+t:80,m:0+t:81+s:2048"   # 沪A主板+科创板+深A主板+创业板+北交所
    total_sum, total_cnt, page, completed = 0.0, 0, 1, False
    while page <= 60:
        j = fetch_json(EM_CLIST.format(page=page, fs=fs),
                       referer="https://quote.eastmoney.com/", retries=2)
        d = (j or {}).get("data") or {}
        rows = d.get("diff") or []
        total_cnt = int(d.get("total") or 0)
        for r in rows:
            v = r.get("f20")
            if isinstance(v, (int, float)):
                total_sum += float(v)                          # 元
        if not rows or (total_cnt and page * 100 >= total_cnt):
            completed = bool(total_cnt)
            break
        page += 1
    if completed and total_cnt and total_sum > 0:
        v = round(total_sum / 1e8, 1)
        _cache_put("mv_all_a", v)
        _cache_put("mv_all_a_last", v)
        return v, "live"
    lkg = _cache_get("mv_all_a_last", 86400 * 2)
    if lkg is not None:
        return lkg, "lkg"
    return None, None


def get_market_risk() -> dict:
    """大盘风险指标：上证/创业板 PE-TTM、A股总市值（沪深京全A）、巴菲特指标（总市值/GDP）。

    口径（均为实时探测验证过的稳定源）：
      * PE-TTM   ：腾讯行情串 f39（TTM 口径），上证 sh000001 / 创业板指 sz399006；
      * A股总市值：主源=东财 clist 全市场（沪深京全A，同花顺全A口径）f20 逐页求和；
                   失败回退腾讯综指口径（沪 sh000001 f45 + 深 sz399106 f45 + 北求和，含B股近似）；
                   分项 sh_mv_yi/sz_mv_yi/bj_mv_yi 恒为腾讯口径，仅供对照。
      * GDP      ：东财 RPT_ECONOMY_GDP。巴菲特指标分母按市场惯例用「当年全年名义GDP预估」：
                   当年全年未公布时按季节占比法推算（当年最新累计期现值÷上年同期占比），
                   分项字段 gdp_est_yi 为分母、gdp_yi 为最新实际值、gdp_basis 为推算依据。
    任一分项失败置 None 不影响其他分项；整体缓存 300 秒，全部失败时不缓存以便下次重试。
    """
    cached = _cache_get("market_risk", 300)
    if cached:
        return cached

    def _num(parts: list[str], idx: int):
        try:
            v = float(parts[idx])
            return v if v == v else None   # 过滤 NaN
        except (IndexError, ValueError, TypeError):
            return None

    # 1) 腾讯一次请求：两指数 PE + 沪/深市场总市值
    sh_pe = cyb_pe = sh_mv = sz_mv = None
    try:
        text = fetch_text(TX_QUOTE.format(syms="sh000001,sz399006,sz399106"),
                          encoding="gbk", referer="http://gu.qq.com/")
        fields: dict[str, list[str]] = {}
        for line in text.splitlines():
            if "=" not in line:
                continue
            sym = line.split("=")[0].replace("v_", "").strip()
            body = line.split("=", 1)[1].strip().strip(";").strip('"')
            parts = body.split("~")
            if len(parts) > 46:
                fields[sym] = parts
        if "sh000001" in fields:
            sh_pe = _num(fields["sh000001"], 39)
            sh_mv = _num(fields["sh000001"], 45)   # 亿
        if "sz399006" in fields:
            cyb_pe = _num(fields["sz399006"], 39)
        if "sz399106" in fields:
            sz_mv = _num(fields["sz399106"], 45)   # 亿
    except Exception:  # noqa: BLE001
        pass

    # 2) 北交所总市值：东财 clist 按 100 条/页分页求和（total 约在 250~360 间浮动）
    #    push2 clist 存在风控性 RemoteDisconnected（https/http 均可能被拒），
    #    故先用最近一次成功值兜底（缓存 30 分钟），过期才重新拉取并带重试
    bj_mv = _cache_get("market_risk_bj", 1800)
    if bj_mv is None:
        try:
            total_sum, total_cnt, page = 0.0, 0, 1
            while page <= 10:
                j = fetch_json(EM_CLIST.format(page=page, fs="m:0+t:81+s:2048"),
                               referer="https://quote.eastmoney.com/", retries=2)
                d = (j or {}).get("data") or {}
                rows = d.get("diff") or []
                total_cnt = int(d.get("total") or 0)
                for r in rows:
                    v = r.get("f20")
                    if isinstance(v, (int, float)):
                        total_sum += float(v)          # 元
                if not rows or (total_cnt and page * 100 >= total_cnt):
                    break
                page += 1
            if total_cnt and total_sum > 0:            # 防空页误写 0
                bj_mv = round(total_sum / 1e8, 1)      # 元 → 亿
                _cache_put("market_risk_bj", bj_mv)
        except Exception:  # noqa: BLE001
            pass

    # 3) GDP：巴菲特指标无官方每日发布值，市场惯例分母用「当年全年名义GDP预估数」。
    #    优先取当年全年实际值；未公布时按季节占比法推算：
    #    当年最新累计期现值 ÷（上年同期累计现值 ÷ 上年全年现值）
    gdp_yi = None        # 最新已公布实际值（亿元）
    gdp_period = None
    gdp_est_yi = None    # 当年全年名义GDP（实际值或推算预估值，亿元）
    gdp_basis = ""       # 预估依据说明
    try:
        j = fetch_json(EM_GDP, referer="https://data.eastmoney.com/")
        rows = ((j or {}).get("result") or {}).get("data") or []
        if rows:
            latest_t = str(rows[0].get("TIME") or "")
            this_year = str(_dt.date.today().year)

            def _val(row):
                v = (row or {}).get("DOMESTICL_PRODUCT_BASE")
                return float(v) if isinstance(v, (int, float)) and v > 0 else None

            if latest_t.endswith("第1-4季度"):
                v = _val(rows[0])
                if v:
                    gdp_yi = round(v, 1)
                    gdp_period = latest_t
                    gdp_est_yi = gdp_yi
                    gdp_basis = "当年全年实际值" if latest_t.startswith(this_year) \
                        else f"当年未公布，暂用{latest_t[:4]}年实际值"
            else:
                cur_year = latest_t[:4]                    # 如 "2026"
                cur_row = next((r for r in rows
                                if str(r.get("TIME") or "").startswith(cur_year + "年")), None)
                cur_t = str((cur_row or {}).get("TIME") or "")
                suffix = cur_t.split("年", 1)[-1]          # 如 "第1-2季度"
                prev = {str(r.get("TIME") or ""): r for r in rows}
                cv, pv, pa = _val(cur_row), _val(prev.get(f"{int(cur_year) - 1}年{suffix}")), \
                    _val(prev.get(f"{int(cur_year) - 1}年第1-4季度"))
                if cv and pv and pa:
                    gdp_est_yi = round(cv * pa / pv, 1)    # 季节占比法
                    share = pv / pa * 100
                    gdp_basis = (f"按{cur_t}现值{cv / 1e4:.1f}万亿"
                                 f"÷上年同期占比{share:.1f}%推算")
                if cv:
                    gdp_yi = round(cv, 1)                  # 已公布的最新累计实际值
                    gdp_period = cur_t
    except Exception:  # noqa: BLE001
        pass

    # 4) 分子总市值：主源=东财全市场逐页求和（沪深京全A口径，同花顺全A/Wind 同口径），
    #    实拉失败用最近成功值（≤48小时）；再失败回退腾讯综指口径（含B股近似）
    mv_val, mv_kind = None, None
    try:
        mv_val, mv_kind = _mv_all_a()
    except Exception:  # noqa: BLE001
        pass
    if mv_val is not None:
        total_mv = mv_val
        mv_src = "all_a" if mv_kind == "live" else "all_a_lkg"
    else:
        mv_src = "composite"
        total_mv = None
        if sh_mv is not None and sz_mv is not None:
            total_mv = round(sh_mv + sz_mv + (bj_mv or 0.0), 1)
    buffett_pct = None
    if total_mv and gdp_est_yi:
        buffett_pct = round(total_mv / gdp_est_yi * 100, 1)

    out = {
        "sh_pe": sh_pe, "cyb_pe": cyb_pe,
        "sh_mv_yi": sh_mv, "sz_mv_yi": sz_mv, "bj_mv_yi": bj_mv,
        "total_mv_yi": total_mv, "mv_src": mv_src,
        "gdp_yi": gdp_yi, "gdp_period": gdp_period,
        "gdp_est_yi": gdp_est_yi, "gdp_basis": gdp_basis,
        "buffett_pct": buffett_pct,
    }
    if sh_pe is None and total_mv is None:
        return out   # 全部失败：不写缓存，下次请求直接重试
    return _cache_put("market_risk", out)


def search(keyword: str, limit: int = 8) -> list[dict]:
    """按代码或名称搜索（用于 Web 输入联想）。"""
    keyword = (keyword or "").strip()
    if not keyword:
        return []
    results: list[dict] = []
    try:
        data = fetch_json(EM_SUGGEST.format(q=urllib.parse.quote(keyword)), encoding="utf-8")
        for item in ((data or {}).get("QuotationCodeTable") or {}).get("Data") or []:
            classify = str(item.get("Classify", ""))
            # NEEQ = 北交所/股转系统在东财 suggest 里的遗留分类名，920 京A 股票均为 NEEQ
            if classify and classify not in ("AStock", "Index", "Bond", "Fund", "NEEQ"):
                continue
            code6 = str(item.get("Code", ""))
            if str(item.get("MktNum")) == "1":
                market = "SH"
            elif code6[:2] in ("92", "83", "87", "43", "88") or "京" in str(item.get("SecurityTypeName", "")):
                market = "BJ"
            else:
                market = "SZ"
            # 北交所优先：920 京A 排在 NEEQ 新三板基础层/创新层之前
            rank = 0 if code6.startswith("92") else 1
            results.append({"code": code6, "name": str(item.get("Name", "")),
                            "market": market, "_rank": rank})
            if len(results) >= limit + 4:
                break
    except Exception:  # noqa: BLE001
        results = []
    results.sort(key=lambda it: it.pop("_rank", 1))
    if results:
        return results
    try:
        text = fetch_text(TX_SEARCH.format(q=urllib.parse.quote(keyword)), encoding="gbk",
                          referer="http://gu.qq.com/")
        body = text.split('="', 1)[-1].rstrip('";\n ')
        for chunk in body.split("^"):
            cells = chunk.split("~")
            if len(cells) >= 3 and re.fullmatch(r"\d{6}", cells[1]):
                results.append({"code": cells[1], "name": cells[2],
                                "market": cells[0].upper()})
            if len(results) >= limit:
                break
    except Exception:  # noqa: BLE001
        pass
    return results


def resolve_input(raw: str) -> tuple[str, str | None, str]:
    """把用户输入（代码或名称/拼音）解析成带市场前缀的 symbol。

    返回 (symbol, resolved_name, via)：via 为 'code' 或 'search'。
    代码格式优先走 normalize_code（既有行为零变更）；否则按名称搜索取最佳 A 股匹配。
    用 normalize_code(code) 按代码首位推导市场前缀，比 search() 返回的 market
    （仅 SH/SZ，会把北交所误判为 SZ）更准。
    """
    try:
        return normalize_code(raw), None, "code"
    except ValueError:
        pass
    items = search(raw, limit=8)
    if not items:
        raise ValueError(f"未找到与「{raw}」匹配的A股，请用代码如 600519 或名称如 贵州茅台")
    target = (raw or "").strip()
    exact = [it for it in items if it.get("name") == target]
    pick = exact[0] if exact else items[0]
    return normalize_code(pick["code"]), pick.get("name"), "search"


# --------------------------------------------------------------------------- #
# 财务数据
# --------------------------------------------------------------------------- #
def _secu_code(symbol: str) -> str:
    """返回东财 SECUCODE 格式：600519.SH / 000001.SZ / 430047.BJ。"""
    market, code = split_code(symbol)
    return f"{code}.{market.upper()}"


def get_financials(symbol: str) -> dict | None:
    """获取公司重要财务指标：ROE、股息率、营收、净利润、毛利率、资产负债率等。

    数据来源：东方财富 datacenter 接口（主要指标 + 分红送转）。
    任一接口失败不影响另一个；全部失败返回 None，由调用方走降级提示。
    """
    symbol, _, _ = resolve_input(symbol)
    symbol = _resolve_bj_new_code(symbol)
    cached = _cache_get("fin:" + symbol, 300)
    if cached:
        return cached

    secu = _secu_code(symbol)
    ref = "https://emweb.securities.eastmoney.com/"
    result: dict = {}
    source_parts: list[str] = []

    # ---- 主要财务指标（含 ROE / 资产负债率 / 净利率 / ROIC）----
    try:
        data = fetch_json(EM_FIN_MAIN.format(secu=urllib.parse.quote(secu, safe="")),
                          encoding="utf-8", referer=ref)
        rows = ((data or {}).get("result") or {}).get("data") or []
        if rows:
            row = rows[0]
            result["roe"] = _f(row.get("ROEJQ"))
            result["roic"] = _f(row.get("ROIC"))
            result["debt_ratio"] = _f(row.get("ZCFZL"))
            result["net_margin"] = _f(row.get("XSJLL"))
            result["gross_margin"] = _f(row.get("XSMLL"))
            result["revenue"] = _f(row.get("TOTALOPERATEREVE"))
            result["revenue_yoy"] = _f(row.get("TOTALOPERATEREVETZ"))
            result["net_profit"] = _f(row.get("PARENTNETPROFIT"))
            result["net_profit_yoy"] = _f(row.get("PARENTNETPROFITTZ"))
            result["eps"] = _f(row.get("EPSJB"))
            result["bps"] = _f(row.get("BPS"))
            result["deducted_eps"] = _f(row.get("EPSKCJB"))
            result["deducted_net_profit"] = _f(row.get("KCFJCXSYJLR"))
            result["report_date"] = str(row.get("REPORT_DATE", ""))[:10]
            result["report_type"] = str(row.get("REPORT_DATE_NAME", ""))
            source_parts.append("主要指标")
    except Exception:  # noqa: BLE001
        pass

    # ---- 分红送转（含股息率 / 分红方案）----
    try:
        data2 = fetch_json(EM_FIN_CPD.format(secu=urllib.parse.quote(secu, safe="")),
                           encoding="utf-8", referer=ref)
        rows2 = ((data2 or {}).get("result") or {}).get("data") or []
        # 分红统计：TTM（过去12个月）+ 各 PAYYEAR 求和
        ttm_div, _, year_dividends = _dividend_stats(rows2)
        # 找最新一条有 ZXGXL 或 ASSIGNDSCRPT 的记录（用于展示分红方案文本）
        div_row = None
        for r in rows2:
            desc = str(r.get("ASSIGNDSCRPT", "") or "")
            if r.get("ZXGXL") is not None or (desc and "不分配" not in desc):
                div_row = r
                break
        if not div_row and rows2:
            div_row = rows2[0]
        if div_row:
            # 实时股息率：TTM 过去12个月分红 / 当前股价
            dy_from_price = None
            if ttm_div > 0:
                try:
                    q = get_quote(symbol)
                    if q and q.price > 0:
                        dy_from_price = ttm_div / q.price * 100.0
                except Exception:  # noqa: BLE001
                    pass
            best_year = max(year_dividends.keys()) if year_dividends else ""
            result["dividend_yield"] = round(dy_from_price, 2) if dy_from_price else _f(div_row.get("ZXGXL"))
            result["dividend_plan"] = str(div_row.get("ASSIGNDSCRPT", "")).strip()
            result["dividend_year"] = best_year or str(div_row.get("PAYYEAR", ""))
            if ttm_div > 0:
                result["dividend_per_share"] = round(ttm_div, 4)
            elif best_year:
                result["dividend_per_share"] = round(year_dividends.get(best_year, 0.0), 4)
            # 如果主要指标接口没取到，用分红接口补
            if "roe" not in result:
                result["roe"] = _f(div_row.get("WEIGHTAVG_ROE"))
            if "eps" not in result:
                result["eps"] = _f(div_row.get("BASIC_EPS"))
            if "bps" not in result:
                result["bps"] = _f(div_row.get("BPS"))
            if "gross_margin" not in result:
                result["gross_margin"] = _f(div_row.get("XSMLL"))
            if "revenue" not in result:
                result["revenue"] = _f(div_row.get("TOTAL_OPERATE_INCOME"))
            if "net_profit" not in result:
                result["net_profit"] = _f(div_row.get("PARENT_NETPROFIT"))
            if "report_type" not in result:
                result["report_type"] = str(div_row.get("DATATYPE", ""))
            source_parts.append("分红送转")
    except Exception:  # noqa: BLE001
        pass

    if not result:
        return None

    result["source"] = "、".join(source_parts) if source_parts else "东方财富"
    _SOURCE_LOG["financials"] = result["source"]
    return _cache_put("fin:" + symbol, result)


def get_financial_trend(symbol: str) -> dict | None:
    """获取年度财务趋势数据（最近 10~12 年年报），用于画 10 年趋势图。

    返回格式：
        {
            "dates": ["2014", "2015", ..., "2025"],
            "roe": [31.96, 26.23, ..., 32.53],
            "roic": [...],
            "gross_margin": [...],
            "net_margin": [...],
            "debt_ratio": [...],
            "revenue": [...],        # 元
            "net_profit": [...],     # 元
            "eps": [...],
            "bps": [...],
            "dividend_yield": [...], # %
        }
    所有数组按时间升序排列。任一接口失败返回 None 或部分字段缺失。
    """
    symbol, _, _ = resolve_input(symbol)
    cached = _cache_get("fintrend:" + symbol, 600)
    if cached:
        return cached

    secu = _secu_code(symbol)
    ref = "https://emweb.securities.eastmoney.com/"

    # ---- 主要指标：每年取一条（优先年报，否则最新期）----
    annual_rows: list[dict] = []
    try:
        data = fetch_json(EM_FIN_TREND_MAIN.format(secu=urllib.parse.quote(secu, safe="")),
                          encoding="utf-8", referer=ref)
        rows = ((data or {}).get("result") or {}).get("data") or []
        # 东财返回倒序（最新在前），按年份分组，每年优先年报
        year_best: dict[str, dict] = {}
        for r in rows:
            rd = str(r.get("REPORT_DATE", ""))
            if not rd.startswith("20"):
                continue
            year = rd[:4]
            is_annual = "-12-31" in rd
            if year not in year_best:
                year_best[year] = r  # 该年第一条（最新期）
            elif is_annual and "-12-31" not in str(year_best[year].get("REPORT_DATE", "")):
                year_best[year] = r  # 优先年报替换非年报
        annual_rows = sorted(year_best.values(),
                             key=lambda x: str(x.get("REPORT_DATE", "")))
    except Exception:  # noqa: BLE001
        pass

    # ---- 分红送转：有分红记录的（排除不分配）----
    div_rows: list[dict] = []
    try:
        data2 = fetch_json(EM_FIN_TREND_CPD.format(secu=urllib.parse.quote(secu, safe="")),
                           encoding="utf-8", referer=ref)
        rows2 = ((data2 or {}).get("result") or {}).get("data") or []
        for r in rows2:
            desc = str(r.get("ASSIGNDSCRPT", "") or "")
            if r.get("ZXGXL") is not None or (desc and "不分配" not in desc and "不转增" not in desc):
                div_rows.append(r)
        div_rows.reverse()
    except Exception:  # noqa: BLE001
        pass

    if not annual_rows and not div_rows:
        return None

    result: dict = {}
    result["dates"] = []
    keys = ["roe", "roic", "gross_margin", "net_margin", "debt_ratio",
            "revenue", "net_profit", "eps", "bps"]
    for k in keys:
        result[k] = []

    # 从 MAINFINADATA 提取
    for r in annual_rows:
        rd = str(r.get("REPORT_DATE", ""))
        year = rd[:4]
        result["dates"].append(year)
        result["roe"].append(_f(r.get("ROEJQ")))
        result["roic"].append(_f(r.get("ROIC")))
        result["gross_margin"].append(_f(r.get("XSMLL")))
        result["net_margin"].append(_f(r.get("XSJLL")))
        result["debt_ratio"].append(_f(r.get("ZCFZL")))
        result["revenue"].append(_f(r.get("TOTALOPERATEREVE")))
        result["net_profit"].append(_f(r.get("PARENTNETPROFIT")))
        result["eps"].append(_f(r.get("EPSJB")))
        result["bps"].append(_f(r.get("BPS")))

    # 从 CPD 提取股息率：历史用东财 ZXGXL（同年求和），最新年用 TTM（过去12个月分红/当前股价）
    ttm_div, year_zxgxl, year_div_total = _dividend_stats(div_rows)

    # 实时股息率（TTM：过去12个月分红 / 当前股价）
    dy_latest: float | None = None
    if ttm_div > 0:
        try:
            q = get_quote(symbol)
            if q and q.price > 0:
                dy_latest = ttm_div / q.price * 100.0
        except Exception:  # noqa: BLE001
            pass

    # 对齐到 dates：历史用东财 ZXGXL 求和，最新年用 TTM 实时计算
    result["dividend_yield"] = []
    for year in result["dates"]:
        dy = year_zxgxl.get(year)               # 历史年份用 ZXGXL 求和
        if dy_latest is not None and year == result["dates"][-1]:
            dy = round(dy_latest, 2)            # 最新年用 TTM
        result["dividend_yield"].append(dy)

    # 如果只有 CPD 数据（极少见），补 dates
    if not result["dates"] and (year_zxgxl or year_div_total):
        years = sorted(set(list(year_zxgxl.keys()) + list(year_div_total.keys())))
        result["dates"] = years
        for k in keys:
            result[k] = [None] * len(years)
        result["dividend_yield"] = []
        for y in years:
            dy = year_zxgxl.get(y)
            if dy_latest is not None and y == years[-1]:
                dy = round(dy_latest, 2)
            result["dividend_yield"].append(dy)

    return _cache_put("fintrend:" + symbol, result)
