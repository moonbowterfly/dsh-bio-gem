# ledger.py — 阶段A-M3 prediction ledger（预测账本，可追踪可实验兑现）
# 文件: ~/.dsh/dsh-bio-gem/ledger/predictions.jsonl（追加式，行=一条预测；目录不存在则创建）
# 幂等: 同 model+condition+type+content 哈希去重，重复运行不追加。
# 完整性: 逐行 JSON 解析校验，损坏行跳过并在返回里报 corrupt_rows + 行号（不阻塞）；
#         写入失败只 WARN 不使主流程失败。update 重写文件但保留损坏行原样（不删行）。
# 证据分级优先级（任务书锁定）: EVIDENCE_literature > EVIDENCE_sequence > EVIDENCE_rule > EVIDENCE_math
import os
import sys
import json
import hashlib
from datetime import datetime

LEDGER_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "ledger")
LEDGER_PATH = os.path.join(LEDGER_DIR, "predictions.jsonl")

TIER_PRIORITY = ["EVIDENCE_literature", "EVIDENCE_sequence", "EVIDENCE_rule", "EVIDENCE_math"]  # 高→低
STATUSES = ("unverified", "literature_supported", "literature_contradicted", "experimentally_verified")
TYPES = ("essentiality", "phenotype", "synthetic_lethal", "secretion", "other")
DEFAULT_TIER = "EVIDENCE_rule"


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _content_hash(model, condition, rtype, content):
    raw = "\x1f".join([model or "", condition or "", rtype or "", content or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load_rows(path=None):
    """读全部行；损坏行跳过。返回 (rows, corrupt:[{line,error}])。文件不存在 -> ([], [])。"""
    p = path or LEDGER_PATH
    rows, corrupt = [], []
    if not os.path.exists(p):
        return rows, corrupt
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception as e:
                corrupt.append({"line": i, "error": str(e)[:120]})
    return rows, corrupt


def _max_id_num(rows):
    mx = 0
    for r in rows:
        pid = str(r.get("prediction_id") or "")
        if pid.startswith("P") and pid[1:].isdigit():
            mx = max(mx, int(pid[1:]))
    return mx


def register_predictions(new_rows, path=None):
    """追加式登记（幂等）。返回 {appended, skipped_duplicates, total_after, corrupt_rows, path,
    prediction_ids, warn}；写入失败仅 WARN（返回内附 warn 字段），绝不抛异常阻塞主流程。"""
    p = path or LEDGER_PATH
    existing, corrupt = load_rows(p)
    seen = {_content_hash(r.get("model"), r.get("condition"), r.get("type"), r.get("content"))
            for r in existing}
    out = {"appended": 0, "skipped_duplicates": 0, "total_after": len(existing),
           "corrupt_rows": len(corrupt), "corrupt": corrupt, "path": p, "prediction_ids": [],
           "warn": None}
    to_add = []
    for row in new_rows:
        h = _content_hash(row.get("model"), row.get("condition"), row.get("type"), row.get("content"))
        if h in seen:
            out["skipped_duplicates"] += 1
            continue
        seen.add(h)
        to_add.append(row)
    if not to_add:
        return out
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        n = _max_id_num(existing)
        with open(p, "a", encoding="utf-8") as f:
            for row in to_add:
                n += 1
                row.setdefault("prediction_id", f"P{n:04d}")
                row.setdefault("status", "unverified")
                row.setdefault("source_refs", [])
                row.setdefault("comparison_refs", [])
                row.setdefault("created_at", _now_iso())
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
                out["prediction_ids"].append(row["prediction_id"])
                out["appended"] += 1
        out["total_after"] = len(existing) + out["appended"]
    except Exception as e:  # 写入失败只 WARN 不使主流程失败
        out["warn"] = f"ledger append failed: {type(e).__name__}: {e}"
        sys.stderr.write(f"[ledger] WARN {out['warn']}\n")
    return out


def query_ledger(rtype=None, status=None, condition=None, model=None,
                 limit=None, offset=0, path=None):
    """条件过滤（type/status/condition/model 前缀匹配、大小写不敏感、可组合）+ 分页。"""
    rows, corrupt = load_rows(path or LEDGER_PATH)

    def _pref(v, q):
        return str(v or "").lower().startswith(str(q).lower())

    hits = [r for r in rows
            if (not rtype or _pref(r.get("type"), rtype))
            and (not status or _pref(r.get("status"), status))
            and (not condition or _pref(r.get("condition"), condition))
            and (not model or _pref(r.get("model"), model))]
    sliced = hits[offset:] if not limit else hits[offset:offset + limit]
    return {"total": len(rows), "matched": len(hits), "offset": offset,
            "results": sliced, "corrupt_rows": len(corrupt), "corrupt": corrupt}


def update_row(prediction_id, status=None, source_refs=None, comparison_refs=None, path=None):
    """按 prediction_id 更新 status/source_refs/comparison_refs，维护 updated_at。
    文件重写但逐行保留（损坏行原样保留，不删行）。"""
    p = path or LEDGER_PATH
    if status is not None and status not in STATUSES:
        return {"ok": False, "error": f"invalid status {status!r}（{STATUSES}）"}
    if not os.path.exists(p):
        return {"ok": False, "error": f"ledger not found: {p}"}
    rows, corrupt = load_rows(p)
    found = 0
    updated_row = None
    for r in rows:
        if r.get("prediction_id") == prediction_id:
            found += 1
            if status is not None:
                r["status"] = status
            if source_refs is not None:
                r["source_refs"] = source_refs
            if comparison_refs is not None:
                r["comparison_refs"] = comparison_refs
            r["updated_at"] = _now_iso()
            updated_row = r
    if not found:
        return {"ok": False, "error": f"prediction_id not found: {prediction_id}"}
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
        with open(p, "w", encoding="utf-8") as f:
            for line in lines:
                stripped = line.strip()
                try:
                    obj = json.loads(stripped)
                except Exception:
                    f.write(line)  # 损坏行原样保留（不删行）
                    continue
                if isinstance(obj, dict) and obj.get("prediction_id") == prediction_id and found:
                    f.write(json.dumps(updated_row, ensure_ascii=False) + "\n")
                    found -= 1
                else:
                    f.write(line if line.endswith("\n") else line + "\n")
    except Exception as e:
        return {"ok": False, "error": f"ledger update write failed: {type(e).__name__}: {e}"}
    return {"ok": True, "updated": prediction_id, "row": updated_row,
            "corrupt_rows": len(corrupt)}


def ledger_summary(path=None):
    """gem_report 摘要用：{total, by_status, by_type}；文件不存在 -> {total: 0}。"""
    rows, corrupt = load_rows(path or LEDGER_PATH)
    by_status, by_type = {}, {}
    for r in rows:
        s = r.get("status") or "unspecified"
        t = r.get("type") or "other"
        by_status[s] = by_status.get(s, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1
    return {"total": len(rows), "by_status": by_status, "by_type": by_type,
            "corrupt_rows": len(corrupt)}


# ---------------------------------------------------------------------------
# 自动登记（evidence_tier 取支撑反应 evidence 集合中最高者；无标注默认 EVIDENCE_rule 并注明）
# ---------------------------------------------------------------------------
def best_evidence_tier(reactions, default=DEFAULT_TIER):
    """reactions: 支撑反应列表（cobra Reaction）。返回 (tier, defaulted)。"""
    tiers = []
    for r in reactions or []:
        ev = (getattr(r, "notes", None) or {}).get("evidence")
        if ev:
            tiers.append(ev)
    for t in TIER_PRIORITY:
        if t in tiers:
            return t, False
    return default, True


def register_essentiality(model_path, scan_result, model=None, condition=None,
                          lineage_version=None, path=None):
    """gem_essentiality 自动登记：每必需基因一条（type=essentiality，status=unverified）。"""
    rows = []
    for gid in (scan_result or {}).get("essential_genes") or []:
        tier, defaulted = DEFAULT_TIER, True
        try:
            if model is not None:
                tier, defaulted = best_evidence_tier(model.genes.get_by_id(gid).reactions)
        except Exception:
            pass
        rows.append({
            "type": "essentiality",
            "content": f"{gid} 在 {condition} 培养基下必需",
            "model": model_path,
            "model_lineage_version": lineage_version,
            "condition": condition,
            "evidence_tier": tier,
            "status": "unverified",
            "source_refs": [],
            "comparison_refs": [],
            **({"evidence_note": "支撑反应无 evidence 标注，默认 EVIDENCE_rule"}
               if defaulted else {}),
        })
    return register_predictions(rows, path)


def register_phenotype(model_path, g4_results, condition=None, lineage_version=None,
                       model=None, path=None):
    """gem_phenotype 自动登记：每底物一条（G4 结果；type=phenotype，status=unverified）。
    evidence 取底物交换反应的 evidence 标注（无则默认 EVIDENCE_rule 并注明）。"""
    rows = []
    for r in g4_results or []:
        sub = r.get("substrate")
        if not sub:
            continue
        tier, defaulted = DEFAULT_TIER, True
        try:
            exid = r.get("exchange")
            if model is not None and exid and exid in [x.id for x in model.reactions]:
                tier, defaulted = best_evidence_tier([model.reactions.get_by_id(exid)])
        except Exception:
            pass
        rows.append({
            "type": "phenotype",
            "content": (f"底物 {sub} 预测{'生长' if r.get('predicted') else '不生长'}"
                        f"（文献={r.get('published')}，匹配={r.get('match')}，"
                        f"growth={r.get('growth')} mmol/gDW/h）"),
            "model": model_path,
            "model_lineage_version": lineage_version,
            "condition": condition,
            "evidence_tier": tier,
            "status": "unverified",
            "source_refs": [],
            "comparison_refs": [],
            **({"evidence_note": "底物交换反应无 evidence 标注，默认 EVIDENCE_rule"}
               if defaulted else {}),
        })
    return register_predictions(rows, path)


def register_secretion(model_path, secretion_rows, condition=None,
                       lineage_version=None, path=None):
    """gem_secretion 自动登记：每个可分泌代谢物一条（type=secretion，status=unverified，
    evidence_tier=EVIDENCE_math——production envelope 线性规划结果）。"""
    rows = []
    for r in secretion_rows or []:
        rows.append({
            "type": "secretion",
            "content": (f"代谢物 {r.get('met_id')}({r.get('name') or ''}) 在 {condition} 下"
                        f"模型预测可分泌（max_prod={r.get('max_prod')} mmol/gDW/h）"),
            "model": model_path,
            "model_lineage_version": lineage_version,
            "condition": condition,
            "evidence_tier": "EVIDENCE_math",
            "status": "unverified",
            "source_refs": [],
            "comparison_refs": [],
            "evidence_note": "production envelope 线性规划结果（纯拓扑），未考虑毒性/渗透压/调控",
        })
    return register_predictions(rows, path)


def register_synthetic_lethal(model_path, pair_rows, condition=None,
                              lineage_version=None, path=None):
    """gem_double_knockout 自动登记：每合成致死对一条（type=synthetic_lethal，
    status=unverified，evidence_tier=EVIDENCE_math——双敲 LP 判定）。"""
    rows = []
    for r in pair_rows or []:
        ga, gb = r.get("gene_a"), r.get("gene_b")
        rows.append({
            "type": "synthetic_lethal",
            "content": f"{ga} 与 {gb} 在 {condition} 下合成致死",
            "model": model_path,
            "model_lineage_version": lineage_version,
            "condition": condition,
            "evidence_tier": "EVIDENCE_math",
            "status": "unverified",
            "source_refs": [],
            "comparison_refs": [],
            "evidence_note": "双敲 LP 判定（单敲双活>1e-6 且双敲死<=1e-6）；"
                             "假设生成供实验设计参考，非结论",
        })
    return register_predictions(rows, path)


if __name__ == "__main__":
    # 双协议（stdin / argv 文件）+ selftest（临时路径，全功能演示）
    if "--selftest" in sys.argv:
        import tempfile
        d = tempfile.mkdtemp(prefix="ledger-selftest-")
        p = os.path.join(d, "predictions.jsonl")
        mk = lambda i: {"type": "essentiality", "content": f"g{i} 在 AB 培养基下必需",
                        "model": "F:/m.xml", "model_lineage_version": "0.1.4", "condition": "AB",
                        "evidence_tier": "EVIDENCE_rule", "status": "unverified"}
        r1 = register_predictions([mk(1), mk(2), mk(3)], path=p)
        assert r1["appended"] == 3 and not r1["warn"], r1
        # 幂等：同内容复跑不追加
        r2 = register_predictions([mk(1), mk(2), mk(3)], path=p)
        assert r2["appended"] == 0 and r2["skipped_duplicates"] == 3, r2
        # 新增一条 + ID 连续
        r3 = register_predictions([{**mk(4), "type": "phenotype",
                                    "content": "底物 Sucrose 预测不生长（文献=1，匹配=False）"}], path=p)
        assert r3["appended"] == 1 and r3["prediction_ids"] == ["P0004"], r3
        # query 过滤（前缀、可组合）
        q = query_ledger(rtype="essentiality", path=p)
        assert q["matched"] == 3 and q["total"] == 4, q
        q2 = query_ledger(condition="AB", status="unverified", path=p)
        assert q2["matched"] == 4, q2
        q3 = query_ledger(rtype="pheno", path=p)  # 前缀匹配
        assert q3["matched"] == 1, q3
        # update status + updated_at
        u = update_row("P0001", status="experimentally_verified",
                       source_refs=["doi:10.1000/test"], path=p)
        assert u["ok"] and u["row"]["status"] == "experimentally_verified" and "updated_at" in u["row"], u
        assert update_row("P0001", status="bogus", path=p)["ok"] is False
        assert update_row("P9999", status="unverified", path=p)["ok"] is False
        # corrupt 容错：手工塞坏行
        with open(p, "a", encoding="utf-8") as f:
            f.write('{"broken json...\n')
        rows, corrupt = load_rows(p)
        assert len(rows) == 4 and len(corrupt) == 1 and corrupt[0]["line"] == 5, corrupt
        s = ledger_summary(path=p)
        assert s["total"] == 4 and s["by_status"] == {"experimentally_verified": 1,
                                                      "unverified": 3}, s
        # update 不破坏坏行（重写后坏行仍在）
        update_row("P0002", status="literature_supported", path=p)
        rows2, corrupt2 = load_rows(p)
        assert len(corrupt2) == 1 and len(rows2) == 4, (corrupt2, rows2)
        print(json.dumps({"ok": True, "result": {"selftest": "pass", "summary": s}}))
    else:
        args = {}
        if len(sys.argv) > 1:
            with open(sys.argv[1], encoding="utf-8") as f:
                args = json.load(f)
        elif not sys.stdin.isatty():
            args = json.loads(sys.stdin.read())
        a = args.get("args", args)
        action = a.get("action", "list")
        lp = a.get("ledger_path")
        if action == "list":
            print(json.dumps({"ok": True, "result": query_ledger(
                limit=a.get("limit"), offset=a.get("offset", 0), path=lp)}, ensure_ascii=False))
        elif action == "query":
            print(json.dumps({"ok": True, "result": query_ledger(
                rtype=a.get("type"), status=a.get("status"), condition=a.get("condition"),
                model=a.get("model"), limit=a.get("limit"), offset=a.get("offset", 0),
                path=lp)}, ensure_ascii=False))
        elif action == "update":
            print(json.dumps({"ok": True, "result": update_row(
                a.get("prediction_id"), status=a.get("status"),
                source_refs=a.get("source_refs"), comparison_refs=a.get("comparison_refs"),
                path=lp)}, ensure_ascii=False))
        elif action == "summary":
            print(json.dumps({"ok": True, "result": ledger_summary(path=lp)}, ensure_ascii=False))
        else:
            print(json.dumps({"ok": False, "error": f"unknown ledger action: {action}（list|query|update|summary）"}))
