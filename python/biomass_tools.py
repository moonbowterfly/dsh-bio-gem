# biomass_tools.py — biomass 精修工具链（Q2 任务一）
# inspect（只读）: biomass 组分表 + 类别分布（氨基酸/核酸/脂质/辅因子/金属/其他）+ 原子总量
#                  + 可选参考对照（内置 iML1515 biomass；iNX1344_v4 按代谢物名同义尽力翻译，翻不了明示 unmapped）
# apply（显式）: biomass_profile 覆盖表（op=set|add|remove）→ 副本替换 biomass → 强制 G1-G6 重验
#               + 三联对照（生长/表型/必需基因 delta）→ model_lineage 追加（有 card 时）
# 原则: 默认不应用任何 profile；生长变差 WARN 不阻塞；C58 CarveMe AB 0.624 锚点保护（delta 如实报告）。
# units: growth 一律 mmol/gDW/h。
import os
import re
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cobra

EX_PREFIX = ("EX_", "DM_", "SK_")
DEFAULT_UNIVERSAL = r"D:\Program\hermes\temp\gem_universal\iML1515.xml"
DEFAULT_INX = r"F:\A_NGJ plan\Zcode\models\iNX1344_v4.xml"
GROWTH_UNITS = "mmol/gDW/h"
EPS = 1e-6

# 分类词表（name 规约匹配为主，id 规约为辅；覆盖 BiGG/ModelSEED/MetaCyc 常见命名）
_AA_NAMES = {
    "l-alanine", "glycine", "l-arginine", "l-asparagine", "l-aspartate", "l-cysteine",
    "l-glutamine", "l-glutamate", "l-histidine", "l-isoleucine", "l-leucine", "l-lysine",
    "l-methionine", "l-phenylalanine", "l-proline", "l-serine", "l-threonine",
    "l-tryptophan", "l-tyrosine", "l-valine",
}
_NUC_NAMES = {"atp", "adp", "amp", "gtp", "gdp", "gmp", "ctp", "cdp", "cmp",
              "utp", "udp", "ump", "datp", "dctp", "dgtp", "dttp", "damp"}
_COFACTOR_NAMES = {"coenzyme a", "s-adenosyl-l-methionine", "10-formyltetrahydrofolate",
                   "5,10-methylenetetrahydrofolate", "5,6,7,8-tetrahydrofolate",
                   "flavin adenine dinucleotide oxidized",
                   "nicotinamide adenine dinucleotide",
                   "nicotinamide adenine dinucleotide phosphate",
                   "pyridoxal 5'-phosphate", "riboflavin", "thiamine diphosphate",
                   "menaquinol 8", "menaquinone 8", "ubiquinol-8", "ubiquinone-8",
                   "coenzyme b", "coenzyme m", "heme b", "siroheme", "biotin",
                   "tetrahydrobiopterin", "5-methyltetrahydrofolate",
                   "flavin mononucleotide"}
_METAL_NAMES = {"ca2+", "cl-", "co2+", "cu2+", "cu1+", "fe2+", "fe3+", "k+", "mg2+",
                "mn2+", "zn2+", "ni2+", "mo6+", "mobd", "se2+", "cobalt", "copper",
                "calcium", "chloride", "iron", "iron (fe3+)", "potassium", "magnesium",
                "manganese", "zinc", "nickel", "sulfate"}
_LIPID_HINTS = ("undecaprenyl", "muramoyl", "lipid", "dag", "cdp-dag", "phosphatidyl",
                "cardiolipin", "phosphatidylglycerol", "phosphatidylethanolamine",
                "acyl-carrier", "holo-", "menaquinol", "2-oxo-3-methyl")
_OTHER_NUC_NAME_RE = re.compile(r"^(d?)(a|c|g|u|t)tp\b", re.I)


def _norm(s):
    return (s or "").strip().lower()


def classify_met(met):
    nm = _norm(met.name)
    # 名字里可能带公式尾巴（CarveMe: "ADP C10H12N5O10P2"）——取公式前段
    nm0 = re.split(r"\s+[A-Z][a-z]?\d", nm)[0].strip()
    if nm0 in _AA_NAMES:
        return "amino_acid"
    base = re.sub(r"[-_ ]?c[0ep]0?$", "", nm0)
    if base in _NUC_NAMES or _OTHER_NUC_NAME_RE.match(nm0):
        return "nucleotide"
    if nm0 in _COFACTOR_NAMES or base in _COFACTOR_NAMES:
        return "cofactor"
    if nm0 in _METAL_NAMES or base in _METAL_NAMES:
        return "metal_ion"
    if any(h in nm0 for h in _LIPID_HINTS):
        return "lipid_cellwall"
    # id 兜底（BiGG 惯例）
    bid = re.sub(r"[-_ ]?[cpen]0?$", "", (met.id or ""))
    if bid.endswith("__L") or bid.endswith("__D"):
        return "amino_acid"
    if bid in _NUC_NAMES:
        return "nucleotide"
    return "other"


def find_biomass(m):
    """FBA 目标反应（objective_coefficient != 0）；多个时取组分最多的。"""
    objs = [r for r in m.reactions if abs(r.objective_coefficient or 0) > 0]
    if not objs:
        return None, []
    objs.sort(key=lambda r: -len(r.metabolites))
    return objs[0], objs[1:]


def _element_totals(rxn):
    from validate import parse_formula
    totals = {}
    for met, coeff in rxn.metabolites.items():
        if not met.formula:
            continue
        for el, n in parse_formula(met.formula).items():
            totals[el] = totals.get(el, 0.0) + abs(coeff) * n
    return {k: round(v, 4) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])}


def _category_dist(comps):
    d = {}
    for c in comps:
        d[c["category"]] = d.get(c["category"], 0) + 1
    return dict(sorted(d.items(), key=lambda kv: -kv[1]))


def inspect_biomass(model_path, reference=None, universal_path=None, inx_path=None):
    """只读：biomass 组分摘要 + 可选参考对照。不改模型。"""
    from silentio import silent_read_sbml
    m = silent_read_sbml(model_path)
    rxn, others = find_biomass(m)
    if rxn is None:
        return {"error": f"no objective reaction found in {model_path}"}
    comps = []
    for met, coeff in rxn.metabolites.items():
        comps.append({"met_id": met.id, "name": met.name or met.id,
                      "coeff": round(coeff, 6), "compartment": met.compartment,
                      "category": classify_met(met)})
    comps.sort(key=lambda c: (c["category"], c["met_id"]))
    result = {
        "model": model_path,
        "biomass_reaction": rxn.id, "biomass_name": rxn.name or "",
        "bounds": list(rxn.bounds), "objective_coefficient": rxn.objective_coefficient,
        "n_components": len(comps), "units": GROWTH_UNITS,
        "components": comps,
        "category_distribution": _category_dist(comps),
        "element_totals": _element_totals(rxn),
        "other_objective_reactions": [r.id for r in others],
    }
    # ---- 参考对照（只读，尽力翻译；翻译不了明示 unmapped）----
    refs = {}
    upath = universal_path or DEFAULT_UNIVERSAL
    if reference in ("iML1515", "both") and os.path.exists(upath):
        um = silent_read_sbml(upath)
        urxn, _ = find_biomass(um)
        if urxn is not None:
            urefs = [{"met_id": x.id, "name": x.name or x.id, "coeff": round(c, 6),
                      "category": classify_met(x)} for x, c in urxn.metabolites.items()]
            refs["iML1515"] = {"biomass_reaction": urxn.id, "n_components": len(urefs),
                               "category_distribution": _category_dist(urefs)}
    ipath = inx_path or DEFAULT_INX
    if reference in ("iNX1344_v4", "both") and os.path.exists(ipath):
        im = silent_read_sbml(ipath)
        irxn, _ = find_biomass(im)
        if irxn is not None:
            icomps = [{"met_id": x.id, "name": (x.name or ""), "category": classify_met(x)}
                      for x, _c in irxn.metabolites.items()]
            # 名字同义翻译（本模型名 ↔ iNX1344 名；未命名=unmapped）
            def nkey(s):
                s = re.split(r"\s+[A-Z][a-z]?\d", _norm(s))[0].strip()
                return re.sub(r"[-_ ]?c[0ep]0?$", "", s)
            theirs = {}
            for c in icomps:
                if c["name"]:
                    theirs.setdefault(nkey(c["name"]), c)
            mapped, unmapped_inx = 0, 0
            for c in comps:
                if nkey(c["name"]) in theirs:
                    mapped += 1
                else:
                    unmapped_inx += 1
            refs["iNX1344_v4"] = {
                "biomass_reaction": irxn.id, "n_components": len(icomps),
                "category_distribution": _category_dist(icomps),
                "translation": {"matched_by_name": mapped, "unmapped": unmapped_inx,
                                "note": "MetaCyc↔BiGG 按代谢物名同义尽力翻译；未命名/无同名的计入 unmapped，不强行全翻"},
            }
    if refs:
        result["references"] = refs
    return {"ok": True, "result": result}


def _essential_sample(m, resolved_med, genes, sample_size=40):
    """确定性抽样敲除（免 FVA；同子集双侧对比，delta 语义成立）。返回 essential 集合。"""
    all_ids = sorted(g.id for g in m.genes)
    if not all_ids:
        return set()
    stride = max(1, len(all_ids) // max(1, sample_size))
    subset = set(all_ids[::stride][:sample_size])
    from l3_fix import _set_medium
    out = set()
    for gid in sorted(subset):
        try:
            with m:
                m.genes.get_by_id(gid).knock_out()
                _set_medium(m, resolved_med)
                v = m.optimize().objective_value
        except KeyError:
            continue
        if v is not None and v < EPS:
            out.add(gid)
    return out


def apply_biomass(model_path, biomass_profile, medium=None, phenotype_table=None,
                  out=None, essential_sample=40, note=None):
    """显式覆盖表应用：[{"met_id","coeff","op":"set|add|remove"}] → 副本替换 biomass →
    强制 G1-G6 重验 + 三联对照（growth/表型/必需基因 delta）→ lineage 追加（有 card 时）。"""
    from silentio import silent_read_sbml, silent_write_sbml
    from validate import validate_model
    from gapfind import expand_medium, resolve_medium
    from model_card import append_operation, GROWTH_UNITS
    if not biomass_profile:
        return {"ok": False, "error": "provide biomass_profile（显式覆盖表 [{met_id, coeff, op: set|add|remove}]）；"
                                      "inspect 才是只读，默认不应用任何 profile"}
    t0 = time.time()
    m = silent_read_sbml(model_path)
    rxn, _ = find_biomass(m)
    if rxn is None:
        return {"ok": False, "error": "no objective/biomass reaction found"}
    med, preset = expand_medium(medium or {})
    resolved_med, unresolved = resolve_medium(m, med)
    table_ok = phenotype_table and os.path.exists(phenotype_table)

    # ---- before（原模型、原 biomass；改动前先采对照）----
    ess_before = _essential_sample(m, resolved_med, m.genes, essential_sample)
    before_v = validate_model(model_path, medium=resolved_med,
                              phenotype_table=phenotype_table if table_ok else None,
                              carbon_mode="sole" if table_ok else "supplement")

    # ---- 应用覆盖表（内存副本）----
    applied, skipped = [], []
    for p in biomass_profile:
        mid, op = p.get("met_id"), (p.get("op") or "set").lower()
        if mid not in m.metabolites:
            skipped.append({**p, "why": "metabolite not in model"})
            continue
        met = m.metabolites.get_by_id(mid)
        in_rxn = met in rxn.metabolites
        old = rxn.metabolites.get(met)
        try:
            new_coeff = float(p.get("coeff"))
        except (TypeError, ValueError):
            skipped.append({**p, "why": "coeff missing/not numeric"})
            continue
        if op == "set":
            if not in_rxn:
                skipped.append({**p, "why": "met not in biomass（先 add）"})
                continue
            final = -abs(new_coeff) if old < 0 else abs(new_coeff)  # 沿用原符号约定
            # cobra add_metabolites 是增量语义——绝对设定必须按差值到达目标
            rxn.add_metabolites({met: final - old})
        elif op == "add":
            if in_rxn:
                skipped.append({**p, "why": "already in biomass（用 set）"})
                continue
            final = -abs(new_coeff)
            rxn.add_metabolites({met: final})
        elif op == "remove":
            if not in_rxn:
                skipped.append({**p, "why": "not in biomass"})
                continue
            final = 0.0
            rxn.add_metabolites({met: -old})
        else:
            skipped.append({**p, "why": f"unknown op: {op}"})
            continue
        actual = rxn.metabolites.get(met)
        applied.append({"op": op, "met_id": mid, "old": old, "new": actual})
        if abs((actual or 0.0) - final) > 1e-9:
            skipped.append({**p, "why": f"post-set verify failed: got {actual}, want {final}"})
    if not applied:
        return {"ok": False, "error": "no profile entry applied", "skipped": skipped}

    # ---- 落盘 + after 重验 ----
    if not out:
        out = model_path[:-4] + "_bm.xml" if model_path.endswith(".xml") else model_path + "_bm.xml"
    silent_write_sbml(m, out)
    after_v = validate_model(out, medium=resolved_med,
                             phenotype_table=phenotype_table if table_ok else None,
                             carbon_mode="sole" if table_ok else "supplement")
    ess_after = _essential_sample(m, resolved_med, m.genes, essential_sample)
    lost = sorted(ess_before - ess_after)
    gained = sorted(ess_after - ess_before)

    growth_b = (before_v.get("g3") or {}).get("growth_medium")
    growth_a = (after_v.get("g3") or {}).get("growth_medium")
    warn = []
    if growth_b is not None and growth_a is not None and growth_a < growth_b - 1e-6:
        warn.append(f"growth decreased {growth_b:.6f} -> {growth_a:.6f}（如实报告，不阻塞）")
    ph_b = before_v.get("g4") or {}
    ph_a = after_v.get("g4") or {}

    result = {
        "biomass_reaction": rxn.id, "applied": applied, "skipped": skipped,
        "out": out, "units": GROWTH_UNITS,
        "before_after": {
            "growth": {"before": growth_b, "after": growth_a, "units": GROWTH_UNITS},
            "phenotype": {"before": {"matched": ph_b.get("matched"), "total": ph_b.get("total")},
                          "after": {"matched": ph_a.get("matched"), "total": ph_a.get("total")}},
            "essential": {"sampled": min(essential_sample, len(m.genes)), "before": sorted(ess_before),
                          "after": sorted(ess_after), "lost": lost, "gained": gained},
            "validate_before": {k: (before_v.get(k) or {}).get("status") for k in ("g1", "g2", "g3", "g6")},
            "validate_after": {k: (after_v.get(k) or {}).get("status") for k in ("g1", "g2", "g3", "g4", "g6")},
        },
        "medium_preset": preset, "medium_unresolved": unresolved,
        "warnings": warn, "elapsed_s": round(time.time() - t0, 1),
    }
    # lineage（原模型旁有 card 时：先传播卡到产物旁再追加，保证新模型自带完整 lineage）
    from model_card import append_operation, load_card, card_path_for, propagate_card
    propagate_card(model_path, out)
    card = append_operation(out, "biomass_apply", reactions_added=0, reactions_removed=0,
                            detail={"profile_ops": applied, "growth": [growth_b, growth_a],
                                    "essential_delta": {"lost": lost, "gained": gained},
                                    "note": note})
    result["card"] = card_path_for(out) if card else None
    result["card_version"] = (card or {}).get("model_lineage", {}).get("version")
    return {"ok": True, "result": result}


def card_path_for(model_path):
    from model_card import card_path_for as _c
    return _c(model_path)


if __name__ == "__main__":
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    if args.get("action") == "apply":
        print(json.dumps(apply_biomass(args.get("model"), args.get("biomass_profile"),
                                       medium=args.get("medium"), phenotype_table=args.get("phenotype_table"),
                                       out=args.get("out"), essential_sample=args.get("essential_sample", 40),
                                       note=args.get("note")), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(inspect_biomass(args.get("model"), reference=args.get("reference"),
                                         universal_path=args.get("universal_path"),
                                         inx_path=args.get("inx_path")), ensure_ascii=False, indent=2))
