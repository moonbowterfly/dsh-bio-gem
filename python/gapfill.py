# gapfill.py — dsh-bio-gem 规则级补洞（L1 交换 / L2 转运，M1 不做 L3/MILP）
# 防过补四闸门（ARCHITECTURE §6）：规则优先、max_add 封顶、逐条 provenance、修复后建议重验。
# 用法: apply_fixes(model, medium=None, substrates=None, max_add=20, out=None)
#       -> {"applied": [...], "skipped": [...], "out": path}
import os
import shutil
import sys
import cobra

# Python -I isolated 模式下脚本目录不进 sys.path——显式插入以导入同目录 gapfind
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gapfind import find_gaps, match_ex, build_ex_index, build_met_index, SYN, norm


def _new_e0_met(m, c0_id, tag):
    """由胞内代谢物复制胞外 e0 版本（name/formula/charge 继承，compartment=e0）。"""
    src = m.metabolites.get_by_id(c0_id)
    e0_id = c0_id[:-3] + "_e0" if c0_id.endswith("_c0") else c0_id + "_e0"
    if e0_id in m.metabolites:
        return m.metabolites.get_by_id(e0_id)
    new = cobra.Metabolite(e0_id, name=(src.name or ""),
                           compartment="e0", formula=src.formula, charge=src.charge)
    new.notes["source"] = "gem-gapfill"
    new.notes["reason"] = f"L1: extracellular form of {c0_id} for exchange ({tag})"
    m.add_metabolites([new])
    return new


def _add_ex_rxn(m, e0_met, tag):
    ex_id = "EX_" + e0_met.id
    if ex_id in m.reactions:
        return None
    r = cobra.Reaction(ex_id, name=(e0_met.name or "") + " exchange",
                       lower_bound=0.0, upper_bound=1000.0)
    r.add_metabolites({e0_met: -1})
    m.add_reactions([r])
    r.notes["source"] = "gem-gapfill"
    r.notes["reason"] = f"L1: add exchange for medium component ({tag})"
    return r


def _add_tx_rxn(m, e0_id, c0_id, tag):
    tx_id = "rxnGEM_tx_" + (e0_id.split("_")[0] if "_" in e0_id else e0_id)
    if tx_id in m.reactions:
        return None
    r = cobra.Reaction(tx_id, name=(m.metabolites.get_by_id(e0_id).name or "") + " transport (gem)",
                       lower_bound=0.0, upper_bound=1000.0)
    r.add_metabolites({m.metabolites.get_by_id(e0_id): -1,
                       m.metabolites.get_by_id(c0_id): 1})
    m.add_reactions([r])
    r.notes["source"] = "gem-gapfill"
    r.notes["reason"] = f"L2: connect extracellular {e0_id} to cytosol ({tag})"
    return r


def apply_fixes(model_path, medium=None, substrates=None, max_add=20, out=None):
    from silentio import silent_read_sbml, silent_write_sbml
    if not (medium or substrates):
        return {"error": "nothing to fix: provide medium and/or substrates"}
    gaps = find_gaps(model_path, medium=medium, substrates=substrates)
    m = silent_read_sbml(model_path)
    ex_idx = build_ex_index(m)
    met_idx = build_met_index(m)
    applied, skipped = [], []

    def _budget():
        return len(applied) < max_add

    # ---- L1: 补交换 ----
    for g in gaps["L1"]:
        if not _budget():
            skipped.append({**g, "why": "max_add cap"})
            continue
        if g["type"] == "exchange_missing":
            mm = g["exchange"][3:]  # 去掉 EX_
            base = mm[:-3] if mm.endswith("_e0") else mm
            c0 = base + "_c0"
            if c0 not in m.metabolites:
                skipped.append({**g, "why": "no c0 metabolite to copy (unfixable by rules)"})
                continue
            e0 = _new_e0_met(m, c0, g["exchange"])
            exr = _add_ex_rxn(m, e0, g["exchange"])
            txr = _add_tx_rxn(m, e0.id, c0, g["exchange"])
            if exr or txr:
                applied.append({"type": "L1_exchange", "exchange": g["exchange"],
                                "metabolite_e0": e0.id, "ex_rxn": exr.id if exr else None,
                                "tx_rxn": txr.id if txr else None})
        elif g["type"] == "exchange_missing_name":
            key = norm(g["substrate"])
            sid = met_idx.get(key)
            if not sid:
                s = SYN.get(g["substrate"].strip().lower())
                if s:
                    sid = met_idx.get(norm(s))
            if not sid:
                skipped.append({**g, "why": "no c0 metabolite by name (unfixable by rules)"})
                continue
            c0 = sid
            e0 = _new_e0_met(m, c0, g["substrate"])
            exr = _add_ex_rxn(m, e0, g["substrate"])
            txr = _add_tx_rxn(m, e0.id, c0, g["substrate"])
            if exr or txr:
                applied.append({"type": "L1_exchange", "substrate": g["substrate"],
                                "metabolite_e0": e0.id, "ex_rxn": exr.id if exr else None,
                                "tx_rxn": txr.id if txr else None})

    # ---- L2: 补转运 ----
    for g in gaps["L2"]:
        if not _budget():
            skipped.append({**g, "why": "max_add cap"})
            continue
        if g["fixable"] != "yes":
            skipped.append({**g, "why": "no c0 metabolite (unfixable by rules)"})
            continue
        e0_id = g["metabolite_e0"]
        c0_id = g["metabolite_c0"]
        txr = _add_tx_rxn(m, e0_id, c0_id, e0_id)
        if txr:
            applied.append({"type": "L2_transport", "metabolite_e0": e0_id,
                            "metabolite_c0": c0_id, "tx_rxn": txr.id})
        else:
            skipped.append({**g, "why": "tx reaction already exists"})

    # ---- L3: 仅报告（M1 不自动补）----
    # ---- 保存 ----
    if not applied:
        return {"applied": [], "skipped": skipped, "gaps": gaps,
                "out": None, "note": "no rule-level fix applied"}
    out_path = out or (model_path[:-4] + "_gf.xml" if model_path.endswith(".xml") else model_path + "_gf.xml")
    backup = None
    # 只有就地覆写（out==model）才备份原文件；输出到新文件时原模型未被改动，无需备份
    if os.path.normpath(out_path) == os.path.normpath(model_path):
        backup = model_path + ".bak"
        if not os.path.exists(backup):
            shutil.copy2(model_path, backup)
    silent_write_sbml(m, out_path)
    return {"applied": applied, "skipped": skipped, "gaps": {
        "L1": len(gaps["L1"]), "L2": len(gaps["L2"]), "L3": len(gaps["L3"])},
        "out": out_path, "backup": backup, "fixed_rxns": len(applied)}


if __name__ == "__main__":
    import json, sys
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    print(json.dumps(apply_fixes(args.get("model"), args.get("medium"),
                                 args.get("substrates"), args.get("max_add", 20),
                                 args.get("out")), ensure_ascii=False, indent=2))