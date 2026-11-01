# -*- coding: utf-8 -*-
"""数据层冒烟测试：python tests/smoke_datasource.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from wyckoff import datasource as ds  # noqa: E402

for raw in ["600519", "sh600519", "300750.SZ", "000001", "SH000001", "430047"]:
    try:
        print(f"normalize({raw!r}) = {ds.normalize_code(raw)}")
    except Exception as e:  # noqa: BLE001
        print(f"normalize({raw!r}) ERR {e}")

for symbol in ["sh600519", "sz300750"]:
    print("=" * 60)
    try:
        q = ds.get_quote(symbol)
        print(f"[quote] {q.name} {q.symbol} 价={q.price} 涨跌={q.change_pct:.2f}% 量={q.volume}手 "
              f"额={q.amount/1e8:.2f}亿 换手={q.turnover}% 量比={q.volume_ratio} PE={q.pe} "
              f"总市值={q.total_cap/1e8:.0f}亿 源={q.source} 时间={q.updated}")
    except Exception as e:  # noqa: BLE001
        print("[quote] ERR", e)

    try:
        bars = ds.get_bars(symbol, bars=320, period="day", adjust="qfq")
        print(f"[day] n={len(bars)} 首={bars[0].date} 末={bars[-1].date} 末收={bars[-1].close} 末量={bars[-1].volume}")
    except Exception as e:  # noqa: BLE001
        print("[day] ERR", e)

    try:
        wk = ds.get_bars(symbol, bars=120, period="week", adjust="qfq")
        print(f"[week] n={len(wk)} 首={wk[0].date} 末={wk[-1].date}")
    except Exception as e:  # noqa: BLE001
        print("[week] ERR", e)

    try:
        mk = ds.get_intraday(symbol, bars=60, period="m5")
        print(f"[m5] n={len(mk)} 首={mk[0].date} 末={mk[-1].date} 末收={mk[-1].close}")
    except Exception as e:  # noqa: BLE001
        print("[m5] ERR", e)

print("=" * 60)
print("[index]", ds.get_index_quotes())
print("[search]", ds.search("茅台"))
