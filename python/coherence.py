# coherence.py — dsh-bio-gem 模型自洽性前置诊断（G0）
#
# 目的：在 G3/G4/G5 与 gapfind 之前，先判断**模型自身数据质量是否允许下结论**，
# 避免把「未映射代谢物」这类数据问题，误报成「通路缺口 / 必需基因异常」。
#
# 实测来源（2026-09-10 E2E，iNX1344_v3 —— MetaCyc 风格 id 的公开模型）：
#   - gem_gapfind 报 5 个 L3「内部通路缺口」，根因实为 biomass 前体未映射；
#   - agent 为证伪这些假阳性，手写 cobra 代码 18 次（占该轮调用的一半）。
#
# ⚠️⚠️ 判据设计原则：**零误报优先**。以下两类判据在设计中被实测否决，切勿加回：
#
#  1. 「biomass 元素配平」——对 biomass 方程**不适用**。标准 biomass 方程代表大分子
#     聚合，产物侧用占位代谢物表示生物量（无独立化学式），元素净不平衡是**预期**
#     行为而非缺陷。对照实验：教科书模型 e_coli_core 的 Biomass_Ecoli_core 净不平衡
#     C -42.56 / N -5.45 / P -3.68，用它判据会把公认良好的模型判成 FAIL。
#  2. 「前体可达性（demand 逐前体 FBA）」——初版实现同样在 e_coli_core 上把
#     atp_c / accoa_c / nad_c / nadph_c 误报为「既不能合成也不能摄取」。全开交换下
#     的 demand 语义与胞内辅因子/能量货币的循环补给路径纠缠，判据未成熟。
#     正确方法（agent 在 E2E 中手工探索过）待重新设计后引入。
#
# 保留的判据都经过「问题模型报出真问题 + 标准模型零误报」双向验证：
#   - id 体系识别（信息性，决定下游名称映射口径）
#   - biomass 未映射前体（无 name / 无 formula）→ iNX1344_v3 报 8 个，e_coli_core 报 0 个
#   - 产物侧出现 ATP（生长方向疑似写反）→ 两个模型均不报
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 代谢物 id 命名体系 → 正则（命中率 < ID_FRACTION 记为 mixed/unknown）
ID_SYSTEM_PATTERNS = (
    ("bigg", re.compile(r"^[a-z][a-z0-9]{1,}_[a-z]\d?$")),      # atp_c / h2o_c / g6p_c
    ("metacyc", re.compile(r"^[Mm]?\d{5}(?:_[a-z]\d?)?$")),     # M00002_c / cpd00002_c0
    ("carveme", re.compile(r"^M_[a-z0-9]+_[a-z]\d?$")),         # M_atp_c
)
ID_FRACTION = 0.5


# ---------------------------------------------------------------- ID 体系
def detect_id_system(model):
    """按代谢物 id 命名习惯识别 ID 体系（决定下游关卡的名称映射口径）。"""
    mets = [x for x in model.metabolites if x.id]
    if not mets:
        return {"system": "unknown", "fractions": {}, "sampled": 0}
    hits = {name: 0 for name, _ in ID_SYSTEM_PATTERNS}
    for met in mets:
        for name, pat in ID_SYSTEM_PATTERNS:
            if pat.match(met.id):
                hits[name] += 1
                break
    n = len(mets)
    fracs = {k: round(v / n, 4) for k, v in hits.items()}
    best = max(fracs, key=fracs.get)
    system = best if fracs[best] >= ID_FRACTION else ("mixed" if any(fracs.values()) else "unknown")
    return {"system": system, "fractions": fracs, "sampled": n}


# ---------------------------------------------------------------- biomass
def find_biomass(model):
    """定位 biomass 反应：先按 id/name 命中，再退回 objective 变量。"""
    for r in model.reactions:
        if "biomass" in f"{r.id} {r.name or ''}".lower():
            return r, "id_or_name"
    try:
        syms = [s.name for s in model.objective.expression.free_symbols]
    except Exception:  # noqa: BLE001
        syms = []
    live = [r for r in model.reactions if r.id in syms]
    if len(live) == 1:
        return live[0], "objective"
    if live:
        return live[0], "objective_multi"
    return None, "not_found"


def check_biomass(model):
    """biomass 可用性：未映射前体（主判据）+ 产物侧 ATP（方向异常）。

    不做元素配平判定——标准 biomass 方程本就不配平（见模块头注释）。
    """
    bio, source = find_biomass(model)
    if bio is None:
        return {"status": "WARN", "found_by": source,
                "notes": ["未定位到 biomass 反应（id/name 与 objective 均未命中）→ 无法评估生长目标"]}

    prods = [k for k, v in bio.metabolites.items() if v > 0]
    subs = [k for k, v in bio.metabolites.items() if v < 0]
    unmapped = [k.id for k in bio.metabolites if not (k.formula or "").strip()]
    unnamed = [k.id for k in bio.metabolites if not (k.name or "").strip()]
    # 方向异常：产物侧出现 ATP（生长应消耗 ATP、产出 ADP）。ADP/Pi 在产物侧属正常。
    atp_in_products = [k.id for k in prods if (k.name or "").strip().upper() == "ATP"]

    notes, status = [], "PASS"
    if unmapped:
        status = "WARN"
        notes.append(f"biomass 含 {len(unmapped)} 个未映射代谢物（无 formula）→ "
                     "其质量未定义，配平/缺口类结论均不可靠；先补映射再解读下游结果")
    if unnamed:
        notes.append(f"另有 {len(unnamed)} 个无名称代谢物 → 报告可读性受限")
    if atp_in_products:
        status = "FAIL"
        notes.append(f"产物侧出现 ATP（{atp_in_products[:3]}）→ 生长方向疑似写反")
    if not notes:
        notes.append("biomass 组成部分映射完整，未发现方向异常")

    return {
        "status": status, "found_by": source,
        "reaction": bio.id, "reaction_name": bio.name or "",
        "bounds": [bio.lower_bound, bio.upper_bound],
        "n_substrates": len(subs), "n_products": len(prods),
        "unmapped_metabolites": unmapped, "unnamed_metabolites": unnamed[:10],
        "atp_in_products": atp_in_products,
        "notes": notes,
    }


# ---------------------------------------------------------------- 汇总
def model_coherence(model):
    """模型数据质量诊断：ID 体系 + biomass 可用性。"""
    idrep = detect_id_system(model)
    biorep = check_biomass(model)

    rank = {"PASS": 0, "WARN": 1, "FAIL": 2}
    status = max((biorep.get("status", "PASS"),), key=lambda s: rank.get(s, 0))

    hints = []
    if biorep.get("unmapped_metabolites"):
        hints.append(f"biomass 含未映射代谢物 {biorep['unmapped_metabolites'][:3]} → "
                     "先补 name/formula，再信任何配平 / 缺口 / 必需性结论")
    if biorep.get("atp_in_products"):
        hints.append("biomass 产物侧出现 ATP → 生长方向疑似写反，先修方程")
    if idrep["system"] in ("metacyc", "mixed", "unknown"):
        hints.append(f"id 体系为 {idrep['system']}（非 BiGG）→ 培养基/代谢物名称需跨体系匹配，"
                     "天然名解析失败时先判为命名口径问题，而非模型缺陷")

    return {
        "status": status,
        "id_system": idrep,
        "biomass": biorep,
        "downstream_hint": "；".join(hints),
    }


def coherence_from_path(model_path):
    from silentio import silent_read_sbml
    return model_coherence(silent_read_sbml(model_path))
