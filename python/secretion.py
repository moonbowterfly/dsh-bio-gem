# secretion.py — 阶段C-C1 可分泌代谢物谱（菌种通用）
# 候选 = 介质层两级策略导出的交换反应（build_ex_index：EX_ 型与 boundary 型模型都适用）。
# 可分泌判定 = production envelope 扫描：固定生长分数 {0.25,0.5,0.75,0.9,0.99,1.0} 下产物交换最大化
#   （强制 biomass 通量 >= fraction*wt，最大化交换反应的分泌方向通量）；
#   任一分数 >0 且产物交换 > 1e-6 → 可分泌。
# 边界声明（方案文件要求，内置于输出）：未考虑毒性/渗透压/调控，纯拓扑/线性规划结果。
# 退化护栏（阶段 A/B 教训）：被测模型 wt<=EPS（介质下不生长，如 AB 预设对非根瘤菌科物种——
#   阶段B-B3 molybdate 教训）→ 不扫描不登记账本，输出 degenerate:true + 介质适配提示。
import os
import sys
import csv
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from silentio import silent_read_sbml
from gapfind import build_ex_index, ex_index_is_boundary
from essential_scan import setup_model_medium
from sensitivity import find_biomass_gam

EPS = 1e-6
GROWTH_FRACTIONS = [0.25, 0.5, 0.75, 0.9, 0.99, 1.0]
BOUNDARY_NOTE = "未考虑毒性/渗透压/调控，纯拓扑/线性规划结果"


def _envelope_for(m, bio, rxn, wt, fractions):
    """单候选 envelope：各生长分数下分泌方向最大通量。返回 (env_rows, max_prod, growth_at_max)。"""
    met = list(rxn.metabolites)[0]
    c = rxn.metabolites[met]
    env, best, best_f = [], 0.0, None
    for f in fractions:
        with m:
            bio.lower_bound = f * wt
            m.objective = rxn
            m.objective_direction = "max" if c < 0 else "min"
            v = m.slim_optimize()
        prod = round(abs(v), 6) if (v is not None and v == v) else 0.0
        env.append({"fraction": f, "prod": prod})
        if prod > best:
            best, best_f = prod, f
    return env, best, (best_f if best_f is not None else 0.0)


def secretion(model_path, medium=None, fractions=None, export_csv=None,
              ledger_refs=True, ledger_path=None, progress=None):
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    medium = medium or {"medium_name": "AB"}
    fractions = sorted(fractions or GROWTH_FRACTIONS)
    t0 = time.time()
    m = silent_read_sbml(model_path)
    idx = build_ex_index(m)
    boundary_style = ex_index_is_boundary(idx)
    resolved, unresolved, preset = setup_model_medium(m, medium)
    gi = find_biomass_gam(m)
    bio = m.reactions.get_by_id(gi["biomass_rxn"])
    with m:
        wt = m.optimize().objective_value
    wt = round(float(wt), 6) if wt is not None else 0.0
    log(f"[secretion] {model_path} medium={medium} wt={wt} candidates={len(set(idx.values()))} "
        f"boundary_style={boundary_style}")

    out = {
        "model": model_path, "medium": medium, "medium_preset": preset,
        "units": "mmol/gDW/h", "boundary_note": BOUNDARY_NOTE,
        "growth_fractions": fractions, "boundary_style": boundary_style,
        "candidates": len(set(idx.values())), "unresolved_medium": unresolved,
        "wt_growth": wt,
        "degenerate": wt <= EPS,
    }
    if out["degenerate"]:
        out["degenerate_note"] = (f"wt_growth={wt}<=EPS：被测模型在指定介质下不生长，production envelope 无意义，"
                                  "未扫描、未登记账本。提示：内置介质预设为根瘤菌科（C58）调校，非根瘤菌模型需先做介质适配"
                                  "（阶段B-B3 教训：iML1515 严格 AB 缺 molybdate）。")
        try:
            from benchmark import medium_adaptation_hints
            out["medium_adaptation_hints"] = medium_adaptation_hints(model_path, medium)
        except Exception as e:
            sys.stderr.write(f"[secretion] hints WARN: {type(e).__name__}: {e}\n")
        log(f"[secretion] DEGENERATE wt={wt} <= EPS：不扫描不登记")
        return out

    rows = []
    for rid in sorted(set(idx.values())):
        rxn = m.reactions.get_by_id(rid)
        mets = list(rxn.metabolites)
        if len(mets) != 1:
            continue  # 交换候选恒单代谢物；多代谢物防御性跳过
        env, best, best_f = _envelope_for(m, bio, rxn, wt, fractions)
        met = mets[0]
        rows.append({"rxn": rid, "met_id": met.id, "name": met.name or "",
                     "max_prod": best, "growth_at_max": round(best_f * wt, 6),
                     "feasible": best > EPS, "envelope": env})
    feasible = [r for r in rows if r["feasible"]]
    log(f"[secretion] scan done: {len(rows)} candidates, {len(feasible)} feasible "
        f"({round(time.time() - t0, 1)}s)")

    # 账本登记（每个可分泌代谢物一条 type=secretion；幂等 sha256 去重）
    reg = None
    if ledger_refs:
        try:
            import ledger as _ledger
            from model_card import load_card as _load_card
            lineage_v = ((_load_card(model_path) or {}).get("model_lineage") or {}).get("version")
            cond = preset or (f"custom({len(resolved)} EX)" if resolved else "unspecified")
            reg = _ledger.register_secretion(model_path, feasible, condition=cond,
                                             lineage_version=lineage_v, path=ledger_path)
            out["ledger_registration"] = reg
            log(f"[secretion] ledger: appended={reg['appended']} skipped={reg['skipped_duplicates']}")
        except Exception as e:
            sys.stderr.write(f"[secretion] ledger registration WARN: {type(e).__name__}: {e}\n")

    out.update({
        "secretable_count": len(feasible),
        "results": rows,
        "timing_seconds": round(time.time() - t0, 1),
    })
    if reg is not None:
        out["ledger_registration"] = reg
    if export_csv:
        n = _export_csv(export_csv, rows, out)
        out["export_csv"] = export_csv
        out["export_csv_rows"] = n
        out["export_csv_bytes"] = os.path.getsize(export_csv)
        log(f"[secretion] CSV {export_csv}: {n} rows")
    return out


def _export_csv(path, rows, out):
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["# boundary_note", out["boundary_note"]])
        w.writerow(["rxn", "met_id", "name", "feasible", "max_prod", "growth_at_max",
                    "fraction", "prod"])
        for r in rows:
            for e in r["envelope"]:
                w.writerow([r["rxn"], r["met_id"], r["name"], int(r["feasible"]),
                            r["max_prod"], r["growth_at_max"], e["fraction"], e["prod"]])
                n += 1
    return n


if __name__ == "__main__":
    args = {}
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            args = json.load(f)
    elif not sys.stdin.isatty():
        args = json.loads(sys.stdin.read())
    a = args.get("args", args)
    print(json.dumps({"ok": True, "result": secretion(
        a.get("model"), medium=a.get("medium"), fractions=a.get("fractions"),
        export_csv=a.get("export_csv"), ledger_refs=a.get("ledger_refs", True),
        ledger_path=a.get("ledger_path"))}, ensure_ascii=False))
