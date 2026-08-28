"""dsh-bio-gem Python 操作层 — JSON 协议分发器（GEM 构建/验证/补洞/报告）。

协议与 dsh-bio-genie 的 bio_ops.py 同族：
  TS 侧通过 stdin 发送 {"op": "...", "args": {...}}，
  本脚本执行后将 {"ok": true, "result": ...} 或 {"ok": false, "error": "..."} 写到 stdout。

契约（bridge 层继承）：
  - 捕获所有代码异常后恒返回 ok:true，traceback 写 stderr（带 "Traceback (most recent call last)" 头）——
    代码级失败判定必须在 TS 侧检测该头 → needs_repair=true。
  - 输出前 _sanitize_json 递归规范化（-0.0→0.0, NaN/inf→null），规避 dsh snapshot 校验。

op 一览（M1 · 定稿 2026-08-29）：
  model_info    读 SBML 输出模型摘要（gem_report 的底层）
  validate      五道验证关卡 G1 加载 / G2 元素平衡 / G3 生长真实性 / G4 表型(条件) / G5 必需性抽检(条件)
  gapfind       缺口分级诊断 L1 缺 exchange / L2 缺转运 / L3 内部路径
  gapfill       规则级补洞（L1/L2 自动，逐条打标 provenance）
  build         CarveMe 构建（后台长任务）+ 注释输入支持
"""
import json
import os
import sys
import traceback

# Windows 下 sys.stdin/stdout 默认按 GBK（locale）编解码，而 Node 侧以 UTF-8 写入/读取。
# 不显式重配置会导致中文参数/结果损坏。强制 UTF-8。
sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

# Python -I isolated 模式下脚本目录不进 sys.path——显式插入以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_MODEL_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "models")


def _sanitize_json(obj):
    """递归规范化：-0.0 -> 0.0, NaN/inf -> None（dsh snapshotToolValue 只接受 lossless JSON）。"""
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        if obj == 0.0 and str(obj).startswith("-"):
            return 0.0
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_json(v) for v in obj]
    return obj


# ---------------------------------------------------------------------------
# op: model_info — SBML 摘要（gem_report 底层）
# ---------------------------------------------------------------------------
def op_model_info(args):
    """读 SBML：返回基因/反应/代谢物统计 + 复制子统计（多染色体/多质粒）。"""
    import cobra
    path = args.get("model")
    if not path or not os.path.exists(path):
        return {"ok": False, "error": f"model file not found: {path}"}
    m = cobra.io.read_sbml_model(path)
    n_ex = sum(1 for r in m.reactions if r.id.startswith("EX_"))
    n_dm = sum(1 for r in m.reactions if r.id.startswith("DM_"))
    n_boundary = sum(1 for r in m.reactions if r.boundary)
    # 复制子统计：模型基因 ID 前缀 NC_XXXX_N -> 复制子
    from collections import Counter
    repl = Counter()
    for g in m.genes:
        parts = g.id.split("_")
        if len(parts) >= 3 and parts[0] == "NC":
            repl["_".join(parts[:2])] += 1
        else:
            repl["other"] += 1
    return {"ok": True, "result": {
        "path": path,
        "genes": len(m.genes),
        "reactions": len(m.reactions),
        "metabolites": len(m.metabolites),
        "compartments": list(m.compartments.values()) or list(m.compartments.keys()),
        "exchanges": n_ex,
        "demands": n_dm,
        "boundary": n_boundary,
        "replicons": dict(repl),
        "objective": m.objective.name or m.objective.expression is not None and "set" or "None",
    }}


# ---------------------------------------------------------------------------
# op: validate — 五道验证关卡（G1-G5）
# ---------------------------------------------------------------------------
def op_validate(args):
    from validate import validate_model
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    rep = validate_model(
        model,
        medium=args.get("medium"),
        phenotype_table=args.get("phenotype_table"),
        essential_test=args.get("essential_test"),
        reference_growth=args.get("reference_growth"),
        reference_essential=args.get("reference_essential"),
        carbon_mode=args.get("carbon_mode", "supplement"),
    )
    return {"ok": True, "result": rep}


# ---------------------------------------------------------------------------
# 分发器
# ---------------------------------------------------------------------------
OPS = {
    "model_info": op_model_info,
    "validate": op_validate,
}


def main():
    line = sys.stdin.read()
    try:
        req = json.loads(line)
        op = req.get("op", "")
        args = req.get("args", {}) or {}
    except Exception:
        # 协议级失败：返回 ok:false + 说明
        print(json.dumps({"ok": False, "error": "protocol error: invalid JSON on stdin"}))
        return
    fn = OPS.get(op)
    if fn is None:
        print(json.dumps({"ok": False, "error": f"unknown op: {op}"}))
        return
    try:
        out = fn(args)
        if isinstance(out, dict) and "ok" in out:
            print(json.dumps(_sanitize_json(out), ensure_ascii=False))
        else:
            print(json.dumps(_sanitize_json({"ok": True, "result": out}), ensure_ascii=False))
    except Exception as e:
        # 捕获所有异常，恒返回 ok:true（traceback 写 stderr 头，TS 侧检测）
        sys.stderr.write("Traceback (most recent call last):\n")
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": True, "result": None,
                          "error_hint": f"op {op} failed: {type(e).__name__}: {e}"},
                         ensure_ascii=False))


if __name__ == "__main__":
    main()