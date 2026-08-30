# double_knockout.py — 阶段C-C2 双敲 v1（合成致死预测，L2 非平凡）
# 候选池（预算可控）：① GPR 结构先验——纯 or 型且恰 2 基因的反应 = 穷尽型同工酶对（廉价必做）；
#   ② FVA 预筛活性反应关联基因中"共享反应"的基因对（复用 essential_scan.prescreen_candidates +
#   scan_essentiality；全扫受 max_pairs 预算上限，默认 5000，超限截断+报告）。
# 判定：单敲双活（>EPS）且双敲死（<=EPS）→ 合成致死对。单敲生长值按对惰性计算并缓存。
# 假设声明（方案文件要求，内置于输出与 description）：细菌双敲验证率无大规模实验数据支撑，
#   本结果=假设生成，供实验设计参考非结论。
# 退化护栏（阶段 A/B 教训）：wt<=EPS（介质下不生长）→ 不扫描不登记账本，degenerate:true + 介质适配提示。
import os
import re
import sys
import csv
import time
import json
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from silentio import silent_read_sbml
from essential_scan import setup_model_medium, scan_essentiality, EPS

ASSUMPTION_NOTE = "细菌双敲验证率无大规模实验数据支撑，本结果=假设生成，供实验设计参考非结论"


def _gpr_or_pairs(m):
    """穷尽型 or 同工酶对：纯 or GPR 且恰 2 个不同基因的反应。返回去重 {(a,b): [rxn...]}（a<b）。"""
    out = {}
    for r in m.reactions:
        gpr = str(r.gpr or "").strip()
        if not gpr or " and " in gpr.lower():
            continue
        genes = sorted({g.strip() for g in re.split(r"\bor\b", gpr, flags=re.I) if g.strip()})
        if len(genes) == 2:
            out.setdefault((genes[0], genes[1]), []).append(r.id)
    return out


def _double_growth(m, ga, gb):
    with m:
        m.genes.get_by_id(ga).knock_out()
        m.genes.get_by_id(gb).knock_out()
        v = m.optimize().objective_value
    return round(float(v), 6) if (v is not None and v == v) else 0.0


def _single_growth(m, gid):
    with m:
        m.genes.get_by_id(gid).knock_out()
        v = m.optimize().objective_value
    return round(float(v), 6) if (v is not None and v == v) else 0.0


def double_knockout(model_path, medium=None, max_pairs=5000, export_csv=None,
                    ledger_refs=True, ledger_path=None, progress=None):
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    medium = medium or {"medium_name": "AB"}
    t0 = time.time()
    m = silent_read_sbml(model_path)
    resolved, unresolved, preset = setup_model_medium(m, medium)
    with m:
        wt = m.optimize().objective_value
    wt = round(float(wt), 6) if wt is not None else 0.0
    log(f"[dk] {model_path} medium={medium} wt={wt} max_pairs={max_pairs}")

    out = {"model": model_path, "medium": medium, "medium_preset": preset,
           "wt_growth": wt, "units": "mmol/gDW/h", "assumption_note": ASSUMPTION_NOTE,
           "max_pairs": max_pairs, "eps": EPS,
           "degenerate": wt <= EPS}
    if out["degenerate"]:
        out["degenerate_note"] = (f"wt_growth={wt}<=EPS：被测模型在指定介质下不生长，双敲判定无意义，"
                                  "未扫描、未登记账本。提示：内置介质预设为根瘤菌科（C58）调校，"
                                  "非根瘤菌模型需先做介质适配（阶段B-B3 molybdate 教训）。")
        log(f"[dk] DEGENERATE wt={wt} <= EPS：不扫描不登记")
        return out

    # 1) 全量必需性扫描（复用 essential_scan 核心）：essential 集 + FVA 活性候选基因
    t_scan = time.time()
    scan = scan_essentiality(m, return_candidates=True)
    essential = set(scan["essential_genes"])
    alive = set(scan["candidate_genes"]) - essential
    log(f"[dk] scan done ({round(time.time()-t_scan,1)}s): tested={scan['tested_genes']} "
        f"essential={len(essential)} alive_singles={len(alive)}")

    # 2) 候选池
    prior = _gpr_or_pairs(m)
    pool = []  # (a, b, rationale)
    seen = set()
    for (a, b), rxns in sorted(prior.items()):
        if a in alive and b in alive:
            pool.append((a, b, f"GPR先验(穷尽型or同工酶，反应:{','.join(sorted(rxns)[:3])})"))
            seen.add((a, b))
    n_prior = len(pool)
    # ② 共享反应的存活基因对（确定性排序；超限截断）
    budget = max_pairs - n_prior
    scan_pairs_total = 0
    active_rxns = scan.get("active_rxn_ids") or []
    if budget > 0:
        shared = set()
        for rid in sorted(active_rxns):
            gset = sorted(g for g in (x.id for x in m.reactions.get_by_id(rid).genes)
                          if g in alive)
            for a, b in combinations(gset, 2):
                if (a, b) not in seen:
                    shared.add((a, b))
        shared = sorted(shared)
        scan_pairs_total = len(shared)
        for a, b in shared[:budget]:
            pool.append((a, b, "全扫(共享活性反应)"))
    truncated = scan_pairs_total > max(0, budget)
    log(f"[dk] pool: prior={n_prior} scan_total={scan_pairs_total} "
        f"scan_tested={min(scan_pairs_total, max(0, budget))} truncated={truncated}")

    # 3) 逐对判定：双敲 LP；SL -> 补算两个单敲值（缓存）
    results = []
    singles = {}
    tested = 0
    for a, b, rationale in pool:
        tested += 1
        gd = _double_growth(m, a, b)
        if gd <= EPS:
            for g in (a, b):
                if g not in singles:
                    singles[g] = _single_growth(m, g)
            results.append({"pair": [a, b], "single_a_growth": singles[a],
                            "single_b_growth": singles[b], "double_growth": gd,
                            "rationale": rationale, "source": "gem_double_knockout"})
            log(f"[dk] SL {a} x {b} (double={gd}, {rationale[:24]})")
        if tested % 500 == 0:
            log(f"[dk] tested {tested}/{len(pool)}, SL so far {len(results)}")

    # 4) 账本登记（每对一条 type=synthetic_lethal；幂等）
    reg = None
    if ledger_refs and results:
        try:
            import ledger as _ledger
            from model_card import load_card as _load_card
            lineage_v = ((_load_card(model_path) or {}).get("model_lineage") or {}).get("version")
            cond = preset or (f"custom({len(resolved)} EX)" if resolved else "unspecified")
            pair_rows = [{"gene_a": r["pair"][0], "gene_b": r["pair"][1]} for r in results]
            reg = _ledger.register_synthetic_lethal(model_path, pair_rows, condition=cond,
                                                    lineage_version=lineage_v, path=ledger_path)
            log(f"[dk] ledger: appended={reg['appended']} skipped={reg['skipped_duplicates']}")
        except Exception as e:
            sys.stderr.write(f"[dk] ledger registration WARN: {type(e).__name__}: {e}\n")

    out.update({
        "genes_tested_single": scan["tested_genes"],
        "essential_count": len(essential),
        "alive_singles": len(alive),
        "pairs_tested": tested,
        "pairs_found": len(results),
        "budget": {"prior_pairs": n_prior, "scan_pairs_total": scan_pairs_total,
                   "scan_pairs_tested": min(scan_pairs_total, max(0, budget)),
                   "truncated": truncated},
        "results": results,
        "timing_seconds": round(time.time() - t0, 1),
    })
    if reg is not None:
        out["ledger_registration"] = reg
    if export_csv:
        n = _export_csv(export_csv, results, out)
        out["export_csv"] = export_csv
        out["export_csv_rows"] = n
        out["export_csv_bytes"] = os.path.getsize(export_csv)
        log(f"[dk] CSV {export_csv}: {n} rows")
    return out


def _export_csv(path, results, out):
    n = 0
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["# assumption", out["assumption_note"]])
        w.writerow(["gene_a", "gene_b", "single_a_growth", "single_b_growth",
                    "double_growth", "rationale", "source"])
        for r in results:
            w.writerow([r["pair"][0], r["pair"][1], r["single_a_growth"],
                        r["single_b_growth"], r["double_growth"], r["rationale"], r["source"]])
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
    print(json.dumps({"ok": True, "result": double_knockout(
        a.get("model"), medium=a.get("medium"), max_pairs=a.get("max_pairs", 5000),
        export_csv=a.get("export_csv"), ledger_refs=a.get("ledger_refs", True),
        ledger_path=a.get("ledger_path"))}, ensure_ascii=False))
