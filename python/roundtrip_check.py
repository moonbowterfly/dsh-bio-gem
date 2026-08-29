# roundtrip_check.py — SBML 往返保真自检（Q2 工程质量件 A）
# cobra 读 → write_sbml_model → 读回：断言反应/代谢物/基因数一致 + GPR 字符串精确一致
# （GLM 经典暗坑：序列化静默丢 GPR——fbc v2 写入路径回归护栏；≥5 个复合 GPR 反应样本必查）
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def roundtrip_check(model_path, min_complex_gpr=5):
    from silentio import silent_read_sbml, silent_write_sbml
    m = silent_read_sbml(model_path)
    gpr = {r.id: r.gene_reaction_rule for r in m.reactions if r.gene_reaction_rule}
    # 复合 GPR：and/or 混合或带括号（最容易在序列化中丢结构）
    complex_ids = [k for k, v in gpr.items() if (" and " in v and " or " in v) or "(" in v]
    out = os.path.join(tempfile.mkdtemp(prefix="gem-rt-"), os.path.basename(model_path))
    silent_write_sbml(m, out)
    m2 = silent_read_sbml(out)
    gpr2 = {r.id: r.gene_reaction_rule for r in m2.reactions if r.gene_reaction_rule}
    counts = {"reactions": [len(m.reactions), len(m2.reactions)],
              "metabolites": [len(m.metabolites), len(m2.metabolites)],
              "genes": [len(m.genes), len(m2.genes)]}
    counts_ok = all(v[0] == v[1] for v in counts.values())
    diffs = {k: {"before": gpr[k], "after": gpr2.get(k)} for k in gpr if gpr[k] != gpr2.get(k)}
    sampled = sorted(complex_ids)[:min_complex_gpr]
    sample_diffs = [k for k in sampled if gpr[k] != gpr2.get(k)]
    return {
        "ok": counts_ok and not diffs and len(sampled) >= min_complex_gpr and not sample_diffs,
        "model": model_path,
        "counts": counts,
        "gpr_total": len(gpr), "gpr_complex_available": len(complex_ids),
        "gpr_compared": len(sampled), "gpr_diffs": len(diffs), "gpr_sample_diffs": sample_diffs,
        "detail_first_diffs": dict(list(diffs.items())[:3]),
    }


if __name__ == "__main__":
    # 协议与 gem_ops 一致：JSON 走 stdin（smoke runPy 直传）；兼容 argv 文件
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    args = json.loads(raw) if raw.strip() else (
        json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {})
    print(json.dumps(roundtrip_check(args.get("model"), args.get("min_complex_gpr", 5)),
                     ensure_ascii=False, indent=2))
