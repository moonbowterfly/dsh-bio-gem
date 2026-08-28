# gapseq_wsl.py — dsh-bio-gem M2：gapseq 引擎的 WSL 桥（Windows → WSL2）
# 实测背景（农杆菌 C58 项目 2026-08）：gapseq 2.1.0 部署于 WSL2 Ubuntu-22.04
# conda env "gapseq"（TUNA 渠道），序列库 F:\Datasets\gapseq\db\Bacteria（v1.5）。
# 关键坑（HANDOFF-01/03）：
#   - wsl.exe 输出是 UTF-16LE 乱码 -> decode('utf-16-le') 再清理
#   - 复杂命令写 .sh 文件再执行（多层引号易坏）；本模块用 subprocess 传 bash -lc 简单命令，
#     长逻辑写脚本文件到 /mnt/f 再跑
#   - gapseq 序列库"假已装"（缺 version_seqDB.json）会导致 UniProt 灾难级在线下载
#     -> 探测必须验证 update-sequences 输出 up-to-date
import json
import os
import shutil
import subprocess
import sys
import time

# Windows 下 stdout/stderr 默认 GBK——统一 UTF-8（独立脚本也要）
for _s in ("stdout", "stderr"):
    try:
        getattr(sys, _s).reconfigure(encoding="utf-8")
    except Exception:
        pass

WSL_DISTRO = os.environ.get("GEM_GAPSEQ_DISTRO", "Ubuntu-22.04")
CONDA_SH = "/opt/miniforge3/etc/profile.d/conda.sh"
GAPSEQ_ENV = "gapseq"
SEQDB_WIN = os.environ.get("GEM_GAPSEQ_DB", r"F:\Datasets\gapseq\db")
SEQDB_MNT = "/mnt/f/Datasets/gapseq/db" if not os.environ.get("GEM_GAPSEQ_DB") else "/mnt/f/Datasets/gapseq/db"


def wsl_run(bash_cmd, timeout=300):
    """在 WSL root 下执行 bash 命令，返回 (rc, stdout_utf8, stderr_utf8)。"""
    try:
        r = subprocess.run(
            ["wsl.exe", "-d", WSL_DISTRO, "-u", "root", "--", "bash", "-lc", bash_cmd],
            capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return (-1, "", "wsl.exe not found")
    except subprocess.TimeoutExpired:
        return (-2, "", f"timeout {timeout}s")
    out = _utf16_decode(r.stdout)
    err = _utf16_decode(r.stderr)
    return (r.returncode, out, err)


def _utf16_decode(b):
    """wsl.exe 输出解码：新版（Win11）直接 UTF-8；旧版 UTF-16LE（含 BOM/00 填充）。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError:
        pass
    try:
        return b.decode("utf-16-le", errors="ignore").replace("\x00", "").strip()
    except Exception:
        return b.decode("utf-8", errors="ignore").strip()


def probe():
    """能力探测：wsl / 发行版 / conda env / gapseq 版本 / 序列库注册。
    返回 dict（capability 级别：OK / DEGRADED / MISSING + 详情）。"""
    res = {"capable": False, "level": "MISSING", "checks": {}}

    rc, out, err = wsl_run("uname -a", 30)
    res["checks"]["wsl"] = rc == 0
    if rc != 0:
        res["detail"] = f"wsl 不可用: {out[:200] or err[:200]}"
        return res
    rc, out, err = wsl_run("grep -iE '^ID=' /etc/os-release 2>/dev/null", 30)
    res["checks"]["distro"] = rc == 0 and "ubuntu" in out.lower()
    if not res["checks"]["distro"]:
        res["detail"] = f"发行版非预期（期望 Ubuntu）: {out[:200] or err[:200]}"
        res["level"] = "DEGRADED"
        return res

    rc, out, err = wsl_run(
        f"source {CONDA_SH} && conda activate {GAPSEQ_ENV} && gapseq -v 2>&1", 120)
    res["checks"]["gapseq"] = rc == 0 and "gapseq" in out.lower()
    if not res["checks"]["gapseq"]:
        res["detail"] = f"gapseq 环境不可用: {out[:300] or err[:300]}"
        res["level"] = "DEGRADED"
        return res
    import re
    mm = re.search(r"gapseq version:\s*([\d.]+)", out)
    res["gapseq_version"] = mm.group(1) if mm else out[:80]

    # 序列库注册检查（防假已装 -> UniProt 在线灾难）
    rc, out, err = wsl_run(
        f"source {CONDA_SH} && conda activate {GAPSEQ_ENV} && "
        f"gapseq update-sequences -t Bacteria -D {SEQDB_MNT} -q -c 2>&1", 300)
    ok = ("up-to-date" in out.lower()) or ("already" in out.lower() and "up-to-date" in out.lower())
    res["checks"]["seqdb"] = ok
    if not ok:
        res["detail"] = f"序列库未注册（可能触发在线下载灾难）: {out[:400]}"
        res["level"] = "DEGRADED"
        return res

    res["capable"] = True
    res["level"] = "OK"
    return res


def run_gapseq(input_fna, work_win, name="model", progress=None):
    """在 WSL 侧运行 gapseq doall。
    input_fna : Windows 绝对路径的核苷酸 fasta（将被复制到 WSL 侧 /opt/gem-gapseq-work/）
    work_win  : Windows 输出目录（产物 C58.xml 等拷回路径，缺省 ~/.dsh/dsh-bio-gem/models）
    进度事件通过 progress(path, event) 回调落盘。返回 (model_xml_path, doall_log_tail)。
    """
    import tempfile
    work_dir = work_win or os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "models")
    os.makedirs(work_dir, exist_ok=True)
    wsl_work = "/opt/gem-gapseq-work"
    wsl_run(f"mkdir -p {wsl_work} && rm -f {wsl_work}/*", 60)
    # 先探测分发包（/mnt/<盘>/...），再复制
    win_drive = input_fna.split(":")[0].lower()
    mnt = f"/mnt/{win_drive}"
    rel = input_fna.split(":", 1)[1].replace("\\", "/")
    src = f"{mnt}{rel}"
    sh = (f"cp '{src}' {wsl_work}/input.fna && cd {wsl_work} && "
          f"source {CONDA_SH} && conda activate {GAPSEQ_ENV} && "
          f"gapseq doall -s input.fna -o out -b {name} > doall.log 2>&1; "
          f"echo $? > doall.rc")
    if progress:
        progress({"event": "gapseq_start", "wsl_cmd": "gapseq doall ...（可能 30-60min）"})
    # doall 长任务：轮询 doall.rc 哨兵（完成即退出），每 2min 报一次进度
    try:
        rc = -2
        begin = time.time()
        while time.time() - begin < 7200:
            rc2, out2, err2 = wsl_run(
                f"test -f {wsl_work}/doall.rc && cat {wsl_work}/doall.rc || echo RUNNING", 60)
            if "RUNNING" not in out2 and out2.strip():
                try:
                    rc = int(out2.strip().split()[-1] or 0)
                except ValueError:
                    rc = 0
                break
            if progress:
                progress({"event": "gapseq_progress",
                          "elapsed_s": int(time.time() - begin),
                          "doall_log_tail": _tail_wsl(wsl_work, 40)})
            time.sleep(120)
        if rc == -2:
            if progress:
                progress({"event": "gapseq_timeout"})
            raise RuntimeError("gapseq doall timeout 2h")
        if rc != 0:
            # doall 内部失败：日志尾部作为诊断
            _r, _o, _e = wsl_run(f"tail -60 {wsl_work}/doall.log", 60)
            raise RuntimeError(f"gapseq doall rc={rc}; 日志尾部: {_o[-800:] or _e[-400:]}")
    finally:
        pass

    # 产物定位（gapseq 输出 out/<name>.xml）
    rc2, out2, err2 = wsl_run(f"ls {wsl_work}/out/", 60)
    print("wsl out dir:", out2[:400])
    # 拷贝回 Windows
    r = subprocess.run(
        ["wsl.exe", "-d", WSL_DISTRO, "-u", "root", "--", "bash", "-lc",
         f"cp {wsl_work}/out/*.xml {wsl_work}/out/*.faa.gz {wsl_work}/out/*.tbl {work_win.replace(chr(92), '/')} 2>/dev/null; "
         f"cp {wsl_work}/doall.log {work_win.replace(chr(92), '/')}/gapseq_doall.log 2>/dev/null"],
        capture_output=True, timeout=300)
    # 查找 xml
    xmls = [f for f in os.listdir(work_dir) if f.endswith(".xml")]
    if not xmls:
        raise RuntimeError(f"gapseq 未产出 xml（doall 日志尾部）: {_tail(work_dir, 'gapseq_doall.log')}")
    model = os.path.join(work_dir, xmls[0])
    return model, _tail(work_dir, "gapseq_doall.log")


def _tail_wsl(wsl_work, n=40):
    """WSL 侧 doall.log 尾部（进度事件携带，供前端旁观）。"""
    _r, _o, _e = wsl_run(f"tail -{n} {wsl_work}/doall.log 2>/dev/null", 30)
    return (_o or "").strip()


def _tail(work_dir, logname, n=60):
    p = os.path.join(work_dir, logname)
    if not os.path.exists(p):
        return ""
    with open(p, encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()
    return "".join(lines[-n:])


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if cmd == "probe":
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
    elif cmd == "version":
        rc, out, err = wsl_run(
            f"source {CONDA_SH} && conda activate {GAPSEQ_ENV} && gapseq -v 2>&1", 120)
        print(out[:300] or err[:300])
    else:
        print("unknown cmd")