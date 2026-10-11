# test_model_catalog.py — model_catalog 定向测试（纯 stdlib 面构造，不触网、不落真实资产）
#
# 运行：PYTHONDONTWRITEBYTECODE=1 <gem-python> -I test/test_model_catalog.py
# 覆盖：目录结构纪律 / SHA256 校验 / 幂等 / 冲突不覆盖 / force / sha_mismatch 丢弃 /
#       zip 内提取（含缺 inner 响亮失败）/ 下载异常 / list 与 verify 三态 / 未知 id。
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "python"))

import model_catalog as mc

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name}  {detail}")
        FAILURES.append(name)


def fake_fetch(payload_bytes, record=None):
    """返回一个 fetch(url, dest_tmp) 函数，把固定字节写入目标；record 记录调用。"""

    def _fetch(url, dest_tmp):
        if record is not None:
            record.append(url)
        with open(dest_tmp, "wb") as f:
            f.write(payload_bytes)
        return "test"

    return _fetch


def fetch_that_must_not_be_called(url, dest_tmp):
    raise AssertionError("fetch should not have been called")


def main():
    # ---- 1. 真实 CATALOG 结构纪律 ----
    check("catalog >= 3 entries", len(mc.CATALOG) >= 3, f"got {len(mc.CATALOG)}")
    ok_fields = True
    ids = set()
    for e in mc.CATALOG:
        for k in ("id", "name", "organism", "source", "url", "filename", "sha256", "size", "license", "citation"):
            if k not in e or not e[k]:
                ok_fields = False
                print(f"    missing field {k} in {e.get('id')}")
        if not (isinstance(e.get("sha256"), str) and len(e["sha256"]) == 64 and all(c in "0123456789abcdef" for c in e["sha256"])):
            ok_fields = False
            print(f"    bad sha256 in {e.get('id')}")
        if not (isinstance(e.get("size"), int) and e["size"] > 0):
            ok_fields = False
        if not str(e.get("url", "")).startswith("https://"):
            ok_fields = False
        if e["id"] in ids:
            ok_fields = False
        ids.add(e["id"])
    check("catalog entry fields valid (sha256/size/https/unique id)", ok_fields)

    # ---- 2. _sha256_file ----
    tmp = tempfile.mkdtemp(prefix="gem_mc_test_")
    try:
        p = os.path.join(tmp, "x.bin")
        payload = b"SBML-TEST-" + os.urandom(64)
        with open(p, "wb") as f:
            f.write(payload)
        check("_sha256_file matches hashlib", mc._sha256_file(p) == hashlib.sha256(payload).hexdigest())

        # ---- 3. monkeypatch catalog for install-path tests ----
        orig_catalog = list(mc.CATALOG)
        good = b"<?xml version=\"1.0\"?><sbml>" + b"a" * 200
        good_sha = hashlib.sha256(good).hexdigest()
        mc.CATALOG = [{
            "id": "test-model", "name": "Test-Model", "organism": "Testus organismus",
            "strain": "T1", "kind": "curated", "description": "for tests",
            "source": "unit test", "url": "https://example.invalid/test.xml",
            "filename": "Test.xml", "archive": None, "inner": None,
            "sha256": good_sha, "size": len(good), "license": "CC0", "citation": "n/a",
        }]
        md = os.path.join(tmp, "models")
        os.makedirs(md, exist_ok=True)

        # 3a. list on empty dir
        lst = mc.list_models(md)
        check("list: empty dir -> not installed", lst["items"][0]["installed"] is False)
        check("list: count/installed_ok", lst["count"] == 1 and lst["installed_ok"] == 0)

        # 3b. verify not_installed
        vr = mc.verify_models(md)
        check("verify: not_installed", vr["ok"] is False and vr["results"][0]["status"] == "not_installed")

        # 3c. install success (fake fetch) + ledger
        calls = []
        res = mc.install_model(md, fetch=fake_fetch(good, calls))
        r0 = res["results"][0]
        dest = os.path.join(md, "Test.xml")
        check("install: ok/status", res["ok"] is True and r0["status"] == "installed", str(r0))
        check("install: file in place with right sha", os.path.exists(dest) and mc._sha256_file(dest) == good_sha)
        check("install: one fetch call", len(calls) == 1)
        led = json.loads(open(os.path.join(md, "installed.json"), encoding="utf-8").read())
        check("install: ledger updated", led["entries"]["test-model"]["sha256"] == good_sha)
        check("install: no temp residue", not os.path.exists(dest + ".download") and not os.path.exists(dest + ".inner"))

        # 3d. idempotent: must NOT call fetch
        res = mc.install_model(md, fetch=fetch_that_must_not_be_called)
        check("install: idempotent already_installed", res["ok"] is True and res["results"][0]["status"] == "already_installed")

        # 3e. conflict: local wrong content -> no overwrite, no fetch
        with open(dest, "wb") as f:
            f.write(b"local-modified-content")
        before = open(dest, "rb").read()
        res = mc.install_model(md, fetch=fetch_that_must_not_be_called)
        r0 = res["results"][0]
        check("install: conflict detected", res["ok"] is False and r0["status"] == "conflict", str(r0))
        check("install: conflict does not overwrite", open(dest, "rb").read() == before)

        # 3f. force -> overwrite
        res = mc.install_model(md, force=True, fetch=fake_fetch(good))
        check("install: force overwrite", res["ok"] is True and mc._sha256_file(dest) == good_sha)

        # 3g. sha_mismatch -> discard, no residue
        os.remove(dest)
        res = mc.install_model(md, fetch=fake_fetch(b"WRONG-CONTENT"))
        r0 = res["results"][0]
        check("install: sha_mismatch", res["ok"] is False and r0["status"] == "sha_mismatch", str(r0))
        check("install: mismatch discarded (no dest, no temp)", not os.path.exists(dest) and not os.path.exists(dest + ".download"))

        # 3h. fetch raises -> error status
        def boom(url, dest_tmp):
            raise RuntimeError("network down")

        res = mc.install_model(md, fetch=boom)
        check("install: fetch error -> error status", res["ok"] is False and res["results"][0]["status"] == "error")

        # 3i. zip entry extraction
        inner_name = "inner.xml"
        zbuf = os.path.join(tmp, "bundle.zip")
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr(inner_name, good.decode())
        zbytes = open(zbuf, "rb").read()
        mc.CATALOG = [{
            "id": "test-zip", "name": "Test-Zip", "organism": "Testus", "strain": "-", "kind": "curated",
            "description": "zip test", "source": "unit test", "url": "https://example.invalid/b.zip",
            "filename": "Zipped.xml", "archive": "zip", "inner": inner_name,
            "sha256": good_sha, "size": len(good), "license": "CC0", "citation": "n/a",
        }]
        zmd = os.path.join(tmp, "models_zip")
        os.makedirs(zmd, exist_ok=True)
        res = mc.install_model(zmd, fetch=fake_fetch(zbytes))
        zdest = os.path.join(zmd, "Zipped.xml")
        check("zip: installed + extracted right sha", res["ok"] is True and mc._sha256_file(zdest) == good_sha, str(res["results"][0]))
        check("zip: no residue", not os.path.exists(zdest + ".download") and not os.path.exists(zdest + ".inner"))

        # 3j. zip missing inner -> error with clear message
        bad_zip = os.path.join(tmp, "bad.zip")
        with zipfile.ZipFile(bad_zip, "w") as z:
            z.writestr("other.xml", "x")
        os.remove(zdest)
        res = mc.install_model(zmd, fetch=fake_fetch(open(bad_zip, "rb").read()))
        r0 = res["results"][0]
        check("zip: missing inner -> error mentions name", r0["status"] == "error" and inner_name in r0.get("error", ""), str(r0))

        # 3k. unknown id
        res = mc.install_model(md, entry_id="nope")
        check("install: unknown id", res["ok"] is False and "unknown model id" in res.get("error", ""))
        vr = mc.verify_models(md, entry_id="nope")
        check("verify: unknown id", vr["ok"] is False and "unknown model id" in vr.get("error", ""))

        # 3l. verify match after restore
        mc.CATALOG = [{
            "id": "test-model", "name": "Test-Model", "organism": "Testus organismus", "strain": "T1",
            "kind": "curated", "description": "for tests", "source": "unit test",
            "url": "https://example.invalid/test.xml", "filename": "Test.xml", "archive": None, "inner": None,
            "sha256": good_sha, "size": len(good), "license": "CC0", "citation": "n/a",
        }]
        with open(os.path.join(md, "Test.xml"), "wb") as f:
            f.write(good)
        vr = mc.verify_models(md)
        check("verify: match", vr["ok"] is True and vr["results"][0]["status"] == "match")

        vr = mc.verify_models(md, entry_id="test-model")
        check("verify: single entry match", vr["ok"] is True and len(vr["results"]) == 1)

        mc.CATALOG = orig_catalog
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURES: {FAILURES}")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
