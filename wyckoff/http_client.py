# -*- coding: utf-8 -*-
"""
HTTP 取数工具。

设计要点（这是本工具能在各种网络环境里跑起来的关键）：
1. 先按原样请求（通常是 https）。
2. 如果 https 因为 SSL/连接被中断等原因失败，且开关允许，则自动把协议降级成 http 重试。
   —— 部分公司网络 / 代理只放行 http，或中间设备会掐断 TLS，此时该降级能让工具继续工作。
3. 支持多编码（东财返回 utf-8，腾讯/新浪返回 gbk），自动嗅探，避免中文乱码。
4. 带重试与退避；所有失败统一抛 DataSourceError，方便上层切换数据源。
"""
from __future__ import annotations

import gzip
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

DEFAULT_TIMEOUT = 8.0

# 某些网络（公司代理/中间设备）会掐断 TLS。一旦某个主机的 https 失败，就在一段时间内
# 直接走 http，避免每次取数都白白等一次超时重试（实测可把单次分析从 20 秒降到 5 秒左右）。
_HTTPS_BLOCK: dict[str, float] = {}
HTTPS_BLOCK_TTL = 600.0


class DataSourceError(RuntimeError):
    """取数失败。"""


def force_http() -> bool:
    """WYCKOFF_FORCE_HTTP=1 时只走 http（受限网络可显式指定）。"""
    return os.environ.get("WYCKOFF_FORCE_HTTP", "").strip().lower() in ("1", "true", "yes", "on")


def _decode(raw: bytes, encoding: str | None) -> str:
    """按指定编码解码；不指定或解出乱码时自动嗅探。"""
    candidates = []
    if encoding:
        candidates.append(encoding)
    candidates += ["utf-8", "gbk", "gb18030"]
    best = ""
    for enc in candidates:
        try:
            text = raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        # 出现大量替换字符说明编码不对
        if text.count("\ufffd") > len(text) * 0.005:
            continue
        if len(text) > len(best):
            best = text
        if encoding and enc == encoding:
            break
    if not best:
        best = raw.decode(candidates[0], "replace")
    return best


def _maybe_decompress(raw: bytes, headers) -> bytes:
    enc = (headers.get("Content-Encoding") or "").lower()
    try:
        if "gzip" in enc:
            return gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        if "deflate" in enc:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    except Exception:  # noqa: BLE001 - 解压失败就用原始内容
        return raw
    return raw


def _candidate_urls(url: str, allow_http: bool) -> list[str]:
    if not (allow_http and url.startswith("https://")):
        return [url]
    plain = "http://" + url[len("https://"):]
    host = urllib.parse.urlsplit(url).netloc
    if force_http() or _HTTPS_BLOCK.get(host, 0.0) > time.time():
        return [plain, url]
    return [url, plain]


def fetch_bytes(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 1,
    headers: dict | None = None,
    allow_http: bool = True,
    referer: str | None = None,
) -> bytes:
    """请求 URL 并返回原始字节；https 失败自动降级 http。"""
    merged = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Connection": "close",
    }
    if referer:
        merged["Referer"] = referer
    if headers:
        merged.update(headers)

    errors: list[str] = []
    for candidate in _candidate_urls(url, allow_http):
        for attempt in range(max(1, retries)):
            try:
                req = urllib.request.Request(candidate, headers=merged)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read()
                    return _maybe_decompress(raw, resp.headers)
            except urllib.error.HTTPError as exc:  # 404/503 等
                errors.append(f"{candidate} -> HTTP {exc.code}")
                if exc.code in (400, 401, 403, 404):
                    break
            except Exception as exc:  # noqa: BLE001 - SSL/超时/连接重置都归为一类
                errors.append(f"{candidate} -> {type(exc).__name__}: {exc}")
                if candidate.startswith("https://") and not isinstance(exc, urllib.error.HTTPError):
                    _HTTPS_BLOCK[urllib.parse.urlsplit(candidate).netloc] = time.time() + HTTPS_BLOCK_TTL
            time.sleep(0.35 * (attempt + 1))
    raise DataSourceError("; ".join(errors[-3:]) or "unknown error")


def fetch_text(
    url: str,
    encoding: str | None = "utf-8",
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 1,
    headers: dict | None = None,
    allow_http: bool = True,
    referer: str | None = None,
) -> str:
    raw = fetch_bytes(url, timeout=timeout, retries=retries, headers=headers,
                      allow_http=allow_http, referer=referer)
    return _decode(raw, encoding)


def fetch_json(url: str, **kwargs) -> dict:
    """请求并解析 JSON（东财的 JSONP 包装会被自动剥离）。"""
    text = fetch_text(url, **kwargs).strip()
    if not text:
        raise DataSourceError("empty response")
    if not text.startswith("{") and not text.startswith("["):
        start = min([p for p in (text.find("{"), text.find("[")) if p >= 0] or [-1])
        if start < 0:
            raise DataSourceError(f"not json: {text[:80]!r}")
        end = max(text.rfind("}"), text.rfind("]"))
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"json decode failed: {exc}; head={text[:80]!r}") from exc
