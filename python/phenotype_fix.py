# phenotype_fix.py — 路线 A3：表型回填迭代
# 流程: G4 表型对照（supplement）-> 失配底物逐个 gapfind 分级
#       -> L1/L2 规则补洞（累积到新模型）-> L3 列候选清单 -> 重跑 G4 对比匹配率
# 输出: {before_rate, after_rate, fixed, unresolved_L3, model}
import os
import sys
import cobra

from silentio import silent_read_sbml, silent_write_sbml
from validate import validate_model
from gapfind import find_gaps, expand_medium, resolve_medium
from gapfill import apply_fixes


def _note(*a):
    """进度走 stderr（stdout 是 JSON 协议通道，绝不能污染）。"""
    print(*a, file=sys.stderr)


# 排除纯 N 源底物（sole 无碳时不长属正常，非缺口）
N_SOURCES = {"nh3", "nitrate", "nitrite", "ammonia", "ammonium", "nitrogen", "urea"}
def _is_n_source(name):
    return (name or "").strip().lower() in N_SOURCES


def phenotype_fix(model_path, phenotype_table=None, medium=None, max_add=20, out=None):
    if not phenotype_table or not os.path.exists(phenotype_table):
        return {"error": "phenotype_table 必需（TSV: substrate<TAB>published 0/1）"}
    med, preset = expand_medium(medium)
    med_resolved, unresolved = resolve_medium(silent_read_sbml(model_path), med) if med else ({}, [])

    def g4_on(path):
        rep = validate_model(path, medium=med, phenotype_table=phenotype_table,
                             carbon_mode="sole")   # 缺口检测用唯一碳源语义（防 AB 葡萄糖掩盖）
        g4 = rep.get("g4") or {}
        if g4.get("status") == "SKIP":
            return None, g4
        return (g4.get("matched", 0), g4.get("total", 0)), g4

    # before（sole 语义）
    (bm, bt), g4b = g4_on(model_path)
    before_rate = bm / bt if bt else None
    targets = [x for x in (g4b.get("results") or [])
               if x.get("published") == 1 and x.get("predicted") == 0
               and not _is_n_source(x.get("substrate"))]
    print(f"[before] 匹配 {bm}/{bt}（{before_rate:.1%}）；需修复底物 {len(targets)} 个", file=sys.stderr)

    # 逐个修复（L1/L2 规则；L3 列清单）
    cur = model_path
    fixed, l3_list = [], []
    if targets:
        for t in targets:
            gf = apply_fixes(cur, medium=med, substrates=[t["substrate"]],
                             max_add=max_add, out=(out or (model_path[:-4] + "_pf.xml")))
            new_fixed = gf.get("applied") or []
            if new_fixed and gf.get("out"):
                cur = gf["out"]  # 累积：后续修复基于最新模型
            fixed.extend(new_fixed)
            # L3 候选（gapfind 单独跑该底物）
            gaps = find_gaps(cur, medium=med, substrates=[t["substrate"]])
            for x in gaps.get("L3", []):
                l3_list.append({"substrate": t["substrate"], **x})
        print(f"修复 {len(fixed)} 项；L3 候选 {len(l3_list)} 个", file=sys.stderr)

    # after
    (am, at), g4a = g4_on(cur)
    after_rate = am / at if at else None
    print(f"[after] 匹配 {am}/{at}（{after_rate:.1%}）", file=sys.stderr)

    result = {
        "before": {"matched": bm, "total": bt, "rate": before_rate},
        "after": {"matched": am, "total": at, "rate": after_rate},
        "improved": (after_rate or 0) > (before_rate or 0),
        "fixed": fixed,
        "l3_remaining": l3_list,
        "after_results": g4a.get("results") or [],
        "model": cur,
        "medium_preset": preset,
        "medium_unresolved": unresolved,
    }
    # 模型卡 schema v2 回写（源模型旁有 card 才传播+追加；无卡不凭空造卡）
    card_version = None
    try:
        from model_card import append_operation, load_card, propagate_card, set_verified_phenotypes
        propagate_card(model_path, cur)
        if load_card(cur) is not None:
            set_verified_phenotypes(cur, result, semantics="sole")
            card = append_operation(cur, "phenotype_fix", reactions_added=len(fixed),
                                    detail={"before": result["before"], "after": result["after"],
                                            "l3_remaining": len(l3_list)})
            card_version = (card or {}).get("model_lineage", {}).get("version")
    except Exception:
        pass
    result["card_version"] = card_version
    return result


if __name__ == "__main__":
    import json
    import sys
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    print(json.dumps(phenotype_fix(args.get("model"), args.get("phenotype_table"),
                                   args.get("medium"), args.get("max_add", 20),
                                   args.get("out")), ensure_ascii=False, indent=2))