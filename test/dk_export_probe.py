#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""dk 导出探针 — 直调 double_knockout._export_csv 验证 2026-10-05 修复。

由 smoke.js 经 runPy 调用（payload 文件 = argv[1]，内容 {"csv_path": ...}）：
断言由 smoke.js 侧做，本脚本只负责执行与回传关键事实（stdout JSON）：
- CSV 第一行必须是标准表头 `gene_a,...`（不再是 `"# assumption",...` 双字段行）
- 缺失父目录自动创建（mkdrd -p）
- 旁车 <csv>.meta.json 存在且含 assumption_note
"""
import json
import os
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"))

from double_knockout import _export_csv  # noqa: E402

payload = {}
if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
    with open(sys.argv[1], encoding="utf-8") as fh:
        payload = json.load(fh)

csv_path = payload["csv_path"]
results = [{
    "pair": ["geneA", "geneB"],
    "single_a_growth": 0.5, "single_b_growth": 0.5, "double_growth": 0.0,
    "rationale": "smoke-test fixture", "source": "smoke",
}]
out = {
    "assumption_note": "SMOKE_NOTE_测试假设声明",
    "model": "fixture.xml", "medium": {"medium_name": "AB"}, "medium_preset": "AB",
    "wt_growth": 0.5, "units": "1/h", "eps": 1e-6, "max_pairs": 1, "pairs_found": 1,
}
n = _export_csv(csv_path, results, out)

first_line = ""
if os.path.exists(csv_path):
    with open(csv_path, encoding="utf-8-sig") as fh:
        first_line = fh.readline().strip()

meta_note = None
if os.path.exists(csv_path + ".meta.json"):
    with open(csv_path + ".meta.json", encoding="utf-8") as fh:
        meta_note = (json.load(fh) or {}).get("assumption_note")

print(json.dumps({
    "n": n,
    "first_line": first_line,
    "meta_note": meta_note,
    "meta_path_field": out.get("export_csv_meta"),
    "csv_exists": os.path.exists(csv_path),
}, ensure_ascii=False))
