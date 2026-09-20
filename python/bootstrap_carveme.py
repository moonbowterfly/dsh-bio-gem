# bootstrap_carveme.py — CarveMe 运行时「零手动部署」（探测 → uv venv → carveme → diamond → 冒烟 → manifest）
#
# 产品原则：用户零手动安装。gem_build(engine=carveme) 首次调用时自动补齐运行时：
#   1. 探测现有 venv（carve.exe + diamond.exe + 冒烟通过）→ 就绪则秒回
#   2. 缺则部署：uv venv（uv 探测链见下）→ uv pip install carveme → 下载 diamond 官方二进制 → 冒烟
#   3. 全过程写进度事件（progress.jsonl），失败给「可执行指引」而不是裸异常
#
# uv 探测链：GEM_UV env → genie 自举 uv（~/.dsh/dsh-bio-genie/bin/uv.exe）→ PATH uv
# diamond 来源：GitHub release 固定版本（v2.2.8, diamond-windows.zip, 3.4MB 纯二进制）
#   下载策略：直连 → 环境变量代理（HTTPS_PROXY/HTTP_PROXY）→ 失败给指引
#
# 幂等性：重复调用不重复部署；diamond 单独缺失时只补 diamond；manifest.json 记录版本与来源
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile

DEFAULT_CARVE_VENV = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "venv-carveme")
GEM_ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem")
DIAMOND_VERSION = "v2.2.8"
DIAMOND_URL = f"https://github.com/bbuchfink/diamond/releases/download/{DIAMOND_VERSION}/diamond-windows.zip"
GENIE_UV = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-genie", "bin", "uv.exe")

VERIFY_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300


def _log(progress_path, event):
    """进度事件（与 build.py 同格式；progress_path 为 None 时静默）。"""
    if not progress_path:
        return
    try:
        ev = {"ts": time.time(), **event}
        with open(progress_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _run(cmd, timeout=VERIFY_TIMEOUT, env=None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env, encoding="utf-8", errors="replace")


def find_uv():
    """uv 探测链：GEM_UV → genie 自举 → PATH。返回 (uv_path, source) 或 (None, None)。"""
    env_uv = os.environ.get("GEM_UV")
    if env_uv and os.path.exists(env_uv):
        return env_uv, "env:GEM_UV"
    if os.path.exists(GENIE_UV):
        return GENIE_UV, "genie-bootstrap"
    which = shutil.which("uv")
    if which:
        return which, "PATH"
    return None, None


def _carve_exe(venv=None):
    return os.path.join(venv or DEFAULT_CARVE_VENV, "Scripts", "carve.exe")


def _diamond_exe(venv=None):
    return os.path.join(venv or DEFAULT_CARVE_VENV, "Scripts", "diamond.exe")


def _probe(venv=None, deep=False):
    """就绪探测。
    deep=False（默认）：文件存在 + manifest 信任 —— 秒级（gem_build 每次调用的日常路径）；
    deep=True：额外跑 diamond/carve 冒烟（部署完成后复验一次用）。
    返回 (ok, detail)。"""
    carve, diamond = _carve_exe(venv), _diamond_exe(venv)
    if not os.path.exists(carve):
        return False, {"missing": "carve.exe"}
    if not os.path.exists(diamond):
        return False, {"missing": "diamond.exe"}
    if not deep and _read_manifest(venv):
        return True, {"mode": "fast-manifest"}
    try:
        r1 = _run([diamond, "--version"])
        if r1.returncode != 0:
            return False, {"diamond_broken": (r1.stderr or r1.stdout or "")[:200]}
        # carve 无 --version：用 -h 验证可启动（exit 0）
        r2 = _run([carve, "-h"])
        if r2.returncode not in (0, 1):  # argparse -h 正常为 0；某些版本非 0
            return False, {"carve_broken": (r2.stderr or r2.stdout or "")[:200]}
        return True, {"diamond": (r1.stdout or "").strip()[:80], "mode": "deep-smoke"}
    except Exception as e:
        return False, {"probe_error": f"{type(e).__name__}: {e}"}


def _download(url, dest, progress_path=None):
    """下载（直连 → 环境代理 → 抛错含指引）。返回使用的通道。"""
    headers = {"User-Agent": "dsh-bio-gem-bootstrap/1.0"}

    def _get(opener=None):
        req = urllib.request.Request(url, headers=headers)
        op = opener.open(req, timeout=DOWNLOAD_TIMEOUT) if opener else urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT)
        with op as resp, open(dest, "wb") as f:
            shutil.copyfileobj(resp, f)

    try:
        _get()
        return "direct"
    except Exception as e1:
        _log(progress_path, {"event": "diamond_direct_failed", "err": str(e1)[:200]})
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy"))
    if proxy:
        try:
            opener = urllib.request.build_opener(
                urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            _get(opener)
            return f"proxy:{proxy}"
        except Exception as e2:
            _log(progress_path, {"event": "diamond_proxy_failed", "err": str(e2)[:200]})
    raise RuntimeError(
        f"无法下载 diamond（{url}）。可执行的手动方案：\n"
        f"  1) 设置代理后重试：set HTTPS_PROXY=http://127.0.0.1:端口\n"
        f"  2) 或手动下载 diamond-windows.zip，把解压出的 diamond.exe 放到：\n"
        f"     {os.path.join(DEFAULT_CARVE_VENV, 'Scripts')}")


def ensure_carveme(venv=None, progress_path=None, force_diamond=False):
    """确保 CarveMe 运行时可用（幂等）。返回 dict：
    {ready, action: already|deployed|repaired|failed, carve, diamond, version, note?, error?}
    """
    venv = venv or DEFAULT_CARVE_VENV
    ok, detail = _probe(venv)
    if ok and not force_diamond:
        return {"ready": True, "action": "already", "carve": _carve_exe(venv),
                "diamond": _diamond_exe(venv), "note": detail.get("diamond", "")}

    _log(progress_path, {"event": "bootstrap_start", "venv": venv, "probe": detail})
    os.makedirs(os.path.dirname(venv), exist_ok=True)

    uv, uv_src = find_uv()
    if not uv:
        return {"ready": False, "action": "failed", "error":
                "未找到 uv（探测链：GEM_UV env → ~/.dsh/dsh-bio-genie/bin/uv.exe → PATH）。"
                "请安装 uv（https://docs.astral.sh/uv/）或安装宿主插件 dsh-bio-genie 以复用其自举 uv。"}

    # ---- 1) venv + carveme（carve.exe 缺失或损坏时） ----
    need_env = not os.path.exists(_carve_exe(venv))
    if need_env:
        _log(progress_path, {"event": "uv_venv", "uv": uv, "uv_source": uv_src})
        try:
            r = _run([uv, "venv", venv, "--python", "3.13"], timeout=300)
            if r.returncode != 0 or not os.path.exists(os.path.join(venv, "Scripts", "python.exe")):
                return {"ready": False, "action": "failed",
                        "error": f"uv venv 创建失败 rc={r.returncode}: {(r.stderr or '')[-300:]}"}
        except Exception as e:
            return {"ready": False, "action": "failed", "error": f"uv venv 异常: {e}"}

        py = os.path.join(venv, "Scripts", "python.exe")
        _log(progress_path, {"event": "uv_pip_carveme", "detail": "uv pip install carveme"})
        try:
            r = _run([uv, "pip", "install", "--python", py, "carveme"], timeout=900)
            if r.returncode != 0:
                return {"ready": False, "action": "failed",
                        "error": f"carveme 安装失败 rc={r.returncode}: {(r.stderr or '')[-400:]}"}
        except Exception as e:
            return {"ready": False, "action": "failed", "error": f"carveme 安装异常: {e}"}

    # ---- 2) diamond（缺失/损坏时补） ----
    if not os.path.exists(_diamond_exe(venv)) or force_diamond:
        scripts_dir = os.path.join(venv, "Scripts")
        os.makedirs(scripts_dir, exist_ok=True)
        tmp_zip = os.path.join(venv, "diamond-download.zip")
        _log(progress_path, {"event": "diamond_download", "url": DIAMOND_URL})
        try:
            channel = _download(DIAMOND_URL, tmp_zip, progress_path)
        except RuntimeError as e:
            return {"ready": False, "action": "failed", "error": str(e)}
        try:
            with zipfile.ZipFile(tmp_zip) as z:
                names = z.namelist()
                if "diamond.exe" not in names:
                    return {"ready": False, "action": "failed",
                            "error": f"diamond zip 内容异常: {names}"}
                z.extract("diamond.exe", scripts_dir)
        except Exception as e:
            return {"ready": False, "action": "failed", "error": f"diamond 解压失败: {e}"}
        finally:
            try:
                os.remove(tmp_zip)
            except OSError:
                pass
        _log(progress_path, {"event": "diamond_ready", "channel": channel})

    # ---- 3) 冒烟复验（deep：真跑 diamond/carve 一次） ----
    ok2, detail2 = _probe(venv, deep=True)
    version = _read_carve_version(venv)
    manifest = {
        "carve_version": version,
        "diamond_version": DIAMOND_VERSION,
        "diamond_url": DIAMOND_URL,
        "uv_source": uv_src,
        "installed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    try:
        with open(os.path.join(venv, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=1)
    except OSError:
        pass

    if not ok2:
        return {"ready": False, "action": "failed",
                "error": f"部署后冒烟未通过: {detail2}"}
    action = "deployed" if need_env else "repaired"
    _log(progress_path, {"event": "bootstrap_done", "action": action, "manifest": manifest})
    return {"ready": True, "action": action, "carve": _carve_exe(venv),
            "diamond": _diamond_exe(venv), "manifest": manifest}


def _read_carve_version(venv=None):
    py = os.path.join(venv or DEFAULT_CARVE_VENV, "Scripts", "python.exe")
    if not os.path.exists(py):
        return None
    try:
        r = _run([py, "-c",
                  "import importlib.metadata as im; print(im.version('carveme'))"])
        if r.returncode == 0:
            return (r.stdout or "").strip()
    except Exception:
        pass
    return None


def carveme_status(venv=None):
    """只读状态（供诊断/工具面板）。"""
    ok, detail = _probe(venv)
    return {"ready": ok, "venv": venv or DEFAULT_CARVE_VENV,
            "carve": _carve_exe(venv), "diamond": _diamond_exe(venv),
            "detail": detail, "manifest": _read_manifest(venv),
            "uv": find_uv()[1]}


def _read_manifest(venv=None):
    p = os.path.join(venv or DEFAULT_CARVE_VENV, "manifest.json")
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


if __name__ == "__main__":
    # CLI：python bootstrap_carveme.py [status|ensure]
    action = sys.argv[1] if len(sys.argv) > 1 else "ensure"
    if action == "status":
        print(json.dumps(carveme_status(), ensure_ascii=False, indent=1))
    else:
        out = ensure_carveme()
        print(json.dumps(out, ensure_ascii=False, indent=1))
        sys.exit(0 if out.get("ready") else 1)
