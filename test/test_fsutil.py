#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""fsutil 单元测试（导出路径 mkdir -p 语义 + meta 侧车；纯 stdlib，任意 python 可跑）。

背景（2026-10-05，round1 GEM-2 + round2 实测）：export_csv 传相对路径指向不存在
子目录 → 原实现直接 FileNotFoundError；CSV 首行 `"# xxx", note` 双字段行会被
pandas 当表头/脏行。修复后：自动建父目录 + 纯数据 CSV + <path>.meta.json 侧车。

运行：python -I test/test_fsutil.py（已挂 gem npm test 链）
"""
import json
import os
import sys
import tempfile

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'python'))

from fsutil import ensure_parent_dir, write_meta_sidecar  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=''):
    global PASS, FAIL
    ok = bool(cond)
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(('PASS  ' if ok else 'FAIL  ') + name + (f'  | {detail}' if detail else ''))


with tempfile.TemporaryDirectory() as td:
    # 1) 缺失的多级子目录自动创建 + 返回绝对路径
    nested = os.path.join(td, 'a', 'b', 'c', 'out.csv')
    abspath = ensure_parent_dir(nested)
    check('缺失多级子目录自动创建', os.path.isdir(os.path.dirname(nested)), abspath)
    check('返回绝对路径', os.path.isabs(abspath), abspath)

    # 2) 幂等（目录已存在时为 no-op）
    abspath2 = ensure_parent_dir(nested)
    check('幂等（重复调用无副作用）', abspath2 == abspath, abspath2)

    # 3) 纯文件名（无目录成分）——只做绝对化，不建目录
    plain = ensure_parent_dir(os.path.join(td, 'plain.csv'))
    check('纯文件名路径正常处理', plain.endswith('plain.csv'), plain)

    # 4) meta 侧车：JSON 可解析、含 payload + generated_at
    write_meta_sidecar(nested, {'boundary_note': '测试声明', 'wt_growth': 0.5})
    meta_path = nested + '.meta.json'
    check('meta 侧车文件写入', os.path.exists(meta_path), meta_path)
    meta = json.load(open(meta_path, encoding='utf-8'))
    check('meta 内容完整（payload + generated_at）',
          meta.get('boundary_note') == '测试声明' and meta.get('wt_growth') == 0.5
          and isinstance(meta.get('generated_at'), str),
          json.dumps(meta, ensure_ascii=False)[:120])

print(f"\n{'-' * 56}\nfsutil: {PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
