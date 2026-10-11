# test_g6_atp_maintenance.py — G6 ATP 维持耗散闸门（v2）定向测试
#
# 运行：PYTHONDONTWRITEBYTECODE=1 <gem-python> -I test/test_g6_atp_maintenance.py
# 全部用内存模型（cobra），不落盘、不碰真实资产。断言失败→非零退出。
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import cobra
from validate import Validator
from energy_gate import g6_atp_maintenance, G6_METHOD_VERSION, DISSIPATION_ID, NET_ATP_ID

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def _met(m, mid, comp="c", formula=None, name=None):
    met = cobra.Metabolite(mid, compartment=comp)
    if formula:
        met.formula = formula
    if name:
        met.name = name
    return met


def _base_model():
    """含五能量角色（同区室 c）、无任何反应的干净模型。"""
    m = cobra.Model("test")
    m.add_metabolites([
        _met(m, "atp_c", formula="C10H16N5O13P3", name="ATP"),
        _met(m, "adp_c", formula="C10H15N5O10P2", name="ADP"),
        _met(m, "pi_c", formula="HO4P", name="Pi"),
        _met(m, "h2o_c", formula="H2O", name="H2O"),
        _met(m, "h_c", formula="H", name="H+"),
    ])
    return m


class _FakeValidator:
    """极轻 Validator 桩：只暴露 .m，供 g6_atp_maintenance 使用。"""
    def __init__(self, model):
        self.m = model


def run_g6(model, context=None):
    return g6_atp_maintenance(_FakeValidator(model), context=context)


def _cycle_model():
    """非法充能循环：ADP+Pi+H -> ATP+H2O（与耗散互为逆反应）。"""
    m = _base_model()
    r = cobra.Reaction("cycle_charge", lower_bound=0.0, upper_bound=1000.0)
    m.add_reactions([r])
    r.add_metabolites({"adp_c": -1, "pi_c": -1, "h_c": -1, "atp_c": 1, "h2o_c": 1})
    return m


print("[1] 非法充能循环：耗散>0（FAIL），单 ATP demand=0（质守恒隐藏循环）")
m = _cycle_model()
rep = run_g6(m)
check("cycle dissipation FAIL", rep["status"] == "FAIL", f"status={rep['status']} raw={rep['raw_flux']}")
check("cycle raw > 0", rep["raw_flux"] is not None and rep["raw_flux"] > 0, f"raw={rep['raw_flux']}")
check("net_atp_generation == 0", rep["diagnostics"]["net_atp_generation"]["raw_flux"] in (0, 0.0, None),
      f"net={rep['diagnostics']['net_atp_generation']['raw_flux']}")
check("net != dissipation (不互换)", rep["diagnostics"]["net_atp_generation"]["raw_flux"] != rep["raw_flux"])
check("method_version v2", rep["method_version"] == G6_METHOD_VERSION)

print("[2] 无充能循环：耗散=0（PASS）")
m = _base_model()
rep = run_g6(m)
check("no-cycle PASS", rep["status"] == "PASS", f"status={rep['status']} raw={rep['raw_flux']}")
check("raw == 0", rep["raw_flux"] == 0.0, f"raw={rep['raw_flux']}")

print("[3] 正常供营养才产能：葡萄糖交换被关闭 → 无泄漏 PASS")
m = _base_model()
glc = _met(m, "glc_e", comp="e", formula="C6H12O6")
m.add_metabolites([glc])
ex = cobra.Reaction("EX_glc_e", lower_bound=-10.0, upper_bound=1000.0)
m.add_reactions([ex])
ex.add_metabolites({"glc_e": -1})
gly = cobra.Reaction("glyc_atp", lower_bound=0.0, upper_bound=1000.0)  # glc -> 2 ATP
m.add_reactions([gly])
gly.add_metabolites({"glc_e": -1, "atp_c": 2, "adp_c": -2, "pi_c": -2, "h_c": -2, "h2o_c": 2})
rep = run_g6(m)
check("nutrient-gated PASS", rep["status"] == "PASS", f"status={rep['status']} raw={rep['raw_flux']}")
check("EX_glc_e closed in detail",
      any(d["id"] == "EX_glc_e" and d["original_bounds"] == (-10.0, 1000.0)
          for d in rep["closed_boundaries"]["detail"]),
      f"closed={rep['closed_boundaries']['detail']}")

print("[4] 伪装 source/sink（非 EX_ 前缀、单代谢物）必须被关闭")
m = _base_model()
src = cobra.Reaction("pseudo_atp_source", lower_bound=0.0, upper_bound=1000.0)  # -> atp_c
m.add_reactions([src])
src.add_metabolites({"atp_c": 1})
snk = cobra.Reaction("pseudo_atp_sink", lower_bound=0.0, upper_bound=1000.0)   # atp_c ->
m.add_reactions([snk])
snk.add_metabolites({"atp_c": -1})
rep = run_g6(m)
closed_ids = {d["id"] for d in rep["closed_boundaries"]["detail"]}
check("disguised source closed", "pseudo_atp_source" in closed_ids, f"closed={closed_ids}")
check("disguised sink closed", "pseudo_atp_sink" in closed_ids, f"closed={closed_ids}")
check("disguised source gives PASS (no leak)", rep["status"] == "PASS", f"raw={rep['raw_flux']}")

print("[5] 角色缺失（无 H2O）→ SKIP，不凭空添加")
m = _base_model()
m.remove_metabolites([m.metabolites.get_by_id("h2o_c")])
rep = run_g6(m)
check("missing role SKIP", rep["status"] == "SKIP", f"status={rep['status']}")
check("reason mentions H2O", "H2O" in (rep.get("reason") or ""), f"reason={rep.get('reason')}")

print("[6] 角色歧义（ATP 跨区室）→ SKIP，不选第一条")
m = _base_model()
atp_e = _met(m, "atp_e", comp="e", formula="C10H16N5O13P3", name="ATP")
m.add_metabolites([atp_e])
rep = run_g6(m)
check("ambiguous ATP SKIP", rep["status"] == "SKIP", f"status={rep['status']} reason={rep.get('reason')}")

print("[7] ATPM 正下界（强制通量）被松开 → 无泄漏 PASS；原 bounds 诊断 infeasible")
m = _base_model()
atpm = cobra.Reaction("ATPM", lower_bound=6.86, upper_bound=1000.0)
m.add_reactions([atpm])
atpm.add_metabolites({"atp_c": -1, "h2o_c": -1, "adp_c": 1, "pi_c": 1, "h_c": 1})
rep = run_g6(m)
check("ATPM relaxed PASS", rep["status"] == "PASS", f"status={rep['status']} raw={rep['raw_flux']}")
check("ATPM in relaxed_bounds",
      any(d["id"] == "ATPM" and d["original"] == (6.86, 1000.0) and d["relaxed"] == (0.0, 1000.0)
          for d in rep["relaxed_bounds"]["detail"]),
      f"relaxed={rep['relaxed_bounds']['detail']}")
check("original_bounds diagnostic not optimal",
      rep["diagnostics"]["original_bounds_dissipation"]["status"] != "optimal",
      f"orig_status={rep['diagnostics']['original_bounds_dissipation']['status']}")

print("[8] 反向强制内部反应方向保持（ub<0 -> ub=0，不开放正向）")
m = _base_model()
rev = cobra.Reaction("forced_reverse", lower_bound=-1000.0, upper_bound=-5.0)
m.add_reactions([rev])
rev.add_metabolites({"atp_c": -1, "adp_c": 1, "pi_c": 1})  # 与能量无关的代谢变换
rep = run_g6(m)
check("forced_reverse relaxed direction-preserving",
      any(d["id"] == "forced_reverse" and d["original"] == (-1000.0, -5.0) and d["relaxed"] == (-1000.0, 0.0)
          for d in rep["relaxed_bounds"]["detail"]),
      f"relaxed={rep['relaxed_bounds']['detail']}")
check("still runs (PASS or FAIL, not crash)", rep["status"] in ("PASS", "FAIL"), f"status={rep['status']}")

print("[9] 异常 solver → ERROR，不 PASS，不抛穿")
m = _cycle_model()

def _boom(*a, **k):
    raise RuntimeError("solver exploded")
m.optimize = _boom
rep = run_g6(m)
check("solver error ERROR", rep["status"] == "ERROR", f"status={rep['status']}")
check("solver error not PASS", rep["status"] != "PASS")

print("[10] 容差边界：0.5x 有效容差 PASS，2x FAIL")
# 用循环模型，循环 ub 决定耗散通量上限
base = run_g6(_base_model())
eff = base["effective_tolerance"]["value"]
m_lo = _cycle_model()
m_lo.reactions.get_by_id("cycle_charge").upper_bound = eff * 0.5
rep_lo = run_g6(m_lo)
m_hi = _cycle_model()
m_hi.reactions.get_by_id("cycle_charge").upper_bound = eff * 2.0
rep_hi = run_g6(m_hi)
check("tolerance-boundary low PASS", rep_lo["status"] == "PASS",
      f"eff={eff} raw={rep_lo['raw_flux']} status={rep_lo['status']}")
check("tolerance-boundary high FAIL", rep_hi["status"] == "FAIL",
      f"eff={eff} raw={rep_hi['raw_flux']} status={rep_hi['status']}")
check("effective tolerance linked to solver",
      "solver_feasibility" in (base["effective_tolerance"]["source"] or ""),
      base["effective_tolerance"]["source"])

print("[11] 无副作用（成功与失败后反应数不变、检查反应已移除）")
for label, model in [("pass", _base_model()), ("fail", _cycle_model())]:
    n_before = len(model.reactions)
    rep = run_g6(model)
    check(f"no side effect ({label}): reactions restored", len(model.reactions) == n_before,
          f"{len(model.reactions)} vs {n_before}")
    check(f"no side effect ({label}): dissipation removed",
          DISSIPATION_ID not in {r.id for r in model.reactions})
    check(f"no side effect ({label}): net-atp removed",
          NET_ATP_ID not in {r.id for r in model.reactions})

print(f"\n{'=' * 60}")
print(f"G6 v2 定向测试：{'ALL PASS' if not FAILURES else 'FAILED ' + str(len(FAILURES))}")
if FAILURES:
    print("失败项：", FAILURES)
    sys.exit(1)
sys.exit(0)
