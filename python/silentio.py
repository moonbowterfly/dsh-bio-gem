# silentio.py — 静默加载 SBML（协议保洁）
# cobra.io.read_sbml_model 在加载某些模型（如 CarveMe fbc2）时会把
# "Adding exchange reaction ..." / "Ignoring reaction ... already exists" 打印到 stdout，
# 污染 JSON 协议输出。所有需要读模型的入口统一走 silent_read_sbml。
import contextlib
import io
import sys
import cobra

# Windows 下 stdout/stderr 默认按 locale（GBK）编解码——统一强制 UTF-8，任何模块 import
# 本文件即生效（gem_ops.py 已有同款 reconfigure，双保险幂等）。
for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8")
    except Exception:
        pass


def silent_read_sbml(path):
    """读 SBML，同时截断 cobra/底层库对 stdout/stderr 的打印。"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cobra.io.read_sbml_model(path)


def silent_write_sbml(m, path):
    """写 SBML（镜像需求：写盘也可能有库打印）。"""
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        cobra.io.write_sbml_model(m, path)