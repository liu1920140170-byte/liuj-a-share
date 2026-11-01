#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""本地网页版服务（只用 Python 标准库，无需安装任何依赖）。

    python server.py            # 默认 http://127.0.0.1:8765
    python server.py --port 9000 --open
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # 跟随终端编码，只避免编码异常中断服务
    except Exception:  # noqa: BLE001
        pass

from wyckoff.api import (analyze, index_chart, screener_results, screener_scan,  # noqa: E402
                         _candle_patterns, _chart_patterns, _key_lines, _minor_trends)
from wyckoff.datasource import (DataSourceError, get_bars, get_financial_trend,  # noqa: E402
                                get_intraday, get_market_risk, get_quote, is_index,
                                rescale_to_current, resolve_input, search as ds_search)
from wyckoff.fundamental import fundamental_report  # noqa: E402
from wyckoff.intraday import intraday_advice  # noqa: E402
from wyckoff.wm_model import wm_analyze  # noqa: E402

ROOT = os.path.dirname(os.path.abspath(__file__))
WEB_FILE = os.path.join(ROOT, "web", "index.html")
DD_FILE = os.path.join(ROOT, "web", "dd.html")
# PWA 静态资源（手机“添加到主屏幕/安装应用”所需）
STATIC_ROUTES = {
    "/manifest.webmanifest": ("web/manifest.webmanifest", "application/manifest+json; charset=utf-8"),
    "/sw.js": ("web/sw.js", "application/javascript; charset=utf-8"),
    "/icon-192.png": ("web/icon-192.png", "image/png"),
    "/icon-512.png": ("web/icon-512.png", "image/png"),
    "/icon-512-maskable.png": ("web/icon-512-maskable.png", "image/png"),
    "/apple-touch-icon.png": ("web/apple-touch-icon.png", "image/png"),
    "/favicon.ico": ("web/icon-192.png", "image/png"),
}
CACHE_TTL = 30.0
_cache: dict[str, tuple[float, dict]] = {}
# 大盘K线图数据缓存（code -> (时间戳, payload)），指数日频数据缓存 5 分钟
_index_chart_cache: dict[str, tuple[float, dict]] = {}
# 最近一次成功 analyze 的完整结果（长驻缓存，供 /api/intraday 轻量轮询复用日线结构，
# 避免 30 秒一次的全量重算触发东财风控）
_last_payload: dict[str, dict] = {}
# W-M 模型结果缓存（code -> (时间戳, payload)），5 分钟；模型含行情快照与板块榜
_wm_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()

PERIOD_CN = {"day": "日线", "m60": "60分钟", "m30": "30分钟", "m15": "15分钟",
             "week": "周线", "month": "月线", "year": "年线"}
MIN_PERIODS = {"m60", "m30", "m15"}


def _aggregate_year(month_rows) -> list[dict]:
    """把月线按自然年聚合成年线（首月开盘 / 末月收盘 / 逐月取极值）。"""
    out: list[dict] = []
    cur: dict | None = None
    cur_year = ""
    for r in month_rows:
        y = str(r.date or "")[:4]
        if not y:
            continue
        if y != cur_year:
            if cur:
                out.append(cur)
            cur_year = y
            cur = {"date": y + "-12-31", "open": r.open, "close": r.close,
                   "high": r.high, "low": r.low, "volume": r.volume}
        elif cur:
            cur["close"] = r.close
            cur["high"] = max(cur["high"], r.high)
            cur["low"] = min(cur["low"], r.low)
            cur["volume"] += r.volume
    if cur:
        out.append(cur)
    return out

BANNER = r"""
  ┌──────────────────────────────────────────────────────────────┐
  │   聪明钱复盘台 · A股  本地网页版                             │
  │   浏览器打开： http://127.0.0.1:{port}/                      │
  │   输入股票代码（如 600519 / 300750 / sh000001）即可得到分析  │
  │   按 Ctrl+C 停止服务                                         │
  └──────────────────────────────────────────────────────────────┘
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "WyckoffAShare/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))

    # ------------------------------------------------------------------ #
    def _send(self, code: int, body: bytes, content_type: str):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: dict, code: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # ------------------------------------------------------------------ #
    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            return self._serve_index()
        if path == "/dd.html":
            return self._serve_dd()
        static = STATIC_ROUTES.get(path)
        if static:
            return self._serve_static(*static)
        if path == "/api/fundamental":
            code = (query.get("code") or [""])[0].strip()
            if not code:
                return self._send_json({"ok": False, "error": "缺少参数 code，例如 /api/fundamental?code=600519"}, 400)
            try:
                return self._send_json(fundamental_report(code))
            except (DataSourceError, ValueError) as exc:
                return self._send_json({"ok": False, "error": f"{exc}"}, 200)
            except Exception as exc:  # noqa: BLE001
                traceback.print_exc()
                return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"}, 200)
        if path == "/api/health":
            return self._send_json({"ok": True, "service": "wyckoff-a-share", "time": time.time()})
        if path == "/api/search":
            keyword = (query.get("q") or [""])[0]
            try:
                return self._send_json({"ok": True, "items": ds_search(keyword)})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc), "items": []})
        if path == "/api/analyze":
            return self._api_analyze(query)
        if path == "/api/intraday":
            return self._api_intraday(query)
        if path == "/api/financial_trend":
            return self._api_financial_trend(query)
        if path == "/api/kline":
            return self._api_kline(query)
        if path == "/api/index_chart":
            return self._api_index_chart(query)
        if path == "/api/market_risk":
            try:
                return self._send_json({"ok": True, **get_market_risk()})
            except Exception as exc:  # noqa: BLE001
                return self._send_json({"ok": False, "error": str(exc)})
        if path == "/api/screener":
            return self._send_json(screener_results())
        if path == "/api/screener/scan":
            # limit 仅用于冒烟验证（只扫前 N 只、不写缓存），正常使用不传
            limit_raw = (query.get("limit") or [""])[0]
            limit = None
            try:
                if limit_raw:
                    limit = max(1, min(9999, int(limit_raw)))
            except ValueError:
                limit = None
            return self._send_json(screener_scan(limit=limit))
        if path == "/api/wm":
            return self._api_wm(query)
        return self._send_json({"ok": False, "error": f"unknown path: {path}"}, 404)

    do_HEAD = do_GET

    # ------------------------------------------------------------------ #
    def _serve_index(self):
        try:
            with open(WEB_FILE, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            return self._send(500, f"无法读取页面文件：{exc}".encode("utf-8"),
                              "text/plain; charset=utf-8")
        return self._send(200, body, "text/html; charset=utf-8")

    def _serve_dd(self):
        try:
            with open(DD_FILE, "rb") as handle:
                body = handle.read()
        except OSError as exc:
            return self._send(500, f"无法读取页面文件：{exc}".encode("utf-8"),
                              "text/plain; charset=utf-8")
        return self._send(200, body, "text/html; charset=utf-8")

    def _serve_static(self, rel: str, content_type: str):
        path = os.path.join(ROOT, rel)
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        return self._send(200, body, content_type)

    def _api_analyze(self, query: dict):
        code = (query.get("code") or [""])[0].strip()
        if not code:
            return self._send_json({"ok": False, "error": "缺少参数 code，例如 /api/analyze?code=600519"}, 400)
        try:
            bars = int((query.get("bars") or ["300"])[0])
        except ValueError:
            bars = 300
        adjust = (query.get("adjust") or ["qfq"])[0]
        key = f"{code}|{bars}|{adjust}"
        with _lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] <= CACHE_TTL:
                return self._send_json(hit[1])
        try:
            payload = analyze(code, bars=bars, adjust=adjust)
        except (DataSourceError, ValueError) as exc:
            return self._send_json({"ok": False, "error": f"{exc}"}, 200)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"}, 200)
        with _lock:
            _cache[key] = (time.time(), payload)
            _last_payload[key] = payload
            if len(_cache) > 120:
                oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:40]
                for k, _ in oldest:
                    _cache.pop(k, None)
            if len(_last_payload) > 120:
                for k in list(_last_payload.keys())[:40]:
                    _last_payload.pop(k, None)
        return self._send_json(payload)

    def _api_intraday(self, query: dict):
        """轻量当日作战接口：复用最近一次完整分析的日线结构，只新取实时快照 + 5 分钟K。

        前端盘中每 30 秒轮询本接口实现「及时」；日线结构（区间/趋势线/决策位）
        与图表展示完全同源，两条入口结论口径一致。
        """
        code = (query.get("code") or [""])[0].strip()
        if not code:
            return self._send_json({"ok": False, "error": "缺少参数 code"}, 400)
        try:
            bars = int((query.get("bars") or ["300"])[0])
        except ValueError:
            bars = 300
        bars = int(max(120, min(9999, bars)))
        adjust = (query.get("adjust") or ["qfq"])[0]
        key = f"{code}|{bars}|{adjust}"
        with _lock:
            payload = _last_payload.get(key)
        if not payload:
            return self._send_json({
                "ok": False,
                "error": "暂无该股的完整分析结果，请先点击「刷新分析」运行一次完整分析"}, 200)
        meta = payload.get("meta") or {}
        symbol = str(meta.get("symbol") or "")
        if not symbol:
            return self._send_json({"ok": False, "error": "历史结果缺少代码信息，请重新运行完整分析"}, 200)
        quote = None
        quote_err = ""
        try:
            quote = get_quote(symbol)
        except Exception as exc:  # noqa: BLE001
            quote_err = str(exc)
        try:
            minute_rows = get_intraday(symbol, bars=96, period="m5")
        except Exception:  # noqa: BLE001
            minute_rows = []
        kline = payload.get("kline") or {}
        dates = kline.get("dates") or []
        daily = {"date": dates[-1] if dates else "",
                 "open": kline.get("open") or [], "high": kline.get("high") or [],
                 "low": kline.get("low") or [], "close": kline.get("close") or [],
                 "volume": kline.get("volume") or [], "adjust": str(meta.get("adjust") or "")}
        try:
            advice = intraday_advice(minute_rows, quote, (payload.get("wyckoff") or {}).get("range") or {},
                                     kline.get("trend_lines") or [], payload.get("decision") or {},
                                     daily=daily, index_mode=is_index(symbol))
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"}, 200)
        if quote_err and advice.get("ok"):
            advice = dict(advice)
            advice["note"] = "; ".join(x for x in (advice.get("note", ""),
                                                   f"实时快照获取失败（{quote_err}）") if x)
        return self._send_json(advice)

    def _api_wm(self, query: dict):
        """W-M 模型：威科夫-均线共振自上而下选股（宏观 → 海选 → 精选 → 执行）。"""
        code = (query.get("code") or [""])[0].strip()
        if not code:
            return self._send_json({"ok": False, "error": "缺少参数 code，例如 /api/wm?code=600519"}, 400)
        try:
            bars = int((query.get("bars") or ["320"])[0])
        except ValueError:
            bars = 320
        period = (query.get("period") or ["day"])[0].strip().lower() or "day"
        if period not in ("day", "week", "month", "m60", "m30"):
            period = "day"
        key = f"{code}|{period}|{bars}"
        with _lock:
            hit = _wm_cache.get(key)
            if hit and time.time() - hit[0] <= 300.0:
                return self._send_json(hit[1])
        try:
            payload = wm_analyze(code, bars=bars, period=period)
        except (DataSourceError, ValueError) as exc:
            return self._send_json({"ok": False, "error": str(exc)}, 200)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"}, 200)
        with _lock:
            _wm_cache[key] = (time.time(), payload)
            if len(_wm_cache) > 60:
                for k in list(_wm_cache.keys())[:20]:
                    _wm_cache.pop(k, None)
        return self._send_json(payload)

    def _api_financial_trend(self, query: dict):
        code = (query.get("code") or [""])[0].strip()
        if not code:
            return self._send_json({"ok": False, "error": "缺少参数 code"}, 400)
        try:
            trend = get_financial_trend(code)
            if not trend:
                return self._send_json({"ok": False, "error": "未获取到历史财务数据"})
            return self._send_json({"ok": True, "trend": trend})
        except (DataSourceError, ValueError) as exc:
            return self._send_json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"})

    def _api_index_chart(self, query: dict):
        """大盘指数K线图数据：K线 + 威科夫事件 + 供给/需求线 + 交易区间（缓存 5 分钟，按 代码+周期+K线数量 缓存）。"""
        code = (query.get("code") or [""])[0].strip().lower()
        if not code:
            return self._send_json({"ok": False, "error": "缺少参数 code，例如 /api/index_chart?code=sh000001"}, 400)
        period = (query.get("period") or ["day"])[0].strip().lower()
        try:
            bars = int((query.get("bars") or ["160"])[0])
        except ValueError:
            bars = 160
        bars = int(max(30, min(9999, bars)))
        key = f"{code}|{period}|{bars}"
        hit = _index_chart_cache.get(key)
        if hit and time.time() - hit[0] <= 300.0:
            return self._send_json(hit[1])
        try:
            payload = index_chart(code, bars=bars, period=period)
        except (DataSourceError, ValueError) as exc:
            return self._send_json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"})
        _index_chart_cache[key] = (time.time(), payload)
        return self._send_json(payload)

    def _api_kline(self, query: dict):
        """独立K线接口：任意周期（日/60分/30分/15分/周/月/年）的K线，供图表周期切换使用。

        period=year 时取月线并按自然年聚合（开=首日开盘，收=末日收盘，高/低/量聚合）；
        period=m60/m30/m15 时取分钟K线（前复权近期数据），并同样识别K线整理形态。
        """
        code = (query.get("code") or [""])[0].strip()
        period = (query.get("period") or ["day"])[0].strip().lower()
        adjust = (query.get("adjust") or ["qfq"])[0].strip().lower()
        if not code:
            return self._send_json({"ok": False, "error": "缺少参数 code，例如 /api/kline?code=600519&period=week"}, 400)
        if period not in PERIOD_CN:
            return self._send_json({"ok": False, "error": f"period 仅支持：{'/'.join(PERIOD_CN)}"}, 400)
        if adjust not in ("qfq", "hfq", "none"):
            adjust = "qfq"
        try:
            symbol, name, _via = resolve_input(code)
            note = ""
            if period in MIN_PERIODS:
                # 小周期分钟K线（东财→腾讯→新浪多源兜底），固定前复权，取最近 1000 根
                rows = get_intraday(symbol, bars=1000, period=period)
                adjust = "qfq"
                note = f"{PERIOD_CN[period]}K线为前复权近期数据（最多1000根），形态识别阈值与日线一致"
                bars_out = [{"date": r.date, "open": r.open, "close": r.close, "high": r.high,
                             "low": r.low, "volume": r.volume} for r in rows]
            else:
                fetch_period = "month" if period == "year" else period
                rows = get_bars(symbol, bars=20000, period=fetch_period, adjust=adjust)
                # 全量历史下，前复权价格可能出现非正值（高分红个股），自动切换后复权；
                # 后复权以首日为基准，量纲与现价差数倍，需按最近前复权收盘等比缩放，
                # 否则图上区间/关键线与复盘报告里的现价、区间数字对不上（茅台差约 6.9 倍）
                if any(b.close <= 0 or b.low <= 0 for b in rows):
                    anchor = rows[-1].close if (rows and rows[-1].close > 0) else 0.0
                    rows = get_bars(symbol, bars=20000, period=fetch_period, adjust="hfq")
                    rows = rescale_to_current(rows, anchor)
                    adjust = "hfq"
                    note = "已改用后复权 · 价格已换算为现价量纲"
                if period == "year":
                    bars_out = _aggregate_year(rows)
                else:
                    bars_out = [{"date": r.date, "open": r.open, "close": r.close, "high": r.high,
                                 "low": r.low, "volume": r.volume} for r in rows]
            if not bars_out:
                return self._send_json({"ok": False, "error": "K线数据为空"})
            # 识别本周期的经典K线整理形态（三角形/楔形/矩形/旗形）+ 多根K线组合形态
            #（吞没/启明星/红三兵等）+ 次级趋势段（TL-UP/TL-DN）+ 关键线（供给/需求线、区间上下沿）
            # 坐标均为全量 bars 下标
            try:
                from types import SimpleNamespace
                if period == "year":
                    pt_bars = [SimpleNamespace(date=b["date"], open=b["open"], high=b["high"],
                                               low=b["low"], close=b["close"], volume=b["volume"])
                               for b in bars_out]
                else:
                    pt_bars = rows  # get_bars 返回的 Bar 对象（含 .close/.high/.low/.date）
                patterns = _chart_patterns(pt_bars)
                candle_patterns = _candle_patterns(pt_bars)
                trends = _minor_trends(pt_bars)
                keys = _key_lines(pt_bars, period)
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                patterns = []
                candle_patterns = []
                trends = []
                keys = {"trend_lines": [], "range": {"valid": False}}
            return self._send_json({
                "ok": True, "symbol": symbol, "name": name or "", "period": period,
                "period_cn": PERIOD_CN[period], "adjust": adjust, "note": note,
                "count": len(bars_out),
                "dates": [b["date"] for b in bars_out],
                "open": [b["open"] for b in bars_out],
                "high": [b["high"] for b in bars_out],
                "low": [b["low"] for b in bars_out],
                "close": [b["close"] for b in bars_out],
                "volume": [b["volume"] for b in bars_out],
                "patterns": patterns,
                "candle_patterns": candle_patterns,
                "trends": trends,
                "trend_lines": keys["trend_lines"],
                "range": keys["range"],
            })
        except (DataSourceError, ValueError) as exc:
            return self._send_json({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._send_json({"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}"})


def serve(port: int = 8765, host: str = "127.0.0.1", open_browser: bool = True) -> int:
    if not os.path.exists(WEB_FILE):
        print(f"警告：未找到页面文件 {WEB_FILE}，仅 API 可用", file=sys.stderr)
    httpd = ThreadingHTTPServer((host, port), Handler)
    # 云平台（Render 等）监听 0.0.0.0，真正对外地址由平台注入
    external = os.environ.get("RENDER_EXTERNAL_URL", "").strip() or f"http://{host}:{port}/"
    url = external
    if host == "0.0.0.0":
        url = external if external.startswith("http") else f"http://0.0.0.0:{port}/"
    print(BANNER.format(port=port))
    print(f"  访问地址：{url}" + ("（本机敲 127.0.0.1 也可）" if host == "0.0.0.0" else ""))
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    # 云平台会把端口写进 PORT、并要求监听 0.0.0.0；本地直接跑时仍走 127.0.0.1:8765
    env_port = os.environ.get("PORT", "").strip()
    cloud = env_port.isdigit()
    parser = argparse.ArgumentParser(description="聪明钱复盘台 · A股 网页版")
    parser.add_argument("--port", type=int, default=int(env_port) if cloud else 8765)
    parser.add_argument("--host", default=os.environ.get("HOST", "").strip()
                        or ("0.0.0.0" if cloud else "127.0.0.1"))
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器（仅本机）")
    parser.add_argument("--http-only", action="store_true", help="强制只使用 http 接口")
    args = parser.parse_args()
    if args.http_only:
        os.environ["WYCKOFF_FORCE_HTTP"] = "1"
    return serve(port=args.port, host=args.host, open_browser=args.open and not cloud)


if __name__ == "__main__":
    raise SystemExit(main())
