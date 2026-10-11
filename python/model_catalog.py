# -*- coding: utf-8 -*-
"""dsh-bio-gem 模型目录（catalog）——链霉菌权威 GEM 的登记、下载与完整性校验。

设计（2026-10-11）：
- 目录登记「已发表、来源可追溯」的链霉菌代谢模型（SBML）。每项含来源 URL、License、
  论文引用、SHA256 与字节数（登记时实测）。
- 下载链路：直连 → 环境代理（HTTP_PROXY/HTTPS_PROXY/ALL_PROXY）回退一次；
  .download 临时文件 + os.replace 原子落盘；支持 zip 打包条目（期刊补充材料）——
  下载后从 zip 内提取指定内层文件再校验，不整包解压。
- 完整性 fail-closed：SHA256 与登记值不一致 → 拒绝落盘/拒报成功（可能是上游更新、
  网络损坏或本地修改——三者都要显式暴露，绝不静默放行）。
- 幂等：本地已存在且校验通过 → already_installed（不重复下载）。
- 台账：<models_dir>/installed.json（原子写；记录 id/sha256/size/时间/channel/citation）。

重要事实（检索核验 2026-10-11）：
- S. venezuelae、S. scabies 等其余物种目前无公开可获取的 curated GEM；
  缺失物种可用 gem_build（carveme/gapseq）从基因组构建草稿模型替代。
- BiGG 库不含链霉菌模型（已核验），故本目录独立维护，不走 bigg: URI。

维护纪律（新增/更新条目时）：
  1) source 必须是可追溯的权威源（官方仓库 commit-pin / 期刊开放补充材料）；
  2) SHA256 与 size 必须实测后写入本文件；上游更新导致校验失败时人工确认再 bump；
  3) 记录 license 与论文引用（先核验再写，勿凭记忆）。
"""
import hashlib
import json
import os
import shutil
import time
import urllib.request
import zipfile

CATALOG_VERSION = "2026-10-11"

# 每个条目的 URL 使用 commit-pin（Sco-GEM/Salb-GEM 为 master HEAD 快照 2026-10-11）
# 或固定编号的期刊补充材料（lividans）——不可变、可复现。
CATALOG = [
    {
        "id": "sco-gem",
        "name": "Sco-GEM",
        "organism": "Streptomyces coelicolor",
        "strain": "A3(2)",
        "kind": "curated-consensus",
        "description": "S. coelicolor A3(2) 共识 GEM（SysBioChalmers Git 版本化维护；源自 iKS1317，含酶约束扩展分支）",
        "source": "SysBioChalmers/Sco-GEM @ commit 418c1a5942cd22b89a9d118c01f72506f2c80e16",
        "url": "https://raw.githubusercontent.com/SysBioChalmers/Sco-GEM/418c1a5942cd22b89a9d118c01f72506f2c80e16/model/Sco-GEM.xml",
        "filename": "Sco-GEM.xml",
        "archive": None,
        "inner": None,
        "sha256": "5c8dab3e6f3346a4b32fd1e359a7318db6824c664fc8ce3ccf49a9bddd78d5a9",
        "size": 10937869,
        "license": "CC-BY-4.0",
        "citation": "Sulheim S, Kumelj T, van Dissel D, et al. (2020) iScience 23(9):101525, doi:10.1016/j.isci.2020.101525",
    },
    {
        "id": "salb-gem",
        "name": "Salb-GEM",
        "organism": "Streptomyces albidoflavus (旧称 S. albus J1074)",
        "strain": "J1074",
        "kind": "curated",
        "description": "S. albidoflavus J1074（天然产物异源表达常用宿主）GEM；2231 反应 / 1872 代谢物 / 1381 基因",
        "source": "SysBioChalmers/Salb-GEM @ commit f04d1ce7675509c64583f9e8e4a2fa996a558e74",
        "url": "https://raw.githubusercontent.com/SysBioChalmers/Salb-GEM/f04d1ce7675509c64583f9e8e4a2fa996a558e74/ModelFiles/xml/Salb-GEM.xml",
        "filename": "Salb-GEM.xml",
        "archive": None,
        "inner": None,
        "sha256": "2897740f7769f77e764483dab0ff809d30270f8b5a10af9a71e099aba84a5562",
        "size": 8084543,
        "license": "CC-BY-4.0",
        "citation": "Kittikunapong C, Ye S, Magadán-Corpas P, et al. (2021) Metabolites 11(5):304, doi:10.3390/metabo11050304",
    },
    {
        "id": "lividans-ijv1220",
        "name": "iJV1220",
        "organism": "Streptomyces lividans",
        "strain": "66 / TK21 / TK24（strain-independent 改进版）",
        "kind": "curated",
        "description": "S. lividans 改进版 GEM（覆盖 66/TK21/TK24 三大常用株系）；从期刊开放补充材料 ZIP 内提取 iJV1220.xml",
        "source": "BMC Microbiology 2018 supplementary (Springer, MOESM2)",
        "url": "https://media.springernature.com/original/springer-static/esm/art%3A10.1186%2Fs12866-018-1199-3/MediaObjects/12866_2018_1199_MOESM2_ESM.zip",
        "filename": "iJV1220.xml",
        "archive": "zip",
        "inner": "iJV1220.xml",
        "sha256": "f31d1bc81751286c9fb27932d02cc0c3a64240659c35b9854c858149cc8f4b26",
        "size": 3299302,
        "license": "CC-BY-4.0",
        "citation": "Valverde JR, Gullón S, Mellado RP (2018) BMC Microbiol 18:59, doi:10.1186/s12866-018-1199-3",
    },
]

_CATALOG_FIELDS = ("id", "name", "organism", "strain", "kind", "description",
                   "source", "license", "citation", "sha256", "size", "filename")


def get_entry(entry_id):
    for e in CATALOG:
        if e["id"] == entry_id:
            return e
    return None


def _sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _download_direct_then_proxy(url, dest_tmp, timeout_direct=45, timeout_proxy=300):
    """直连 → 环境代理（HTTP(S)_PROXY/ALL_PROXY）回退一次。成功返回 channel 名称（str）。

    与 benchmark.fetch_bigg_model 同族策略：getproxies() 读取环境代理；ALL_PROXY 显式映射
    到 http/https（getproxies 的 "all" 键不会自动对 http(s) 请求生效）。"""
    ua = "dsh-bio-gem model-catalog/1.0"
    env_proxies = {k: v for k, v in urllib.request.getproxies().items() if v}
    if env_proxies.get("all"):
        for _sch in ("http", "https"):
            env_proxies.setdefault(_sch, env_proxies["all"])
    attempts = [("direct", None, timeout_direct)]
    if env_proxies:
        attempts.append(("env-proxy", env_proxies, timeout_proxy))
    last_err = None
    for tag, proxies, tmo in attempts:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua})
            opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies or {}))
            with opener.open(req, timeout=tmo) as resp, open(dest_tmp, "wb") as f:
                shutil.copyfileobj(resp, f)
            size = os.path.getsize(dest_tmp)
            if size <= 0:
                raise ValueError("empty response body")
            return tag
        except Exception as e:  # noqa: BLE001 —— 逐通道回退，最终统一报错
            last_err = f"[{tag}] {type(e).__name__}: {str(e)[:180]}"
            if os.path.exists(dest_tmp):
                try:
                    os.remove(dest_tmp)
                except OSError:
                    pass
    raise RuntimeError(f"download failed（直连与代理回退均失败）: {last_err}")


def _extract_from_zip(zip_path, inner_name, dest_path):
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        if inner_name not in names:
            raise ValueError(f"zip 内未找到 {inner_name!r}（包含: {names[:10]}）")
        with z.open(inner_name) as src, open(dest_path, "wb") as f:
            shutil.copyfileobj(src, f)


def _update_ledger(models_dir, entry, extra):
    path = os.path.join(models_dir, "installed.json")
    data = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data.setdefault("entries", {})
    data["catalog_version"] = CATALOG_VERSION
    data["entries"][entry["id"]] = {
        "name": entry["name"],
        "filename": entry["filename"],
        "sha256": entry["sha256"],
        "size": entry["size"],
        "source": entry["source"],
        "license": entry["license"],
        "citation": entry["citation"],
        "installed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        **extra,
    }
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def list_models(models_dir):
    """列出目录与本地安装状态（不触网）。"""
    items = []
    for e in CATALOG:
        item = {k: e.get(k) for k in _CATALOG_FIELDS}
        dest = os.path.join(models_dir, e["filename"])
        if os.path.exists(dest):
            actual = _sha256_file(dest)
            item["installed"] = True
            item["path"] = dest
            item["sha_match"] = actual == e["sha256"]
            item["sha256_local"] = actual
        else:
            item["installed"] = False
            item["path"] = None
        items.append(item)
    installed_ok = sum(1 for i in items if i["installed"] and i.get("sha_match"))
    return {
        "catalog_version": CATALOG_VERSION,
        "models_dir": models_dir,
        "count": len(items),
        "installed_ok": installed_ok,
        "items": items,
        "hints": [
            "本目录为「已发表、可稳定获取」的链霉菌权威模型（S. coelicolor / S. albidoflavus J1074 / S. lividans）。",
            "S. venezuelae、S. scabies 等其余物种目前无公开可获取的 curated GEM（BiGG 亦不含链霉菌，已核验）——可用 gem_build（carveme/gapseq）从基因组构建草稿模型替代。",
            "安装后返回的模型绝对路径可直接传给 gem_report / gem_validate / gem_quality / gem_sample / gem_benchmark 等工具。",
        ],
    }


def install_model(models_dir, entry_id=None, force=False, fetch=None):
    """下载并安装目录中的模型（entry_id=None 安装全部）。

    fetch(url, dest_tmp) -> channel(str)：可注入的下载函数（测试用）；缺省=直连→代理回退。
    返回 {"ok": bool, "results": [单条目结果...]}；单条目 status ∈
      installed / already_installed / conflict / sha_mismatch / error / not_found。
    """
    fetch = fetch or _download_direct_then_proxy
    if entry_id is not None:
        entry = get_entry(entry_id)
        if entry is None:
            return {"ok": False, "results": [], "error": f"unknown model id: {entry_id}（可用: {[e['id'] for e in CATALOG]}）"}
        entries = [entry]
    else:
        entries = list(CATALOG)
    os.makedirs(models_dir, exist_ok=True)
    results = []
    for e in entries:
        results.append(_install_one(e, models_dir, force, fetch))
    ok = all(r["status"] in ("installed", "already_installed") for r in results)
    return {"ok": ok, "results": results}


def _install_one(entry, models_dir, force, fetch):
    dest = os.path.join(models_dir, entry["filename"])
    base = {
        "id": entry["id"],
        "name": entry["name"],
        "organism": entry["organism"],
        "filename": entry["filename"],
        "path": dest,
        "expected_sha256": entry["sha256"],
        "expected_size": entry["size"],
        "license": entry["license"],
        "citation": entry["citation"],
    }
    if os.path.exists(dest):
        actual = _sha256_file(dest)
        if actual == entry["sha256"]:
            return {**base, "status": "already_installed", "sha256": actual}
        if not force:
            return {
                **base, "status": "conflict", "sha256": actual,
                "detail": "本地同名文件与登记 SHA256 不一致（可能为上游更新 / 本地修改 / 旧版本）。未覆盖——人工检查后可用 force=true 重装。",
            }
    tmp = dest + ".download"
    inner_tmp = dest + ".inner"
    try:
        channel = fetch(entry["url"], tmp)
        data_path = tmp
        if entry.get("archive") == "zip":
            _extract_from_zip(tmp, entry["inner"], inner_tmp)
            os.remove(tmp)
            data_path = inner_tmp
        got = _sha256_file(data_path)
        if got != entry["sha256"]:
            os.remove(data_path)
            return {
                **base, "status": "sha_mismatch", "sha256": got, "channel": channel,
                "detail": "下载内容 SHA256 与登记不一致（上游文件疑似变更或传输损坏）。已丢弃下载内容；确认上游更新后人工更新 catalog 登记值。",
            }
        os.replace(data_path, dest)
        size = os.path.getsize(dest)
        _update_ledger(models_dir, entry, {"channel": channel, "size_actual": size})
        return {**base, "status": "installed", "sha256": got, "size_actual": size, "channel": channel}
    except Exception as e:  # noqa: BLE001 —— 全部失败路径显式返回，不静默
        for p in (tmp, inner_tmp):
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return {**base, "status": "error", "error": f"{type(e).__name__}: {str(e)[:300]}"}


def verify_models(models_dir, entry_id=None):
    """校验本地已装文件与登记 SHA256 的一致性（不触网）。"""
    entries = CATALOG if entry_id is None else [e for e in CATALOG if e["id"] == entry_id]
    if entry_id is not None and not entries:
        return {"ok": False, "error": f"unknown model id: {entry_id}（可用: {[e['id'] for e in CATALOG]}）", "results": []}
    results = []
    all_ok = True
    for e in entries:
        dest = os.path.join(models_dir, e["filename"])
        if not os.path.exists(dest):
            all_ok = False
            results.append({"id": e["id"], "status": "not_installed", "path": dest})
            continue
        actual = _sha256_file(dest)
        ok = actual == e["sha256"]
        all_ok = all_ok and ok
        results.append({
            "id": e["id"],
            "status": "match" if ok else "mismatch",
            "path": dest,
            "sha256": actual,
            "expected_sha256": e["sha256"],
            "size": os.path.getsize(dest),
        })
    return {"ok": all_ok, "results": results}
