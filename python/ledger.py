# ledger.py — 阶段A-M3 prediction ledger（预测账本，可追踪可实验兑现）
# 文件: ~/.dsh/dsh-bio-gem/ledger/<模型名>.jsonl（一个模型一个账本，2026-08-31 用户决策）
#       ——默认账本按模型文件 basename 推导（model_ledger_path），显式 ledger_path 仍可覆盖；
#       ——无 model 无 path 的查询/摘要 = 聚合所有模型账本（全局视图，by_model 分布保留）。
# 迁移：旧全局 predictions.jsonl 已拆分为各模型账本（predictions.jsonl.legacy-20260831 保留备份，
#       不再作为活动账本）。
# 幂等: 同 model+condition+type+content 哈希去重，重复运行不追加。
# 完整性: 逐行 JSON 解析校验，损坏行跳过并在返回里报 corrupt_rows + 行号（不阻塞）；
#         写入失败只 WARN 不使主流程失败。update 重写文件但保留损坏行原样（不删行）。
# 证据分级优先级（任务书锁定）: EVIDENCE_literature > EVIDENCE_sequence > EVIDENCE_rule > EVIDENCE_math
import os
import re
import sys
import json
import hashlib
from datetime import datetime

LEDGER_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "ledger")
LEDGER_PATH = os.path.join(LEDGER_DIR, "predictions.jsonl")  # 兼容 fallback（迁移后非活动账本）

TIER_PRIORITY = ["EVIDENCE_literature", "EVIDENCE_sequence", "EVIDENCE_rule", "EVIDENCE_math"]  # 高→低
STATUSES = ("unverified", "literature_supported", "literature_contradicted", "experimentally_verified")
TYPES = ("essentiality", "phenotype", "synthetic_lethal", "secretion", "other")
DEFAULT_TIER = "EVIDENCE_rule"


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _content_hash(model, condition, rtype, content):
    # 阶段D-E2E P1：Windows 路径斜杠/大小写差异（"F:/a" vs "F:\a"）会使同一模型的
    # 重复登记漏过去重——hash 前做 normcase+normpath 归一化（首登记的存储格式不变）。
    m = os.path.normcase(os.path.normpath(model)) if model else ""
    raw = "\x1f".join([m, condition or "", rtype or "", content or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _norm_path(p):
    """路径归一化（normcase+normpath）——防正/反斜杠差异导致前缀匹配失败。"""
    return os.path.normcase(os.path.normpath(p or ""))


# ---------------------------------------------------------------------------
# 账本文件解析：一个模型一个账本（2026-08-31 用户决策）
# ---------------------------------------------------------------------------
def model_ledger_path(model_path):
    """默认账本 = ledger/<模型文件名去扩展名>.jsonl（一个模型一个账本）。"""
    base = os.path.splitext(os.path.basename(model_path or ""))[0] or "default"
    base = re.sub(r"[^A-Za-z0-9_.-]+", "_", base).strip("._") or "default"
    return os.path.join(LEDGER_DIR, base + ".jsonl")


def _ledger_file_list():
    """活动账本文件：ledger/*.jsonl（排除旧全局 predictions.jsonl 与 .bak/.legacy 备份）。"""
    if not os.path.isdir(LEDGER_DIR):
        return []
    files = []
    for x in sorted(os.listdir(LEDGER_DIR)):
        if not x.endswith(".jsonl"):
            continue
        if x == "predictions.jsonl":
            continue  # 旧全局账本（迁移后仅作 legacy 备份，不算活动账本）
        if ".bak" in x.lower() or ".legacy" in x.lower():
            continue
        files.append(os.path.join(LEDGER_DIR, x))
    return files


def load_rows(path=None):
    """读单个账本文件全部行；损坏行跳过。返回 (rows, corrupt:[{line,error}])。文件不存在 -> ([], [])。"""
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


def load_all_rows():
    """聚合所有活动账本（一个模型一个账本 -> 全局视图）。返回 (rows, corrupt)。"""
    rows, corrupt = [], []
    for f in _ledger_file_list():
        r, c = load_rows(f)
        rows.extend(r)
        corrupt.extend(c)
    return rows, corrupt


def _resolve_rows(path=None, model=None):
    """按路径/模型解析要读的账本行：显式 path > model 账本 > 聚合所有。"""
    if path:
        return load_rows(path)
    if model:
        return load_rows(model_ledger_path(model))
    return load_all_rows()


def _max_id_num(rows):
    mx = 0
    for r in rows:
        pid = str(r.get("prediction_id") or "")
        if pid.startswith("P") and pid[1:].isdigit():
            mx = max(mx, int(pid[1:]))
    return mx


def register_predictions(new_rows, path=None):
    """追加式登记（幂等）。默认账本按新行 model 推导（一个模型一个账本）；path 显式则覆盖。
    返回 {appended, skipped_duplicates, total_after, corrupt_rows, path,
    prediction_ids, warn}；写入失败仅 WARN（返回内附 warn 字段），绝不抛异常阻塞主流程。"""
    if not path and new_rows:
        path = model_ledger_path((new_rows[0] or {}).get("model"))
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
                 limit=None, offset=0, path=None, deprecated=None):
    """条件过滤（type/status/condition/model 前缀匹配、大小写不敏感、可组合）+ 分页。
    账本定位：显式 path > model（该模型自己的账本）> 聚合所有模型账本。
    阶段D-P2：deprecated 过滤（True=仅打标行；False=仅未打标行；缺省=全部）。"""
    rows, corrupt = _resolve_rows(path=path, model=model)

    def _pref(v, q):
        return str(v or "").lower().startswith(str(q).lower())

    def _pref_model(v, q):
        # 模型匹配：路径前缀（归一化斜杠/大小写）或 basename 相同（一个模型一个账本的身份=模型名）
        v, q = v or "", q or ""
        if _norm_path(v).startswith(_norm_path(q)):
            return True
        return os.path.splitext(os.path.basename(v))[0] == os.path.splitext(os.path.basename(q))[0]

    def _dep(r):
        return bool(r.get("deprecated"))

    hits = [r for r in rows
            if (not rtype or _pref(r.get("type"), rtype))
            and (not status or _pref(r.get("status"), status))
            and (not condition or _pref(r.get("condition"), condition))
            and (not model or _pref_model(r.get("model"), model))
            and (deprecated is None or _dep(r) == bool(deprecated))]
    sliced = hits[offset:] if not limit else hits[offset:offset + limit]
    return {"total": len(rows), "matched": len(hits), "offset": offset,
            "results": sliced, "corrupt_rows": len(corrupt), "corrupt": corrupt}


def mark_deprecated_duplicates(path=None, progress=None):
    """阶段D-P2：跨斜杠风格重复打标（一次性运维用；不删行、不改既有字段）。
    识别规则：同一归一化 content hash 的行组内，model 含反斜杠且存在正斜杠同预测 -> 打标
    deprecated=true + superseded_by=<正斜杠版 prediction_id>。path=None 时对每个活动账本执行。
    返回聚合计数。"""
    files = [path] if path else _ledger_file_list()
    total = {"path": None, "marked": 0, "rows_total": 0, "corrupt_rows": 0, "per_file": {}}
    for p in files:
        rows, corrupt = load_rows(p)
        by_hash = {}
        for r in rows:
            h = _content_hash(r.get("model"), r.get("condition"), r.get("type"), r.get("content"))
            by_hash.setdefault(h, []).append(r)
        marks = {}
        for group in by_hash.values():
            if len(group) < 2:
                continue
            keepers = [r for r in group if "\\" not in (r.get("model") or "")]
            dupes = [r for r in group if "\\" in (r.get("model") or "")]
            if not keepers or not dupes:
                continue
            keeper = sorted(keepers, key=lambda r: r.get("prediction_id") or "")[0]
            for d in dupes:
                marks[d.get("prediction_id")] = keeper.get("prediction_id")
        marked = 0
        if marks:
            with open(p, encoding="utf-8") as f:
                lines = f.readlines()
            with open(p, "w", encoding="utf-8", newline="") as f:
                for line in lines:
                    stripped = line.strip()
                    try:
                        obj = json.loads(stripped)
                    except Exception:
                        f.write(line)  # 损坏行原样保留
                        continue
                    pid = obj.get("prediction_id") if isinstance(obj, dict) else None
                    if pid in marks and not obj.get("deprecated"):
                        obj["deprecated"] = True
                        obj["deprecated_note"] = (f"Windows 路径斜杠风格重复；同预测见 "
                                                  f"prediction_id {marks[pid]}（正斜杠版）")
                        obj["superseded_by"] = marks[pid]
                        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
                        marked += 1
                    else:
                        f.write(line if line.endswith("\n") else line + "\n")
        if progress:
            progress(f"[ledger] {os.path.basename(p)}: marked {marked} deprecated rows (of {len(rows)})")
        total["marked"] += marked
        total["rows_total"] += len(rows)
        total["corrupt_rows"] += len(corrupt)
        total["per_file"][os.path.basename(p)] = {"marked": marked, "rows": len(rows)}
    total["path"] = path or LEDGER_DIR
    return total


def _update_file(prediction_id, p, status=None, source_refs=None, comparison_refs=None):
    """单文件内按 prediction_id 更新 status/source_refs/comparison_refs，维护 updated_at。
    文件重写但逐行保留（损坏行原样保留，不删行）。文件不存在 -> None（调用方继续找）。"""
    if not os.path.exists(p):
        return None
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
                if isinstance(obj, dict) and obj.get("prediction_id") == prediction_id:
                    f.write(json.dumps(updated_row, ensure_ascii=False) + "\n")
                    found -= 1
                else:
                    f.write(line if line.endswith("\n") else line + "\n")
    except Exception as e:
        return {"ok": False, "error": f"ledger update write failed: {type(e).__name__}: {e}"}
    return {"ok": True, "updated": prediction_id, "row": updated_row,
            "corrupt_rows": len(corrupt)}


def update_row(prediction_id, status=None, source_refs=None, comparison_refs=None, path=None):
    """按 prediction_id 更新。显式 path 定位单文件；path=None 时遍历所有活动账本找 id
    （各账本独立编号后 prediction_id 只在账本内唯一——全局扫确保能找到）。"""
    if status is not None and status not in STATUSES:
        return {"ok": False, "error": f"invalid status {status!r}（{STATUSES}）"}
    if path:
        return _update_file(prediction_id, path, status=status,
                            source_refs=source_refs, comparison_refs=comparison_refs)
    files = _ledger_file_list()
    if os.path.exists(LEDGER_PATH) and LEDGER_PATH not in files:
        files = files + [LEDGER_PATH]  # 兼容旧全局账本（若还在）
    for f in files:
        r = _update_file(prediction_id, f, status=status,
                         source_refs=source_refs, comparison_refs=comparison_refs)
        if r is not None:
            return r
    return {"ok": False, "error": f"prediction_id not found: {prediction_id} (no ledger file contains it)"}


def ledger_summary(path=None, model=None):
    """账本摘要：{total, by_status, by_type, by_model, deprecated_count}。
    账本定位：显式 path > model（该模型自己的账本）> 聚合所有模型账本。
    P1-3（2026-08-31）：按模型给 own_model_entries=该模型账本条数，防「把全局账本当作本模型预测」误读。
    2026-08-31 用户决策后：一个模型一个账本，model 定位时 total 即该模型预测数。"""
    rows, corrupt = _resolve_rows(path=path, model=model)
    by_status, by_type, by_model = {}, {}, {}
    dep = 0
    own = None
    if model:
        own = len(rows)  # 该模型账本的行数（一个模型一个账本）
    for r in rows:
        s = r.get("status") or "unspecified"
        t = r.get("type") or "other"
        mm = r.get("model") or ""
        by_status[s] = by_status.get(s, 0) + 1
        by_type[t] = by_type.get(t, 0) + 1
        if r.get("deprecated"):
            dep += 1
        if mm:
            by_model[mm] = by_model.get(mm, 0) + 1
    rep = {"total": len(rows), "by_status": by_status, "by_type": by_type,
           "by_model": by_model, "deprecated_count": dep, "corrupt_rows": len(corrupt)}
    if model is not None or own is not None:
        rep["own_model_entries"] = own
        rep["own_model_note"] = ("own_model_entries=该模型账本条数（一个模型一个账本："
                                 "账本文件按模型名分，这里就是本模型预测全量；"
                                 "聚合视图的全局分布见 by_model/其他账本）")
    return rep


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
    """gem_essentiality 自动登记：每必需基因一条（type=essentiality，status=unverified）。
    path=None 时按 model_path 写入该模型的账本（model_ledger_path）。"""
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
            **( {"evidence_note": "支撑反应无 evidence 标注，默认 EVIDENCE_rule"}
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
            **( {"evidence_note": "底物交换反应无 evidence 标注，默认 EVIDENCE_rule"}
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
        # 阶段D P1：model 路径斜杠/大小写不同但指向同一文件 -> 仍判重复
        r2b = register_predictions([{**mk(1), "model": "f:\\m.XML"}], path=p)
        assert r2b["appended"] == 0 and r2b["skipped_duplicates"] == 1, r2b
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
        # 阶段D-P2：直写一条反斜杠 model 的历史态重复行（模拟归一化修复前的存量），
        # mark_deprecated_duplicates 打标（不删行）+ query 过滤 + summary deprecated_count
        n_before = len(load_rows(p)[0])
        with open(p, "a", encoding="utf-8") as f:
            hist = dict(mk(1))
            hist["model"] = "f:\\m.xml"  # 同预测、反斜杠风格（历史态）
            f.write(json.dumps(hist, ensure_ascii=False) + "\n")
        mk_res = mark_deprecated_duplicates(path=p)
        assert mk_res["marked"] == 1, mk_res
        rows3, _ = load_rows(p)
        assert len(rows3) == n_before + 1  # 不删行：仅 +1 条历史行
        dep_rows = [r for r in rows3 if r.get("deprecated")]
        assert len(dep_rows) == 1 and dep_rows[0]["superseded_by"] == "P0001", dep_rows
        q_dep = query_ledger(deprecated=True, path=p)
        assert q_dep["matched"] == 1
        q_ok = query_ledger(deprecated=False, rtype="essentiality", path=p)
        assert q_ok["matched"] == 3  # 原始 3 条 essentiality 不受影响
        s3 = ledger_summary(path=p)
        assert s3["deprecated_count"] == 1 and s3["total"] == n_before + 1, s3
        # 2026-08-31：一个模型一个账本——model_ledger_path 按 basename 推导
        assert model_ledger_path("F:/x/C58.xml") == os.path.join(LEDGER_DIR, "C58.jsonl")
        assert model_ledger_path("C:\\Users\\u\\.dsh\\dsh-bio-gem\\models\\LBA9402.xml") == \
            os.path.join(LEDGER_DIR, "LBA9402.jsonl")
        assert model_ledger_path("") == os.path.join(LEDGER_DIR, "default.jsonl")
        # register path=None -> 该模型账本文件
        dir2 = tempfile.mkdtemp(prefix="ledger-selftest-")
        import importlib
        _ld = os.path.dirname(os.path.abspath(__file__))
        import tempfile as _tf
        # 用临时目录覆盖 LEDGER_DIR 验证默认推导（不污染真实账本）
        old_dir = LEDGER_DIR
        try:
            ledger_mod = sys.modules[__name__]
            ledger_mod.LEDGER_DIR = dir2
            ledger_mod.LEDGER_PATH = os.path.join(dir2, "predictions.jsonl")
            rn = register_predictions([mk(1)])
            assert rn["appended"] == 1 and rn["path"] == os.path.join(dir2, "m.jsonl"), rn
            qn = query_ledger(model="F:/m.xml")
            assert qn["total"] == 1 and qn["matched"] == 1, qn
            sn = ledger_summary(model="F:/m.xml")
            assert sn["total"] == 1 and sn["own_model_entries"] == 1, sn
        finally:
            ledger_mod.LEDGER_DIR = old_dir
            ledger_mod.LEDGER_PATH = os.path.join(old_dir, "predictions.jsonl")
        print(json.dumps({"ok": True, "result": {"selftest": "pass", "summary": s3}}))
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
            print(json.dumps({"ok": True, "result": ledger_summary(
                path=lp, model=a.get("model"))}, ensure_ascii=False))
        else:
            print(json.dumps({"ok": False, "error": f"unknown ledger action: {action}（list|query|update|summary）"}))