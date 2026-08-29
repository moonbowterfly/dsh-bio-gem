# budget.py — 防过补第五闸门（B' 后半新增，全局预算）
# 规格: PROMPT-L3 §2「单模型补洞历史累计新增反应数 ≤ max(5, 模型总反应数*5%)；
#       超限返回 budget_exceeded + confirm_required，需显式 confirm_budget=true 才能继续」。
# 计数口径: 反应 notes["source"] ∈ {gem-gapfill, gem-l3fix}（两代补洞的 provenance 约定）。
# 用法: budget_gate(m, planned=2, confirm_budget=False) -> None（放行）或 budget_exceeded dict
GAP_SOURCE_TAGS = ("gem-gapfill", "gem-l3fix")


def prior_added(m):
    """模型补洞历史：带补洞 provenance 标记的反应数。"""
    n = 0
    for r in m.reactions:
        src = (r.notes or {}).get("source")
        if src in GAP_SOURCE_TAGS:
            n += 1
    return n


def budget_for(m):
    """预算上限 = max(5, 模型总反应数 * 5%)。"""
    return max(5, int(len(m.reactions) * 0.05))


def budget_gate(m, planned=0, confirm_budget=False):
    """第五闸门：历史累计 + 本批 planned 是否超预算。
    返回 None = 放行；dict = 超限（调用方原样返回给协议层，含 confirm_required）。"""
    hist = prior_added(m)
    cap = budget_for(m)
    if hist + planned <= cap:
        return None
    return {
        "error": "budget_exceeded",
        "confirm_required": True,
        "prior_added": hist,
        "planned": planned,
        "budget": cap,
        "note": "补洞历史累计新增反应已超 max(5, 5%·总反应数) 预算；"
                "继续需显式传 confirm_budget=true（防过补第五闸门）",
    }


if __name__ == "__main__":
    import json, sys
    raw = sys.stdin.read() if not sys.stdin.isatty() else (sys.argv[1] if len(sys.argv) > 1 else "{}")
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if (sys.stdin.isatty() and len(sys.argv) > 1) else json.loads(raw or "{}")
    cap = max(5, int(args.get("n_reactions", 100) * 0.05))
    hist = args.get("prior_added", 0)
    planned = args.get("planned", 0)
    if hist + planned <= cap:
        print(json.dumps({"ok": True, "budget": cap, "prior_added": hist, "planned": planned}))
    else:
        print(json.dumps({"error": "budget_exceeded", "confirm_required": args.get("confirm_budget") is not True,
                          "budget": cap, "prior_added": hist, "planned": planned}))
