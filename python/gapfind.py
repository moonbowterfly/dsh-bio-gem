# gapfind.py — dsh-bio-gem 缺口分级诊断（L1 缺交换 / L2 缺转运 / L3 内部路径）
# 规格: docs/ARCHITECTURE.md §6；已知规律（P1 实测）：多数"不能利用某碳源"缺口是 L1/L2 而非 L3。
# 用法: find_gaps(model, medium=None, substrates=None) -> {"L1": [...], "L2": [...], "L3": [...]}
import os
import re
import sys
import cobra

# Python -I isolated 模式下脚本目录不进 sys.path——显式插入以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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

# CarveMe/BiGG 风格别名（跨引擎：gapseq 缩写 -> BiGG 全名；用于 resolve_medium）
CARVE_ALIAS = {
    "ca2+": "calcium", "k+": "potassium", "mg2+": "magnesium", "na+": "sodium",
    "mn2+": "manganese", "zn2+": "zinc", "ni2+": "nickel", "cu2+": "copper",
    "cl-": "chloride", "nh3": "ammonium", "nh4+": "ammonium", "ammonium": "ammonium",
    "o2": "o2", "h2o": "h2o", "co2": "co2", "cobalt": "co2+", "co2+": "co2+",
    "fe2+": "fe2+", "fe3+": "fe3+", "h+": "h+", "h": "h+",
    "d-glucose": "d-glucose", "phosphate": "phosphate", "sulfate": "sulfate",
    "glucose": "d-glucose", "ammonia": "ammonium",
    # gapseq 命名（无 + 后缀）与 CarveMe 全名双候选
    "mg": ["mg", "magnesium"], "mg2+": ["magnesium", "mg"],
    "mn": ["mn2+", "manganese"], "zn": ["zn2+", "zinc"], "ca": ["ca2+", "calcium"],
    "k": ["k+", "potassium"], "na": ["na+", "sodium"], "cl": ["cl-", "chloride"],
    "fe": ["fe2+", "iron"],
}

# 常用介质预设（自然名成分 -> lb）。agent 只需传 {"medium_name": "AB"} 即可
# 获得完整成分（金属离子绝不能省——gapseq 生物质方程直接消耗，缺金属生长恒 0）。
MEDIA_PRESETS = {
    "AB": {
        "D-Glucose": -5, "NH3": -10, "O2": -12.5, "CO2": -15, "H+": -20, "H2O": -100,
        "Phosphate": -10, "Sulfate": -10, "Cl-": -10, "Mn2+": -10, "Zn2+": -10,
        "Co2+": -10, "Ni2+": -1, "Fe3+": -0.1, "Fe2+": -10, "Ca2+": -10, "Cu2+": -10,
        "K+": -10, "Mg2+": -10, "Na+": -10,
    },
    "M9": {},  # 动态：由 _m9_preset() 从 carveme media_db 提取（BiGG ID 形式）
}


def _m9_preset():
    """M9 预设：从 carveme media_db.tsv 提取（BiGG compound 名 -> EX_<c>_e，lb -10）。
    找不到 carveme 时回退常用 M9 成分（BiGG ID 静态表）。"""
    import csv
    home = os.path.expanduser("~")
    db = os.path.join(home, ".dsh", "dsh-bio-gem", "venv-carveme", "Lib",
                      "site-packages", "carveme", "data", "input", "media_db.tsv")
    comps = set()
    if os.path.exists(db):
        with open(db, encoding="utf-8") as f:
            rd = csv.DictReader(f, delimiter="\t")
            for row in rd:
                if row.get("medium") == "M9" and row.get("compound"):
                    comps.add(row["compound"].strip())
    if comps:
        return {"EX_" + c + "_e": -10.0 for c in sorted(comps)}
    return {"EX_glc__D_e": -10.0, "EX_nh4_e": -10.0, "EX_o2_e": -12.5,
            "EX_pi_e": -10.0, "EX_so4_e": -10.0, "EX_k_e": -10.0,
            "EX_mg2_e": -10.0, "EX_ca2_e": -10.0, "EX_fe2_e": -10.0,
            "EX_fe3_e": -0.1, "EX_mn2_e": -10.0, "EX_zn2_e": -10.0,
            "EX_cobalt2_e": -10.0, "EX_ni2_e": -10.0, "EX_cu2_e": -10.0,
            "EX_cl_e": -10.0, "EX_na1_e": -10.0, "EX_h2o_e": -100.0,
            "EX_h_e": -20.0, "EX_co2_e": -15.0}


def expand_medium(medium):
    """medium 展开：支持 {"medium_name": "AB", ...覆盖成分}；M9 动态提取。
    返回 (merged_dict, preset_name_or_None)。"""
    if not medium:
        return medium or {}, None
    med = dict(medium)
    name = med.pop("medium_name", None)
    merged = {}
    if name:
        if name in MEDIA_PRESETS:
            preset = MEDIA_PRESETS[name]
            if name == "M9" and not preset:
                preset = _m9_preset()
            merged.update(preset)
        else:
            pass  # 未知预设：保留用户成分，调用方可记 unresolved
    merged.update(med)
    return merged, name


# 代谢物 c0 短名别名（自然名/常用名 -> 模型胞内代谢物名；用于 L1 名字补洞）
# 实测：gapseq 用 BiGG 短名（D-Gluconate-c0 名存为 'GLCN-c0'），SYN 只管 EX 名映射
MET_ALIAS = {
    "gluconate": "glcn", "d-gluconate": "glcn", "6-phospho-d-gluconate": "6pgc",
    "sucrose": "sucrose", "glucose-1-phosphate": "glucose-1-phosphate",
    "d-glucose-1-phosphate": "glucose-1-phosphate", "g1p": "glucose-1-phosphate",
    "d-glucose": "d-glucose", "glucose": "d-glucose",
    "l-malate": "mal__l", "malate": "mal__l", "malic acid": "mal__l",
    "citrate": "cit", "succinate": "succ", "d-ribose": "rib__d", "ribose": "rib__d",
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
    """底物名 -> EX id；三层：精确(含 SYN 别名) -> CARVE_ALIAS -> 受控子串回退。
    子串回退防误伤（2026-08-29 实测）：'o2' 曾命中 'R Acetoin C4H8O2'（名字尾部含 o2）
    导致 O2 交换错配 -> 模型"AB 不生长"假象。规则：
      - 短 key（<=4 且不含 '+'）：只允许前缀匹配（n.startswith(key)）
      - 含 '+' 的 key（金属离子）：允许前缀或后缀（iron(fe3+)→ironfe3+ endswith fe3+）
      - 长 key（>=5）：允许子串
    """
    key = norm(sub)
    if key in ex_idx:
        return ex_idx[key]
    s = SYN.get((sub or "").strip().lower())
    if s and norm(s) in ex_idx:
        return ex_idx[norm(s)]
    a = CARVE_ALIAS.get(key)
    cands = a if isinstance(a, list) else [a]
    for a in cands:
        if not a:
            continue
        if a in ex_idx:
            return ex_idx[a]
        for n, rid in ex_idx.items():
            if n.startswith(a) or (("+" in a or len(a) >= 5) and a in n):
                return rid
    for n, rid in ex_idx.items():
        # 只做正向子串（key 是 n 的子串）；反向（n in key）误伤严重：
        # "no" in "arabinose"、"co" in "gluconate"、"phosphate" in "glucose-1-phosphate"
        if key in n:
            if n.startswith(key):
                return rid
            if "+" in key or len(key) >= 5:
                return rid
    return None


def resolve_medium(m, medium):
    """介质字典 -> {EX_id: lb}。键可为 EX ID（直接用）或自然名/别名（跨引擎匹配）。
    返回 (resolved, unresolved_names)。"""
    ex_idx = build_ex_index(m)
    resolved, unresolved = {}, []
    for k, lb in (medium or {}).items():
        if k.startswith("EX_") and k in m.reactions:
            resolved[k] = lb
            continue
        exid = match_ex(k, ex_idx)
        if exid:
            resolved[exid] = lb
        else:
            unresolved.append(k)
    return resolved, unresolved


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


def _growth_with_solo(m, medium, extra_ex, lb=-10.0):
    """严格语义：基底 medium 去掉含碳交换 + 额外交换（唯一碳源防 AB 背景掩盖）。"""
    from silentio import silent_read_sbml
    with m:
        for r in m.reactions:
            if r.id.startswith(EX_PREFIX) or r.boundary:
                r.lower_bound = 0.0
        for rid, v in (medium or {}).items():
            if rid in m.reactions:
                met = list(m.reactions.get_by_id(rid).metabolites)[0]
                if met.formula and "C" in (met.formula or ""):
                    continue  # 去碳
                m.reactions.get_by_id(rid).lower_bound = v
        if extra_ex and extra_ex in m.reactions:
            m.reactions.get_by_id(extra_ex).lower_bound = lb
        return m.optimize().objective_value


def find_gaps(model_path, medium=None, substrates=None):
    from silentio import silent_read_sbml
    m = silent_read_sbml(model_path)
    ex_idx = build_ex_index(m)
    met_idx = build_met_index(m)
    L1, L2, L3 = [], [], []

    # medium 预设展开（支持 {"medium_name": "AB"|"M9"}）+ 跨引擎介质归一化
    medium, preset_name = expand_medium(medium)
    resolved_med, unresolved_names = resolve_medium(m, medium)

    # ---- L1: 缺交换 ----
    # (a) 用户显式声明的 EX ID 模型没有
    for rid, lb in (medium or {}).items():
        if rid.startswith("EX_") and rid not in m.reactions:
            L1.append({
                "type": "exchange_missing",
                "exchange": rid, "medium_lb": lb,
                "fixable": "yes" if _mids_exist(m, rid) else "no",
            })
    # (a2) 自然名匹配不到任何交换
    for nm in unresolved_names:
        L1.append({
            "type": "exchange_unresolved_name", "substrate": nm,
            "fixable": "yes" if _c0_exists(m, nm, met_idx) else "no",
        })
    # (b) 底物名匹配不到 EX（模型无对应交换）
    for sub in (substrates or []):
        exid = match_ex(sub, ex_idx)
        if not exid:
            L1.append({
                "type": "exchange_missing_name", "substrate": sub,
                "fixable": "yes" if _c0_exists(m, sub, met_idx) else "no",
            })
    # ---- L2: 缺转运（e0 代谢物无非 EX 出口）----
    cand_e0 = set()
    for rid, lb in resolved_med.items():
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

    # ---- L3: 内部路径（有交换+转运但 FBA 不长；严格语义=唯一碳源防 AB 背景掩盖）----
    for sub in (substrates or []):
        exid = match_ex(sub, ex_idx)
        if not exid or exid not in m.reactions:
            continue  # L1 已报
        g = _growth_with_solo(m, resolved_med, exid)
        if g < 1e-6:
            L3.append({"type": "internal_path", "substrate": sub,
                       "exchange": exid, "growth": round(g, 6),
                       "note": "需要文献反应或人工审核（M1 不自动补）"})

    return {"L1": L1, "L2": L2, "L3": L3,
            "medium_unresolved": unresolved_names,
            "resolved_exchanges": sorted(resolved_med)}


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