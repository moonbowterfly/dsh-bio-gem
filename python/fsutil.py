"""fsutil.py — 文件系统小工具（导出路径处理，2026-10-05 增补）。

背景（round1 TC4 观察 GEM-2，round2 复核）：export_csv/export_path 传相对路径
且父目录不存在时，直接 FileNotFoundError ——对用户是零价值的报错（需手动 mkdir）。
统一改为自动创建父目录（mkdir -p 语义），返回绝对路径。
- 部分导出曾把声明文本写成 CSV 首行 `"# xxx", note` 双字段行——标准 CSV 解析器
  （pandas 默认）会把它当表头或脏行（实测 agent 需专门跳过 '#' 行）。
  统一改为：CSV 保持纯数据表，声明与参数写入旁车文件 <path>.meta.json。
"""
from __future__ import annotations

import json
import os
import time


def ensure_parent_dir(path: str) -> str:
    """确保导出路径的父目录存在，返回绝对路径。

    空 dirname（纯文件名 → 当前目录）时只返回绝对路径；
    目录已存在时为幂等 no-op。
    """
    abspath = os.path.abspath(path)
    parent = os.path.dirname(abspath)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return abspath


def write_meta_sidecar(path: str, payload: dict) -> str | None:
    """在导出文件旁写 <path>.meta.json（声明/参数/时间戳）。失败仅告警返回 None。

    调用方约定：CSV 本体只放纯数据；任何注释性说明（assumption/boundary 等）
    放进 payload，由本函数落盘。
    """
    meta_path = path + ".meta.json"
    try:
        data = dict(payload)
        data.setdefault("generated_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        return meta_path
    except Exception:
        return None
