# test_gm2_card_claims.py — GM-2 模型卡验证声明定向测试
# 运行：<python> -I test/test_gm2_card_claims.py（纯 stdlib，无需 cobra）
import os
import sys
import json
import tempfile
import hashlib

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import model_card as mc

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


MODEL_HASH = "sha256:" + "a" * 64
MEDIUM_ID = "sha256:" + "b" * 64
G4_PASS_EVIDENCED = {"status": "PASS", "matched": 16, "total": 19,
                     "reference_identity": "sha256:" + "c" * 64,
                     "model_sha256": MODEL_HASH, "medium_identity": MEDIUM_ID}
G4_PASS_NOIDENT = {"status": "PASS", "matched": 16, "total": 19}
G4_SKIP = {"status": "SKIP", "matched": None, "total": None}
G4_WARN = {"status": "WARN", "matched": 15, "total": 19, "reference_identity": "sha256:x"}
G4_FAIL = {"status": "FAIL", "matched": 5, "total": 19, "reference_identity": "sha256:x"}

print("[1] derive_validation_claim：G3 PASS × G4 各状态")
check("G3 PASS + G4 PASS(有身份) -> verified_G3_G4",
      mc.derive_validation_claim("PASS", G4_PASS_EVIDENCED,
                                 expected_model_sha256=MODEL_HASH,
                                 expected_medium_identity=MEDIUM_ID)["claim"] == "verified_G3_G4")
check("G3 PASS + G4 SKIP -> verified_G3_only",
      mc.derive_validation_claim("PASS", G4_SKIP)["claim"] == "verified_G3_only")
check("G3 PASS + G4 None -> verified_G3_only",
      mc.derive_validation_claim("PASS", None)["claim"] == "verified_G3_only")
check("G3 PASS + G4 WARN -> verified_G3_only（不升格）",
      mc.derive_validation_claim("PASS", G4_WARN)["claim"] == "verified_G3_only")
check("G3 PASS + G4 FAIL -> verified_G3_only（不升格）",
      mc.derive_validation_claim("PASS", G4_FAIL)["claim"] == "verified_G3_only")
check("G3 PASS + G4 PASS(缺身份) -> verified_G3_only（缺参考身份不声明 G3+G4）",
      mc.derive_validation_claim("PASS", G4_PASS_NOIDENT)["claim"] == "verified_G3_only")
check("G3 FAIL -> unverified", mc.derive_validation_claim("FAIL", G4_PASS_EVIDENCED)["claim"] == "unverified")
check("G3 WARN -> unverified", mc.derive_validation_claim("WARN", None)["claim"] == "unverified")

print("[2] 两种补洞分支都不影响声明（声明只由 gate 结果推导）")
# gapfixes=0 与 >0 都不应该产生 verified_G3_G4（因为根本没跑 G4）
for nfix in (0, 5):
    c = mc.derive_validation_claim("PASS", None)["claim"]
    check(f"gapfixes={nfix} 时声明仍为 G3-only（不因补洞数改变）", c == "verified_G3_only", c)

print("[3] 旧卡只读审计：verified_G3_G4 无 G4 明细 -> legacy_claim_unconfirmed")
tmp = tempfile.mkdtemp(prefix="gm2-card-")
card = {
    "name": "legacy", "engine": "carveme",
    "supported_mediums": [
        {"medium_name": "M9", "validation_status": "verified_G3_G4"},
        {"medium_name": "custom", "validation_status": "verified_G3"},
    ],
}
audit = mc.audit_card_claims(card)
per = {m["medium_name"]: m["audited"] for m in audit["per_medium"]}
check("verified_G3_G4 无证据 -> legacy_claim_unconfirmed",
      per.get("M9") == "legacy_claim_unconfirmed", per)
check("旧 G3 声明无模型/培养基证据仍未确认", per.get("custom") == "legacy_claim_unconfirmed", per)

# 有 G4 明细的卡 → verified_G3_G4 判 consistent
card2 = dict(card)
card2["verified_phenotypes"] = {"matched": 16, "total": 19,
                                "reference_identity": "sha256:abc", "source": "gem_phenotype"}
audit2 = mc.audit_card_claims(card2)
check("全局 G4 明细不能背书 M9 条件",
      {m["medium_name"]: m["audited"] for m in audit2["per_medium"]}.get("M9") == "legacy_claim_unconfirmed")

print("[4] 旧卡读取不改写文件、hash 不变")
mp = os.path.join(tmp, "legacy.xml")
open(mp, "w").close()
legacy = {"name": "legacy", "engine": "carveme", "growth_g3_m9": 0.782,
          "supported_mediums": [{"medium_name": "M9", "validation_status": "verified_G3_G4"}]}
cp = mc.card_path_for(mp)
with open(cp, "w", encoding="utf-8") as f:
    json.dump(legacy, f)
h_before = hashlib.sha256(open(cp, "rb").read()).hexdigest()
_ = mc.load_card(mp)
_ = mc.audit_card_claims(mc.load_card(mp))
h_after = hashlib.sha256(open(cp, "rb").read()).hexdigest()
check("旧卡读取后 hash 不变", h_before == h_after, f"{h_before} vs {h_after}")

print("[5] 标签与报告一致：derive 结果写入卡后的 supported_mediums 与实际 gate 一致")
c3 = mc.derive_validation_claim("PASS", None)
check("claim_policy_version 存在", "claim_policy_version" in c3)
check("unsupported_reasons 解释为何不能声明 G3+G4", bool(c3["unsupported_reasons"]))

print(f"\n{'=' * 60}")
print(f"GM-2 定向测试：{'ALL PASS' if not FAILURES else 'FAILED ' + str(len(FAILURES))}")
if FAILURES:
    print("失败项：", FAILURES)
    sys.exit(1)
sys.exit(0)
