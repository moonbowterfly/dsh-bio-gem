# validate.py — dsh-bio-gem 五道验证关卡（M1）
# G1 加载统计 / G2 内部反应元素平衡 / G3 生长真实性 / G4 底物表型(条件) / G5 必需基因抽检(条件)
# 规格: docs/ARCHITECTURE.md §5；判据口径 = FBA objective_value（mmol/gDW/h，不用 μ）
# 实现从 HANDOFF-03 五道关卡协议产品化（农杆菌项目验证过的逻辑）
import re
import os
import json
import sys
import cobra

# Python -I isolated 模式下脚本目录不进 sys.path——显式插入以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

EX_PREFIX = ("EX_", "DM_", "SK_")
CORE_ELEMS = ("C", "N", "P", "S")   # 硬核：不平衡必须 = 0
REPORT_ELEMS = ("H", "O")           # 报告不阻塞
ELM_RE = re.compile(r"([A-Z][a-z]?)(\d*)")


def parse_formula(f):
    """C10H13N5O13P3 -> {"C":10,...}; 忽略 R/X 等通用占位。"""
    d = {}
    if not f:
        return d
    for m in ELM_RE.finditer(f):
        el = m.group(1)
        n = int(m.group(2) or 1)
        d[el] = d.get(el, 0) + n
    return d


def _rxn_elem_balance(rxn):
    """内部反应元素平衡：返回 {elem: delta}（delta=产物-底物，应接近 0）。"""
    bal = {}
    for met, coeff in rxn.metabolites.items():
        if not met.formula:
            continue
        d = parse_formula(met.formula)
        for el, n in d.items():
            bal[el] = bal.get(el, 0) + coeff * n
    return bal


class Validator:
    def __init__(self, model_path):
        from silentio import silent_read_sbml
        self.path = model_path
        self.m = silent_read_sbml(model_path)

    # ------------------------------------------------------------------ G1
    def g1_load(self):
        m = self.m
        from collections import Counter
        repl = Counter()
        bad_ids = []
        for g in m.genes:
            parts = g.id.split("_")
            if len(parts) >= 3 and parts[0] == "NC":
                repl["_".join(parts[:2])] += 1
            else:
                repl["other"] += 1
                bad_ids.append(g.id)
        n_genes_with_rxn = sum(1 for g in m.genes if len(g.reactions) > 0)
        rep = {
            "status": "PASS" if not bad_ids else "WARN",
            "genes": len(m.genes), "reactions": len(m.reactions),
            "metabolites": len(m.metabolites),
            "replicons": dict(repl),
            "non_nc_gene_ids": bad_ids[:10],
            "gpr_gene_coverage": round(n_genes_with_rxn / len(m.genes), 4) if m.genes else 0,
        }
        return rep

    # ------------------------------------------------------------------ G2
    def g2_balance(self):
        m = self.m
        internal = [r for r in m.reactions
                    if not (r.id.startswith(EX_PREFIX) or r.boundary)]
        formula_coverage = sum(1 for x in m.metabolites if x.formula) / len(m.metabolites)
        bad_core = {}   # elem -> [rxn ids]
        bad_report = {}
        checked = 0
        for r in internal:
            if not all(x.formula for x in r.metabolites):
                continue  # 公式缺失不计入不平衡（先报覆盖率）
            checked += 1
            bal = _rxn_elem_balance(r)
            for el in CORE_ELEMS:
                v = bal.get(el, 0)
                if abs(v) > 1e-6:
                    bad_core.setdefault(el, []).append(r.id)
            for el in REPORT_ELEMS:
                v = bal.get(el, 0)
                if abs(v) > 2:  # charged 公式惯例噪声容忍 ±2
                    bad_report.setdefault(el, []).append(r.id)
        n_bad = sum(len(v) for v in bad_core.values())
        frac = 1.0 - n_bad / checked if checked else 0.0
        status = "PASS" if n_bad == 0 else ("WARN" if frac >= 0.85 else "FAIL")
        rep = {
            "status": status,
            "internal_reactions": len(internal), "formula_checked": checked,
            "metabolite_formula_coverage": round(formula_coverage, 4),
            "core_unbalanced": {k: len(v) for k, v in bad_core.items()},
            "core_unbalanced_examples": {k: v[:5] for k, v in bad_core.items()},
            "h_o_report": {k: len(v) for k, v in bad_report.items()},
            "core_balance_frac": round(frac, 4),
        }
        return rep

    # ------------------------------------------------------------------ G3
    def g3_growth(self, medium, reference_growth=None):
        """medium: {EX_id: lower_bound}; 三态：medium / no-carbon / all-closed。"""
        m = self.m

        def _setup(medium_dict):
            for r in m.reactions:
                if r.id.startswith(EX_PREFIX) or r.boundary:
                    r.lower_bound = 0.0
            for rid, lb in (medium_dict or {}).items():
                if rid in m.reactions:
                    m.reactions.get_by_id(rid).lower_bound = lb
                else:
                    return rid
            return None

        miss = _setup(medium or {})
        if miss:
            return {"status": "FAIL", "reason": f"medium exchange not in model: {miss}",
                    "medium_provided": bool(medium)}
        with m:
            wt = m.optimize().objective_value
        # no-carbon: 去掉 formula 含 C 的交换
        no_c_medium = dict(medium or {})
        for rid in list(no_c_medium):
            if rid.startswith("EX_") and rid in m.reactions:
                met = list(m.reactions.get_by_id(rid).metabolites)[0]
                if met.formula and "C" in parse_formula(met.formula):
                    del no_c_medium[rid]
        miss = _setup(no_c_medium)
        with m:
            g_no_c = m.optimize().objective_value
        miss = _setup({})  # all closed
        with m:
            g_closed = m.optimize().objective_value
        ok_grow = wt > 1e-6
        ok_noc = abs(g_no_c) < 1e-6
        ok_closed = abs(g_closed) < 1e-6
        ratio = None
        if reference_growth and reference_growth > 0:
            ratio = wt / reference_growth
        if not medium:
            status = "WARN"  # 无声明培养基 -> 无法验证
        elif ok_grow and ok_noc and ok_closed and (ratio is None or ratio >= 0.99):
            status = "PASS"
        else:
            status = "FAIL"  # 有培养基但生长不达标（或对照泄漏）——构建侧必须走补洞闭环
        rep = {
            "status": status,
            "medium_provided": bool(medium),
            "growth_medium": round(wt, 6),
            "growth_no_carbon": round(g_no_c, 6),
            "growth_all_closed": round(g_closed, 6),
            "ratio_vs_reference": round(ratio, 4) if ratio is not None else None,
            "checks": {"medium>0": ok_grow, "no_carbon==0": ok_noc, "closed==0": ok_closed},
        }
        return rep

    # ------------------------------------------------------------------ G4
    def g4_phenotype(self, table_path=None, substrates=None, medium=None, carbon_mode="supplement"):
        """条件执行：需参照表（TSV: substrate<TAB>published 0/1）或 substrates+published。
        carbon_mode: supplement=基准培养基不变+底物-10（对齐 HANDOFF-03 关卡4 基线 16/19→17/19）；
                     sole=去含碳交换后底物-10（唯一碳源严格语义，氮源类测试会误判）。"""
        if table_path and os.path.exists(table_path):
            rows = []
            with open(table_path, encoding="utf-8") as f:
                for line in f:
                    line = line.rstrip("\r\n")
                    if not line or line.startswith("#") or line.startswith("substrate"):
                        continue
                    p = line.split("\t")
                    if len(p) >= 2:
                        rows.append((p[0].strip(), int(float(p[1]))))
        elif substrates:
            rows = substrates
        else:
            return {"status": "SKIP", "reason": "no phenotype reference provided"}
        if not medium:
            return {"status": "SKIP", "reason": "G4 needs medium to define base"}
        from gapfind import build_ex_index, match_ex, SYN
        m = self.m
        # 统一走 gapfind 的 build_ex_index + match_ex（修复过子串误配规则；勿再各自实现）
        ex_idx = build_ex_index(m)
        results = []
        matched = 0
        for sub, pub in rows:
            exid = match_ex(sub, ex_idx)
            # 基底：medium（supplement）或去碳后加底物（sole）
            med2 = dict(medium)
            if carbon_mode == "sole":
                for rid in list(med2):
                    if rid.startswith("EX_") and rid in m.reactions:
                        met = list(m.reactions.get_by_id(rid).metabolites)[0]
                        if met.formula and "C" in parse_formula(met.formula):
                            del med2[rid]
            if exid and exid in m.reactions:
                med2[exid] = -10.0
            for r in m.reactions:
                if r.id.startswith(EX_PREFIX) or r.boundary:
                    r.lower_bound = 0.0
            for rid, lb in med2.items():
                if rid in m.reactions:
                    m.reactions.get_by_id(rid).lower_bound = lb
            with m:
                g = m.optimize().objective_value
            pred = g > 1e-6
            ok = (pred == bool(pub))
            if ok:
                matched += 1
            results.append({"substrate": sub, "published": int(pub), "predicted": int(pred),
                            "growth": round(g, 6), "exchange": exid or None, "match": bool(ok)})
        rep = {
            "status": "PASS" if rows and matched / len(rows) >= 0.8 else ("WARN" if rows else "SKIP"),
            "carbon_mode": carbon_mode,
            "matched": matched, "total": len(rows),
            "rate": round(matched / len(rows), 4) if rows else None,
            "results": results,
        }
        return rep

    # ------------------------------------------------------------------ G5
    def g5_essentiality(self, essential_test, medium, reference_essential=None):
        """条件执行：对给定基因列表逐一手工敲除（with m: 循环），输出每个基因的必要性。
        若给 reference_essential（已知必需基因集）→ 对交集算召回。"""
        if not essential_test:
            return {"status": "SKIP", "reason": "no essential_test gene list provided"}
        if not medium:
            return {"status": "SKIP", "reason": "G5 needs medium"}
        m = self.m
        present = [g for g in essential_test if g in m.genes]
        if len(present) / len(essential_test) < 0.8:
            return {"status": "SKIP", "reason": "gene mapping coverage < 80%",
                    "present": len(present), "total": len(essential_test)}
        results = []
        for gid in present:
            with m:
                for r in m.reactions:
                    if r.id.startswith(EX_PREFIX) or r.boundary:
                        r.lower_bound = 0.0
                for rid, lb in medium.items():
                    if rid in m.reactions:
                        m.reactions.get_by_id(rid).lower_bound = lb
                m.genes.get_by_id(gid).knock_out()
                g = m.optimize().objective_value
            results.append({"gene": gid, "growth": round(g, 6),
                            "essential": bool(g < 1e-6)})
        n_ess = sum(1 for r_ in results if r_["essential"])
        recall = None
        if reference_essential:
            ref = set(reference_essential)
            tp = sum(1 for r_ in results if r_["essential"] and r_["gene"] in ref)
            recall = round(tp / len(ref), 4) if ref else None
        rep = {
            "status": "PASS" if (recall is None or recall >= 0.4) else "WARN",
            "tested": len(results), "essential_found": n_ess,
            "recall_vs_reference": recall,
            "details": results,
        }
        return rep

    # ------------------------------------------------------------------ run
    def run(self, medium=None, phenotype_table=None, essential_test=None,
            reference_growth=None, reference_essential=None, carbon_mode="supplement"):
        from gapfind import resolve_medium, expand_medium
        medium, _preset = expand_medium(medium)
        resolved_med, unresolved = resolve_medium(self.m, medium) if medium else ({}, [])
        report = {"model": self.path,
                  "units": {"growth": "mmol/gDW/h", "note": "objective_value 是 FBA 通量（mmol/gDW/h），不是比生长速率 μ（h⁻¹）"},
                  "g1": self.g1_load(),
                  "g2": self.g2_balance()}
        g3 = self.g3_growth(resolved_med, reference_growth)
        if unresolved:
            g3["medium_unresolved"] = unresolved
        report["g3"] = g3
        if phenotype_table:
            report["g4"] = self.g4_phenotype(table_path=phenotype_table, medium=resolved_med,
                                             carbon_mode=carbon_mode)
        else:
            report["g4"] = {"status": "SKIP", "reason": "no phenotype reference provided"}
        if essential_test:
            report["g5"] = self.g5_essentiality(essential_test, resolved_med, reference_essential)
        else:
            report["g5"] = {"status": "SKIP", "reason": "no essential_test provided"}
        # 总判定：G1/G2/G3 全 PASS（或 SKIP 默认）+ 其余不阻塞
        blocked = ["g1", "g2", "g3"]
        fails = [k for k in blocked if report[k]["status"] == "FAIL"]
        warns = [k for k in blocked if report[k]["status"] == "WARN"]
        report["overall"] = "FAIL" if fails else ("WARN" if warns else "PASS")
        report["blocking"] = blocked
        return report


def validate_model(model_path, medium=None, phenotype_table=None,
                   essential_test=None, reference_growth=None, reference_essential=None,
                   carbon_mode="supplement"):
    v = Validator(model_path)
    return v.run(medium=medium, phenotype_table=phenotype_table,
                 essential_test=essential_test, reference_growth=reference_growth,
                 reference_essential=reference_essential, carbon_mode=carbon_mode)


if __name__ == "__main__":
    # 命令行直跑（开发/测试用）：python validate.py <model.xml> [--medium-json x] [--table t] [--g5 g1,g2]
    import sys
    path = sys.argv[1]
    med = None
    table = None
    g5 = None
    refg = None
    i = 2
    while i < len(sys.argv):
        if sys.argv[i] == "--medium-json" and i + 1 < len(sys.argv):
            med = json.loads(sys.argv[i + 1]); i += 2
        elif sys.argv[i] == "--table" and i + 1 < len(sys.argv):
            table = sys.argv[i + 1]; i += 2
        elif sys.argv[i] == "--g5" and i + 1 < len(sys.argv):
            g5 = sys.argv[i + 1].split(","); i += 2
        elif sys.argv[i] == "--ref-growth" and i + 1 < len(sys.argv):
            refg = float(sys.argv[i + 1]); i += 2
        else:
            i += 1
    rep = validate_model(path, medium=med, phenotype_table=table, essential_test=g5,
                         reference_growth=refg)
    print(json.dumps(rep, ensure_ascii=False, indent=2))