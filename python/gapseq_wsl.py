# gapseq_wsl.py — dsh-bio-gem M2：gapseq 引擎的 WSL 桥（Windows → WSL2）
# 实测背景（农杆菌 C58 项目 2026-08）：gapseq 2.1.0 部署于 WSL2 Ubuntu-22.04
# conda env "gapseq"（TUNA 渠道），序列库 F:\Datasets\gapseq\db\Bacteria（v1.5）。
# 关键实测（2026-08-29）：
#   - 新版 wsl.exe（Win11）输出 UTF-8；旧版 UTF-16LE —— decode 双兼容（UTF-8 strict 优先）
#   - wsl.exe 命令行传含空格路径 + 引号必坏 -> 一切路径进脚本文件，脚本用 base64 传输
#   - doall 是 bash 脚本：输入为位置参数（gapseq doall FILE.fna），-m/-f 组合实测触发 usage
#   - wsl.exe 会话 teardown 会杀后台作业 -> nohup + setsid + sleep 3 保活
# 架构（用户 2026-08-29 指示）：原子步骤思想 —— launch/status/fetch 拆为独立函数，
# 由 dsh agent 编排（gem_gapseq 工具：setup/launch/status/fetch），利用 agent 自愈推进长任务。
import json
import os
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
WSL_WORK = "/opt/gem-gapseq-work"


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
    return (r.returncode, _decode(r.stdout), _decode(r.stderr))


def _decode(b):
    """wsl.exe 输出解码：新版（Win11）UTF-8；旧版 UTF-16LE（含 BOM/00 填充）。"""
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
    """能力探测：wsl / 发行版 / gapseq 环境 / 序列库注册（防假已装 UniProt 灾难）。
    返回 dict（level: OK / DEGRADED / MISSING + checks）。"""
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
    seqdb = "/mnt/f/Datasets/gapseq/db"
    rc, out, err = wsl_run(
        f"source {CONDA_SH} && conda activate {GAPSEQ_ENV} && "
        f"gapseq update-sequences -t Bacteria -D {seqdb} -q -c 2>&1", 300)
    res["checks"]["seqdb"] = "up-to-date" in out.lower()
    if not res["checks"]["seqdb"]:
        res["detail"] = f"序列库未注册（可能触发在线下载灾难）: {out[:400]}"
        res["level"] = "DEGRADED"
        return res
    res["capable"] = True
    res["level"] = "OK"
    return res


# ---------------------------------------------------------------------------
# 原子步骤（dsh agent 编排：setup -> launch -> status* -> fetch）
# ---------------------------------------------------------------------------
def launch_gapseq(input_fna, name="model", work_win=None):
    """拷贝输入 + 构造 doall 脚本（base64 防引号）+ nohup/setsid 后台启动（立即返回）。
    单实例保护：已有 running 任务时拒绝重复 launch（返回 launched:false + reason）。
    返回 {launched, work_win, wsl_work, name, reason?}。"""
    import base64
    # 协议层单实例保护：正在运行则拒绝
    st = status_gapseq()
    if st["state"] == "running":
        return {"launched": False, "reason": "已有 doall 在运行（state=running），先 action=status 等待完成，不要重复 launch",
                "work_win": work_win, "wsl_work": WSL_WORK, "name": name}
    work_dir = work_win or os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "models")
    os.makedirs(work_dir, exist_ok=True)
    if not os.path.exists(input_fna):
        raise ValueError(f"输入文件不存在: {input_fna}")
    win_drive = input_fna.split(":")[0].lower()
    src = f"/mnt/{win_drive}{input_fna.split(':', 1)[1].replace(chr(92), '/')}"

    script = (
        "#!/bin/bash\n"
        # 残留 blastp 清理（blastp 不会出现在本脚本命令行里，pkill 无自伤风险；
        # doall 相关进程不在此清——靠协议层 running 保护防重复启动）
        "pkill -9 -f blastp 2>/dev/null; sleep 1\n"
        f"mkdir -p {WSL_WORK} && rm -f {WSL_WORK}/*\n"
        f"cp '{src}' {WSL_WORK}/{name}.fna || {{ echo 'COPY_FAIL' > {WSL_WORK}/doall.rc; exit 1; }}\n"
        f"cd {WSL_WORK}\n"
        f"source {CONDA_SH}\n"
        f"conda activate {GAPSEQ_ENV}\n"
        # doall 只吃位置参数（实测 -m/-f/-D 任何 option 组合都会触发 usage——doall.sh getopts 有坑）；
        # 默认介质 auto + 默认序列库目录（envs/gapseq/share/gapseq/dat/seq/Bacteria 已完整部署 1.1GB）
        f"gapseq doall {name}.fna > doall.log 2>&1\n"
        "echo $? > doall.rc\n"
    )
    b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
    rc, out, err = wsl_run(
        f"mkdir -p {WSL_WORK} && echo {b64} | base64 -d > {WSL_WORK}/run_doall.sh && "
        f"chmod +x {WSL_WORK}/run_doall.sh && ls -la {WSL_WORK}/run_doall.sh", 60)
    if "run_doall.sh" not in out:
        raise RuntimeError(f"doall 脚本未落盘: {out[:300] or err[:300]}")
    rc, out, err = wsl_run(
        f"cd / && nohup setsid {WSL_WORK}/run_doall.sh >/dev/null 2>&1 < /dev/null & "
        f"sleep 3 && echo LAUNCHED", 60)
    return {"launched": "LAUNCHED" in out, "work_win": work_dir,
            "wsl_work": WSL_WORK, "name": name, "launch_out": out[:120]}


def status_gapseq():
    """查 doall 状态：先看进程（blastp/gapseq_find 在跑 = running），再看哨兵 doall.rc。
    返回 {state: idle|running|done|failed|unknown, rc, log_tail, hint}。"""
    log_tail = _tail_wsl(WSL_WORK, 40)
    # 1) 真实进程存在性
    _r, proc_out, _e = wsl_run(
        "ps aux | grep -E 'blastp|gapseq_find' | grep -v grep | wc -l", 30)
    try:
        proc_n = int((proc_out.strip() or "0").split()[0])
    except (ValueError, IndexError):
        proc_n = -1
    # 2) 哨兵
    _r, out, _e = wsl_run(
        f"test -f {WSL_WORK}/doall.rc && cat {WSL_WORK}/doall.rc || echo NONE", 30)
    has_sentinel = "NONE" not in out
    if proc_n > 0 and not has_sentinel:
        return {"state": "running", "rc": None, "log_tail": log_tail,
                "note": "doall 运行中（30-60min），2-5 分钟后再查"}
    if proc_n == 0 and not has_sentinel:
        return {"state": "idle", "rc": None, "log_tail": log_tail,
                "note": "无运行任务，可 action=launch 启动"}
    try:
        rc_val = int(out.strip().split()[-1])
    except (ValueError, IndexError):
        rc_val = None
    if rc_val == 0:
        return {"state": "done", "rc": 0, "log_tail": log_tail}
    if rc_val is not None:
        return {"state": "failed", "rc": rc_val, "log_tail": log_tail,
                "hint": "COPY_FAIL=输入拷贝失败（检查 /mnt/f 路径可读）；其它 rc 见日志尾部"}
    return {"state": "unknown", "rc": None, "log_tail": log_tail}


def fetch_gapseq(work_win, name="model", log_tail=None):
    """doall 完成后把 WSL 产物拷回 Windows。返回 {model, files, wsl_out_ls, log_local}。"""
    work_dir = work_win or os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "models")
    os.makedirs(work_dir, exist_ok=True)
    rc2, out2, err2 = wsl_run(f"ls {WSL_WORK}/ {WSL_WORK}/*.xml 2>/dev/null", 60)
    wsl_ls = out2[:400]
    win_slash = work_dir.replace("\\", "/")
    wsl_run(
        f"cp {WSL_WORK}/*.xml {WSL_WORK}/*.faa.gz {WSL_WORK}/*.tbl {win_slash}/ 2>/dev/null; "
        f"cp {WSL_WORK}/doall.log {win_slash}/gapseq_doall.log 2>/dev/null", 180)
    files = [f for f in os.listdir(work_dir)
             if f.endswith((".xml", ".faa.gz", ".tbl")) or f == "gapseq_doall.log"]
    xmls = [f for f in files if f.endswith(".xml")]
    if not xmls:
        raise RuntimeError(f"gapseq 未产出 xml（WSL 目录: {wsl_ls}；日志尾部: {log_tail or ''}）")
    model = os.path.join(work_dir, xmls[0])
    return {"model": model, "files": files, "wsl_out_ls": wsl_ls,
            "log_local": os.path.join(work_dir, "gapseq_doall.log")}


def run_gapseq(input_fna, work_win, name="model", progress=None):
    """组合封装（build.py 兼容）：launch -> 轮询 -> fetch。
    新架构推荐 agent 直接用原子步骤（gem_gapseq launch/status/fetch）编排。"""
    launch_gapseq(input_fna, name=name, work_win=work_win)
    st = time.time()
    while True:
        stt = status_gapseq()
        if stt["state"] != "running":
            break
        if progress:
            progress({"event": "gapseq_progress", "elapsed_s": int(time.time() - st),
                      "log_tail": stt.get("log_tail", "")})
        time.sleep(120)
    if stt["state"] == "failed":
        raise RuntimeError(f"gapseq doall rc={stt.get('rc')}; 日志尾部: {stt.get('log_tail', '')}")
    if stt["state"] != "done":
        raise RuntimeError(f"gapseq doall 异常状态: {stt}")
    fet = fetch_gapseq(work_win, name=name, log_tail=stt.get("log_tail"))
    return fet["model"], _tail(work_win, "gapseq_doall.log")


def _tail_wsl(wsl_work, n=40):
    """WSL 侧 doall.log 尾部（进度事件携带）。"""
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
    import sys as _sys
    cmd = _sys.argv[1] if len(_sys.argv) > 1 else "probe"
    if cmd == "probe":
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
    elif cmd == "version":
        rc, out, err = wsl_run(
            f"source {CONDA_SH} && conda activate {GAPSEQ_ENV} && gapseq -v 2>&1", 120)
        print(out[:300] or err[:300])
    else:
        print("unknown cmd")