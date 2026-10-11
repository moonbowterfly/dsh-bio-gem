# energy_gate.py — G6 ATP 维持耗散闸门（方法 v2，2026-10-07 G 系列一期 GM-1）
#
# 正式定义（任务书 GM-1）：无外界供能时，ATP 水解耗散能否维持正通量。
#   主检查：ATP + H2O → ADP + Pi + H+（同一区室），全部边界 (0,0) 关闭后
#   以该耗散反应为 objective 最大化，读取实际通量。
#   附加诊断：单 ATP → ∅ demand =「净 ATP 生成」（仅诊断，不与主检查互换）。
#
# 与旧 G6（gem.g6_atp_leak.v1，2026-10-07 前默认）的区别：
#   v1 bug：创建 ATP demand 后仍优化原 biomass objective，读取的是 biomass 通量；
#           只关 lower_bound（分泌方向仍开放）；未校验 solver status。
#   v2 修复：objective 显式设为耗散反应、边界双向关闭 (0,0)、仅 optimal 判定、
#           有效容差关联 solver feasibility tolerance、内部强制下界按声明的检查尺度松开。
#
# 无副作用纪律：全部修改在 cobra context（`with m:`）内进行；出口自检
#   （反应数不变、耗散/诊断反应不在模型内）。原模型 objective/bounds 永不改写。
import cobra
import math
import re

G6_METHOD_VERSION = "gem.g6_atp_maintenance.v2"

# 五角色代谢物基础 id（区室无关；匹配时先剥离区室后缀 _c/_c0/_e/...）
# 覆盖 CarveMe（M_*_c / atp_c）与 BioCyc-MetaCyc（cpd00002_c0 / M00002_c）等 ID 体系。
ROLE_BASE_IDS = {
    "ATP": ("atp", "cpd00002", "m_atp", "m00002"),
    "ADP": ("adp", "cpd00008", "m_adp", "m00008"),
    "Pi":  ("pi", "cpd00009", "m_pi", "m00009"),
    "H2O": ("h2o", "cpd00001", "m_h2o", "m00001"),
    "H":   ("h", "cpd00067", "m_h", "m00067"),
}
# 名称兼容回退（精确匹配，大小写不敏感）
ROLE_NAMES = {
    "ATP": ("ATP",), "ADP": ("ADP",),
    "Pi": ("Pi", "PI", "phosphate", "orthophosphate", "inorganic phosphate"),
    "H2O": ("H2O", "water", "WATER"),
    "H": ("H+", "proton"),
}
# 分子式回退（含去质子化变体）
ROLE_FORMULAS = {
    "ATP": ("C10H16N5O13P3", "C10H12N5O13P3"),
    "ADP": ("C10H15N5O10P2", "C10H12N5O10P2", "C10H15N5O10P", "C10H12N5O10P"),
    "Pi": ("HO4P", "PO4H", "PO4H3", "H3O4P", "O4P"),
    "H2O": ("H2O",),
    "H": ("H",),
}
# 公式回退的矛盾名排除：ATP/ADP 分子式与 dGTP/dGDP 等脱氧/其他核苷酸共享，
# 仅凭公式会把它们误判为 ATP/ADP。公式匹配时若 name 含这些 token 则排除。
FORMULA_FORBIDDEN_NAME_TOKENS = {
    "ATP": ("gtp", "datp", "dgtp", "dttp", "dctp", "dutp", "utp", "ctp", "itp"),
    "ADP": ("gdp", "dadp", "dgdp", "dtdp", "dcdp", "dudp", "udp", "cdp", "idp"),
}

DISSIPATION_ID = "GEM_G6_atp_dissipation"
NET_ATP_ID = "GEM_G6_net_atp_generation"

_COMP_RE = re.compile(r"_([a-z]\d*)$")  # 区室后缀（c / c0 / e / e0 / p ...）


def _formula_norm(f):
    return (f or "").replace(" ", "").replace("+", "").strip()


def _base_id(met_id):
    """剥离常见区室后缀（_c/_c0/_e/...）得到区室无关基础 id。"""
    return _COMP_RE.sub("", (met_id or "").lower())


def _name_forbidden(met, role):
    name = (met.name or "").lower()
    return any(tok in name for tok in FORMULA_FORBIDDEN_NAME_TOKENS.get(role, ()))


def _role_candidates(m, role):
    """按 基础id → 精确名 → 公式 三级解析角色候选（跨区室可见）。

    - id：剥离区室后缀后匹配 ROLE_BASE_IDS——既覆盖 _c/_c0/_e 变体，
      又避免把 dGTP（与 ATP 同分子式）这类非目标分子误纳入（dgtp 不在基础 id 集合）。
    - name：精确名（大小写不敏感）。
    - formula：最后兜底，带矛盾名排除（dGTP 等）。
    返回 (candidates, strategy)。"""
    bases = ROLE_BASE_IDS[role]
    cands = [x for x in m.metabolites if _base_id(x.id) in bases]
    if cands:
        return cands, "id_pattern"
    names = {n.lower() for n in ROLE_NAMES[role]}
    cands = [x for x in m.metabolites if (x.name or "").strip().lower() in names]
    if cands:
        return cands, "name_match"
    forms = {_formula_norm(f) for f in ROLE_FORMULAS[role]}
    cands = [x for x in m.metabolites
             if _formula_norm(x.formula) in forms and not _name_forbidden(x, role)]
    return cands, "formula_match"


def resolve_energy_metabolites(m):
    """解析五个能量角色到同一区室（ATP 为锚点）。

    返回 (resolved: {role: metabolite}, report: dict)。
    失败时 resolved=None，report 带 skip 原因（缺失/多候选/区室冲突——不选第一条、不造代谢物）。
    规则：ATP 候选跨区室 → 区室冲突 SKIP；ATP 单区室多候选 → 歧义 SKIP；
         其余四角色在 ATP 区室内解析（同角色其他区室不算冲突，仅记录模型级候选数）。
    """
    report = {"per_role": {}, "compartment": None}
    atp_cands, atp_strat = _role_candidates(m, "ATP")
    report["per_role"]["ATP"] = {
        "resolved_by": atp_strat,
        "n_candidates_model_wide": len(atp_cands),
        "candidates": [{"id": x.id, "name": x.name, "compartment": x.compartment}
                       for x in atp_cands[:10]],
    }
    if not atp_cands:
        report["skip_reason"] = "ATP 未解析（基础 id/精确名/公式 三级均未命中）"
        return None, report
    comps = sorted({x.compartment for x in atp_cands})
    if len(comps) > 1:
        report["skip_reason"] = (f"ATP 候选跨区室 {comps}——不能随便选第一条；"
                                 f"请在模型内显式标注后重跑")
        return None, report
    if len(atp_cands) > 1:
        report["skip_reason"] = (f"ATP 在区室 {comps[0]} 有 {len(atp_cands)} 个候选："
                                 f"{[x.id for x in atp_cands[:5]]}——歧义，不猜")
        return None, report
    comp = comps[0]
    report["compartment"] = comp

    resolved = {"ATP": atp_cands[0]}
    for role in ("ADP", "Pi", "H2O", "H"):
        cands, strat = _role_candidates(m, role)
        in_comp = [x for x in cands if x.compartment == comp]
        report["per_role"][role] = {
            "resolved_by": strat,
            "n_candidates_model_wide": len(cands),
            "n_candidates_in_compartment": len(in_comp),
            "candidates": [{"id": x.id, "name": x.name, "compartment": x.compartment}
                           for x in cands[:10]],
        }
        if len(in_comp) == 0:
            report["skip_reason"] = (
                f"{role} 在区室 {comp} 未找到（模型级候选 {len(cands)} 个）——"
                f"主检查需要 ATP+H2O→ADP+Pi+H+ 全部在同一区室；不凭空添加代谢物")
            return None, report
        if len(in_comp) > 1:
            report["skip_reason"] = (
                f"{role} 在区室 {comp} 有 {len(in_comp)} 个候选："
                f"{[x.id for x in in_comp[:5]]}——歧义，不猜")
            return None, report
        resolved[role] = in_comp[0]
    return resolved, report


def _solver_feasibility_tolerance(m, default=1e-7):
    """尽力读 solver feasibility tolerance（读不到用保守 default，不编造）。"""
    try:
        tol = m.solver.configuration.tolerances.feasibility
        value = float(tol)
        return value if math.isfinite(value) and value > 0 else None
    except Exception:
        return default


def _close_boundaries(m):
    """关闭全部边界反应为 (0,0)。识别不依赖 EX_ 前缀（cobra boundary = 单代谢物反应）。

    返回 (n_closed, changed)：changed = [{'id', 'original_bounds'}...]（context 内会被回滚）。"""
    changed = []
    n = 0
    for r in m.reactions:
        if r.boundary:
            changed.append({"id": r.id, "original_bounds": (r.lower_bound, r.upper_bound)})
            r.bounds = (0.0, 0.0)
            n += 1
    return n, changed


def _relax_forced_internal_bounds(m):
    """主检查尺度（显式声明）：内部反应的强制非零通量松开到方向保持的零底。

    规则（保留原方向、fixed-zero 不动）：
      - lb > 0（强制正向）→ lb = 0（ub 保持；原方向 [0, ub] 保持）
      - ub < 0（强制反向）→ ub = 0（lb 保持；原方向 [lb, 0] 保持）
      - (0,0) 与常规区间不动；边界反应已由 _close_boundaries 处理。
    返回 changed = [{'id', 'original', 'relaxed'}...]（原 bounds 的诊断另行保存）。"""
    changed = []
    for r in m.reactions:
        if r.boundary:
            continue
        lb, ub = r.lower_bound, r.upper_bound
        if lb > 0:
            r.lower_bound = 0.0
            changed.append({"id": r.id, "original": (lb, ub), "relaxed": (0.0, ub)})
        elif ub < 0:
            r.upper_bound = 0.0
            changed.append({"id": r.id, "original": (lb, ub), "relaxed": (lb, 0.0)})
    return changed


def _run_flux_max(m, rxn_id):
    """以 rxn 为 objective 最大化。返回 (status, raw_flux)——非 optimal/异常返回错误态。"""
    m.objective = m.reactions.get_by_id(rxn_id)
    m.objective.direction = "max"
    try:
        sol = m.optimize()
    except Exception as e:  # solver 异常不得 PASS、不得抛穿闸门
        return "solver_error", None, f"{type(e).__name__}: {e}"
    status = str(sol.status)
    if status != "optimal":
        return status, None, None
    raw = sol.fluxes[rxn_id] if rxn_id in sol.fluxes.index else sol.objective_value
    try:
        raw = float(raw)
    except (TypeError, ValueError):
        return "non_numeric_flux", None, f"flux={raw!r}"
    if raw != raw or raw in (float("inf"), float("-inf")):  # NaN/Inf 防御
        return "non_finite_flux", None, f"flux={raw!r}"
    return "optimal", raw, None


def g6_atp_maintenance(self, context=None):
    """G6 主闸门（方法 v2）。作为 Validator 方法绑定（validate.py 注册）。"""
    m = self.m
    ctx = context or {}
    n_reactions_before = len(m.reactions)
    out = {
        "method_version": G6_METHOD_VERSION,
        "definition": ("无外界供能时，ATP 水解耗散（ATP+H2O→ADP+Pi+H+，同一区室）"
                       "能否维持正通量；正通量 = 能量守恒违反（充能循环）"),
        "post_gapfill": bool(ctx.get("post_gapfill")),
        "units": "mmol/gDW/h",
    }

    resolved, res_report = resolve_energy_metabolites(m)
    out["metabolite_resolution"] = res_report
    if resolved is None:
        out["status"] = "SKIP"
        out["reason"] = res_report.get("skip_reason")
        out["tried_patterns"] = {k: list(v) for k, v in ROLE_BASE_IDS.items()}
        out["note"] = ("SKIP 系角色解析失败（非模型缺陷证明）；主检查要求五角色代谢物"
                       "在同一区室可无歧义解析，不选第一条、不凭空添加")
        return out

    comp = res_report["compartment"]
    atp, adp, pi, h2o, h = (resolved["ATP"], resolved["ADP"], resolved["Pi"],
                            resolved["H2O"], resolved["H"])
    out["metabolite_resolution"]["resolved"] = {
        "ATP": atp.id, "ADP": adp.id, "Pi": pi.id, "H2O": h2o.id, "H": h.id}

    eff_tol_source = None
    with m:
        # 1) 边界双向关闭 (0,0)——不依赖 EX_ 前缀；不为可行性放开任何水/H+/Pi/氧/碳源
        n_closed, closed_detail = _close_boundaries(m)
        # 2) 内部强制下界松开（声明的主检查尺度；原 bounds 另存做附加诊断）
        relaxed = _relax_forced_internal_bounds(m)
        # 3) 耗散反应（主检查）
        dm = cobra.Reaction(DISSIPATION_ID, name="G6 ATP 水解耗散（主检查）",
                            lower_bound=0.0, upper_bound=1000.0)
        m.add_reactions([dm])
        dm.add_metabolites({atp: -1, h2o: -1, adp: 1, pi: 1, h: 1})
        status, raw, err = _run_flux_max(m, DISSIPATION_ID)

        # 附加诊断 A：净 ATP 生成（单 ATP → ∅；不与主检查互换）
        net = cobra.Reaction(NET_ATP_ID, name="G6 净 ATP 生成（附加诊断）",
                             lower_bound=0.0, upper_bound=1000.0)
        m.add_reactions([net])
        net.add_metabolites({atp: -1})
        net_status, net_raw, net_err = _run_flux_max(m, NET_ATP_ID)

        # 附加诊断 B：原 bounds（不松开内部强制下界）下的耗散
        for ch in relaxed:
            r = m.reactions.get_by_id(ch["id"])
            r.bounds = ch["original"]
        orig_status, orig_raw, orig_err = _run_flux_max(m, DISSIPATION_ID)

        feas = _solver_feasibility_tolerance(m)
        solver_name = str(m.solver.interface.__name__).split(".")[-1]
        if feas is None:
            status, raw, err = "invalid_solver_tolerance", None, "non-finite feasibility tolerance"
        eff_tol = max(1e-6, feas) if feas is not None else None
        eff_tol_source = (f"max(1e-6, solver_feasibility={feas:g}, solver={solver_name})"
                          if feas is not None else "solver feasibility tolerance invalid")

        # 容差比较：先比较原值（raw 未取整）
        if status == "optimal":
            main_pass = raw <= eff_tol
        else:
            main_pass = False

        out.update({
            "scope": {"boundaries": "全部边界反应 (0,0)（cobra boundary 语义，不依赖 EX_ 前缀）",
                      "internal_forced_bounds": "松开到方向保持零底（lb>0→0 / ub<0→0；fixed-zero 不动）",
                      "dissipation_compartment": comp,
                      "excretion_allowances": "无（未提供任何允许排出的额外模式）"},
            "dissipation_reaction": {
                "id": DISSIPATION_ID,
                "equation": "ATP + H2O -> ADP + Pi + H+",
                "stoichiometry": {atp.id: -1, h2o.id: -1, adp.id: 1, pi.id: 1, h.id: 1},
                "objective": "max", "bounds": (0.0, 1000.0)},
            "closed_boundaries": {"n": n_closed, "detail": closed_detail[:200]},
            "relaxed_bounds": {"n": len(relaxed), "detail": relaxed[:200]},
            "solver": {"name": solver_name, "status": status},
            "effective_tolerance": {"value": eff_tol, "source": eff_tol_source,
                                     "units": "mmol/gDW/h"},
            "raw_flux": raw,
            "atp_leak_flux": (round(raw, 6) if raw is not None else None),
            "atp_leak_flux_meaning": ("v2 起为 ATP 水解耗散反应的原始通量（原 bounds 不参与）；"
                                      "v1 该字段实为 biomass objective 通量，语义已废弃"),
            "diagnostics": {
                "net_atp_generation": {
                    "reaction": "ATP -> ∅（单代谢物 demand，仅诊断）",
                    "status": net_status, "raw_flux": net_raw,
                    "note": "净 ATP 生成与耗散不可互换：质守恒使部分循环只对完整水解反应可见"},
                "original_bounds_dissipation": {
                    "status": orig_status, "raw_flux": orig_raw,
                    "note": "原 bounds（含内部强制下界）下的同一耗散最大化；"
                            "infeasible 提示强制通量与关闭边界矛盾（诊断，不参与判定）"},
            },
        })
        if status != "optimal":
            out["status"] = "ERROR" if status in ("solver_error", "non_numeric_flux",
                                                 "non_finite_flux", "invalid_solver_tolerance") else "FAIL"
            out["reason"] = (f"主检查求解未达 optimal（status={status}）"
                             + (f"：{err}" if err else "")
                             + "——求解失败不得 PASS")
        elif main_pass:
            out["status"] = "PASS"
        else:
            out["status"] = "FAIL"
            out["reason"] = (f"耗散通量 {raw!r} > 有效容差 {eff_tol!r}（{eff_tol_source}）"
                             "——无外界供能时可维持 ATP 水解耗散 = 能量循环泄漏")

    # 无副作用自检：context 回滚后模型应回到原样
    assert DISSIPATION_ID not in {r.id for r in m.reactions}, "G6 v2 副作用：耗散反应未回滚"
    assert NET_ATP_ID not in {r.id for r in m.reactions}, "G6 v2 副作用：诊断反应未回滚"
    assert len(m.reactions) == n_reactions_before, "G6 v2 副作用：反应数变化"
    out["side_effect_check"] = "pass（context 回滚后反应数不变，检查反应已移除）"
    return out
