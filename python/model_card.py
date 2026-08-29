# model_card.py — 模型卡 schema v2 统一写入器（B' 收尾 + Q2 工程质量件）
# 职责: init_card（build 起卡）/ load / save / append_operation（lineage 版本递增 + changelog）
#       / set_verified_phenotypes（phenotype 结果）/ set_essential_genes（必需基因 + 证据分级）
# 纪律: 各工具完成后**仅当产物模型旁已有 card** 才向后追加；无卡不动（不凭空造卡）。
# 兼容: build.py 旧卡（无 schema 字段）读取时即时迁移到 v2（新增字段缺失不报错）。
# units: growth_rate 一律 mmol/gDW/h（schema v2 规定，勿用 1/h）。
import os
import json
import time

CARD_SUFFIX = ".card.json"
SCHEMA = "v2"
GROWTH_UNITS = "mmol/gDW/h"


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def card_path_for(model_path):
    return (model_path[:-4] if model_path.endswith(".xml") else model_path) + CARD_SUFFIX


def load_card(model_path):
    p = card_path_for(model_path)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_card(model_path, card):
    p = card_path_for(model_path)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    return p


def _ensure_v2(card):
    """legacy 卡（build 直写，无 schema/lineage）即时迁移到 v2。
    v3 卡（阶段A-M2 起，含 robustness 章节）视为已迁移，不降级。"""
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


def set_verified_phenotypes(model_path, phenotype_result, semantics="sole"):
    """phenotype_fix 结果写入 card.verified_phenotypes。
    phenotype_result: phenotype_fix() 返回（含 before/after/after_results/model）。"""
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
    card["verified_phenotypes"] = {
        "semantics": semantics, "units": GROWTH_UNITS,
        "matched": after.get("matched"), "total": after.get("total"),
        "rate": after.get("rate"), "source": "gem_phenotype", "updated_at": _now(),
        "results": rows,
    }
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
    """sensitivity 结果写入 card.robustness（阶段A-M2：schema v3 起步，向后兼容 v2 卡只增字段）。
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
        # 阶段A-M2：robustness 章节（v2→v3 只增字段）+ 无 card 不造卡
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
