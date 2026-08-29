# essential_scan.py — G5 全量必需性扫描（路线 P0：FVA 预筛 + 手工敲除）
# 流程: 介质设置 -> FVA(全范围) 预筛可通量基因（死基因免敲）-> 手工单基因敲除
#       -> 必需基因列表 + 模型卡章节数据（证据分级配色字段）
# Windows 纪律: FVA processes=1（无 fork）；手工敲除循环；GLPK 快速线性
# 阶段A-M2: 介质 setup 与扫描核心拆为 setup_model_medium/scan_essentiality 供 sensitivity 复用
#           （行为不变，C58 AB 必需 155 锚点必须原样复现）
import os
import sys
import time
import cobra
from cobra.flux_analysis import flux_variability_analysis

from silentio import silent_read_sbml
from gapfind import expand_medium, resolve_medium

EPS = 1e-6
FVA_EPS = 1e-9


def setup_model_medium(m, medium=None):
    """介质 setup（对齐 validate G3）：expand_medium -> 全交换清零 -> resolve_medium 设 bounds。
    阶段A-M2 抽出：essential_scan 与 sensitivity 共用同一介质口径。返回 (resolved, unresolved, preset)。"""
    med, preset = expand_medium(medium) if medium else ({}, None)
    resolved, unresolved = resolve_medium(m, med) if med else ({}, [])
    for r in m.reactions:
        if r.id.startswith(("EX_", "DM_", "SK_")) or r.boundary:
            r.lower_bound = 0.0
    for exid, lb in resolved.items():
        if exid in m.reactions:
            m.reactions.get_by_id(exid).lower_bound = lb
    return resolved, unresolved, preset


def scan_essentiality(m, gene_subset=None, progress=None):
    """必需性扫描核心（阶段A-M2 抽出复用）。输入：介质 bounds 已设好的模型。
    FVA(fraction=0) 预筛 -> 可通量反应关联基因 -> 手工敲除（<EPS 判必需）。
    返回 {wt_growth, total_genes, fva_tested_reactions, active_reactions, tested_genes,
    essential_count, essential_genes, organs, fva_seconds, knock_seconds}。"""
    with m:
        wt = m.optimize().objective_value
    print(f"[scan] wt = {wt:.6f}; genes = {len(m.genes)}", file=sys.stderr)

    # 1) FVA 预筛（fraction_of_optimum=0 全范围）：无通量的反应关联基因免敲
    t0 = time.time()
    fva = flux_variability_analysis(m, fraction_of_optimum=0.0, processes=1)
    fva_s = round(time.time() - t0, 1)
    active_rxns = set(fva.index[((fva["maximum"] > FVA_EPS) | (fva["minimum"] < -FVA_EPS))])
    print(f"[scan] FVA {fva_s}s；可通量反应 {len(active_rxns)}/{len(fva)}", file=sys.stderr)

    cand_genes = set()
    for g in m.genes:
        if gene_subset and g.id not in gene_subset:
            continue
        if any(r.id in active_rxns for r in g.reactions):
            cand_genes.add(g.id)
    print(f"[scan] 候选基因（关联可通量反应）{len(cand_genes)}", file=sys.stderr)
    print(f"[scan]         预计免敲 {max(0, len(m.genes) - len(cand_genes))} 个（{max(0, len(m.genes)-len(cand_genes))/max(1,len(m.genes))*100:.0f}%）", file=sys.stderr)

    # 2) 手工敲除循环（HANDOFF-03：不用 single_gene_deletion）
    t0 = time.time()
    essential = []
    for gid in sorted(cand_genes):
        with m:
            m.genes.get_by_id(gid).knock_out()
            v = m.optimize().objective_value
        if v < EPS:
            essential.append(gid)
        if progress and len(essential) % 50 == 0:
            progress({"tested": len(cand_genes), "essential_so_far": len(essential)})
    knock_s = round(time.time() - t0, 1)

    return {
        "wt_growth": round(wt, 6),
        "total_genes": len(m.genes),
        "fva_tested_reactions": len(fva),
        "active_reactions": len(active_rxns),
        "tested_genes": len(cand_genes),
        "essential_count": len(essential),
        "essential_genes": essential,
        "organs": {"essential_count": len(essential), "viable_ratio": round(1 - len(essential)/max(1, len(cand_genes)), 4)},
        "fva_seconds": fva_s,
        "knock_seconds": knock_s,
    }


def essential_scan(model_path, medium=None, gene_subset=None, progress=None, ledger_path=None):
    """全量必需基因扫描。返回 {wt, total_genes, tested, essential, essential_genes,
    predicted_viable, timeout_aborted, fva_seconds, knock_seconds}。"""
    m = silent_read_sbml(model_path)
    resolved, unresolved, preset = setup_model_medium(m, medium)
    print(f"[scan] medium_unresolved = {unresolved}", file=sys.stderr)

    result = scan_essentiality(m, gene_subset=gene_subset, progress=progress)
    result.update({
        "medium_preset": preset,
        "medium_unresolved": unresolved,
        "note": "必需判定=A 培养基下敲除生长<1e-6；evidence 分级按基因支撑反应是否含 EVIDENCE_math（Q2）",
    })
    # 模型卡 schema v2 回写（产物旁已有 card 才写；无卡不凭空造卡）
    try:
        from model_card import load_card, set_essential_genes
        if load_card(model_path) is not None:
            set_essential_genes(model_path, result, model=m)
    except Exception:
        pass
    # 阶段A-M3: prediction ledger 自动登记（每必需基因一条；幂等去重；写入失败仅 WARN 不阻塞）
    try:
        import ledger as _ledger
        from model_card import load_card as _load_card
        lineage_v = ((_load_card(model_path) or {}).get("model_lineage") or {}).get("version")
        cond = preset or (f"custom({len(resolved)} EX)" if resolved else "unspecified")
        result["ledger_registration"] = _ledger.register_essentiality(
            model_path, result, model=m, condition=cond, lineage_version=lineage_v,
            path=ledger_path)
    except Exception as e:
        sys.stderr.write(f"[scan] ledger registration WARN: {type(e).__name__}: {e}\n")
    return result


if __name__ == "__main__":
    import json
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    print(json.dumps(essential_scan(args.get("model"), args.get("medium"),
                                    args.get("gene_subset")), ensure_ascii=False, indent=2))
