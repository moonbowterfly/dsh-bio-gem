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
# op: gapfind — 缺口分级诊断（L1/L2/L3）
# ---------------------------------------------------------------------------
def op_gapfind(args):
    from gapfind import find_gaps
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    return {"ok": True, "result": find_gaps(model, medium=args.get("medium"),
                                            substrates=args.get("substrates"))}


# ---------------------------------------------------------------------------
# op: gapfill — 规则级补洞（L1/L2，provenance 打标）
# ---------------------------------------------------------------------------
def op_gapfill(args):
    from gapfill import apply_fixes
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    if not (args.get("medium") or args.get("substrates")):
        return {"ok": False, "error": "need medium and/or substrates to drive gapfill"}
    return {"ok": True, "result": apply_fixes(
        model, medium=args.get("medium"), substrates=args.get("substrates"),
        max_add=args.get("max_add", 20), out=args.get("out"),
        confirm_budget=args.get("confirm_budget", False))}


# ---------------------------------------------------------------------------
# op: gapseq — 原子步骤（setup/launch/status/fetch），agent 编排长任务
# ---------------------------------------------------------------------------
def op_gapseq(args):
    from gapseq_wsl import probe, launch_gapseq, status_gapseq, fetch_gapseq
    action = args.get("action", "setup")
    if action == "setup":
        return {"ok": True, "result": probe()}
    if action == "launch":
        if not args.get("input"):
            return {"ok": False, "error": "launch 需要 input（核苷酸 .fna 绝对路径）"}
        r = launch_gapseq(args["input"], name=args.get("name", "model"),
                          work_win=args.get("out_dir"))
        return {"ok": True, "result": r}
    if action == "status":
        return {"ok": True, "result": status_gapseq()}
    if action == "fetch":
        r = fetch_gapseq(args.get("out_dir"), name=args.get("name", "model"))
        return {"ok": True, "result": r}
    return {"ok": False, "error": f"unknown gapseq action: {action}（setup|launch|status|fetch）"}


# ---------------------------------------------------------------------------
# op: phenotype_fix — 表型回填迭代（A3）
# ---------------------------------------------------------------------------
def op_phenotype_fix(args):
    from phenotype_fix import phenotype_fix
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    r = phenotype_fix(model, phenotype_table=args.get("phenotype_table"),
                      medium=args.get("medium"), max_add=args.get("max_add", 20),
                      out=args.get("out"))
    return {"ok": True, "result": r}


# ---------------------------------------------------------------------------
# op: essential_scan — G5 全量必需基因扫描（路线 P0）
# ---------------------------------------------------------------------------
def op_essential_scan(args):
    from essential_scan import essential_scan
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    r = essential_scan(model, medium=args.get("medium"), gene_subset=args.get("gene_subset"))
    return {"ok": True, "result": r}


# ---------------------------------------------------------------------------
# op: annotate — 基因组注释（官方优先 + pyrodigal 兜底）
# ---------------------------------------------------------------------------
def op_annotate(args):
    from annotate import nucleotide_to_protein
    fna = args.get("fna")
    if not fna or not os.path.exists(fna):
        return {"ok": False, "error": f"fna file not found: {fna}"}
    faa, src, stats = nucleotide_to_protein(fna, args.get("out"))
    return {"ok": True, "result": {"faa": faa, "source": src, "stats": stats}}


# ---------------------------------------------------------------------------
# op: media_resolve — 跨引擎介质解析 RPC（genie 消费侧统一走此；防介质语义漂移）
# ---------------------------------------------------------------------------
def op_media_resolve(args):
    from gapfind import expand_medium, resolve_medium
    from silentio import silent_read_sbml
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    m = silent_read_sbml(model)
    med, preset = expand_medium(args.get("medium"))
    resolved, unresolved = resolve_medium(m, med)
    return {"ok": True, "result": {
        "resolved_exchanges": sorted(resolved),
        "medium_preset": preset,
        "unresolved": unresolved,
        "model": model,
    }}


# ---------------------------------------------------------------------------
# op: l3_fix — B' 后半：L3 内部路径补洞（L3a 模型内连通性 + L3b 白名单/BiGG 反应式）
# 证据分级 EVIDENCE_sequence/math；防过补第五闸门（budget.py）；补后 G1-G6 重验 + G6 失败回滚
# ---------------------------------------------------------------------------
def op_l3_fix(args):
    from l3_fix import l3_fix
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    if not (args.get("medium") or args.get("substrates")):
        return {"ok": False, "error": "need medium and substrates to drive L3 diagnosis"}
    return {"ok": True, "result": l3_fix(
        model, medium=args.get("medium"), substrates=args.get("substrates"),
        out=args.get("out"), allow_math=args.get("allow_math", False),
        confirm_budget=args.get("confirm_budget", False),
        whitelist=args.get("whitelist"), faa=args.get("faa"),
        species=args.get("species"), max_iter=args.get("max_iter", 1),
        universal_path=args.get("universal_path"))}


# ---------------------------------------------------------------------------
# op: fluxscan — 阶段A-M1 通量区间制（FVA 区间 + pFBA 点值 + 条件对区间分离判定）
# 语义 bsp 锁稿：overlap = 求解器伪影禁止引用；判定公式见 fluxscan.judge_interval（单测锁定）
# ---------------------------------------------------------------------------
def op_fluxscan(args):
    from fluxscan import fluxscan, DEFAULT_FRACTION, DEFAULT_TOL
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    if not args.get("conditions"):
        return {"ok": False, "error": "conditions required（非空数组，每项 {name, medium, substrates?, carbon_mode?}，name 唯一）"}
    return {"ok": True, "result": fluxscan(
        model, args.get("conditions"), reactions=args.get("reactions"),
        pairs=args.get("pairs"),
        fraction_of_optimum=args.get("fraction_of_optimum", DEFAULT_FRACTION),
        tolerance=args.get("tolerance", DEFAULT_TOL),
        only_diff=args.get("only_diff", False), export_csv=args.get("export_csv"))}


# ---------------------------------------------------------------------------
# op: sensitivity — 阶段A-M2 结构性灵敏度（GAM×biomass 网格 22 组合 + 必需性重扫 + 单组分漂移）
# action=probe 秒级只读（GAM 载体定位/组分计数）；缺省 full=22 组合全量（约 35-45min，长任务）
# ---------------------------------------------------------------------------
def op_sensitivity(args):
    from sensitivity import sensitivity, find_biomass_gam
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    if args.get("action") == "probe":
        from silentio import silent_read_sbml
        m = silent_read_sbml(model)
        info = find_biomass_gam(m)
        info["genes"] = len(m.genes)
        info["reactions"] = len(m.reactions)
        return {"ok": True, "result": info}
    baseline_check = None
    if args.get("baseline_check_path"):
        with open(args["baseline_check_path"], encoding="utf-8") as f:
            baseline_check = json.load(f)
    return {"ok": True, "result": sensitivity(
        model, medium=args.get("medium"),
        biomass_scales=args.get("biomass_scales"), gam_grid=args.get("gam_grid"),
        run_component_sensitivity=args.get("run_component_sensitivity", True),
        run_drift=args.get("run_drift", True), top_n=args.get("top_n", 10),
        export_csv=args.get("export_csv"), baseline_check=baseline_check)}


# ---------------------------------------------------------------------------
# 分发器
# ---------------------------------------------------------------------------
OPS = {
    "model_info": op_model_info,
    "validate": op_validate,
    "gapfind": op_gapfind,
    "gapfill": op_gapfill,
    "gapseq": op_gapseq,
    "phenotype_fix": op_phenotype_fix,
    "essential_scan": op_essential_scan,
    "annotate": op_annotate,
    "media_resolve": op_media_resolve,
    "l3_fix": op_l3_fix,
}


# ---------------------------------------------------------------------------
# op: biomass_inspect / biomass_apply — Q2 任务一：biomass 精修（可选 profile，不默认替换）
# ---------------------------------------------------------------------------
def op_biomass_inspect(args):
    from biomass_tools import inspect_biomass
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    r = inspect_biomass(model, reference=args.get("reference"),
                        universal_path=args.get("universal_path"), inx_path=args.get("inx_path"))
    if r.get("ok"):
        return {"ok": True, "result": r["result"]}
    return {"ok": False, "error": r.get("error") or "inspect failed"}


def op_biomass_apply(args):
    from biomass_tools import apply_biomass
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    if not args.get("biomass_profile"):
        return {"ok": False, "error": "provide biomass_profile（显式覆盖表 [{met_id, coeff, op: set|add|remove}]）；"
                                      "默认不应用任何 profile，只读诊断请用 action=inspect"}
    r = apply_biomass(model, args.get("biomass_profile"), medium=args.get("medium"),
                      phenotype_table=args.get("phenotype_table"), out=args.get("out"),
                      essential_sample=args.get("essential_sample", 40), note=args.get("note"))
    if r.get("ok"):
        return {"ok": True, "result": r["result"]}
    return {"ok": False, "error": r.get("error") or "apply failed",
            "skipped": r.get("skipped")}


# OPS 引用上面的 op 函数——biomass 两 op 定义在 main 前补充注册（避免前向引用 NameError）
OPS["biomass_inspect"] = op_biomass_inspect
OPS["biomass_apply"] = op_biomass_apply
OPS["fluxscan"] = op_fluxscan
OPS["sensitivity"] = op_sensitivity


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