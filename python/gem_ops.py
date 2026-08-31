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
    # 阶段A-M3: prediction ledger 基率摘要（文件不存在 -> {total: 0}；ledger_path 可覆盖默认账本）
    # P1-3：传 model=path 让摘要给 by_model 分布 + own_model_entries（防「本模型账本 N 条」误报）
    ledger_summary, ledger_context = None, None
    try:
        import ledger as _ledger
        ledger_summary = _ledger.ledger_summary(path=args.get("ledger_path"), model=path)
        n_unverified = ledger_summary["by_status"].get("unverified", 0)
        own = ledger_summary.get("own_model_entries")
        if ledger_summary["total"]:
            if own is not None:
                ledger_context = (f"预测账本共 {ledger_summary['total']} 条，其中本模型 {own} 条"
                                  f"（own_model_entries；其余属其他模型，by_model 见上），"
                                  f"{n_unverified} 条 unverified："
                                  "全部为模型推导预测（essentiality/phenotype 等），实验或文献兑现前不应当作事实引用；"
                                  "状态分布即预测可信度基率，回填后 by_status 向 literature_supported/"
                                  "experimentally_verified 迁移。")
            else:
                ledger_context = (f"预测账本共 {ledger_summary['total']} 条（其中 {n_unverified} 条 unverified）："
                                  "全部为模型推导预测（essentiality/phenotype 等），实验或文献兑现前不应当作事实引用；"
                                  "状态分布即预测可信度基率，回填后 by_status 向 literature_supported/"
                                  "experimentally_verified 迁移。")
    except Exception as e:
        ledger_summary = {"error": str(e)[:120]}
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
        "ledger_summary": ledger_summary,
        **({"ledger_context": ledger_context} if ledger_context else {}),
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
                      out=args.get("out"), ledger_path=args.get("ledger_path"))
    return {"ok": True, "result": r}


# ---------------------------------------------------------------------------
# op: essential_scan — G5 全量必需基因扫描（路线 P0）
# ---------------------------------------------------------------------------
def op_essential_scan(args):
    from essential_scan import essential_scan
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    r = essential_scan(model, medium=args.get("medium"), gene_subset=args.get("gene_subset"),
                       ledger_path=args.get("ledger_path"), gene_table=args.get("gene_table"))
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
    result = {"faa": faa, "source": src, "stats": stats}
    # P1-4：GFF 路径产出 gene_table.tsv（坐标ID→locus_tag/product），供 gem_essentiality 解读必需基因功能
    if stats.get("gene_table"):
        result["gene_table"] = stats["gene_table"]
    return {"ok": True, "result": result}


# ---------------------------------------------------------------------------
# op: media_resolve — 跨引擎介质解析 RPC（genie 消费侧统一走此；防介质语义漂移）
# ---------------------------------------------------------------------------
def op_media_resolve(args):
    from gapfind import expand_medium, resolve_medium, build_ex_index, ex_index_is_boundary, ex_display_name
    from silentio import silent_read_sbml
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    m = silent_read_sbml(model)
    med, preset = expand_medium(args.get("medium"))
    resolved, unresolved = resolve_medium(m, med)
    idx = build_ex_index(m)
    boundary_style = ex_index_is_boundary(idx)
    result = {
        "resolved_exchanges": sorted(resolved),
        "medium_preset": preset,
        "unresolved": unresolved,
        "model": model,
        # 阶段B-B1 附加（只增）：两级策略②启用标注 + 规范展示名
        "boundary_style": boundary_style,
    }
    if boundary_style:
        result["resolved_display"] = [ex_display_name(m, rid) for rid in sorted(resolved)]
    return {"ok": True, "result": result}


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


# ---------------------------------------------------------------------------
# op: ledger — 阶段A-M3 prediction ledger（list/query/update；只读/追加/更新，不删行）
# 默认账本 ~/.dsh/dsh-bio-gem/ledger/predictions.jsonl；ledger_path 可覆盖（测试用临时路径）
# ---------------------------------------------------------------------------
def op_ledger(args):
    import ledger as _ledger
    action = args.get("action", "list")
    lp = args.get("ledger_path")
    if action == "list":
        return {"ok": True, "result": _ledger.query_ledger(
            limit=args.get("limit"), offset=args.get("offset", 0), path=lp)}
    if action == "query":
        return {"ok": True, "result": _ledger.query_ledger(
            rtype=args.get("type"), status=args.get("status"), condition=args.get("condition"),
            model=args.get("model"), limit=args.get("limit"), offset=args.get("offset", 0),
            deprecated=args.get("deprecated"), path=lp)}
    if action == "update":
        r = _ledger.update_row(args.get("prediction_id"), status=args.get("status"),
                               source_refs=args.get("source_refs"),
                               comparison_refs=args.get("comparison_refs"), path=lp)
        if r.get("ok"):
            return {"ok": True, "result": r}
        return {"ok": False, "error": r.get("error") or "update failed"}
    return {"ok": False, "error": f"unknown ledger action: {action}（list|query|update）"}


# ---------------------------------------------------------------------------
# op: benchmark — 阶段B-B1 通用基准对比（六关并列/生长/含边界介质回退/biomass 探针/必需性对比
#     含退化护栏/表型/可复现性/账本 comparison_refs 回填；md 落盘可选）
# ---------------------------------------------------------------------------
def op_benchmark(args):
    from benchmark import benchmark
    model_a, model_b = args.get("model_a"), args.get("model_b")
    for k, v in (("model_a", model_a), ("model_b", model_b)):
        if not v:
            return {"ok": False, "error": f"{k} required"}
        if not v.startswith("bigg:") and not os.path.exists(v):
            # bigg:<id> URI 由 benchmark 层下载解析（B3）；本地路径仍要求存在
            return {"ok": False, "error": f"{k} file not found: {v}"}
    return {"ok": True, "result": benchmark(
        model_a, model_b, medium=args.get("medium"),
        phenotype_table=args.get("phenotype_table"),
        reference_essential=args.get("reference_essential"),
        essential_full=args.get("essential_full", False),
        ledger_refs=args.get("ledger_refs", True),
        export_md=args.get("export_md"), ledger_path=args.get("ledger_path"))}


OPS["ledger"] = op_ledger
OPS["benchmark"] = op_benchmark


# ---------------------------------------------------------------------------
# op: secretion — 阶段C-C1 可分泌代谢物谱（production envelope 扫描；纯拓扑边界声明内置；
#     wt<=EPS 退化护栏不登记；type=secretion 账本登记幂等）
# ---------------------------------------------------------------------------
def op_secretion(args):
    from secretion import secretion
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    return {"ok": True, "result": secretion(
        model, medium=args.get("medium"), fractions=args.get("fractions"),
        export_csv=args.get("export_csv"), ledger_refs=args.get("ledger_refs", True),
        ledger_path=args.get("ledger_path"),
        # P0-1：默认 summary（防大输出被引擎省略截断）；全量用 mode=full 或 export_csv 落盘
        mode=args.get("mode", "summary"), summary_top=args.get("summary_top", 20))}

OPS["secretion"] = op_secretion


# ---------------------------------------------------------------------------
# op: double_knockout — 阶段C-C2 双敲 v1（合成致死；GPR 穷尽先验 + FVA 预筛全扫，max_pairs 预算；
#     假设声明内置；wt<=EPS 退化护栏不登记；type=synthetic_lethal 账本登记幂等）
# ---------------------------------------------------------------------------
def op_double_knockout(args):
    from double_knockout import double_knockout
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    return {"ok": True, "result": double_knockout(
        model, medium=args.get("medium"), max_pairs=args.get("max_pairs", 5000),
        export_csv=args.get("export_csv"), ledger_refs=args.get("ledger_refs", True),
        ledger_path=args.get("ledger_path"))}


OPS["double_knockout"] = op_double_knockout


# ---------------------------------------------------------------------------
# op: enrichment — 阶段C-C3 必需基因通路富集（超几何单侧 + BH FDR；通路源=SBML groups
#     [gapseq MetaCyc PWY]；无注释模型按契约 annotation_unavailable 兜底；不登记账本）
# ---------------------------------------------------------------------------
def op_enrichment(args):
    from enrichment import enrichment
    model = args.get("model")
    if not model or not os.path.exists(model):
        return {"ok": False, "error": f"model file not found: {model}"}
    return {"ok": True, "result": enrichment(
        model, gene_list=args.get("gene_list"), pathway_source=args.get("pathway_source"),
        ledger_path=args.get("ledger_path"), export_csv=args.get("export_csv"))}


OPS["enrichment"] = op_enrichment


# ---------------------------------------------------------------------------
# op: targets — 阶段C-C4 靶点清单规范导出（账本三类预测 -> 锁定 schema；供下游引物/编辑
#     工具直接输入；与账本计数闭合；引物/质粒设计本身不做）
# ---------------------------------------------------------------------------
def op_targets(args):
    from targets import targets
    return {"ok": True, "result": targets(
        model_path=args.get("model"), types=args.get("types"), condition=args.get("condition"),
        ledger_path=args.get("ledger_path"), export_format=args.get("export_format", "csv"),
        export_path=args.get("export_path"))}


OPS["targets"] = op_targets



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