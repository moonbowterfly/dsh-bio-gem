# benchmark.py — 阶段B-B1 gem_benchmark 通用基准对比（与物种无关）
# 六件通用能力：①介质解析两级策略复用（gapfind.build_ex_index 回退 boundary）②biomass 可行性探针
# ③六关 G1-G6 并列 ④必需性对比+差异归因（退化侧只报结构不做垃圾对比）⑤账本 comparison_refs 回填
# （update 语义幂等可重入）⑥可复现性评估。
# 纪律：reference_essential（如 iNX1344 论文 195）只做报告标注，严禁冒充模型输出；
#       任一侧 wt_growth<=EPS 时该侧 essential 集退化（判定恒真），不做差异对比。
import os
import sys
import time
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from silentio import silent_read_sbml
from gapfind import expand_medium, resolve_medium, build_ex_index, \
    ex_index_is_boundary, has_ex_layer, ex_display_name
from essential_scan import setup_model_medium, scan_essentiality
from sensitivity import find_biomass_gam
from validate import Validator

EPS = 1e-6
UNITS_NOTE = "growth=mmol/gDW/h；必需判定=敲除生长<1e-6"
DEG_MSG = "wt<=EPS：必需性判定恒真（v=0 使全部候选判'必需'），essential 集无生物学意义"


# ---------------------------------------------------------------------------
# 通用件
# ---------------------------------------------------------------------------
def detect_id_system(m):
    """ID 体系探测（启发式，如实描述）。返回 {genes, reactions, metabolites, ex_style, counts}。"""
    g_ids = [g.id for g in list(m.genes)[:20]]
    r_ids = [r.id for r in m.reactions[:50]]
    m_ids = [x.id for x in m.metabolites[:50]]

    def _label(samples, rules):
        for pat, label in rules:
            if samples and all(s.startswith(pat) for s in samples):
                return label
        return samples[0] if samples else "n/a"

    genes_label = _label(g_ids, [("NC_", "NC_XXXXXX_N（RefSeq 复制子 locus）"), ("Atu", "AtuXXXX"),
                                 ("b", "bXXXX（BiGG）"), ("WP", "WP_XXXX")])
    rxn_label = _label(r_ids, [("Rnxatu", "RnxatuXXXX"), ("rxn", "rxnXXXXX_c0"), ("R_", "R_XXXX"),
                               ("EX_", "EX_XXXX")])
    met_label = _label(m_ids, [("cpd", "cpdXXXXXX_c0"), ("M0", "M00XXX_c"), ("M_", "M_XXXX_c")])
    n_ex = sum(1 for r in m.reactions if r.id.startswith("EX_"))
    n_bnd = sum(1 for r in m.reactions if r.boundary and len(r.metabolites) == 1)
    if n_ex:
        ex_style = f"EX_（{n_ex} 个前缀交换反应）"
    else:
        ex_style = f"boundary（{n_bnd} 个单代谢物 boundary 反应，无 EX_ 前缀）"
    return {"genes": genes_label, "reactions": rxn_label, "metabolites": met_label,
            "ex_style": ex_style, "ex_reactions": n_ex, "boundary_reactions": n_bnd}


def growth_on(model_path, medium):
    """G3 同款介质 setup + wt FBA。返回 (wt, resolved, unresolved, preset, boundary_style)。"""
    m = silent_read_sbml(model_path)
    resolved, unresolved, preset = setup_model_medium(m, medium)
    boundary_style = ex_index_is_boundary(build_ex_index(m))
    with m:
        wt = m.optimize().objective_value
    return (round(float(wt), 6) if wt is not None else 0.0), resolved, unresolved, preset, boundary_style


def biomass_probe(model_path):
    """biomass 可行性探针（M5 诊断正规化）：全交换开放（lb=-10）下逐组分 demand 可净产测试。
    输出不可净产组分清单——结构性断供与介质选择无关（通用体检项）。"""
    import cobra
    m = silent_read_sbml(model_path)
    gi = find_biomass_gam(m)
    bio = m.reactions.get_by_id(gi["biomass_rxn"])
    for r in m.reactions:
        if r.id.startswith(("EX_", "DM_", "SK_")) or r.boundary:
            r.lower_bound = -10.0
            r.upper_bound = 1000.0
    unproducible = []
    for met in list(bio.metabolites):
        dm = cobra.Reaction("DM_bench_probe_tmp", lower_bound=0.0, upper_bound=1000.0)
        dm.add_metabolites({met: -1.0})
        m.add_reactions([dm])
        m.objective = dm
        s = m.optimize()
        if s.objective_value is None or s.objective_value <= 1e-9:
            unproducible.append(met.id)
        m.remove_reactions([dm])
    return {"object_id": bio.id, "components": len(bio.metabolites),
            "gam_carrier": {"type": gi["carrier_type"], "gam_orig": gi["gam_orig"]},
            "unproducible": sorted(unproducible),
            "probe_note": "全交换开放（lb=-10）下逐组分 demand 净产测试；不可净产=结构性断供，与介质选择无关"}


def essential_sample_scan(model_path, medium, sample_size=40):
    """抽检模式（G5 口径）：确定性步进抽样 + 直接敲除（免 FVA 预筛）。"""
    m = silent_read_sbml(model_path)
    resolved, unresolved, preset = setup_model_medium(m, medium)
    with m:
        wt = m.optimize().objective_value
    wt = round(float(wt), 6) if wt is not None else 0.0
    all_ids = sorted(g.id for g in m.genes)
    stride = max(1, len(all_ids) // max(1, sample_size))
    subset = sorted(all_ids[::stride][:sample_size])
    essential = []
    for gid in subset:
        try:
            with m:
                m.genes.get_by_id(gid).knock_out()
                v = m.optimize().objective_value
        except KeyError:
            continue
        if v is not None and v < EPS:
            essential.append(gid)
    return {"wt_growth": wt, "essential_genes": essential, "tested_genes": len(subset),
            "sample_size": sample_size, "mode": "sample(G5 口径：确定性步进抽样直敲 40)"}


def _essential_full_scan(model_path, medium, log, tag):
    m = silent_read_sbml(model_path)
    setup_model_medium(m, medium)
    r = scan_essentiality(m)
    log(f"[bench] essentiality {tag}: wt={r['wt_growth']} essential={r['essential_count']}")
    return {"wt_growth": r["wt_growth"], "essential_genes": r["essential_genes"],
            "tested_genes": r["tested_genes"], "mode": "full(essential_scan 可复用函数)"}


def map_genes(genes, model_b):
    """基因映射尽力而为（与物种无关）：策略1 identity（同 id）；策略2 gene.name 匹配。
    反应桥（EC/名字）需两侧注释充分；本机两命名空间注释层不足时不启用，如实报告。"""
    b_by_id = {g.id for g in model_b.genes}
    b_by_name = {}
    for g in model_b.genes:
        if g.name:
            b_by_name.setdefault(g.name, g.id)
    mapping, unmapped = {}, []
    for gid in genes:
        if gid in b_by_id:
            mapping[gid] = gid
        elif gid in b_by_name:
            mapping[gid] = b_by_name[gid]
        else:
            unmapped.append(gid)
    strategy = "identity(gene id 同名) + gene.name"
    note = ("跨命名空间（如 NC_ RefSeq locus vs Atu 旧 locus）无离线直桥时覆盖率如实为低值；"
            "反应桥（EC/名字等价类）需两侧注释充分，注释层不足未启用（不硬造映射）。")
    return {"mapping": mapping, "unmapped": unmapped, "strategy": strategy, "note": note}


def essentiality_compare(path_a, path_b, medium, essential_full, log):
    """必需性对比。任一侧 wt<=EPS -> 该侧退化：只报结构信息，不做差异对比（严禁 155 vs 1066 垃圾对比）。
    返回 (输出 dict, a 原始 essential set, b 原始 essential set)。"""
    log(f"[bench] essentiality scan a ({'full' if essential_full else 'sample40'})...")
    a = _essential_full_scan(path_a, medium, log, "a") if essential_full \
        else essential_sample_scan(path_a, medium, 40)
    log("[bench] essentiality scan b ...")
    b = _essential_full_scan(path_b, medium, log, "b") if essential_full \
        else essential_sample_scan(path_b, medium, 40)

    out = {"full": bool(essential_full), "mode": a["mode"],
           "a_wt": a["wt_growth"], "b_wt": b["wt_growth"],
           "a_count": len(a["essential_genes"]), "b_count": len(b["essential_genes"]),
           "a_tested": a["tested_genes"], "b_tested": b["tested_genes"],
           "a_degenerate": a["wt_growth"] <= EPS, "b_degenerate": b["wt_growth"] <= EPS}
    if out["a_degenerate"]:
        out["a_note"] = DEG_MSG
    if out["b_degenerate"]:
        out["b_note"] = DEG_MSG

    a_set, b_set = set(a["essential_genes"]), set(b["essential_genes"])
    if out["a_degenerate"] or out["b_degenerate"]:
        out["mapping"] = {"strategy": "not_attempted（存在退化侧）", "covered_genes": 0,
                          "coverage_ratio": 0.0, "unmapped_genes": [],
                          "map_note": "任一侧 wt<=EPS 时必需集退化，差异对比与映射无意义，已按契约跳过"}
        out["intersection"] = None
        out["union"] = None
        out["a_only"] = None
        out["b_only"] = None
        return out, a_set, b_set

    mp = map_genes(sorted(a_set), silent_read_sbml(path_b))
    fwd = mp["mapping"]
    b_inv = {v: k for k, v in fwd.items()}
    a_set_m = {g for g in a_set if g in fwd}
    inter = {g for g in a_set_m if fwd[g] in b_set}
    a_only = {g for g in a_set_m if fwd[g] not in b_set}
    b_only = {gb for gb in b_set if b_inv.get(gb) is None or b_inv[gb] not in a_set}
    out["mapping"] = {"strategy": mp["strategy"], "covered_genes": len(fwd),
                      "coverage_ratio": round(len(fwd) / max(1, len(a_set)), 4),
                      "unmapped_genes": mp["unmapped"], "map_note": mp["note"]}
    out["intersection"] = len(inter)
    out["union"] = len(a_set | b_set)
    out["a_only"] = sorted(a_only)
    out["b_only"] = sorted(b_only)
    out["compare_note"] = "差异分析在可映射子集上进行；unmapped 基因单列不参与判定。"
    return out, a_set, b_set


def phenotype_compare(path_a, path_b, medium, table_path, log):
    """G4 表型对比（sole 语义，对齐 gem_phenotype 缺口检测口径）。"""
    out, tables = {}, {}
    for tag, path in (("a", path_a), ("b", path_b)):
        v = Validator(path)
        med, _ = expand_medium(medium)
        resolved, _ = resolve_medium(v.m, med) if med else ({}, [])
        g4 = v.g4_phenotype(table_path=table_path, medium=resolved, carbon_mode="sole")
        tables[tag] = {r["substrate"]: r for r in (g4.get("results") or [])}
        out[tag] = {"matched": g4.get("matched"), "total": g4.get("total"),
                    "rate": g4.get("rate"), "carbon_mode": "sole"}
        log(f"[bench] phenotype {tag}: {g4.get('matched')}/{g4.get('total')}")
    table = []
    for s in sorted(set(tables["a"]) | set(tables["b"])):
        ra_, rb_ = tables["a"].get(s), tables["b"].get(s)
        table.append({"substrate": s, "published": (ra_ or rb_ or {}).get("published"),
                      "a_predicted": (ra_ or {}).get("predicted"), "a_growth": (ra_ or {}).get("growth"),
                      "b_predicted": (rb_ or {}).get("predicted"), "b_growth": (rb_ or {}).get("growth"),
                      "diff": (ra_ or {}).get("predicted") != (rb_ or {}).get("predicted")})
    out["table"] = table
    return out


def backfill_ledger(ledger_path, b_path, b_essential_set, b_degenerate, gene_mapping,
                    phenotype_table_rows, log):
    """账本 comparison_refs 回填（update 语义，不新增行；幂等可重入——同 model_b 旧条目替换）。
    essentiality 行：b 退化或基因无映射 -> no_equivalent；有映射 -> agreed/disagreed（附 b 证据）。
    phenotype 行：按底物在 b 表型对比表中找等价预测 -> agreed/disagreed。"""
    import ledger as _ledger_mod
    rows, corrupt = _ledger_mod.load_rows(ledger_path)
    stats = {"total_predictions_checked": len(rows), "corrupt_rows": len(corrupt),
             "agreed": 0, "disagreed": 0, "no_equivalent": 0, "updated_rows": 0}
    if not rows:
        stats["note"] = "账本为空"
        return stats
    pheno_b = {r["substrate"]: r for r in (phenotype_table_rows or [])
               if r.get("b_predicted") is not None}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    for row in rows:
        rtype = row.get("type")
        verdict, evidence = "no_equivalent", {}
        if rtype == "essentiality":
            content = row.get("content") or ""
            gid = content.split(" 在 ")[0].strip()
            if b_degenerate:
                evidence = {"reason": "b 侧 essentiality 退化（wt<=EPS），无有效等价预测"}
            else:
                bid = (gene_mapping or {}).get(gid)
                if bid is None:
                    evidence = {"reason": f"gene {gid} 无跨命名空间映射"}
                else:
                    b_flag = bid in (b_essential_set or set())
                    verdict = "agreed" if b_flag else "disagreed"
                    evidence = {"b_gene": bid, "b_essential": b_flag}
        elif rtype == "phenotype":
            content = row.get("content") or ""
            sub = content.split("底物 ")[-1].split(" 预测")[0].strip() if "底物 " in content else ""
            brow = pheno_b.get(sub)
            a_pred = 1 if "预测生长（" in content else (0 if "预测不生长" in content else None)
            if brow is None or a_pred is None:
                evidence = {"reason": f"底物 {sub} 在 b 表型对比中无等价结果"}
            else:
                verdict = "agreed" if a_pred == brow["b_predicted"] else "disagreed"
                evidence = {"b_substrate": sub, "b_predicted": brow["b_predicted"],
                            "b_growth": brow["b_growth"]}
        stats[verdict] += 1
        entry = {"model_b": b_path, "verdict": verdict, "evidence": evidence, "at": now}
        old = row.get("comparison_refs") or []
        new_refs = [e for e in old if e.get("model_b") != b_path] + [entry]
        if new_refs != old:  # 幂等：同内容不更新不计数
            r = _ledger_mod.update_row(row.get("prediction_id"), comparison_refs=new_refs,
                                       path=ledger_path)
            if r.get("ok"):
                stats["updated_rows"] += 1
            else:
                log(f"[bench] ledger update WARN {row.get('prediction_id')}: {r.get('error')}")
    return stats


def reproducibility(gates_report, probe):
    """可复现性评估：加载性/注释覆盖/结构性发现（notes 由真实发现自动生成）。"""
    notes = []
    g1, g2 = gates_report.get("g1") or {}, gates_report.get("g2") or {}
    n_unprod = len(probe.get("unproducible") or [])
    if n_unprod:
        notes.append(f"biomass_probe: {n_unprod} 个 biomass 组分不可净产（结构性断供，详见 biomass_probe）")
    fc = g2.get("metabolite_formula_coverage")
    if fc is not None and fc < 0.9:
        notes.append(f"formula 覆盖率 {fc}（<0.9，影响元素平衡与能量 stub 识别）")
    gc = g1.get("gpr_gene_coverage")
    if gc is not None and gc < 0.8:
        notes.append(f"GPR 基因覆盖 {gc}（<0.8）")
    return {"loadable": True, "gpr_coverage": gc, "formula_coverage": fc,
            "reactions": g1.get("reactions"), "genes": g1.get("genes"), "notes": notes}


def write_md(path, out):
    """论文级 Markdown 落盘。"""
    L = []
    L.append("# GEM 基准对比报告\n")
    L.append(f"- **Model A**: `{out['model_a']}`")
    L.append(f"- **Model B**: `{out['model_b']}`")
    L.append(f"- **Medium**: `{json.dumps(out['medium'], ensure_ascii=False)}`")
    L.append(f"- **Units**: {out['units_note']}")
    L.append(f"- **Generated**: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n")
    L.append("## 1. ID 体系\n")
    L.append("| 维度 | A | B |")
    L.append("|---|---|---|")
    for k in ("genes", "reactions", "metabolites", "ex_style"):
        L.append(f"| {k} | {out['id_systems']['a'][k]} | {out['id_systems']['b'][k]} |")
    L.append("\n## 2. 六道验证关卡（G1-G6 并列）\n")
    L.append("| Gate | A | B |")
    L.append("|---|---|---|")
    for g in out["gates"]:
        sa = json.dumps(g["a"], ensure_ascii=False)[:150] if g["a"] else "n/a"
        sb = json.dumps(g["b"], ensure_ascii=False)[:150] if g["b"] else "n/a"
        L.append(f"| {g['gate']} | `{sa}` | `{sb}` |")
    L.append("\n## 3. 生长（声明介质，单点 FBA 口径）\n")
    for tag, name in (("a", "A"), ("b", "B")):
        g = out["growth"][tag]
        L.append(f"- **{name}**: growth=**{g['growth']}** mmol/gDW/h, resolved={g['resolved_exchanges']}, "
                 f"boundary_style={g['boundary_style']}, unresolved={g['unresolved']}")
        if g.get("resolved_display"):
            L.append(f"  - resolved_display: {g['resolved_display']}")
    L.append("\n## 4. Biomass 可行性探针（结构性断供，与介质无关）\n")
    for tag, name in (("a", "A"), ("b", "B")):
        p = out["biomass_probe"][tag]
        L.append(f"- **{name}**: biomass=`{p['object_id']}` ({p['components']} 组分, "
                 f"GAM carrier={p['gam_carrier']['type']}/GAM_ORIG={p['gam_carrier']['gam_orig']})")
        L.append(f"  - unproducible（不可净产）: {p['unproducible']}")
    e = out["essentiality"]
    L.append("\n## 5. 必需性对比\n")
    L.append(f"- mode={e['mode']}")
    L.append(f"- A: wt={e['a_wt']}, essential={e['a_count']}/{e['a_tested']}, degenerate={e['a_degenerate']}")
    L.append(f"- B: wt={e['b_wt']}, essential={e['b_count']}/{e['b_tested']}, degenerate={e['b_degenerate']}")
    if e.get("a_note"):
        L.append(f"- A note: {e['a_note']}")
    if e.get("b_note"):
        L.append(f"- B note: {e['b_note']}")
    if e.get("intersection") is not None:
        mp = e["mapping"]
        L.append(f"- mapping: {mp['strategy']} coverage={mp['coverage_ratio']} "
                 f"({mp['covered_genes']} mapped / {len(mp['unmapped_genes'])} unmapped)")
        L.append(f"- intersection={e['intersection']}, union={e['union']}, "
                 f"a_only={len(e['a_only'] or [])}, b_only={len(e['b_only'] or [])}")
        if e["a_only"]:
            L.append(f"  - a_only: {e['a_only'][:20]}")
        if e["b_only"]:
            L.append(f"  - b_only: {e['b_only'][:20]}")
    if e["a_degenerate"] or e["b_degenerate"]:
        L.append("- **差异对比已按契约跳过（存在退化侧），仅报结构信息——严禁 155 vs 1066 式无意义对比**")
    if out.get("reference_essential"):
        L.append(f"- 文献值标注（不参与计算）：`{json.dumps(out['reference_essential'], ensure_ascii=False)}`")
    if out.get("phenotype"):
        p = out["phenotype"]
        L.append("\n## 6. 表型对比（G4 sole 语义）\n")
        L.append(f"- A: {p['a']['matched']}/{p['a']['total']} ({p['a']['rate']}); "
                 f"B: {p['b']['matched']}/{p['b']['total']} ({p['b']['rate']})\n")
        L.append("| substrate | published | A_pred | A_growth | B_pred | B_growth | diff |")
        L.append("|---|---|---|---|---|---|---|")
        for r in p["table"]:
            L.append(f"| {r['substrate']} | {r['published']} | {r['a_predicted']} | {r['a_growth']} | "
                     f"{r['b_predicted']} | {r['b_growth']} | {r['diff']} |")
    L.append("\n## 7. 可复现性评估\n")
    for tag, name in (("a", "A"), ("b", "B")):
        r = out["reproducibility"][tag]
        L.append(f"- **{name}**: loadable={r['loadable']}, GPR 覆盖={r['gpr_coverage']}, "
                 f"formula 覆盖={r['formula_coverage']}, reactions={r['reactions']}, genes={r['genes']}")
        for n in r["notes"]:
            L.append(f"  - {n}")
    if out.get("comparison_refs"):
        c = out["comparison_refs"]
        L.append("\n## 8. 账本 comparison_refs 回填（update 语义，幂等可重入）\n")
        L.append(f"- checked={c['total_predictions_checked']}, agreed={c['agreed']}, "
                 f"disagreed={c['disagreed']}, no_equivalent={c['no_equivalent']}, "
                 f"updated_rows={c['updated_rows']}")
    L.append("\n---\n*generated by gem_benchmark*")
    data = "\n".join(L) + "\n"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(data)
    return len(data)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def benchmark(model_a, model_b, medium=None, phenotype_table=None, reference_essential=None,
              essential_full=False, ledger_refs=True, export_md=None, ledger_path=None,
              progress=None):
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    medium = medium or {"medium_name": "AB"}
    t0 = time.time()
    log(f"[bench] A={model_a}")
    log(f"[bench] B={model_b}")
    log(f"[bench] medium={medium} essential_full={essential_full} ledger_refs={ledger_refs}")

    # ①③ 六关并列
    gate_reports = {}
    for tag, path in (("a", model_a), ("b", model_b)):
        rep = Validator(path).run(medium=medium)
        gate_reports[tag] = rep
        log(f"[bench] gates {tag}: overall={rep.get('overall')} "
            f"g3={rep['g3']['status']}/{rep['g3']['growth_medium']}")
    gates = [{"gate": g, "a": gate_reports["a"].get(g), "b": gate_reports["b"].get(g)}
             for g in ("g1", "g2", "g3", "g4", "g5", "g6")]

    # growth（介质解析计数 + boundary 规范展示）
    growth = {}
    for tag, path in (("a", model_a), ("b", model_b)):
        wt, resolved, unresolved, preset, bstyle = growth_on(path, medium)
        entry = {"growth": wt, "resolved_exchanges": len(resolved), "unresolved": unresolved,
                 "medium_preset": preset, "boundary_style": bstyle, "units": "mmol/gDW/h",
                 "point_value_note": "单点 FBA 值，非解空间硬结论；条件间对比用 gem_fluxscan（区间制）"}
        if bstyle:
            entry["resolved_display"] = [ex_display_name(silent_read_sbml(path), rid)
                                         for rid in sorted(resolved)]
        growth[tag] = entry
        log(f"[bench] growth {tag}: {wt} resolved={len(resolved)} boundary_style={bstyle}")

    # ② biomass 探针
    probe = {}
    for tag, path in (("a", model_a), ("b", model_b)):
        probe[tag] = biomass_probe(path)
        log(f"[bench] biomass_probe {tag}: {probe[tag]['object_id']} components={probe[tag]['components']} "
            f"unproducible={len(probe[tag]['unproducible'])}")

    # ④ 必需性对比（退化护栏前置）
    ess, a_set, b_set = essentiality_compare(model_a, model_b, medium, essential_full, log)

    # ⑤⑥ 表型对比 + 可复现性
    pheno = phenotype_compare(model_a, model_b, medium, phenotype_table, log) \
        if phenotype_table and os.path.exists(phenotype_table) else None
    repro = {tag: reproducibility(gate_reports[tag], probe[tag])
             for tag in ("a", "b")}
    if not has_ex_layer(silent_read_sbml(model_b)):
        repro["b"]["notes"].insert(
            0, "无 EX_ 前缀交换层（boundary 单代谢物反应充当交换），介质层按两级策略回退（boundary_style=true）")

    # ⑤ 账本回填（update 语义幂等；全账本基因一次映射）
    refs_stats = None
    if ledger_refs:
        import ledger as _ledger_mod
        rows, _ = _ledger_mod.load_rows(ledger_path)
        a_genes = sorted({(r.get("content") or "").split(" 在 ")[0].strip()
                          for r in rows if r.get("type") == "essentiality"})
        mp_all = map_genes(a_genes, silent_read_sbml(model_b))
        refs_stats = backfill_ledger(ledger_path, model_b, b_set, ess["b_degenerate"],
                                     mp_all["mapping"], (pheno or {}).get("table"), log)
        log(f"[bench] ledger backfill: {json.dumps(refs_stats, ensure_ascii=False)}")

    out = {
        "model_a": model_a, "model_b": model_b, "medium": medium,
        "id_systems": {"a": detect_id_system(silent_read_sbml(model_a)),
                       "b": detect_id_system(silent_read_sbml(model_b))},
        "gates": gates,
        "growth": growth,
        "biomass_probe": probe,
        "essentiality": ess,
        "phenotype": pheno,
        "reproducibility": repro,
        "ledger_refs": bool(ledger_refs),
        "reference_essential": reference_essential,
        "reference_note": ("reference_essential 为文献值，仅并列标注（文献值 vs 本工具重算值），"
                           "不参与任何计算、严禁冒充模型输出" if reference_essential else None),
        "units_note": UNITS_NOTE,
        "timing_seconds": round(time.time() - t0, 1),
    }
    if refs_stats is not None:
        out["comparison_refs"] = refs_stats
    if export_md:
        n = write_md(export_md, out)
        out["summary_md_path"] = export_md
        out["summary_md_bytes"] = n
        log(f"[bench] md -> {export_md} ({n} bytes)")
    return out


if __name__ == "__main__":
    args = {}
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            args = json.load(f)
    elif not sys.stdin.isatty():
        args = json.loads(sys.stdin.read())
    a = args.get("args", args)
    print(json.dumps({"ok": True, "result": benchmark(
        a.get("model_a"), a.get("model_b"), medium=a.get("medium"),
        phenotype_table=a.get("phenotype_table"),
        reference_essential=a.get("reference_essential"),
        essential_full=a.get("essential_full", False),
        ledger_refs=a.get("ledger_refs", True), export_md=a.get("export_md"),
        ledger_path=a.get("ledger_path"))}, ensure_ascii=False))
