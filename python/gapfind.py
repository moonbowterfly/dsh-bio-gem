# gapfind.py — dsh-bio-gem 缺口分级诊断（L1 缺交换 / L2 缺转运 / L3 内部路径）
# 规格: docs/ARCHITECTURE.md §6；已知规律（P1 实测）：多数"不能利用某碳源"缺口是 L1/L2 而非 L3。
# 用法: find_gaps(model, medium=None, substrates=None) -> {"L1": [...], "L2": [...], "L3": [...]}
import os
import re
import cobra

EX_PREFIX = ("EX_", "DM_", "SK_")

# 培养基常见成分别名（自然名 -> 模型代谢物名，去 -e0 后缀的小写形式）
SYN = {
    "orthophosphate": "phosphate", "oxygen": "o2", "l-ornithine": "ornithine",
    "d-galactose": "galactose", "d-xylose": "xylose", "malate": "l-malate",
    "mannose": "d-mannose", "ribose": "d-ribose", "mn": "mn2+",
    "ca": "ca2+", "cl": "cl-", "k": "k+", "mg": "mg",
    "sodium cation": "na+", "fe3+": "fe3", "fe2+": "fe2+",
    "copper": "cu2+", "zinc cation": "zn2+", "biotin": "biot",
    "thiamine": "thiamin", "raffinose": "trhl", "alpha,alpha-trehalose": "trhl",
    "trehalose": "trhl", "4-aminobutanoate": "gaba", "akg": "2-oxoglutarate",
    "fumarate": "fumarate", "beta-d-glucose": "d-glucose", "urea": "urea",
    "galactonate": "d-galactonate", "lactose": "beta-lactose",
    "mantiol": "d-mannitol", "maltotriose": "amylotriose",
    "sn-glycerol 3-phosphate": "glycerol-3-phosphate",
    "malic acid": "l-malate", "gluconate": "d-gluconate",
    "glucose 1-phosphate": "glucose-1-phosphate", "glumate": "l-glutamate",
}


def norm(s):
    return "".join(ch for ch in (s or "").strip().lower() if ch.isalnum() or ch in "+-")


def build_met_index(m, compartment="c0"):
    """胞内代谢物名索引（去 -c0 后缀小写）-> met id。"""
    idx = {}
    for x in m.metabolites:
        if x.compartment == compartment:
            nm = (x.name or "").strip().lower()
            if nm.endswith("-c0"):
                nm = nm[:-3]
            if nm:
                idx.setdefault(norm(nm), x.id)
    return idx


def build_ex_index(m):
    """EX 交换名索引（去 -e0 后缀小写）-> EX 反应 id。"""
    idx = {}
    for r in m.reactions:
        if r.id.startswith("EX_"):
            for x in r.metabolites:
                nm = (x.name or "").strip().lower()
                if nm.endswith("-e0"):
                    nm = nm[:-3]
                if nm:
                    idx.setdefault(norm(nm), r.id)
    return idx


def match_ex(sub, ex_idx):
    """底物名 -> EX id；别名表 + 子串回退。"""
    key = norm(sub)
    if key in ex_idx:
        return ex_idx[key]
    s = SYN.get((sub or "").strip().lower())
    if s and norm(s) in ex_idx:
        return ex_idx[norm(s)]
    for n, rid in ex_idx.items():
        if key and (key in n or n in key):
            return rid
    return None


def _met_has_outlets(m, met_id):
    """代谢物 e0 是否有非 EX 反应消耗（转运面）。"""
    met = m.metabolites.get_by_id(met_id)
    outlets = [r.id for r in met.reactions if not r.id.startswith(EX_PREFIX) and not r.boundary]
    return outlets


def _growth_with(m, medium, extra_ex, lb=-10.0):
    """基底 medium + 额外交换 extra_ex G3 式判定；返回生长值。"""
    with m:
        for r in m.reactions:
            if r.id.startswith(EX_PREFIX) or r.boundary:
                r.lower_bound = 0.0
        for rid, v in (medium or {}).items():
            if rid in m.reactions:
                m.reactions.get_by_id(rid).lower_bound = v
        if extra_ex and extra_ex in m.reactions:
            m.reactions.get_by_id(extra_ex).lower_bound = lb
        return m.optimize().objective_value


def find_gaps(model_path, medium=None, substrates=None):
    m = cobra.io.read_sbml_model(model_path)
    ex_idx = build_ex_index(m)
    met_idx = build_met_index(m)
    L1, L2, L3 = [], [], []

    # ---- L1: 缺交换 ----
    # (a) 培养基声明但模型没有的交换反应 ID
    for rid, lb in (medium or {}).items():
        if rid not in m.reactions:
            L1.append({
                "type": "exchange_missing",
                "exchange": rid, "medium_lb": lb,
                "fixable": "yes" if _mids_exist(m, rid) else "no",
            })
    # (b) 底物名匹配不到 EX（模型无对应交换）
    for sub in (substrates or []):
        exid = match_ex(sub, ex_idx)
        if not exid:
            L1.append({
                "type": "exchange_missing_name", "substrate": sub,
                "fixable": "yes" if _c0_exists(m, sub, met_idx) else "no",
            })
    # (c) 培养基已声明的交换：检查已被 L1 覆盖的同名底物
    # ---- L2: 缺转运（e0 代谢物无非 EX 出口）----
    cand_e0 = set()
    for rid, lb in (medium or {}).items():
        if rid.startswith("EX_") and rid in m.reactions:
            for x in m.reactions.get_by_id(rid).metabolites:
                if x.compartment == "e0":
                    cand_e0.add(x.id)
    for sub in (substrates or []):
        exid = match_ex(sub, ex_idx)
        if exid and exid in m.reactions:
            for x in m.reactions.get_by_id(exid).metabolites:
                if x.compartment == "e0":
                    cand_e0.add(x.id)
    for mid in sorted(cand_e0):
        outlets = _met_has_outlets(m, mid)
        if not outlets:
            c0 = mid[:-3] + "c0" if mid.endswith("_e0") else None
            L2.append({
                "type": "transport_missing", "metabolite_e0": mid,
                "metabolite_c0": c0 if c0 and c0 in m.metabolites else None,
                "fixable": "yes" if (c0 and c0 in m.metabolites) else "no",
            })

    # ---- L3: 内部路径（有交换+转运但 FBA 不长）----
    for sub in (substrates or []):
        exid = match_ex(sub, ex_idx)
        if not exid or exid not in m.reactions:
            continue  # L1 已报
        g = _growth_with(m, medium, exid)
        if g < 1e-6:
            L3.append({"type": "internal_path", "substrate": sub,
                       "exchange": exid, "growth": round(g, 6),
                       "note": "需要文献反应或人工审核（M1 不自动补）"})

    return {"L1": L1, "L2": L2, "L3": L3}


def _mids_exist(m, ex_id):
    """EX_<mid>_e0 的形如解析，检查 mid 胞外/胞内是否存在。"""
    mm = re.match(r"EX_(\S+?)_e0$", ex_id)
    if not mm:
        return False
    base = mm.group(1)
    c0 = base + "_c0"
    e0 = base + "_e0"
    return (c0 in m.metabolites) or (e0 in m.metabolites)


def _c0_exists(m, sub, met_idx):
    """底物名 -> 胞内代谢物是否存在（决定可否规则补交换+转运）。"""
    key = norm(sub)
    if key in met_idx:
        return True
    s = SYN.get((sub or "").strip().lower())
    return bool(s and norm(s) in met_idx)


if __name__ == "__main__":
    import json, sys
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    print(json.dumps(find_gaps(args.get("model"), args.get("medium"), args.get("substrates")),
                     ensure_ascii=False, indent=2))