# sensitivity.py — 阶段A-M2 结构性灵敏度（把"模型不确定"量化）
# 网格（全量，不抽样）: biomass 组分系数 ×{0.75, 1.0, 1.25} × GAM {1,5,10,20,30,40,50} = 21 扫点
#   + 1 基准组合（biomass×1.0 且 GAM=原始值，不扰动）= 22 组合；每组合 wt growth + 必需性重扫
#   （复用 essential_scan.setup_model_medium / scan_essentiality——M2 顺带工程改进）。
# 正交化: C58 的 GAM 载体在 biomass 方程 bio1 内部（ATP 水解 stub 五元组 ATP/H2O/ADP/Pi/H+，
#   GAM=40.0 mmol ATP/gDW 以 ADP 系数为净水电解量）——biomass 组分缩放排除该 stub 与 Biomass
#   产物（目标汇连接组分），GAM 网格只动 stub（等比缩放 X/GAM_ORIG）。
# 锚点: 基准组合与 essential_scan 完全同参数 -> 必须精确复现 155；且 155 全部在
#   always_essential ∪ conditionally_essential（基准在网格内故断言必成立）。
# 生长/通量数值口径: 单点 FBA objective_value（mmol/gDW/h）；区间制对比请用 gem_fluxscan。
import os
import sys
import csv
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from silentio import silent_read_sbml
from validate import parse_formula
from essential_scan import setup_model_medium, scan_essentiality

BIOMASS_SCALES = [0.75, 1.0, 1.25]
GAM_GRID = [1, 5, 10, 20, 30, 40, 50]
EPS = 1e-6
LP_TIMEOUT_S = 30  # 单次 LP 求解上限（秒）。正常 <0.05s；仅防个别扰动 LP 的 GLPK 病态停摆（实测会卡死）


def _set_lp_timeout(m, seconds=LP_TIMEOUT_S):
    """GLPK 病态停摆护栏：optlang configuration.timeout。设置失败不影响主流程。"""
    try:
        m.solver.configuration.timeout = seconds
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GAM 载体定位（跨命名空间：公式级识别，不依赖 MetaCyc/BiGG/ModelSEED id 体系）
# ---------------------------------------------------------------------------
def _classify_energy_met(met):
    """按元素组成识别能量 stub 角色（ATP/ADP/Pi/H2O/H+）。返回角色名或 None。"""
    f = parse_formula(met.formula or "")
    c, p, n, o, h = f.get("C", 0), f.get("P", 0), f.get("N", 0), f.get("O", 0), f.get("H", 0)
    if c == 10 and p == 3 and n == 5:
        return "atp"
    if c == 10 and p == 2 and n == 5:
        return "adp"
    if c == 0 and p == 1 and n == 0:
        return "pi"
    if c == 0 and p == 0 and n == 0 and o == 1 and h == 2:
        return "h2o"
    if c == 0 and p == 0 and n == 0 and o == 0 and h == 1:
        return "h"
    return None


def find_biomass_gam(m):
    """定位 biomass 反应与 GAM 载体。返回
    {biomass_rxn, objective_rxn, biomass_product, carrier_type, gam_mets{role:met_id},
     gam_orig, stub_coeffs{role:coeff}, n_components, scaled_components}。
    carrier_type: inside_biomass（stub 在 biomass 方程内）| independent_reaction | not_found。"""
    obj_rxns = [r for r in m.reactions if r.objective_coefficient != 0]
    if not obj_rxns:
        raise ValueError("no objective reaction found")
    cands, product_id = [], None
    for r in obj_rxns:
        if len(r.metabolites) >= 10:
            cands.append(r)
        else:
            # gapseq 惯例：objective 是 Biomass 代谢物的汇（EX/DM），真 biomass 是其生产者
            for met in r.metabolites:
                product_id = met.id
                for r2 in met.reactions:
                    if r2.id != r.id and len(r2.metabolites) >= 10:
                        cands.append(r2)
    if not cands:
        raise ValueError("biomass reaction not found（objective 及其汇生产者均 <10 组分）")
    bio = max(cands, key=lambda r: len(r.metabolites))

    stub, stub_coeffs = {}, {}
    unclassified_energy_scale = []
    for met, coeff in bio.metabolites.items():
        role = _classify_energy_met(met)
        if role and abs(coeff) > 1.0 and role not in stub:
            stub[role] = met.id
            stub_coeffs[role] = coeff
    # 阶段A-M5 适配（iNX1344_v4 探索结论）：H2O 角色的代谢物可能公式缺失（如 M00001_c
    # formula=None），且其量级与 GAM 净水电解量（ADP 系数）一致——量级回退补判。
    if {"atp", "adp", "pi", "h"} <= set(stub) and "h2o" not in stub:
        gam_scale = abs(stub_coeffs["adp"])
        for met, coeff in bio.metabolites.items():
            if met.id in set(stub.values()) or abs(coeff) <= 1.0:
                continue
            if _classify_energy_met(met) is None \
                    and abs(abs(coeff) - gam_scale) / gam_scale < 0.05:
                stub["h2o"] = met.id
                stub_coeffs["h2o"] = coeff
                break
    carrier, gam_orig = "not_found", None
    if all(k in stub for k in ("atp", "adp", "pi", "h2o", "h")):
        carrier = "inside_biomass"
        gam_orig = abs(stub_coeffs["adp"])
    else:
        # 独立载体候选：纯 ATPM 型维持反应——反应内全部代谢物都是能量 stub 角色
        # （M5 探索教训：含额外底物的 ATP 水解反应如谷氨酰胺合成酶会误命中，须排除）
        for r in m.reactions:
            roles = [_classify_energy_met(met)
                     for met in r.metabolites]
            if r.metabolites and all(roles) and set(roles) <= {"atp", "adp", "pi", "h2o", "h"} \
                    and {"atp", "adp", "pi"} <= set(roles):
                carrier = "independent_reaction"
                gam_mets = {met.id for met in r.metabolites}
                gam_orig = abs(r.lower_bound) if r.lower_bound > 0 else None
                return {"biomass_rxn": bio.id, "objective_rxn": obj_rxns[0].id,
                        "biomass_product": product_id, "carrier_type": carrier,
                        "gam_reaction": r.id, "gam_mets": sorted(gam_mets),
                        "gam_orig": gam_orig, "stub_coeffs": {},
                        "n_components": len(bio.metabolites),
                        "scaled_components": len(bio.metabolites)}
    skip = set(stub.values()) | ({product_id} if product_id else set())
    return {"biomass_rxn": bio.id, "objective_rxn": obj_rxns[0].id,
            "biomass_product": product_id, "carrier_type": carrier,
            "gam_mets": stub, "gam_orig": gam_orig, "stub_coeffs": stub_coeffs,
            "n_components": len(bio.metabolites),
            "scaled_components": len(bio.metabolites) - len(skip)}


def _apply_biomass_scale(m, bio_id, f, gam_info):
    """biomass 组分缩放 ×f（排除 GAM stub 与 Biomass 产物——正交化）。
    cobra add_metabolites 是增量语义：绝对设定必须用 delta = 目标-现值（Q2 教训），回读校验。
    注意：原始系数必须在 add 之前快照（add 后 bio.metabolites 已是新值）。"""
    bio = m.reactions.get_by_id(bio_id)
    skip = set((gam_info.get("gam_mets") or {}).values()) | \
        ({gam_info["biomass_product"]} if gam_info.get("biomass_product") else set())
    originals = {met: old for met, old in bio.metabolites.items() if met.id not in skip}
    bio.add_metabolites({met: old * f - old for met, old in originals.items()})
    errs = []
    for met, old in originals.items():
        want = old * f
        got = bio.metabolites.get(met)
        if got is None or abs(got - want) > 1e-9:
            errs.append({"met": met.id, "want": want, "got": got})
    if errs:
        raise RuntimeError(f"biomass scale verify failed: {errs[:3]}")


def _apply_gam(m, bio_id, gam_value, gam_info):
    """GAM 设为 gam_value：5 元 stub 等比缩放 gam_value/GAM_ORIG（保持 gapseq stub 内部比例，
    含 ATP -40.165476 vs ADP +40.0 的记账不对称）。回读校验。"""
    bio = m.reactions.get_by_id(bio_id)
    f = gam_value / gam_info["gam_orig"]
    deltas = {}
    for role, mid in gam_info["gam_mets"].items():
        met = m.metabolites.get_by_id(mid)
        old = bio.metabolites.get(met)
        if old is None:
            raise RuntimeError(f"GAM stub met {mid} not in {bio_id}")
        deltas[met] = old * f - old
    bio.add_metabolites(deltas)
    errs = []
    for role, mid in gam_info["gam_mets"].items():
        met = m.metabolites.get_by_id(mid)
        got = bio.metabolites.get(met)
        want = gam_info["stub_coeffs"][role] * f
        if got is None or abs(got - want) > 1e-6:
            errs.append({"met": mid, "want": want, "got": got})
    if errs:
        raise RuntimeError(f"GAM set verify failed: {errs[:3]}")


# ---------------------------------------------------------------------------
# 稳定性三分类（纯函数，selftest 锁定）
# ---------------------------------------------------------------------------
def classify_stability(combos, total_genes):
    """combos: [{"label", "essential_genes": [...]}, ...]。返回
    {"always_essential", "conditionally_essential":[{gene,pattern}], "never_essential_count"}。"""
    from collections import Counter
    sets = [set(c["essential_genes"]) for c in combos]
    labels = [c["label"] for c in combos]
    always = set.intersection(*sets) if sets else set()
    union = set.union(*sets) if sets else set()
    conditional = union - always
    out_always = sorted(always)
    out_cond = []
    for gid in sorted(conditional):
        on = [labels[i] for i, s in enumerate(sets) if gid in s]
        off = [labels[i] for i, s in enumerate(sets) if gid not in s]
        pattern = (f"essential in {len(on)}/{len(sets)}"
                   + ("；非必需组合: " + ", ".join(off[:6]) + ("…" if len(off) > 6 else "") if off else
                      "；必需组合: " + ", ".join(on[:6]) + ("…" if len(on) > 6 else "")))
        out_cond.append({"gene": gid, "pattern": pattern})
    return {"always_essential": out_always,
            "conditionally_essential": out_cond,
            "never_essential_count": max(0, total_genes - len(union))}


def _combo_scan(model_path, medium, bio_id, gam_info, scale, gam_value, label, log):
    """单组合：fresh read -> (可选)扰动 -> 介质 setup -> wt + 必需性重扫。"""
    m = silent_read_sbml(model_path)
    _set_lp_timeout(m)
    if scale is not None and scale != 1.0:
        _apply_biomass_scale(m, bio_id, scale, gam_info)
    if gam_value is not None and gam_info.get("gam_orig") and abs(gam_value - gam_info["gam_orig"]) > 1e-12:
        _apply_gam(m, bio_id, gam_value, gam_info)
    resolved, unresolved, preset = setup_model_medium(m, medium)
    res = scan_essentiality(m)
    row = {"label": label, "biomass": scale, "gam": gam_value,
           "growth": res["wt_growth"], "essential_count": res["essential_count"],
           "essential_genes": res["essential_genes"], "tested_genes": res["tested_genes"]}
    log(f"[sens] {label}: growth={res['wt_growth']} essential={res['essential_count']} "
        f"(fva {res['fva_seconds']}s knock {res['knock_seconds']}s)")
    return row, m


def sensitivity(model_path, medium=None, biomass_scales=None, gam_grid=None,
                run_component_sensitivity=True, run_drift=True, top_n=10,
                export_csv=None, progress=None, baseline_check=None):
    """M2 主入口。baseline_check: 可选外部基线必需集（来自 essential_scan 直跑）——
    基准组合与其做集合相等断言（任务书锚点）。返回完整结果 dict。"""
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    biomass_scales = biomass_scales or BIOMASS_SCALES
    gam_grid = gam_grid or GAM_GRID
    t_start = time.time()

    # 0) 结构探索（GAM 载体）
    m0 = silent_read_sbml(model_path)
    gam_info = find_biomass_gam(m0)
    log(f"[sens] biomass={gam_info['biomass_rxn']} carrier={gam_info['carrier_type']} "
        f"GAM_ORIG={gam_info['gam_orig']} components={gam_info['n_components']} "
        f"scaled={gam_info['scaled_components']}")
    if gam_info["carrier_type"] != "inside_biomass":
        log(f"[sens] WARN: GAM 载体非 biomass 内部 stub（{gam_info['carrier_type']}），GAM 轴行为见报告")

    grid = []
    # 1) 基准组合（不扰动）——必须与 essential_scan 同参数同结果
    t0 = time.time()
    base_row, m_base = _combo_scan(model_path, medium, gam_info["biomass_rxn"], gam_info,
                                   None, None, f"biomass=1.0,gam=orig({gam_info['gam_orig']})", log)
    base_row["baseline"] = True
    grid.append(base_row)
    baseline_set = set(base_row["essential_genes"])
    degenerate = base_row["growth"] <= EPS  # 阶段A-M5 发现：wt=0 时必需性判定退化（候选全判"必需"）
    if degenerate:
        log("[sens] WARN: 基准组合 wt_growth<=0（介质不可解析/模型不生长）——必需性判定退化，"
            "结果仅证明工具在该模型上跑通，essential 集无生物学意义")
    if baseline_check is not None:
        base_row["baseline_matches_essential_scan"] = (baseline_set == set(baseline_check))
        log(f"[sens] 基准组合 vs essential_scan: {len(baseline_set)} vs {len(set(baseline_check))} "
            f"-> {'EXACT MATCH' if base_row['baseline_matches_essential_scan'] else 'MISMATCH（实现 bug，须修复）'}")

    # 2) 21 扫点（biomass 轴 × GAM 轴；gam=orig 的扫点与基准互为对照）
    for f in biomass_scales:
        for gv in gam_grid:
            label = f"biomass={f},gam={gv}"
            row, _ = _combo_scan(model_path, medium, gam_info["biomass_rxn"], gam_info,
                                 f, float(gv), label, log)
            grid.append(row)
    log(f"[sens] grid done: {len(grid)} combos in {round(time.time()-t0,1)}s")

    # 3) 稳定性三分类 + 锚点断言（155 ⊆ always ∪ conditional）
    combos = [{"label": r["label"], "essential_genes": r["essential_genes"]} for r in grid]
    total_genes = len(m0.genes)
    stability = classify_stability(combos, total_genes)
    covered = set(stability["always_essential"]) | {c["gene"] for c in stability["conditionally_essential"]}
    baseline_assert_ok = baseline_set <= covered
    if not baseline_assert_ok:
        log(f"[sens] ASSERT FAIL: baseline essential not in always∪conditional: "
            f"{sorted(baseline_set - covered)[:10]}")
    log(f"[sens] stability: always={len(stability['always_essential'])} "
        f"conditional={len(stability['conditionally_essential'])} "
        f"never={stability['never_essential_count']} baseline_assert_ok={baseline_assert_ok}")

    # 4) 二级：单组分 ±25% 灵敏度（只测 wt growth；with m 上下文自动回滚 delta）
    #    用 fresh 模型（m_base 经历 FVA+818 敲除上下文，实测复用其求解器状态会病态变慢）
    comp_rows, top_sensitive = [], []
    g0 = base_row["growth"]
    if run_component_sensitivity:
        t0 = time.time()
        m_comp = silent_read_sbml(model_path)
        _set_lp_timeout(m_comp)
        setup_model_medium(m_comp, medium)
        bio = m_comp.reactions.get_by_id(gam_info["biomass_rxn"])
        skip = set(gam_info["gam_mets"].values()) | \
            ({gam_info["biomass_product"]} if gam_info.get("biomass_product") else set())
        comps = [(met, c) for met, c in bio.metabolites.items() if met.id not in skip]
        for idx, (met, old) in enumerate(comps):
            gds = {}
            t_flag = False
            for f in (0.75, 1.25):
                with m_comp:
                    bio.add_metabolites({met: old * f - old})
                    try:
                        v = m_comp.optimize().objective_value
                    except Exception as e:  # LP 超时/求解器异常：如实标记，不阻塞
                        sys.stderr.write(f"[sens] LP fail {met.id} f={f}: {type(e).__name__}: {e}\n")
                        v = None
                        t_flag = True
                got = bio.metabolites.get(met)
                if got is None or abs(got - old) > 1e-9:
                    raise RuntimeError(f"with-context did not revert {met.id}: got {got}, want {old}")
                gds[f] = round(float(v), 6) if v is not None else None
            d75 = round((gds[0.75] - g0) / g0 * 100, 4) if (g0 > EPS and gds[0.75] is not None) else None
            d125 = round((gds[1.25] - g0) / g0 * 100, 4) if (g0 > EPS and gds[1.25] is not None) else None
            comp_rows.append({"component": met.id, "met_name": met.name, "coeff": round(old, 6),
                              "growth_x0.75": gds[0.75], "delta_pct_x0.75": d75,
                              "growth_x1.25": gds[1.25], "delta_pct_x1.25": d125,
                              "lp_timeout": t_flag,
                              "max_abs_delta_pct": max(abs(d75 or 0.0), abs(d125 or 0.0))})
            sys.stderr.write(f"[sens] comp {idx+1}/{len(comps)} {met.id} "
                             f"d75={d75} d125={d125}{' TIMEOUT' if t_flag else ''}\n")
        comp_rows.sort(key=lambda r: -r["max_abs_delta_pct"])
        top_sensitive = [{"component": r["component"], "met_name": r["met_name"],
                          "delta_pct_x0.75": r["delta_pct_x0.75"],
                          "delta_pct_x1.25": r["delta_pct_x1.25"],
                          "max_abs_delta_pct": r["max_abs_delta_pct"]}
                         for r in comp_rows[:top_n]]
        log(f"[sens] component sensitivity: {len(comp_rows)} comps × 2 in {round(time.time()-t0,1)}s; "
            f"top={[(t['component'], t['max_abs_delta_pct']) for t in top_sensitive[:3]]}")

    # 5) top 敏感组分必需性重扫（漂移）——LP 超时的组分跳过（其扰动 LP 有 GLPK 停摆前科）
    drift, skipped_drift = [], []
    if run_drift and run_component_sensitivity and top_sensitive:
        t0 = time.time()
        for t in top_sensitive:
            trow = next((r for r in comp_rows if r["component"] == t["component"]), {})
            if trow.get("lp_timeout"):
                skipped_drift.append(t["component"])
                continue
            met0 = m_comp.metabolites.get_by_id(t["component"])
            old0 = bio.metabolites.get(met0)
            if old0 is None:
                continue
            for f in (0.75, 1.25):
                label = f"{t['component']}×{f}"
                # 前置生长探针：扰动后不生长（刚性组分对，如 ACP↔apo-ACP 任一 ±25% 都使
                # biomass 方程不可满足）时必需性判定无意义（wt=0 会把全部候选判"必需"）——跳过重扫
                m_probe = silent_read_sbml(model_path)
                _set_lp_timeout(m_probe)
                b_probe = m_probe.reactions.get_by_id(gam_info["biomass_rxn"])
                met_probe = m_probe.metabolites.get_by_id(t["component"])
                old_probe = b_probe.metabolites.get(met_probe)
                b_probe.add_metabolites({met_probe: old_probe * f - old_probe})
                setup_model_medium(m_probe, medium)
                with m_probe:
                    g_probe = m_probe.optimize().objective_value
                if g_probe is None or g_probe <= EPS:
                    drift.append({"component": t["component"], "met_name": t["met_name"],
                                  "scale": f, "label": label, "wt_growth": 0.0,
                                  "essentiality_undefined": True,
                                  "note": "扰动后模型不生长（组分刚性/二元脆性），必需性漂移判定无意义，跳过重扫"})
                    log(f"[sens] drift {label}: growth<=0 -> essentiality undefined, skipped")
                    continue
                m = silent_read_sbml(model_path)
                _set_lp_timeout(m)
                b = m.reactions.get_by_id(gam_info["biomass_rxn"])
                met2 = m.metabolites.get_by_id(t["component"])
                old2 = b.metabolites.get(met2)
                b.add_metabolites({met2: old2 * f - old2})
                setup_model_medium(m, medium)
                try:
                    res = scan_essentiality(m)
                except Exception as e:
                    drift.append({"component": t["component"], "met_name": t["met_name"],
                                  "scale": f, "label": label, "error": f"{type(e).__name__}: {e}"})
                    log(f"[sens] drift {label}: FAILED {type(e).__name__}: {e}")
                    continue
                var_set = set(res["essential_genes"])
                drift.append({"component": t["component"], "met_name": t["met_name"],
                              "scale": f, "label": label, "wt_growth": res["wt_growth"],
                              "essential_count": res["essential_count"],
                              "lost_essential": sorted(baseline_set - var_set),
                              "gained_essential": sorted(var_set - baseline_set)})
                log(f"[sens] drift {label}: essential={res['essential_count']} "
                    f"lost={len(baseline_set - var_set)} gained={len(var_set - baseline_set)}")
        log(f"[sens] drift done in {round(time.time()-t0,1)}s; skipped_lp_timeout={skipped_drift}")

    # 6) 模型卡鲁棒性章节（无 card 不造卡）
    card_written = False
    out = {
        "model": model_path,
        "medium": medium,
        "combinations": len(grid),
        "gam_carrier": {k: v for k, v in gam_info.items()},
        "wt_growth_grid": grid,
        "baseline_essential_count": len(baseline_set),
        "baseline_reproduced": bool(base_row.get("baseline_matches_essential_scan",
                                                 baseline_set is not None)) if baseline_check is not None else None,
        "baseline_assert_always_or_conditional": baseline_assert_ok,
        "baseline_growth_degenerate": bool(degenerate),
        "stability": stability,
        "component_sensitivity": {"top_sensitive": top_sensitive, "rows": comp_rows},
        "component_essentiality_drift": drift,
        "card_robustness_written": card_written,
        "units": "mmol/gDW/h",
        "timing_seconds": round(time.time() - t_start, 1),
    }
    try:
        from model_card import set_robustness
        payload = dict(out)
        payload.pop("component_sensitivity", None)
        payload["component_sensitivity"] = {"top_sensitive": top_sensitive}
        r = set_robustness(model_path, payload)
        card_written = r is not None
        out["card_robustness_written"] = card_written
        if not card_written:
            log("[sens] 模型旁无 card -> robustness 章节未写（无卡不造卡纪律）")
    except Exception as e:
        log(f"[sens] card robustness write failed: {e}")

    if export_csv:
        try:
            rows_n = _export_csv(export_csv, grid, comp_rows, drift, stability)
            out["export_csv"] = export_csv
            out["export_csv_rows"] = rows_n
            out["export_csv_bytes"] = os.path.getsize(export_csv)
            log(f"[sens] CSV {export_csv}: {rows_n} rows")
        except Exception as e:  # CSV 失败不拖垮已完成的全量计算结果
            out["export_csv_error"] = f"{type(e).__name__}: {e}"
            log(f"[sens] CSV export FAILED: {out['export_csv_error']}")
    return out


def _export_csv(path, grid, comp_rows, drift, stability):
    rows = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["section", "key1", "key2", "value_num", "value_str"])
        for r in grid:
            w.writerow(["grid", r["label"], "growth", r["growth"], r.get("baseline", False)])
            w.writerow(["grid", r["label"], "essential_count", r["essential_count"],
                        ";".join(r["essential_genes"])[:32000]])
            rows += 2
        for r in comp_rows:
            w.writerow(["component", r["component"], "x0.75", r["growth_x0.75"], r["delta_pct_x0.75"]])
            w.writerow(["component", r["component"], "x1.25", r["growth_x1.25"], r["delta_pct_x1.25"]])
            rows += 2
        for r in drift:
            if r.get("error") or r.get("essentiality_undefined"):
                w.writerow(["drift", r["label"], "skipped", r.get("essential_count", ""),
                            str(r.get("error") or r.get("note"))[:300]])
                rows += 1
                continue
            w.writerow(["drift", r["label"], "essential_count", r["essential_count"],
                        "lost=" + ";".join(r["lost_essential"])[:8000] +
                        "|gained=" + ";".join(r["gained_essential"])[:8000]])
            rows += 1
        for g in stability["always_essential"]:
            w.writerow(["stability", g, "always", 1, ""]); rows += 1
        for c in stability["conditionally_essential"]:
            w.writerow(["stability", c["gene"], "conditional", 0, c["pattern"]]); rows += 1
    return rows


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        # 纯函数级：稳定性三分类（无模型依赖）
        combos = [
            {"label": "A", "essential_genes": ["g1", "g2", "g3"]},
            {"label": "B", "essential_genes": ["g1", "g2", "g4"]},
            {"label": "C", "essential_genes": ["g1", "g2"]},
        ]
        st = classify_stability(combos, total_genes=6)
        assert st["always_essential"] == ["g1", "g2"], st
        assert {c["gene"] for c in st["conditionally_essential"]} == {"g3", "g4"}, st
        assert st["never_essential_count"] == 2, st  # g5, g6
        assert "essential in 1/3" in st["conditionally_essential"][0]["pattern"]
        # 单组合边界：全 always
        st2 = classify_stability([{"label": "A", "essential_genes": ["g1"]}], total_genes=2)
        assert st2["always_essential"] == ["g1"] and st2["never_essential_count"] == 1
        print(json.dumps({"ok": True, "result": {"selftest": "pass"}}))
    else:
        args = {}
        if len(sys.argv) > 1:
            with open(sys.argv[1], encoding="utf-8") as f:
                args = json.load(f)
        elif not sys.stdin.isatty():
            args = json.loads(sys.stdin.read())
        a = args.get("args", args)
        print(json.dumps({"ok": True, "result": sensitivity(
            a.get("model"), medium=a.get("medium"),
            biomass_scales=a.get("biomass_scales"), gam_grid=a.get("gam_grid"),
            run_component_sensitivity=a.get("run_component_sensitivity", True),
            run_drift=a.get("run_drift", True), top_n=a.get("top_n", 10),
            export_csv=a.get("export_csv"))}, ensure_ascii=False))
