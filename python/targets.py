# targets.py — 靶点清单规范导出（下游接口面；菌种通用）
# 汇总来源：账本（essentiality/synthetic_lethal/secretion 预测；essential 默认读账本不重扫）。
# 输出 schema（锁定，每行 11 字段）：target_id/type/genes/met_ids/condition/rationale/
#   evidence_tier/status/growth_or_maxprod/source/exported_at。
# 定位：供下游引物/编辑工具直接输入的规范格式——引物/质粒设计本身不做（方案文件明确）。
# 与账本计数闭合：exported 条数 = 账本对应 type 计数。
import os
import re
import sys
import csv
import time
import json
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fsutil import ensure_parent_dir

SCHEMA_FIELDS = ["target_id", "type", "genes", "met_ids", "condition", "rationale",
                 "evidence_tier", "status", "growth_or_maxprod", "source", "exported_at"]
TYPE_ORDER = ["essentiality", "synthetic_lethal", "secretion"]
TYPE_ALIASES = {"essential": "essentiality", "synthetic_lethal": "synthetic_lethal",
                "secretion": "secretion"}


def _parse_gene(content):
    return (content or "").split(" 在 ")[0].strip()


def _parse_secretion(content):
    """'代谢物 cpd00036_e0(Succinate-e0) 在 AB 下模型预测可分泌（max_prod=6.9273 mmol/gDW/h）'"""
    met = None
    m = re.search(r"代谢物 (\S+?)[(（]", content or "")
    if m:
        met = m.group(1)
    mp = None
    m2 = re.search(r"max_prod=([0-9.eE+-]+)", content or "")
    if m2:
        mp = float(m2.group(1))
    return met, mp


def _parse_pair(content):
    """'geneA 与 geneB 在 AB 下合成致死'"""
    m = re.match(r"(\S+) 与 (\S+) 在 ", content or "")
    return [m.group(1), m.group(2)] if m else []


def targets(model_path=None, types=None, condition=None, ledger_path=None,
            export_format="csv", export_path=None, progress=None, analysis_context=None):
    log = progress or (lambda s: sys.stderr.write(str(s) + "\n"))
    import ledger as _ledger
    query = _ledger.query_ledger(model=model_path, path=ledger_path,
                                 analysis_context=analysis_context)
    rows, corrupt = query["results"], query["corrupt"]
    types = [TYPE_ALIASES.get(t, t) for t in (types or ["essentiality", "synthetic_lethal", "secretion"])]
    types = [t for t in types if t in TYPE_ORDER]

    selected = [r for r in rows
                if r.get("type") in types
                and (condition is None or str(r.get("condition") or "").lower()
                     == str(condition).lower())]
    selected.sort(key=lambda r: (TYPE_ORDER.index(r["type"]), r.get("prediction_id") or ""))

    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    out_rows = []
    for i, r in enumerate(selected, 1):
        rtype = r["type"]
        content = r.get("content") or ""
        subject = r.get("subject") or {}
        genes, met_ids, gmp = [], [], None
        if rtype == "essentiality":
            genes = [g for g in [subject.get("gene") or _parse_gene(content)] if g]
        elif rtype == "synthetic_lethal":
            genes = subject.get("genes") or _parse_pair(content)
        elif rtype == "secretion":
            met, mp = _parse_secretion(content)
            met_ids = [subject.get("metabolite") or met] if (subject.get("metabolite") or met) else []
            gmp = mp
        out_rows.append({
            "target_id": f"T{i:04d}", "type": rtype, "genes": genes, "met_ids": met_ids,
            "condition": r.get("condition"),
            "rationale": content,
            "evidence_tier": r.get("evidence_tier"),
            "status": r.get("status") or "unverified",
            "growth_or_maxprod": gmp,
            "source": f"ledger:{r.get('prediction_id')}",
            "exported_at": now,
        })

    # 计数闭合：exported per type == 账本对应 type 计数（同过滤口径）
    ledger_counts = {}
    for r in rows:
        if (condition is None or str(r.get("condition") or "").lower()
                               == str(condition).lower()):
            ledger_counts[r.get("type")] = ledger_counts.get(r.get("type"), 0) + 1
    exported_counts = {}
    for r in out_rows:
        exported_counts[r["type"]] = exported_counts.get(r["type"], 0) + 1
    closure = {t: {"exported": exported_counts.get(t, 0),
                   "ledger": ledger_counts.get(t, 0),
                   "closed": exported_counts.get(t, 0) == ledger_counts.get(t, 0)}
               for t in types}

    # 落盘（缺省 ~/.dsh/dsh-bio-gem/exports/targets_<ts>.<ext>）
    if export_path is None:
        d = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "exports")
        os.makedirs(d, exist_ok=True)
        export_path = os.path.join(d, f"targets_{time.strftime('%Y%m%d_%H%M%S')}.{export_format}")
    export_path = ensure_parent_dir(export_path)
    if export_format == "json":
        with open(export_path, "w", encoding="utf-8") as f:
            json.dump(out_rows, f, ensure_ascii=False, indent=1)
    else:
        with open(export_path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=SCHEMA_FIELDS, extrasaction="ignore")
            w.writeheader()
            for r in out_rows:
                w.writerow({**r, "genes": ";".join(r["genes"]),
                            "met_ids": ";".join(r["met_ids"])})
    with open(export_path, "rb") as handle:
        output_sha = "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    meta_path = export_path + ".meta.json"
    metadata = {"schema": "gem.targets-meta.v1", "export_sha256": output_sha,
                "model_sha256": _ledger.sbml_sha256(model_path) if model_path else None,
                "identity_excluded": query["identity_excluded"],
                "rows": [{"target_id": out["target_id"], "prediction_uid": source.get("prediction_uid"),
                          "model_sha256": source.get("model_sha256"),
                          "analysis_context": source.get("analysis_context")}
                         for out, source in zip(out_rows, selected)]}
    with open(meta_path, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    log(f"[targets] exported {len(out_rows)} rows -> {export_path}")

    return {
        "model": model_path, "types": types, "condition": condition,
        "schema_fields": SCHEMA_FIELDS,
        "exported_count": len(out_rows),
        "count_closure": closure,
        "corrupt_rows": len(corrupt),
        "export_format": export_format,
        "export_path": export_path,
        "meta_path": meta_path, "export_sha256": output_sha,
        "rows": out_rows,
        "note": "供下游引物/编辑工具直接输入的规范导出；引物/质粒设计本身不在本插件范围（方案文件明确）",
    }


if __name__ == "__main__":
    args = {}
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            args = json.load(f)
    elif not sys.stdin.isatty():
        args = json.loads(sys.stdin.read())
    a = args.get("args", args)
    print(json.dumps({"ok": True, "result": targets(
        model_path=a.get("model"), types=a.get("types"), condition=a.get("condition"),
        ledger_path=a.get("ledger_path"), export_format=a.get("export_format", "csv"),
        export_path=a.get("export_path"))}, ensure_ascii=False))
