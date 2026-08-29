# fluxscan.py — M1 通量区间制（阶段 A 可信度内核第一件）
# 语义（bsp 锁稿，一字不改）：每反应输出 fva_min/fva_max/pfba；条件对比消费区间分离判定；
#   overlap = 点值差异是求解器伪影，禁止引用。
# 计算口径：每 condition 独立 silent_read_sbml 重读模型（勿深拷贝）；介质 setup 对齐 validate G3
#   （expand_medium -> 全交换清零 EX_/DM_/SK_/boundary -> resolve_medium 设 bounds）；
#   substrates 对齐 validate g4_phenotype（supplement=基准不变+底物 lb=-10；sole=去含碳交换[元素级
#   parse_formula]+底物 lb=-10）；FVA processes=1（Windows 无 fork）。
# 区间分离判定（核心，单测锁定）：
#   分离 = ua + tol < lb  -> direction=b_higher
#          ub + tol < la  -> direction=a_higher
#   否则 overlap=true / hard_conclusion=false / direction=null
# 判定用四舍六入到 6 位后的区间值（与输出/CSV 展示值自洽，tol=1e-6 仍完全覆盖求解器噪声）。
import os
import sys
import csv
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from silentio import silent_read_sbml
from gapfind import expand_medium, resolve_medium, build_ex_index, match_ex
from validate import parse_formula

EX_PREFIX = ("EX_", "DM_", "SK_")
DEFAULT_FRACTION = 0.9999
DEFAULT_TOL = 1e-6
ROUND_N = 6

LABEL_OVERLAP = "区间重叠：点值差异=求解器伪影，禁止引用"
LABEL_A_HIGHER = "区间分离：条件 A 恒高于 B（解空间无关硬结论）"
LABEL_B_HIGHER = "区间分离：条件 B 恒高于 A（解空间无关硬结论）"


def judge_interval(la, ua, lb, ub, tol=DEFAULT_TOL):
    """区间分离判定（锁定公式）。输入四舍五入后的区间端点，返回 verdict dict。"""
    if ua + tol < lb:
        return {"overlap": False, "hard_conclusion": True, "direction": "b_higher"}
    if ub + tol < la:
        return {"overlap": False, "hard_conclusion": True, "direction": "a_higher"}
    return {"overlap": True, "hard_conclusion": False, "direction": None}


def _r(x):
    """FVA/pFBA 数值规整：None/NaN -> 0.0（可行模型下不应出现，防御性），-0.0 -> 0.0，保留 6 位。"""
    if x is None:
        return 0.0
    f = float(x)
    if f != f:  # NaN
        return 0.0
    v = round(f, ROUND_N)
    return 0.0 if v == 0.0 else v


def _setup_condition(model_path, cond, log=lambda s: None):
    """单条件：独立重读模型 + 介质 setup（对齐 validate G3）+ 底物（对齐 g4_phenotype）。
    返回 (model, info)；model 的 bounds 已按该条件设好，供 FBA/FVA/pFBA 使用。"""
    from cobra.flux_analysis import flux_variability_analysis, pfba
    name = cond.get("name")
    carbon_mode = cond.get("carbon_mode", "supplement")
    subs = cond.get("substrates") or []
    if carbon_mode not in ("supplement", "sole"):
        raise ValueError(f"condition {name}: unknown carbon_mode {carbon_mode!r}（supplement|sole）")

    t0 = time.time()
    m = silent_read_sbml(model_path)
    t_read = time.time() - t0

    med, preset = expand_medium(cond.get("medium"))
    resolved, unresolved = resolve_medium(m, med) if med else ({}, [])
    for r in m.reactions:
        if r.id.startswith(EX_PREFIX) or r.boundary:
            r.lower_bound = 0.0
    med2 = dict(resolved)
    if carbon_mode == "sole":
        # 去含碳交换（元素级 parse_formula——裸子串会把 Ca/Cl/Co/Cu 误判为碳，历史 bug 勿复刻）
        for rid in list(med2):
            if rid in m.reactions:
                met = list(m.reactions.get_by_id(rid).metabolites)[0]
                if met.formula and "C" in parse_formula(met.formula):
                    del med2[rid]
    ex_idx = build_ex_index(m)
    sub_ex, sub_unresolved = {}, []
    for sub in subs:
        exid = match_ex(sub, ex_idx)
        if exid and exid in m.reactions:
            med2[exid] = -10.0
            sub_ex[sub] = exid
        else:
            sub_unresolved.append(sub)
    for rid, lb in med2.items():
        if rid in m.reactions:
            m.reactions.get_by_id(rid).lower_bound = lb

    t0 = time.time()
    with m:
        wt = m.optimize().objective_value
    if wt is None:
        sys.stderr.write(f"[fluxscan] {name}: FBA infeasible -> growth treated as 0.0\n")
        wt = 0.0
    t_fba = time.time() - t0

    t0 = time.time()
    fva = flux_variability_analysis(m, fraction_of_optimum=DEFAULT_FRACTION
                                    if cond.get("_fraction") is None else cond["_fraction"],
                                    processes=1)
    t_fva = time.time() - t0

    t0 = time.time()
    sol = pfba(m)
    t_pfba = time.time() - t0

    info = {
        "name": name,
        "growth": round(float(wt), ROUND_N),
        "medium_preset": preset,
        "resolved_exchanges": sorted(med2),
        "unresolved": unresolved,
        "substrate_exchanges": sub_ex,
        "substrate_unresolved": sub_unresolved,
        "carbon_mode": carbon_mode,
        "_timing": {"read_s": round(t_read, 1), "fba_s": round(t_fba, 2),
                    "fva_s": round(t_fva, 1), "pfba_s": round(t_pfba, 1)},
    }
    data = {}
    pf = sol.fluxes
    for rid in fva.index:
        data[rid] = {
            "pfba": _r(pf.get(rid, 0.0)),
            "fva_min": _r(fva.at[rid, "minimum"]),
            "fva_max": _r(fva.at[rid, "maximum"]),
        }
    log(f"[fluxscan] condition {name}: growth={info['growth']} read={t_read:.1f}s "
        f"fba={t_fba:.2f}s fva={t_fva:.1f}s pfba={t_pfba:.1f}s "
        f"resolved={len(med2)} unresolved={unresolved} substrate_unresolved={sub_unresolved}")
    return data, info


def _pair_result(a_name, b_name, data_a, data_b, scope, tol, only_diff):
    """条件对比较：scope 反应逐一区间判定。summary 与 comparisons 同口径（同一 scope）。"""
    comparisons = []
    n_hard = n_a = n_b = 0
    for rid in scope:
        ra, rb = data_a[rid], data_b[rid]
        verdict = judge_interval(ra["fva_min"], ra["fva_max"], rb["fva_min"], rb["fva_max"], tol)
        if verdict["hard_conclusion"]:
            n_hard += 1
            if verdict["direction"] == "a_higher":
                n_a += 1
            else:
                n_b += 1
        if only_diff and not verdict["hard_conclusion"]:
            continue
        comparisons.append({
            "rxn": rid,
            "a": {"pfba": ra["pfba"], "fva_min": ra["fva_min"], "fva_max": ra["fva_max"]},
            "b": {"pfba": rb["pfba"], "fva_min": rb["fva_min"], "fva_max": rb["fva_max"]},
            "overlap": verdict["overlap"],
            "hard_conclusion": verdict["hard_conclusion"],
            "direction": verdict["direction"],
            "label": (LABEL_OVERLAP if verdict["overlap"]
                      else (LABEL_A_HIGHER if verdict["direction"] == "a_higher" else LABEL_B_HIGHER)),
        })
    summary = {"total": len(scope), "hard_conclusions": n_hard,
               "overlapping": len(scope) - n_hard, "a_higher": n_a, "b_higher": n_b}
    return {"a": a_name, "b": b_name, "summary": summary, "comparisons": comparisons}


def _export_csv(path, model_reactions, pair_results):
    """全量 CSV：每行 = 条件对 × 反应（双侧区间/点值/判定）。返回 (rows, bytes)。"""
    names = {r.id: r for r in model_reactions}
    rows = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["pair", "cond_a", "cond_b", "rxn", "reaction_name", "gpr",
                    "a_pfba", "a_fva_min", "a_fva_max",
                    "b_pfba", "b_fva_min", "b_fva_max",
                    "overlap", "hard_conclusion", "direction", "label"])
        for pr in pair_results:
            key = f"{pr['a']}|vs|{pr['b']}"
            for c in pr["comparisons"]:
                rxn = names.get(c["rxn"])
                w.writerow([key, pr["a"], pr["b"], c["rxn"],
                            (rxn.name if rxn is not None else ""),
                            (rxn.gpr if rxn is not None else ""),
                            c["a"]["pfba"], c["a"]["fva_min"], c["a"]["fva_max"],
                            c["b"]["pfba"], c["b"]["fva_min"], c["b"]["fva_max"],
                            int(c["overlap"]), int(c["hard_conclusion"]),
                            c["direction"] or "", c["label"]])
                rows += 1
    return rows, os.path.getsize(path)


def fluxscan(model_path, conditions, reactions=None, pairs=None,
             fraction_of_optimum=DEFAULT_FRACTION, tolerance=DEFAULT_TOL,
             only_diff=False, export_csv=None, progress=None):
    """M1 通量区间制主入口。conditions.name 必须唯一（重复直接报错）。
    FVA/pFBA 恒全模型计算；reactions 仅收窄 comparisons/summary 口径（两者同口径）。"""
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    if not conditions or not isinstance(conditions, list):
        raise ValueError("conditions required（非空数组，每项 {name, medium, substrates?, carbon_mode?}）")
    names = [c.get("name") for c in conditions]
    if any(not n for n in names):
        raise ValueError("every condition needs a name")
    if len(set(names)) != len(names):
        dup = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(f"conditions.name must be unique, duplicated: {dup}")

    t0 = time.time()
    m0 = silent_read_sbml(model_path)  # 仅用于 reaction 元数据/scope 校验
    all_ids = [r.id for r in m0.reactions]
    log(f"[fluxscan] model {model_path}: {len(all_ids)} reactions; "
        f"fraction_of_optimum={fraction_of_optimum} tolerance={tolerance}")

    if reactions:
        idset = set(all_ids)
        missing = [x for x in reactions if x not in idset]
        scope = [x for x in reactions if x in idset]  # 保持用户给定顺序
        if not scope:
            raise ValueError(f"none of the given reactions exist in model; missing={missing[:10]}")
    else:
        missing = []
        scope = all_ids

    cond_names = set(names)
    if pairs:
        for p in pairs:
            if len(p) != 2 or p[0] not in cond_names or p[1] not in cond_names:
                raise ValueError(f"invalid pair {p!r}: need two existing condition names")
    else:
        pairs = [[names[i], names[j]] for i in range(len(names)) for j in range(i + 1, len(names))]

    data_by_cond, info_by_cond = {}, {}
    for cond in conditions:
        cond = dict(cond)
        cond["_fraction"] = fraction_of_optimum
        data, info = _setup_condition(model_path, cond, log=log)
        data_by_cond[info["name"]] = data
        info_by_cond[info["name"]] = info

    pair_results = [_pair_result(p[0], p[1], data_by_cond[p[0]], data_by_cond[p[1]],
                                 scope, tolerance, only_diff) for p in pairs]
    total_s = round(time.time() - t0, 1)

    out = {
        "model": model_path,
        "fraction_of_optimum": fraction_of_optimum,
        "tolerance": tolerance,
        "units": "mmol/gDW/h",
        "reactions_not_found": missing,
        "conditions": [{k: v for k, v in info_by_cond[n].items() if not k.startswith("_")}
                       for n in names],
        "pairs": pair_results,
        "timing_seconds": total_s,
    }
    if export_csv:
        rows, size = _export_csv(export_csv, m0.reactions, pair_results)
        out["export_csv"] = export_csv
        out["export_csv_rows"] = rows
        out["export_csv_bytes"] = size
        log(f"[fluxscan] CSV {export_csv}: {rows} rows, {size} bytes")
    log(f"[fluxscan] done in {total_s}s: {len(pair_results)} pairs, scope={len(scope)}")
    return out


if __name__ == "__main__":
    # 双协议（历史坑：stdin 与 argv-file 两派并存，新脚本必须都支持）
    if "--selftest" in sys.argv:
        cases = [
            # (la, ua, lb, ub, tol, direction, hard)  —— 任务书 5 组判定样例
            (1.0, 2.0, 5.0, 6.0, 1e-6, "b_higher", True),      # ① 分离：b 高
            (5.0, 6.0, 1.0, 2.0, 1e-6, "a_higher", True),      # ① 分离：a 高
            (1.0, 3.0, 2.0, 4.0, 1e-6, None, False),           # ② 重叠（伪影）
            (1.0, 3.0, 3.0, 5.0, 1e-6, None, False),           # ② 接触容差内仍 overlap（边界）
            (-5.0, -2.0, 0.0, 0.0, 1e-6, "b_higher", True),    # ③ 零通量/负向
            (-5.0, -2.0, 0.0, 0.0, 0.0, "b_higher", True),     # ④ 精确边界（tol=0）
            # ⑤ 容差：锁定公式下 gap=5e-7 < tol=1e-6 -> overlap（任务书该行样例期望"分离"，
            #    与锁定公式 ua+tol<lb 矛盾：5e-7 不大于 1e-6。按公式实现，差异上报 bsp 裁决）
            (1.0, 1.0000005, 1.000001, 2.0, 1e-6, None, False),
            # 同数字、tol(1e-7)<gap(5e-7) -> 分离：证明容差机制本身生效
            (1.0, 1.0000005, 1.000001, 2.0, 1e-7, "b_higher", True),
            (0.0, 0.0, 3.0, 5.0, 1e-6, "b_higher", True),      # 附加：单侧有通量
            (3.0, 5.0, 0.0, 0.0, 1e-6, "a_higher", True),      # 附加：另一侧 [0,0]
        ]
        for la, ua, lb, ub, tol, d, h in cases:
            v = judge_interval(la, ua, lb, ub, tol)
            assert v["direction"] == d and v["hard_conclusion"] == h and v["overlap"] == (d is None), \
                f"judge_interval({la},{ua},{lb},{ub},tol={tol}) -> {v}, expect direction={d} hard={h}"
        print(json.dumps({"ok": True, "result": {"selftest": "pass", "cases": len(cases)}}))
    else:
        args = {}
        if len(sys.argv) > 1:
            with open(sys.argv[1], encoding="utf-8") as f:
                args = json.load(f)
        elif not sys.stdin.isatty():
            args = json.loads(sys.stdin.read())
        a = args.get("args", args)
        print(json.dumps({"ok": True, "result": fluxscan(
            a.get("model"), a.get("conditions"), reactions=a.get("reactions"),
            pairs=a.get("pairs"), fraction_of_optimum=a.get("fraction_of_optimum", DEFAULT_FRACTION),
            tolerance=a.get("tolerance", DEFAULT_TOL), only_diff=a.get("only_diff", False),
            export_csv=a.get("export_csv"))}, ensure_ascii=False))
