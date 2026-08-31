# enrichment.py — 阶段C-C3 必需基因通路富集（超几何 + BH FDR；菌种通用）
# 通路注释源（C3 开工探索结论）：gapseq 模型在 SBML groups 里写入 MetaCyc 通路分组
#   （C58 实测 1051 个 PWY groups，9942 个反应成员关系）；BiGG 静态下载模型无 groups
#   （iML1515 实测 0）——无 groups 时按契约返回 annotation_unavailable 兜底（不伪造通路）。
# gene_list 缺省从账本读该模型 essentiality 预测的基因。不登记账本（统计描述非单条预测）。
import os
import sys
import csv
import time
import json
from math import comb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from silentio import silent_read_sbml

NOTE_UNAVAILABLE = ("模型无 SBML groups（通路）注释。可补途径：①用 gapseq 重建（自带 MetaCyc pathway "
                    "groups）；②从 BiGG API 取模型 subsystem 后注入 groups；③按 ec-code 注释做粗分类。")


def _hypergeom_sf(x, N, K, n):
    """P(X >= x)，X~Hypergeom(N, K, n)。scipy 优先，缺失回退精确求和。"""
    try:
        from scipy.stats import hypergeom
        return float(hypergeom.sf(x - 1, N, K, n))
    except Exception:
        lo, hi = max(x, 0), min(K, n)
        if hi < lo:
            return 1.0
        total = comb(N, n)
        s = sum(comb(K, i) * comb(N - K, n - i) for i in range(lo, hi + 1))
        return min(1.0, s / total) if total else 1.0


def _bh_fdr(pvals):
    """Benjamini-Hochberg FDR 校正（单调保序）。"""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    fdr = [0.0] * m
    prev = 1.0
    for rank, i in enumerate(reversed(order), 1):
        idx = m - rank + 1  # 从大到小累计 min
        v = min(prev, pvals[i] * m / (m - rank + 1))
        fdr[i] = v
        prev = v
    return fdr


def _pathway_gene_sets(m):
    """SBML groups -> {pathway_name: gene set}（成员反应的 GPR 基因并集）。"""
    out = {}
    for g in getattr(m, "groups", []) or []:
        genes = set()
        for member in g.members:
            try:
                for gene in member.genes:
                    genes.add(gene.id)
            except Exception:
                continue
        if genes:
            out[g.name or g.id] = genes
    return out


def _ledger_essential_genes(model_path, ledger_path):
    import ledger as _ledger
    if not ledger_path:
        ledger_path = _ledger.model_ledger_path(model_path)  # 2026-08-31：默认=该模型自己的账本
    rows, corrupt = _ledger.load_rows(ledger_path)
    genes = []
    for r in rows:
        # 单模型账本语义：账本里都是该模型（basename 组，含路径变体）的预测，不再精确匹配 model
        if r.get("type") == "essentiality":
            gid = (r.get("content") or "").split(" 在 ")[0].strip()
            if gid:
                genes.append(gid)
    return sorted(set(genes)), len(rows), corrupt


def enrichment(model_path, gene_list=None, pathway_source=None, ledger_path=None,
               export_csv=None, progress=None):
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    t0 = time.time()
    m = silent_read_sbml(model_path)

    pw_sets = _pathway_gene_sets(m) if (pathway_source or "groups") == "groups" else {}
    if not pw_sets:
        # 注释缺失兜底（必须）：不伪造通路
        return {
            "model": model_path, "annotation_unavailable": True,
            "note": NOTE_UNAVAILABLE,
            "groups_found": len(getattr(m, "groups", []) or []),
            "ec_code_reactions": sum(1 for r in m.reactions
                                     if (r.annotation or {}).get("ec-code")),
            "gene_list_provided": bool(gene_list),
            "timing_seconds": round(time.time() - t0, 1),
        }

    # gene_list：显式传入优先；缺省从账本 essentiality 预测读取（本模型）
    source_note = None
    if not gene_list:
        gene_list, total_rows, corrupt = _ledger_essential_genes(model_path, ledger_path)
        source_note = (f"gene_list 缺省来自账本 essentiality 预测（model 精确匹配 {model_path}，"
                       f"账本总行 {total_rows}，corrupt {corrupt}）")
        log(f"[enrich] gene_list from ledger: {len(gene_list)} genes")
    gene_list = sorted(set(gene_list or []))

    # 背景 = 模型内所有有 GPR 关联的基因（可被通路注释到的全体基因）
    bg_genes = {g.id for g in m.genes if g.reactions}
    input_in_bg = [g for g in gene_list if g in bg_genes]
    N, n = len(bg_genes), len(input_in_bg)

    rows = []
    for pw_name, pw_genes in pw_sets.items():
        bg_hit = pw_genes & bg_genes
        if not bg_hit:
            continue
        K = len(bg_hit)
        hits = sorted(set(input_in_bg) & bg_hit)
        x = len(hits)
        if x == 0:
            continue
        p = _hypergeom_sf(x, N, K, n)
        exp = K * n / N if N else 0.0
        rows.append({"pathway": pw_name, "genes_hit": hits, "genes_hit_count": x,
                     "background_hit": K, "pathway_size": len(pw_genes),
                     "total_bg": N, "n_input": n, "expected_count": round(exp, 3),
                     "fold_enrichment": round(x / exp, 3) if exp > 0 else None,
                     "p_value": p})
    fdrs = _bh_fdr([r["p_value"] for r in rows])
    for r, f in zip(rows, fdrs):
        r["fdr"] = f
    rows.sort(key=lambda r: (r["p_value"], r["pathway"]))
    sig = [r for r in rows if r["fdr"] <= 0.05]
    log(f"[enrich] pathways tested={len(rows)} significant(FDR<=0.05)={len(sig)}")

    out = {
        "model": model_path,
        "pathway_source": "sbml_groups(MetaCyc PWY)",
        "annotation_unavailable": False,
        "gene_list_source": source_note or "explicit",
        "gene_list": gene_list,
        "background_size": N,
        "n_input": n,
        "n_too_small": n < 10,
        "n_too_small_note": (f"有效映射基因 n={n} < 10，统计功效有限，结果仅供方向参考" if n < 10 else None),
        "pathways_tested": len(rows),
        "significant_count_fdr05": len(sig),
        "results": rows,
        "multiple_testing": "hypergeom 单侧富集 + Benjamini-Hochberg FDR",
        "timing_seconds": round(time.time() - t0, 1),
    }
    if export_csv:
        with open(export_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["pathway", "genes_hit_count", "genes_hit", "background_hit",
                        "pathway_size", "total_bg", "n_input", "expected_count",
                        "fold_enrichment", "p_value", "fdr"])
            for r in rows:
                w.writerow([r["pathway"], r["genes_hit_count"], ";".join(r["genes_hit"]),
                            r["background_hit"], r["pathway_size"], r["total_bg"],
                            r["n_input"], r["expected_count"], r["fold_enrichment"],
                            r["p_value"], r["fdr"]])
        out["export_csv"] = export_csv
        out["export_csv_rows"] = len(rows)
        log(f"[enrich] CSV {export_csv}: {len(rows)} rows")
    return out


if __name__ == "__main__":
    args = {}
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            args = json.load(f)
    elif not sys.stdin.isatty():
        args = json.loads(sys.stdin.read())
    a = args.get("args", args)
    print(json.dumps({"ok": True, "result": enrichment(
        a.get("model"), gene_list=a.get("gene_list"), pathway_source=a.get("pathway_source"),
        ledger_path=a.get("ledger_path"), export_csv=a.get("export_csv"))}, ensure_ascii=False))
