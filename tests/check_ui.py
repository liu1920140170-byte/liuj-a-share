# -*- coding: utf-8 -*-
"""静态检查 web/index.html：JS 语法（交给 node --check）+ DOM id 引用一致性。

    python tests/check_ui.py
"""
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "web", "index.html")
OUT = os.path.join(ROOT, "tests", "_ui_script.js")
NODE = os.environ.get("NODE_BIN", "node")

html = open(HTML, encoding="utf-8").read()

# 1) 抽出所有 script 内容（跳过 src= 外链，本项目不应有外链）
scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
print(f"script 块数量：{len(scripts)}，总行数 {sum(s.count(chr(10)) for s in scripts)}")
open(OUT, "w", encoding="utf-8").write("\n;\n".join(scripts))

# 2) DOM id 一致性
declared = set(re.findall(r'\bid=["\']([\w-]+)["\']', html))
used = set(re.findall(r'getElementById\(\s*["\']([\w-]+)["\']', html))
used |= set(re.findall(r'\$\$?\(\s*["\']([\w-]+)["\']\s*\)', html))
missing = sorted(used - declared)
unused = sorted(declared - used)
print(f"声明的 id：{len(declared)}，引用的 id：{len(used)}")
print("引用了但 HTML 中不存在的 id（会报 null 错误）：", missing or "无")
print("声明了但未用到的 id：", unused or "无")

# 3) 外链 / 依赖检查
external = re.findall(r'(?:src|href)=["\'](https?:)?//[^"\']+', html)
print("外部资源引用（应为空）：", external or "无")

# 4) 关键接口/字段是否被正确使用
for token in ["/api/analyze", "kline", "report_text", "decision", "wyckoff", "range", "checklist"]:
    print(f"  含 {token}: {token in html}")

# 5) node 语法检查
print("\n--- node --check ---")
try:
    proc = subprocess.run([NODE, "--check", OUT], capture_output=True, text=True, timeout=60)
    print("exit:", proc.returncode)
    print(proc.stdout.strip() or "")
    print(proc.stderr.strip() or "")
except FileNotFoundError:
    print("未找到 node，可跳过此步（用 NODE_BIN 环境变量指定 node 路径）")
finally:
    try:
        os.remove(OUT)  # 中间产物，检查完即删
    except OSError:
        pass
sys.exit(0)
