# l3_fix.py — B' 后半：L3 内部路径补洞（两级）
#   L3a 模型内连通性: 先"全内部反应放开方向"LP 预检（快速严谨判负），可行才用 cobra GapFiller
#        universal=模型自身反应池（放开方向副本，NEW id），MILP 选最小集 → 对原反应放宽 bounds（不复制反应）。
#        注: cobra 0.32.1 GapFiller(universal=None) 语义是"空反应池"（只加 demand），不是"模型自身反应池"
#        ——故本实现显式构造自身反应池（2026-08-29 读 cobra 源码确认）。
#   L3b 白名单 + BiGG 反应式: 白名单命中集（build_whitelist B0/B1，缓存 ~/.dsh/dsh-bio-gem/whitelist/）
#        → MetaCyc rxn ID 桥（EC 号/名字规约 + gapseq all-Reactions.tbl 增强）→ iML1515 反应式
#        → 代谢物移植（名字规约匹配 -> COFACTOR_BRIDGE 静态桥[公式/电荷校验] -> 随反应引入新代谢物）。
#        无匹配不强补。PTS 型反应一律排除（Rhizobiaceae 等 PTS-less 机体守则）。
#   证据分级: EVIDENCE_sequence（白名单桥接）/ EVIDENCE_math（LP/MILP 连通性，最弱）；
#        L1/L2 规则补洞为 EVIDENCE_rule（见 gapfill.py）。notes["evidence"] + notes["source"]=gem-l3fix。
#   防过补第五闸门: budget.py（累计新增 ≤ max(5, 5%·总反应)），超限 confirm_required=true 才放行。
#   补后重验: validate G1-G6 全跑；G6 WARN/FAIL → 回滚本批全部改动（删新增反应 + 还原 bounds）。
# 协议: stdout 仅最后一行 JSON（进度走 stderr）；-I 隔离模式 sys.path 显式插入。
import os
import re
import sys
import json
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cobra
from cobra.flux_analysis.gapfilling import GapFiller

EX_PREFIX = ("EX_", "DM_", "SK_")
SOURCE_TAG = "gem-l3fix"
NEW_RXN_SUFFIX = "_l3fix"

# BiGG 基名 -> ModelSEED cpd 号（2026-08-29 本机以 C58 名字+公式+电荷逐一验证；
# 映射时仍做公式/电荷校验，不一致即弃用防静默污染化学计量）
COFACTOR_BRIDGE = {
    "h2o": "00001", "atp": "00002", "nad": "00003", "nadh": "00004",
    "nadp": "00005", "nadph": "00006", "o2": "00007", "adp": "00008",
    "pi": "00009", "coa": "00010", "co2": "00011", "nh4": "00013",
    "glu__L": "00023", "akg": "00024", "gln__L": "00053", "pyr": "00020",
    "accoa": "00022", "succ": "00036", "so4": "00048", "pep": "00061",
    "h": "00067", "f6p": "00072", "g6p": "00079", "e4p": "00236",
    "mal__L": "00130", "r5p": "00101", "ru5p": "00171", "xu5p": "00198",
}
COMP_MAP = {"c": "c0", "e": "e0", "p": "p0"}  # BiGG 区室后缀 -> gapseq 区室 id

WHITELIST_DIR = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "whitelist")
# 本地白名单数据库（license 守则: 仅本机，不进 git/发布包；GEM_WHITELIST_DB_DIR 可覆盖）
RXN_DB_DIR = os.environ.get("GEM_WHITELIST_DB_DIR", r"D:\Program\hermes\temp\gem_whitelist")
DEFAULT_UNIVERSAL = r"D:\Program\hermes\temp\gem_universal\iML1515.xml"

PTS_RE = re.compile(r"(?i)\bpts\b|phosphotransferase|pep:pyr")


def _note(*a):
    print(*a, file=sys.stderr)


def norm_name(s):
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def met_name_key(met):
    """代谢物名规约键：去常见区室尾巴（-c0/-c/-e0 等）后小写字母数字。"""
    nm = (met.name or "").strip()
    nm = re.sub(r"[-_ ]?c[0ep]0?$", "", nm, flags=re.I)
    return norm_name(nm)


def _bigg_base_comp(met_id):
    mm = re.match(r"^(.*)__?([cepn])$", met_id)
    if mm:
        return mm.group(1), mm.group(2)
    parts = met_id.rsplit("_", 1)
    return (parts[0], "c") if len(parts) == 2 and parts[1] in COMP_MAP else (met_id, "c")


def _formula_ok(met_a, met_b):
    fa, fb = (met_a.formula or "").replace(" ", ""), (met_b.formula or "").replace(" ", "")
    if fa and fb and fa.upper() != fb.upper():
        return False
    if met_a.charge is not None and met_b.charge is not None and met_a.charge != met_b.charge:
        return False
    return True


def build_met_name_index(m):
    idx = {}
    for x in m.metabolites:
        idx.setdefault(met_name_key(x), []).append(x)
    return idx


def _has_carbon(formula):
    """元素级含碳判断（裸子串会把 Ca/Cl/Co/Cu 误判为含碳——2026-08-29 实测氯缺失致 sole 全灭）。"""
    from validate import parse_formula
    return "C" in parse_formula(formula or "")


def sole_medium(m, resolved_med, ex_id, lb=-10.0):
    """唯一碳源语义: 基底介质去掉含碳交换 + 目标底物交换打开。返回 {EX_id: lb}。"""
    med = {}
    for rid, v in (resolved_med or {}).items():
        if rid in m.reactions:
            met = list(m.reactions.get_by_id(rid).metabolites)[0]
            if met.formula and _has_carbon(met.formula):
                continue
            med[rid] = v
    if ex_id:
        med[ex_id] = lb
    return med


def _set_medium(m, med):
    for r in m.reactions:
        if r.id.startswith(EX_PREFIX) or r.boundary:
            r.lower_bound = 0.0
    for rid, v in (med or {}).items():
        if rid in m.reactions:
            m.reactions.get_by_id(rid).lower_bound = v


def _growth_sole(m, resolved_med, ex_id, lb=-10.0):
    with m:
        _set_medium(m, sole_medium(m, resolved_med, ex_id, lb))
        return m.optimize().objective_value or 0.0


def _relax_all_internal(m):
    for r in m.reactions:
        if r.id.startswith(EX_PREFIX) or r.boundary:
            continue
        r.bounds = (-1000.0, 1000.0)


def _rollback(cur, applied_all, relaxed_all):
    """回滚本批改动: 删除本批新增反应（孤儿代谢物一并回收）+ 还原放宽的 bounds。"""
    if applied_all:
        cur.remove_reactions([cur.reactions.get_by_id(a["rxn"]) for a in applied_all
                              if a["rxn"] in cur.reactions], remove_orphans=True)
    for rb in relaxed_all:
        r0 = cur.reactions.get_by_id(rb["rxn"])
        r0.bounds = tuple(rb["old_bounds"])
        r0.notes.pop("bound_relaxed_by", None)
        r0.notes.pop("evidence", None)
    return cur


def _hop_reactions(m, ex_id, hops=2):
    """底物交换反应的邻域（k 跳反应-代谢物二部图），限定 L3a MILP 规模。"""
    if ex_id not in m.reactions:
        return set()
    seen_r = {ex_id}
    frontier_m = {x.id for x in m.reactions.get_by_id(ex_id).metabolites}
    seen_m = set(frontier_m)
    for _ in range(hops):
        nxt_r = set()
        for mid in frontier_m:
            nxt_r |= {r.id for r in m.metabolites.get_by_id(mid).reactions}
        nxt_r -= seen_r
        seen_r |= nxt_r
        frontier_m = set()
        for rid in nxt_r:
            frontier_m |= {x.id for x in m.reactions.get_by_id(rid).metabolites}
        frontier_m -= seen_m
        seen_m |= frontier_m
    return seen_r


# ---------------------------------------------------------------------------
# 白名单加载与缓存（重复调用免重跑 diamond）
# ---------------------------------------------------------------------------
def _sha1_file(path, chunk=1 << 20):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(chunk), b""):
            h.update(b)
    return h.hexdigest()


def load_whitelist(species=None, faa=None, whitelist_json=None, force_rebuild=False):
    """白名单命中集 {rxn_id: [seq_ids]}。
    优先级: whitelist_json（现成命中集导入+缓存）> 缓存（faa 内容哈希键）> 现场 diamond（B1）。
    缓存: ~/.dsh/dsh-bio-gem/whitelist/<species>-<sha1[:12]>.json。"""
    os.makedirs(WHITELIST_DIR, exist_ok=True)
    if whitelist_json and os.path.exists(whitelist_json):
        key = _sha1_file(whitelist_json)[:12]
        species = species or os.path.splitext(os.path.basename(whitelist_json))[0]
        cache = os.path.join(WHITELIST_DIR, f"{species}-{key}.json")
        if os.path.exists(cache) and not force_rebuild:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)["rxn_hits"], cache, {"source": "cache"}
        with open(whitelist_json, encoding="utf-8") as f:
            raw = json.load(f)
        rxn_hits = raw.get("rxn_hits", raw) if isinstance(raw, dict) else {}
        json.dump({"species": species, "sha1": key, "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "source": whitelist_json, "rxn_hits": rxn_hits},
                  open(cache, "w", encoding="utf-8"), ensure_ascii=False)
        return rxn_hits, cache, {"source": f"imported:{whitelist_json}"}
    if not faa or not os.path.exists(faa):
        raise FileNotFoundError("whitelist 需要 faa（目标蛋白 fasta）或 whitelist_json（现成命中集）")
    species = species or os.path.splitext(os.path.basename(faa))[0]
    key = _sha1_file(faa)[:12]
    cache = os.path.join(WHITELIST_DIR, f"{species}-{key}.json")
    if os.path.exists(cache) and not force_rebuild:
        with open(cache, encoding="utf-8") as f:
            return json.load(f)["rxn_hits"], cache, {"source": "cache"}
    from build_whitelist import diamond_whitelist
    db_dir = RXN_DB_DIR if os.path.isdir(RXN_DB_DIR) else WHITELIST_DIR
    r = diamond_whitelist(faa, out_dir=WHITELIST_DIR, db_path=os.path.join(db_dir, "rxn_all.dmnd"),
                          rxn_fa=os.path.join(db_dir, "rxn_all.fa"))
    json.dump({"species": species, "faa": faa, "sha1": key,
               "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "params": {"evalue": r.get("evalue"), "min_bitscore": r.get("min_bitscore")},
               "rxn_hits": r["rxn_hits"]},
              open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return r["rxn_hits"], cache, {"source": f"diamond:{r.get('hits_tsv')}"}


# ---------------------------------------------------------------------------
# ID 桥: 白名单 rxn（MetaCyc 风格）-> iML1515 反应
# ---------------------------------------------------------------------------
def _parse_tbl(tbl_path):
    """gapseq all-Reactions.tbl: rxn(MetaCyc id) -> {name, ec}。"""
    out = {}
    if not tbl_path or not os.path.exists(tbl_path):
        return out
    import csv
    with open(tbl_path, encoding="utf-8", errors="ignore") as f:
        rd = csv.DictReader((ln for ln in f if not ln.startswith("#")), delimiter="\t")
        for row in rd:
            rid = (row.get("rxn") or "").strip()
            if not rid:
                continue
            d = out.setdefault(rid, {"name": "", "ec": set()})
            if row.get("name") and not d["name"]:
                d["name"] = row["name"].strip()
            if row.get("ec"):
                d["ec"].add(row["ec"].strip())
    return out


def bridge_iML1515(rxn_ids, universal, tbl=None):
    """白名单 rxn id -> iML1515 反应候选。
    规则: ① id 即 EC（如 1.1.1.127-RXN）→ universal EC 注释；② id 规约名 == universal 反应名规约；
    ③ tbl（gapseq 判定表）补 rxn->EC。返回 {wl_id: {"rxns": [uid], "rule": str}}。"""
    name_idx, ec_idx = {}, {}
    for r in universal.reactions:
        name_idx.setdefault(norm_name(r.name), []).append(r.id)
        for ec in ((r.annotation or {}).get("ec-code") or []):
            ec_idx.setdefault(ec, []).append(r.id)
    bridged = {}
    for rid in rxn_ids:
        cands, rule = set(), None
        mm = re.match(r"^(\d+(?:\.\d+)+)-RXN$", rid)
        ecs = {mm.group(1)} if mm else set()
        if rid in tbl:
            ecs |= {e for e in tbl[rid]["ec"] if re.match(r"^\d+(\.\d+)+$", e)}
        for ec in ecs:
            got = ec_idx.get(ec)
            if got:
                cands |= set(got)
                rule = rule or f"EC {ec}"
        got = name_idx.get(norm_name(rid.replace("-RXN", "")))
        if got:
            cands |= set(got)
            rule = f"{rule}; name" if rule else "name"
        if cands:
            bridged[rid] = {"rxns": sorted(cands), "rule": rule}
    return bridged


def port_reaction(u_rxn, target_m, met_idx, new_met_cache):
    """iML1515 反应 -> 目标模型命名空间（不直接改 target）。
    代谢物三层解析: 名字规约唯一匹配(公式校验) -> COFACTOR_BRIDGE(公式校验) -> 新代谢物(共享缓存)。
    返回 (reaction_or_None, info)。"""
    if PTS_RE.search(u_rxn.name or "") or PTS_RE.search(u_rxn.id):
        return None, {"skipped": "PTS route excluded (PTS-less organism guard)"}

    def _resolve(x):
        base, comp = _bigg_base_comp(x.id)
        comp_id = COMP_MAP.get(comp, comp)
        cands = [c for c in met_idx.get(met_name_key(x), []) if c.compartment == comp_id]
        if len(cands) == 1 and _formula_ok(x, cands[0]):
            return cands[0], "name"
        cpd = COFACTOR_BRIDGE.get(base)
        if cpd:
            tid = f"cpd{cpd}_{comp_id}"
            if tid in target_m.metabolites:
                tgt = target_m.metabolites.get_by_id(tid)
                # 桥表条目经人工核验；BiGG/ModelSEED 质子记账惯例不同（H/±1 常见），
                # 故此处不做严格公式断言，只记录差异供审计（公式完全不同族才拒收：碳数必须一致）
                fx, ft = (x.formula or ""), (tgt.formula or "")
                cx, ct = re.findall(r"C(\d+)", fx), re.findall(r"C(\d+)", ft)
                if cx and ct and cx[0] != ct[0]:
                    return None, "cofactor_carbon_mismatch"
                if not _formula_ok(x, tgt):
                    mismatches.append({"u": x.id, "t": tgt.id, "u_formula": fx, "t_formula": ft})
                return tgt, "cofactor_bridge"
        key = f"{base}_{comp_id}"
        if key in target_m.metabolites:
            key += "_l3fix"  # 同名 id 已被占用（名字匹配失败=语义不同）→ 造独立副本
        if key not in new_met_cache:
            nm = cobra.Metabolite(key, name=(x.name or base), compartment=comp_id,
                                  formula=x.formula, charge=x.charge)
            nm.notes["source"] = SOURCE_TAG
            new_met_cache[key] = nm
        return new_met_cache[key], "new"

    mapped, hows, new_ids, mismatches = {}, {}, [], []
    for x in u_rxn.metabolites:
        tgt, how = _resolve(x)
        if tgt is None:
            return None, {"skipped": f"cofactor mapping failed: {x.id}"}
        mapped[x.id] = tgt
        hows[x.id] = how
        if how == "new":
            new_ids.append(tgt.id)
    if not mapped:
        return None, {"skipped": "no metabolite mappable"}
    n_existing = sum(1 for v in mapped.values() if v.id in target_m.metabolites)
    if n_existing == 0:
        return None, {"skipped": "fully novel subnet (shares no model metabolite)"}
    # 强制下限钳 0：补洞候选必须是"可选反应"（如 ATPM 的 lb=6.86 强制维持能会污染
    # G3 无碳/全关检查与后续底物复测——2026-08-29 实测把甘露醇 sole 测出 -0.045）
    lb, ub = u_rxn.lower_bound, u_rxn.upper_bound
    lb_clamped = lb > 0
    r = cobra.Reaction(u_rxn.id + NEW_RXN_SUFFIX, name=(u_rxn.name or u_rxn.id),
                       lower_bound=min(lb, 0.0), upper_bound=ub)
    r.add_metabolites({mapped[x.id]: c for x, c in u_rxn.metabolites.items()})
    return r, {"n_mapped": n_existing, "n_new": len(new_ids), "new_ids": new_ids,
               "hows": hows, "lb_clamped": lb_clamped}


def build_pool(universal, target_m, bridged, allow_math):
    """L3b 候选池: 桥接（白名单序列证据）+（可选）全 universal 数学池。
    证据口径: 白名单 id 自身 EC 型（X.X.X.X-RXN）或名字直配 → EVIDENCE_sequence；
    经 tbl 间接 EC 桥（如 XYLISOM-RXN→ARAI 的双 EC 注释链）降级 EVIDENCE_math + sequence_hint
    （防证据虚高——命中的是 A 酶序列、加的是 B 酶方程）。
    返回 (pool_model, seq_backed_ids, port_report, new_met_cache)。"""
    met_idx = build_met_name_index(target_m)
    wl_uids = {u for v in bridged.values() for u in v["rxns"]}

    def _strict_seq(wl_id, rule):
        if rule == "name":
            return True
        if rule and rule.startswith("EC") and re.match(r"^\d+(\.\d+)+-RXN$", wl_id):
            return True
        return False

    strict_uids = {u for w, v in bridged.items() for u in v["rxns"] if _strict_seq(w, v["rule"])}
    hint_uids = wl_uids - strict_uids
    cand_uids = set(wl_uids)
    if allow_math:
        cand_uids |= {r.id for r in universal.reactions
                      if not r.boundary and not r.id.startswith(EX_PREFIX)}
    pool_rxns, new_met_cache = [], {}
    report = {"bridged": len(bridged), "ported": 0, "excluded_pts": 0, "skipped": []}
    for uid in sorted(cand_uids):
        if uid not in universal.reactions:
            continue
        r, info = port_reaction(universal.reactions.get_by_id(uid), target_m, met_idx, new_met_cache)
        if r is None:
            if "PTS" in (info.get("skipped") or ""):
                report["excluded_pts"] += 1
            elif len(report["skipped"]) < 12:
                report["skipped"].append({"rxn": uid, "why": info.get("skipped")})
            continue
        pool_rxns.append(r)
        report["ported"] += 1
    pool_model = cobra.Model("l3b_pool")
    if pool_rxns:
        pool_model.add_reactions(pool_rxns)
    n = len(NEW_RXN_SUFFIX)
    seq_backed = {r.id for r in pool_rxns if r.id[:-n] in strict_uids} if pool_rxns else set()
    seq_hinted = {r.id for r in pool_rxns if r.id[:-n] in hint_uids} if pool_rxns else set()
    return pool_model, seq_backed, report, new_met_cache, seq_hinted


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def l3_fix(model_path, medium=None, substrates=None, out=None,
           allow_math=False, confirm_budget=False,
           whitelist=None, faa=None, species=None, max_iter=1, universal_path=None):
    from silentio import silent_read_sbml, silent_write_sbml
    from validate import validate_model
    from gapfind import expand_medium, resolve_medium, match_ex, build_ex_index
    from budget import budget_gate, prior_added, budget_for

    t0 = time.time()
    if not substrates:
        return {"ok": False, "error": "substrates required（L3 由底物驱动诊断）"}
    m = silent_read_sbml(model_path)
    med, preset_name = expand_medium(medium or {})
    resolved_med, unresolved = resolve_medium(m, med)
    ex_idx = build_ex_index(m)

    # 白名单（L3b 用；不可用不阻塞 L3a，记录原因）
    rxn_hits, wl_cache, wl_src = None, None, None
    try:
        rxn_hits, wl_cache, wl_src = load_whitelist(species=species, faa=faa, whitelist_json=whitelist)
        _note(f"[whitelist] {len(rxn_hits)} rxn hits via {wl_src['source']}")
    except Exception as e:
        _note(f"[whitelist] unavailable: {e}")

    universal = None
    upath = universal_path or DEFAULT_UNIVERSAL
    if (rxn_hits or allow_math) and os.path.exists(upath):
        universal = silent_read_sbml(upath)
        _note(f"[universal] {upath}: {len(universal.reactions)} reactions")
    tbl = _parse_tbl(os.path.join(os.path.dirname(model_path),
                                  os.path.splitext(os.path.basename(model_path))[0] + "-all-Reactions.tbl"))

    # L3 诊断（sole 语义）
    l3 = []
    for sub in substrates:
        exid = match_ex(sub, ex_idx)
        g = _growth_sole(m, resolved_med, exid)
        if exid and exid in m.reactions and g < 1e-6:
            l3.append({"substrate": sub, "exchange": exid, "growth_sole_before": round(g, 6)})

    # 第五闸门（入口预检: 每底物至少 1 条新增预估）
    gate0 = budget_gate(m, planned=len(l3), confirm_budget=confirm_budget)
    if gate0 and not rxn_hits and not allow_math:
        return {"ok": False, **gate0}

    bridged = {}
    if rxn_hits and universal is not None:
        bridged = bridge_iML1515(
            [k for k in rxn_hits if k.endswith("-RXN") or re.match(r"^\d+(\.\d+)+-RXN$", k)
             or k.startswith("RXN-")],
            universal, tbl)
        _note(f"[bridge] {len(bridged)} whitelist rxn ids -> iML1515")
    uid_to_wlid = {u: w for w, v in bridged.items() for u in v["rxns"]}

    pool_model, seq_backed, port_report, new_met_cache, seq_hinted = cobra.Model("l3b_pool"), set(), {
        "bridged": 0, "ported": 0, "excluded_pts": 0, "skipped": []}, {}, set()
    if universal is not None:
        pool_model, seq_backed, port_report, new_met_cache, seq_hinted = build_pool(
            universal, m, bridged, allow_math)
        _note(f"[pool] {len(pool_model.reactions)} ported candidates "
              f"(sequence-backed {len(seq_backed)}, seq-hint {len(seq_hinted)}, "
              f"pts-excluded {port_report['excluded_pts']})")

    results, applied_all, relaxed_all = [], [], []
    cur = m
    rolled = False
    for item in l3:
        sub, exid = item["substrate"], item["exchange"]
        entry = dict(item)
        growth_before = _growth_sole(cur, resolved_med, exid)

        # ---- L3a: 模型内连通性（LP 预检 → MILP 放宽 bounds）----
        l3a = {"attempted": True}
        scratch = cur.copy()
        _set_medium(scratch, sole_medium(scratch, resolved_med, exid))
        _relax_all_internal(scratch)
        g_relax = scratch.optimize().objective_value or 0.0
        l3a["lp_relax_growth"] = round(g_relax, 6)
        if g_relax < 1e-6:
            l3a["verdict"] = "not_fixable_in_model（全内部放开方向仍不生长→内部路径缺失，需外部反应式）"
        else:
            hop_rids = _hop_reactions(cur, exid, hops=2)
            uni = cobra.Model("self_universal")
            copies = []
            for rid in sorted(hop_rids):
                r0 = cur.reactions.get_by_id(rid)
                cp = cobra.Reaction("l3a_" + rid, name=r0.name,
                                    lower_bound=-1000.0, upper_bound=1000.0)
                cp.add_metabolites(dict(r0.metabolites))
                copies.append(cp)
            uni.add_reactions(copies)
            mc = cur.copy()
            _set_medium(mc, sole_medium(mc, resolved_med, exid))
            try:
                sols = GapFiller(mc, universal=uni, lower_bound=0.05,
                                 exchange_reactions=False, demand_reactions=False).fill(max_iter)
            except Exception as e:
                _note(f"[l3a] GapFiller failed: {e}")
                sols = []
            picked = [r for r in (sols[0] if sols else []) if r.id.startswith("l3a_")]
            l3a["verdict"] = "no solution" if not picked else "relaxed bounds"
            l3a["picked"] = [r.id[4:] for r in picked]
            for pr in picked:
                orig = pr.id[4:]
                r0 = cur.reactions.get_by_id(orig)
                old = r0.bounds
                r0.bounds = (-1000.0, 1000.0)
                r0.notes["bound_relaxed_by"] = SOURCE_TAG
                r0.notes["evidence"] = "EVIDENCE_math"
                relaxed_all.append({"rxn": orig, "old_bounds": list(old), "substrate": sub,
                                    "evidence": "EVIDENCE_math"})
        growth_a = _growth_sole(cur, resolved_med, exid)
        l3a["growth_sole_after"] = round(growth_a, 6)
        entry["l3a"] = l3a

        # ---- L3b: 白名单/BiGG 反应式（MILP 从候选池取最小集）----
        l3b = {"attempted": len(pool_model.reactions) > 0, "pool": port_report}
        added_here = []
        if len(pool_model.reactions) > 0 and growth_a < 1e-6:
            mb = cur.copy()
            _set_medium(mb, sole_medium(mb, resolved_med, exid))
            try:
                sols = GapFiller(mb, universal=pool_model, lower_bound=0.05,
                                 exchange_reactions=False, demand_reactions=False).fill(max_iter)
            except Exception as e:
                _note(f"[l3b] GapFiller failed: {e}")
                sols = []
            picked = [p for p in (sols[0] if sols else []) if p.id.endswith(NEW_RXN_SUFFIX)]
            gate = budget_gate(cur, planned=len(picked), confirm_budget=confirm_budget)
            if gate:
                entry["l3b"] = {**l3b, **gate}
                results.append(entry)
                break
            for pr in picked:
                src = pool_model.reactions.get_by_id(pr.id)
                if pr.id in seq_backed:
                    ev = "EVIDENCE_sequence"
                elif pr.id in seq_hinted:
                    ev = "EVIDENCE_math"  # tbl 间接桥：有序列线索但非直接对应，保守降级
                else:
                    ev = "EVIDENCE_math"
                rid = pr.id if pr.id not in cur.reactions else pr.id + f"_{len(applied_all)}"
                r = cobra.Reaction(rid, name=(pr.name or pr.id),
                                   lower_bound=pr.lower_bound, upper_bound=pr.upper_bound)
                mm = {}
                for x, c in src.metabolites.items():
                    if x.id in cur.metabolites:
                        mm[cur.metabolites.get_by_id(x.id)] = c
                    else:  # 新代谢物随反应引入（共享缓存对象，防同 id 异对象）
                        nm = new_met_cache.get(x.id)
                        if nm is None or nm.id != x.id:
                            nm = next((v for v in new_met_cache.values() if v.id == x.id), None)
                        if nm is None:
                            nm = cobra.Metabolite(x.id, name=x.name, compartment=x.compartment,
                                                  formula=x.formula, charge=x.charge)
                            nm.notes["source"] = SOURCE_TAG
                            new_met_cache[x.id] = nm
                        mm[nm] = c
                r.add_metabolites(mm)
                cur.add_reactions([r])
                wlid = uid_to_wlid.get(pr.id[:-len(NEW_RXN_SUFFIX)])
                r.notes["source"] = SOURCE_TAG
                r.notes["evidence"] = ev
                r.notes["reason"] = (f"L3b: ported from iML1515 {pr.id[:-len(NEW_RXN_SUFFIX)]}"
                                     f"{' via whitelist ' + wlid if wlid else ''}"
                                     f"{' (tbl-indirect bridge, sequence hint only)' if pr.id in seq_hinted and wlid else ''}"
                                     f"; substrate {sub}")
                added_here.append({"rxn": rid, "evidence": ev, "substrate": sub,
                                   "sequence_backed": ev == "EVIDENCE_sequence",
                                   "sequence_hint": pr.id in seq_hinted and wlid is not None})
                applied_all.append(added_here[-1])
            _note(f"[l3b] {sub}: picked {len(picked)}, added {len(added_here)}")
        growth_b = _growth_sole(cur, resolved_med, exid)
        l3b["growth_sole_after"] = round(growth_b, 6)
        l3b["added"] = added_here
        entry["l3b"] = l3b
        entry["growth_sole_after"] = round(max(growth_a, growth_b), 6)
        entry["verdict"] = "fixed" if growth_b > 1e-6 else "not_fixable"
        if entry["verdict"] == "not_fixable":
            entry["unfixable_evidence"] = [
                f"sole 语义生长 before={growth_before:.6f}（有交换+转运但 FBA 不长，即 L3）",
                f"L3a: {entry['l3a']['verdict']}（lp_relax_growth={entry['l3a'].get('lp_relax_growth')}）",
                (f"L3b: 候选池 {len(pool_model.reactions)} 条（白名单桥接 {port_report['bridged']}"
                 f"→移植 {port_report['ported']}，PTS 排除 {port_report['excluded_pts']}）；"
                 f"MILP 未选出能恢复生长的集合"),
                f"白名单缓存: {wl_cache or 'n/a'}（{len(rxn_hits or {})} 命中；桥规则=EC/名字规约）",
            ]
        results.append(entry)

    resp = {"ok": True, "l3_input": results, "medium_preset": preset_name,
            "medium_unresolved": unresolved,
            "whitelist": {"cache": wl_cache, "source": (wl_src or {}).get("source"),
                          "n_hits": len(rxn_hits or {})},
            "budget": {"prior_added": prior_added(m), "budget": budget_for(m),
                       "added_this_run": len(applied_all) + len(relaxed_all)},
            "applied": applied_all, "bound_relaxed": relaxed_all,
            "rolled_back": False, "out": None, "elapsed_s": None}

    # ---- 落盘 + G1-G6 重验（G6 能量循环哨兵，失败回滚）----
    if applied_all or relaxed_all:
        if not out:
            out = model_path[:-4] + "_l3.xml" if model_path.endswith(".xml") else model_path + "_l3.xml"
        silent_write_sbml(cur, out)
        rep = validate_model(out, medium=resolved_med)
        g6 = rep.get("g6") or {}
        resp["validate"] = {k: (rep.get(k) or {}).get("status") for k in ("g1", "g2", "g3", "g4", "g5", "g6")}
        resp["g6_after"] = g6
        if g6.get("status") != "PASS":
            _rollback(cur, applied_all, relaxed_all)
            silent_write_sbml(cur, out)
            rep2 = validate_model(out, medium=resolved_med)
            resp["rolled_back"] = True
            resp["rollback_reason"] = f"G6 {g6.get('status')} atp_leak={g6.get('atp_leak_flux')}"
            resp["validate"] = {k: (rep2.get(k) or {}).get("status") for k in ("g1", "g2", "g3", "g4", "g5", "g6")}
            resp["g6_after"] = rep2.get("g6")
            resp["applied"] = []
            resp["bound_relaxed"] = []
        resp["out"] = out
    else:
        resp["note"] = "no L3 fix applied（均为不可补或无候选；证据见 l3_input）"

    evc = {"EVIDENCE_sequence": 0, "EVIDENCE_math": 0, "EVIDENCE_rule": 0}
    for a in resp["applied"]:
        evc[a["evidence"]] = evc.get(a["evidence"], 0) + 1
    for rb in resp["bound_relaxed"]:
        evc[rb["evidence"]] = evc.get(rb["evidence"], 0) + 1
    resp["evidence_summary"] = evc
    resp["elapsed_s"] = round(time.time() - t0, 1)
    return resp


if __name__ == "__main__":
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    print(json.dumps(l3_fix(args.get("model"), args.get("medium"), args.get("substrates"),
                            args.get("out"), allow_math=args.get("allow_math", False),
                            confirm_budget=args.get("confirm_budget", False),
                            whitelist=args.get("whitelist"), faa=args.get("faa"),
                            species=args.get("species"), max_iter=args.get("max_iter", 1),
                            universal_path=args.get("universal_path")),
                     ensure_ascii=False, indent=2))
