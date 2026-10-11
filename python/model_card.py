# model_card.py — 模型卡 schema v2 统一写入器（工程质量件）
# 职责: init_card（build 起卡）/ load / save / append_operation（lineage 版本递增 + changelog）
#       / set_verified_phenotypes（phenotype 结果）/ set_essential_genes（必需基因 + 证据分级）
# 纪律: 各工具完成后**仅当产物模型旁已有 card** 才向后追加；无卡不动（不凭空造卡）。
# 兼容: build.py 旧卡（无 schema 字段）读取时即时迁移到 v2（新增字段缺失不报错）。
# units: growth_rate 一律 1/h（比生长速率；biomass 反应 gDW 归一化口径，数值 = μ）。
# 2026-09-21 由 mmol/gDW/h 演进为 1/h：数值不变、生物语义更准（schema 向后兼容，旧卡照读）。
# GM-2（2026-10-07 G 系列一期）：验证声明由实际 gate 结果推导（derive_validation_claim）；
#       旧卡读取时只读审计（audit_card_claims），不改写文件、不追认 PASS。
import os
import json
import time
import copy
import hashlib
import re

CARD_SUFFIX = ".card.json"
SCHEMA = "v2"
GROWTH_UNITS = "1/h"
CLAIM_POLICY_VERSION = "gem.card_claim.v3"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _model_sha256(path):
    if not path or not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def medium_identity(base_bounds, mode, test_results=None):
    """Hash applied base and per-substrate bounds, keeping the condition separate from a label."""
    payload = {"mode": mode, "base_bounds": base_bounds,
               "tests": [{"substrate": row.get("substrate"),
                          "applied_medium_bounds": row.get("applied_medium_bounds")}
                         for row in (test_results or [])]}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"), allow_nan=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_validation_claim(g3_status, g4_evidence=None, *, expected_model_sha256=None,
                            expected_medium_identity=None):
    """GM-2：验证标签由实际 gate 结果推导，不由补洞数/原生模型/文件存在推导。

    g4_evidence: None（未跑）或 dict，至少：
      status: G4 实跑状态（PASS/WARN/FAIL/SKIP）
      matched/total: 表型核对命中数/总数
      reference_identity: 参考数据身份（如表型表 sha256）
    规则：
      G3 PASS + G4 PASS + 有参考身份与检测数 → verified_G3_G4
      G3 PASS + G4 SKIP/未跑/WARN/FAIL       → verified_G3_only（G4 未通过如实呈现）
      G3 未 PASS                              → unverified
    返回 {claim, claim_policy_version, g3_status, g4, unsupported_reasons}。"""
    g4 = None
    if isinstance(g4_evidence, dict):
        g4 = {
            "status": g4_evidence.get("status"),
            "matched": g4_evidence.get("matched"),
            "total": g4_evidence.get("total"),
            "reference_identity": g4_evidence.get("reference_identity"),
            "model_sha256": g4_evidence.get("model_sha256"),
            "medium_identity": g4_evidence.get("medium_identity"),
        }
    reasons = []
    g3_pass = g3_status == "PASS"
    g4_pass = bool(g4 and g4.get("status") == "PASS")
    g4_evidenced = bool(g4 and isinstance(g4.get("total"), int) and g4["total"] > 0
                       and isinstance(g4.get("matched"), int)
                       and 0 <= g4["matched"] <= g4["total"]
                       and isinstance(g4.get("reference_identity"), str)
                       and re.fullmatch(r"sha256:[0-9a-f]{64}", g4["reference_identity"])
                       and expected_model_sha256 and g4.get("model_sha256") == expected_model_sha256
                       and expected_medium_identity and g4.get("medium_identity") == expected_medium_identity)
    if g3_pass and g4_pass and g4_evidenced:
        claim = "verified_G3_G4"
    elif g3_pass:
        claim = "verified_G3_only"
        if g4 is None:
            reasons.append("G4 未运行（无表型参考输入）——只能声明 G3-only")
        elif g4.get("status") == "SKIP":
            reasons.append("G4 SKIP——只能声明 G3-only")
        elif g4.get("status") in ("WARN", "FAIL"):
            reasons.append(f"G4 {g4.get('status')}——不能升格为 G3+G4")
        elif not g4_evidenced:
            reasons.append("G4 结果缺参考数据身份或检测数——不能声明 G3+G4")
    else:
        claim = "unverified"
        reasons.append(f"G3 {g3_status}——生长真实性未通过")
    return {"claim": claim, "claim_policy_version": CLAIM_POLICY_VERSION,
            "g3_status": g3_status, "g4": g4, "unsupported_reasons": reasons}


def audit_card_claims(card, model_path=None):
    """GM-2：旧卡只读审计——不改写文件、不追认 PASS。

    对 supported_mediums 每项核对 validation_status 与卡内实际 G4 证据：
      - 声明 verified_G3_G4 但卡内无 G4 明细（verified_phenotypes 无参考身份/检测数）
        → 该项标记 legacy_claim_unconfirmed。
      - 声明 verified_G3 / verified_G3_only → 一致（G3-only 语义，无 G4 断言）。
    返回 {per_medium: [...], claim_policy_version}；card 为 None → None。"""
    if not isinstance(card, dict):
        return None
    current_hash = _model_sha256(model_path)
    out = []
    for med in card.get("supported_mediums") or []:
        if not isinstance(med, dict):
            continue
        claim = med.get("validation_status")
        entry = {"medium_name": med.get("medium_name"), "claimed": claim}
        evidence = med.get("validation_evidence") or {}
        g3 = (evidence.get("g3") or {}).get("status")
        model_hash = evidence.get("model_sha256")
        medium_identity = med.get("medium_identity")
        derived = derive_validation_claim(
            g3, evidence.get("g4"), expected_model_sha256=current_hash,
            expected_medium_identity=medium_identity)
        valid_g3 = (g3 == "PASS" and current_hash and model_hash == current_hash
                    and medium_identity and evidence.get("medium_identity") == medium_identity)
        if valid_g3 and claim == derived["claim"]:
            entry["audited"] = "consistent"
            entry["effective"] = claim
        else:
            entry["audited"] = "legacy_claim_unconfirmed"
            entry["effective"] = "legacy_claim_unconfirmed"
            entry["reason"] = "缺少与当前模型和培养基一致的关卡证据；保留原声明但不背书"
        out.append(entry)
    return {"claim_policy_version": CLAIM_POLICY_VERSION, "per_medium": out}


def card_path_for(model_path):
    return (model_path[:-4] if model_path.endswith(".xml") else model_path) + CARD_SUFFIX


def load_card(model_path):
    p = card_path_for(model_path)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        card = json.load(f)
    result = copy.deepcopy(card)
    audit = audit_card_claims(result, model_path=model_path)
    result["validation_audit"] = audit
    by_index = audit["per_medium"] if audit else []
    for medium, item in zip(result.get("supported_mediums") or [], by_index):
        medium["claimed_validation_status"] = item["claimed"]
        medium["effective_validation_status"] = item["effective"]
        medium["audit_status"] = item["audited"]
    return result


def save_card(model_path, card):
    p = card_path_for(model_path)
    stored = copy.deepcopy(card)
    stored.pop("validation_audit", None)
    for medium in stored.get("supported_mediums") or []:
        if isinstance(medium, dict):
            for key in ("claimed_validation_status", "effective_validation_status", "audit_status"):
                medium.pop(key, None)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(stored, f, ensure_ascii=False, indent=2)
    return p


def _ensure_v2(card):
    """legacy 卡（build 直写，无 schema/lineage）即时迁移到 v2。
    v3 卡（含 robustness 章节）视为已迁移，不降级。"""
    if card.get("schema") in (SCHEMA, "v3") and "model_lineage" in card:
        return card
    card.setdefault("schema", SCHEMA)
    card.setdefault("growth_units", GROWTH_UNITS)
    card["model_lineage"] = {"version": "0.1.0", "operations": card.get("model_lineage", {}).get("operations", [])}
    card.setdefault("changelog", [])
    if not any("adopt" in str(x).lower() for x in card["changelog"]):
        card["changelog"].append(f"{_now()} legacy card adopted into schema v2 (build-time fields preserved)")
    return card


def init_card(model_path, name=None, engine=None, changelog_note="build", **fields):
    """build 起卡：已有字段 + schema v2 基座（lineage v0.1.0 起始, changelog=[build]）。幂等。"""
    existing = load_card(model_path)
    fresh = existing is None
    card = existing or {"name": name or os.path.basename(model_path), "model": model_path,
                        "created": _now()}
    if engine:
        card.setdefault("engine", engine)
    card.update({k: v for k, v in fields.items() if v is not None})
    if fresh:
        card["schema"] = SCHEMA
        card.setdefault("growth_units", GROWTH_UNITS)
        card["model_lineage"] = {"version": "0.1.0", "operations": []}
        card["changelog"] = []
        if changelog_note:
            card["changelog"].append(f"{_now()} {changelog_note}")
    else:
        _ensure_v2(card)
    p = save_card(model_path, card)
    return card, p


def propagate_card(src_model_path, dst_model_path):
    """把源模型旁的 card 复制到派生产物旁（dst 已有卡则不动）。
    派生工具（gapfill/l3_fix/phenotype_fix/biomass_apply）产物是新文件——
    先传播再 append_operation，保证派生模型自带完整 lineage。"""
    sp, dp = card_path_for(src_model_path), card_path_for(dst_model_path)
    if os.path.exists(sp) and not os.path.exists(dp):
        import shutil
        shutil.copyfile(sp, dp)
        return dp
    return None


def append_operation(model_path, operation, reactions_added=0, reactions_removed=0, detail=None):
    """模型旁已有 card 时追加操作记录：lineage 版本递增（patch 号）+ changelog push。
    无 card 返回 None（调用方不应凭空造卡）。"""
    card = load_card(model_path)
    if card is None:
        return None
    _ensure_v2(card)
    lin = card["model_lineage"]
    major, minor, patch = str(lin.get("version", "0.1.0")).split(".")
    lin["version"] = f"{major}.{minor}.{int(patch) + 1}"
    op = {"seq": len(lin["operations"]) + 1, "at": _now(), "operation": operation,
          "reactions_added": reactions_added, "reactions_removed": reactions_removed}
    if detail:
        op["detail"] = detail
    lin["operations"].append(op)
    card["changelog"].append(f"{_now()} {operation} (+{reactions_added}/-{reactions_removed})"
                             + (f" — {json.dumps(detail, ensure_ascii=False)[:160]}" if detail else ""))
    save_card(model_path, card)
    return card


def set_verified_phenotypes(model_path, phenotype_result, semantics="sole", reference_identity=None):
    """phenotype_fix 结果写入 card.verified_phenotypes。
    phenotype_result: phenotype_fix() 返回（含 before/after/after_results/model）。
    reference_identity: 表型参考数据身份（如 TSV 的 sha256）——GM-2 起记录，供声明审计。"""
    card = load_card(model_path)
    if card is None:
        return None
    _ensure_v2(card)
    after = phenotype_result.get("after") or {}
    rows = []
    for r in (phenotype_result.get("after_results") or []):
        rows.append({"substrate": r.get("substrate"), "published": r.get("published"),
                     "predicted": r.get("predicted"), "growth": r.get("growth"),
                     "exchange": r.get("exchange"), "match": r.get("match"),
                     "source": "phenotype_fix/G4-sole"})
    current_hash = _model_sha256(model_path)
    gate_report = phenotype_result.get("after_gate_report") or {}
    g3 = gate_report.get("g3") or {}
    g4 = gate_report.get("g4") or {}
    base_bounds = phenotype_result.get("resolved_medium_bounds")
    mid = medium_identity(base_bounds, semantics, g4.get("results") or []) if base_bounds is not None else None
    evidence_g4 = {"status": g4.get("status"), "matched": g4.get("matched"),
                   "total": g4.get("total"), "reference_identity": reference_identity,
                   "model_sha256": current_hash, "medium_identity": mid,
                   "carbon_mode": semantics}
    claim = derive_validation_claim(g3.get("status"), evidence_g4,
                                    expected_model_sha256=current_hash,
                                    expected_medium_identity=mid)
    card["verified_phenotypes"] = {
        "semantics": semantics, "units": GROWTH_UNITS,
        "matched": after.get("matched"), "total": after.get("total"),
        "rate": after.get("rate"), "source": "gem_phenotype", "updated_at": _now(),
        "reference_identity": reference_identity,
        "model_sha256": current_hash, "medium_identity": mid,
        "gate_status": g4.get("status"), "claim": claim["claim"],
        "results": rows,
    }
    condition_name = (phenotype_result.get("medium_preset") or "custom") + "/" + semantics
    supported = card.setdefault("supported_mediums", [])
    entry = next((item for item in supported if item.get("medium_identity") == mid
                  and item.get("medium_name") == condition_name), None)
    if entry is None:
        entry = {"medium_name": condition_name}
        supported.append(entry)
    entry.update({"medium_identity": mid, "validation_status": claim["claim"],
                  "validation_evidence": {"policy": CLAIM_POLICY_VERSION,
                                          "model_sha256": current_hash,
                                          "medium_identity": mid,
                                          "g3": g3, "g4": evidence_g4,
                                          "unsupported_reasons": claim["unsupported_reasons"]}})
    save_card(model_path, card)
    return card


def set_essential_genes(model_path, scan_result, model=None):
    """essential_scan 结果写入 card.essential_genes。
    evidence_level: high_confidence 默认；基因支撑反应含 EVIDENCE_math（l3_fix 数学连接/
    bounds 放宽）→ contains_EVIDENCE_math（必需性判定可能被数学证据反应影响，标注降置信）。"""
    card = load_card(model_path)
    if card is None:
        return None
    _ensure_v2(card)
    math_rxn_ids = set()
    if model is not None:
        for r in model.reactions:
            n = r.notes or {}
            if n.get("evidence") == "EVIDENCE_math" or n.get("bound_relaxed_by"):
                math_rxn_ids.add(r.id)
    genes = []
    for gid in (scan_result.get("essential_genes") or []):
        lvl = "high_confidence"
        try:
            if model is not None and any(r.id in math_rxn_ids for r in model.genes.get_by_id(gid).reactions):
                lvl = "contains_EVIDENCE_math"
        except KeyError:
            pass
        genes.append({"gene_id": gid, "evidence_level": lvl})
    n_math = sum(1 for g in genes if g["evidence_level"] != "high_confidence")
    card["essential_genes"] = {
        "units": GROWTH_UNITS, "medium_preset": scan_result.get("medium_preset"),
        "wt_growth": scan_result.get("wt_growth"), "count": len(genes),
        "n_contains_EVIDENCE_math": n_math, "source": "gem_essentiality", "updated_at": _now(),
        "genes": genes,
    }
    save_card(model_path, card)
    return card


def set_robustness(model_path, sensitivity_result):
    """sensitivity 结果写入 card.robustness（schema v3 起步，向后兼容 v2 卡只增字段）。
    sensitivity_result: sensitivity() 返回（含 wt_growth_grid/stability/component_sensitivity/gam_carrier）。
    无 card 返回 None（不凭空造卡——纪律同 set_essential_genes）。"""
    card = load_card(model_path)
    if card is None:
        return None
    _ensure_v2(card)
    card["schema"] = "v3"  # v3 = v2 + robustness 章节（读取方按 JSON 字段访问，向后兼容）
    grid = sensitivity_result.get("wt_growth_grid") or []
    stab = sensitivity_result.get("stability") or {}
    comp = (sensitivity_result.get("component_sensitivity") or {}).get("top_sensitive") or []
    card["robustness"] = {
        "units": GROWTH_UNITS,
        "combinations": sensitivity_result.get("combinations"),
        "baseline_reproduced": sensitivity_result.get("baseline_reproduced"),
        "wt_growth_grid": [{"biomass": r.get("biomass"), "gam": r.get("gam"),
                            "growth": r.get("growth"), "essential_count": r.get("essential_count")}
                           for r in grid],
        "stability": {"always_essential_count": len(stab.get("always_essential") or []),
                      "always_essential": stab.get("always_essential") or [],
                      "conditionally_essential_count": len(stab.get("conditionally_essential") or []),
                      "conditionally_essential": stab.get("conditionally_essential") or [],
                      "never_essential_count": stab.get("never_essential_count")},
        "component_sensitivity_top": comp,
        "gam_carrier": sensitivity_result.get("gam_carrier"),
        "source": "gem_sensitivity", "updated_at": _now(),
    }
    save_card(model_path, card)
    return card


if __name__ == "__main__":
    import sys, tempfile
    # 自检（smoke 用，纯 JSON 层，秒级）：init → append ×2 → 版本递增 → phenotype/essential 形状
    if "--selftest" in sys.argv:
        d = tempfile.mkdtemp(prefix="card-selftest-")
        mp = os.path.join(d, "selftest.xml")
        open(mp, "w").close()
        card, p = init_card(mp, name="selftest", engine="test", validations_m9={"g1": "PASS"})
        assert card["model_lineage"]["version"] == "0.1.0" and card["schema"] == "v2"
        c1 = append_operation(mp, "gapfill", reactions_added=2, detail={"note": "sucrose L1"})
        assert c1["model_lineage"]["version"] == "0.1.1" and len(c1["changelog"]) == 2
        c2 = append_operation(mp, "l3_fix", reactions_added=3)
        assert c2["model_lineage"]["version"] == "0.1.2" and c2["model_lineage"]["operations"][0]["reactions_added"] == 2
        set_verified_phenotypes(mp, {"after": {"matched": 13, "total": 19, "rate": 0.684},
                                     "after_results": [{"substrate": "Arabinose", "published": 1,
                                                        "predicted": 1, "match": True}]})
        set_essential_genes(mp, {"essential_genes": ["NC_003062_2_1", "NC_003062_2_2"],
                                 "wt_growth": 0.52, "medium_preset": "AB"}, model=None)
        back = load_card(mp)
        assert back["verified_phenotypes"]["matched"] == 13
        assert back["essential_genes"]["count"] == 2
        assert back["essential_genes"]["genes"][0]["evidence_level"] == "high_confidence"
        # robustness 章节（v2→v3 只增字段）+ 无 card 不造卡
        assert set_robustness(mp + ".nonexistent", {"wt_growth_grid": []}) is None
        c4 = set_robustness(mp, {"combinations": 22, "baseline_reproduced": True,
                                 "wt_growth_grid": [{"biomass": 1.0, "gam": 40.0, "growth": 0.519981,
                                                     "essential_count": 155}],
                                 "stability": {"always_essential": ["g1"], "conditionally_essential": [],
                                               "never_essential_count": 0},
                                 "component_sensitivity": {"top_sensitive": [{"component": "cpd00023_c0",
                                                                             "delta_pct": -12.5}]},
                                 "gam_carrier": {"type": "inside_biomass", "gam_orig": 40.0}})
        assert c4["schema"] == "v3" and c4["robustness"]["combinations"] == 22
        assert c4["essential_genes"]["count"] == 2 and c4["model_lineage"]["version"] == "0.1.2"
        # legacy 迁移
        legacy = {"name": "legacy", "engine": "carveme", "growth_g3_m9": 0.782}
        with open(card_path_for(mp), "w", encoding="utf-8") as f:
            json.dump(legacy, f)
        c3 = append_operation(mp, "gapfill", reactions_added=1)
        assert c3["schema"] == "v2" and c3["model_lineage"]["version"] == "0.1.1" and c3["growth_g3_m9"] == 0.782
        print('{"ok": true, "result": {"selftest": "pass", "version_after": "%s"}}' % back["model_lineage"]["version"])
    else:
        import json as _j
        args = _j.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
        model = args.get("model")
        op = args.get("action", "show")
        if op == "init":
            card, p = init_card(model, name=args.get("name"), engine=args.get("engine"), **(args.get("fields") or {}))
            print(_j.dumps({"ok": True, "result": {"card": p, "version": card["model_lineage"]["version"]}}))
        else:
            print(_j.dumps({"ok": True, "result": load_card(model)}))
