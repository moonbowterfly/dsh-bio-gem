# precursor_scan.py — dsh-bio-gem 阻塞前体分析（「模型为什么不长」的结构级定位）
#
# 来源（2026-09-11 E2E 绕道归因）：agent 在不生长模型上反复手写「逐前体探测」逻辑
# （6+ 次调用），本模块把它固化为工具。
#
# ⚠️ 判据设计——**刻意不用「绝对可达性」**：
#   初版曾用「全开交换下逐前体 demand 能否净生产」，在教科书模型 e_coli_core 上
#   把 atp_c / accoa_c / nad_c / nadph_c 误报为「既不能合成也不能摄取」。原因是
#   辅因子有循环补给路径，稳态下**不净生产 ≠ 网络不能供给**。该判据已否决。
#
#   现用「**移除测试**」这一相对判据：
#     基线 biomass 通量 > 0  → 直接返回「无阻塞」（对健康模型天然零误报）
#     基线 biomass 通量 = 0  → 逐一移除某个前体的需求，看 biomass 是否恢复通量
#                              恢复了 → 该前体即阻塞点（结论由「恢复与否」直接定义，
#                              不依赖对网络的任何绝对判断）
#   两个验证锚：iNX1344_v3（不生长，应报出阻塞前体）+ e_coli_core（能生长，应报无阻塞）。
import os
import sys

import cobra  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EPS = 1e-6


def _find_biomass(model):
    """定位 biomass 反应：先按 id/name 命中，再退回 objective 变量。"""
    for r in model.reactions:
        if "biomass" in f"{r.id} {r.name or ''}".lower():
            return r
    try:
        syms = [s.name for s in model.objective.expression.free_symbols]
    except Exception:  # noqa: BLE001
        syms = []
    live = [r for r in model.reactions if r.id in syms]
    return live[0] if live else None


def scan_precursors(model_path, medium=None, max_precursors=200):
    """阻塞前体分析。

    medium: 可选，{EX_id: lower_bound} 或 {"medium_name": "AB"/"M9"}（走 gapfind 的解析器）。
    返回 {verdict, baseline_flux, blocking_precursors[], note}。
    """
    from silentio import silent_read_sbml
    from gapfind import expand_medium, resolve_medium

    m = silent_read_sbml(model_path)
    bio = _find_biomass(m)
    if bio is None:
        return {"error": "未定位到 biomass 反应（id/name 与 objective 均未命中）"}

    medium_applied = 0
    unresolved = []
    if medium:
        med, _preset = expand_medium(medium)
        resolved, unresolved = resolve_medium(m, med)
        with m:
            for rid, lb in resolved.items():
                if rid in m.reactions:
                    m.reactions.get_by_id(rid).lower_bound = float(lb)
                    medium_applied += 1
            baseline = m.slim_optimize()
    else:
        baseline = m.slim_optimize()
    baseline = float(baseline) if baseline is not None else 0.0

    out = {
        "model": model_path,
        "biomass_reaction": bio.id,
        "biomass_name": bio.name or "",
        "medium_applied_exchanges": medium_applied,
        "medium_unresolved": unresolved,
        "baseline_flux": round(baseline, 6),
        "units": "mmol/gDW/h",
        "point_value_note": "单点 FBA 值，非解空间硬结论",
        "blocking_precursors": [],
        "n_blocking": 0,
        "verdict": "",
        "note": "",
    }

    # 健康路径：能生长 → 不做逐前体测试（这是零误报的关键）
    if baseline > EPS:
        out["verdict"] = "growable"
        out["note"] = (
            "该条件下 biomass 可携带通量 → **无阻塞前体**。已刻意跳过逐前体测试，"
            "避免对可生长模型产生假阳性。扰动/区间分析请用 gem_fluxscan、gem_sensitivity。"
        )
        return out

    precursors = [k for k, v in bio.metabolites.items() if v < 0][:max_precursors]
    blocking = []
    for met in precursors:
        coeff = bio.metabolites[met]
        with m:
            bio.add_metabolites({met: -coeff})  # 系数清零 = 移除该前体需求
            val = m.slim_optimize()
        val = float(val) if val is not None else 0.0
        if val > EPS:
            blocking.append({
                "metabolite": met.id,
                "name": met.name or "",
                "formula": met.formula or "",
                "coefficient": round(coeff, 4),
                "flux_without_it": round(val, 6),
            })

    out["blocking_precursors"] = blocking
    out["n_blocking"] = len(blocking)
    out["n_precursors_tested"] = len(precursors)
    if blocking:
        out["verdict"] = "blocked"
        out["note"] = (
            "移除下列前体后 biomass 恢复携带通量 → 它们是**阻塞点**。"
            "排查顺序：① 该前体在模型里是否有合成路径（路径缺失=需补反应）；"
            "② 是否被边界/约束卡住；③ 计量或命名是否有问题"
            "（先用 gem_validate 的 g0 数据质量诊断与 g2 配平结论交叉读取）。"
        )
    else:
        out["verdict"] = "infeasible_or_constrained"
        out["note"] = (
            "逐前体移除均未恢复通量 → 阻塞不在单个前体上。可能是 biomass 方程整体计量/方向问题、"
            "约束冲突或能量项缺失；建议先看 gem_validate 的 g0（数据质量）与 g2（配平）。"
        )
    return out
