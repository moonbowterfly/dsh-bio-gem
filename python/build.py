# build.py — dsh-bio-gem M1：CarveMe 基因组→SBML 构建（纯 Windows）
# 流程: 输入(protein.faa 优先; accesson 与 GFF+fna 二期待支持) -> carve 子进程
#       -> validate G1-G3 -> 若 G3 FAIL 且给了 medium -> gapfind/gapfill 闭环 -> 模型卡 sidecar
# 进度: 独立 CLI 模式将事件写入 <out>.progress.jsonl（TS 层 jobs.js 轮询）；CARVE_CMD 可被 env 覆盖。
import json
import os
import subprocess
import sys
import tempfile
import time
import datetime

DEFAULT_CARVE_VENV = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "venv-carveme")
MODEL_ROOT = os.path.join(os.path.expanduser("~"), ".dsh", "dsh-bio-gem", "models")


def _log(progress_path, event):
    ev = {"ts": time.time(), **event}
    with open(progress_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _carve_exe(venv=None):
    venv = venv or DEFAULT_CARVE_VENV
    exe = os.path.join(venv, "Scripts", "carve.exe")
    if not os.path.exists(exe):
        exe = "carve"  # 退回 PATH
    return exe


def resolve_input(input_spec, progress_path=None):
    """输入归一化为 protein.faa 路径。
    支持: *.faa/*.fasta 蛋白序列；CDS 核苷酸(*.fna/.fasta)提示用蛋白；GFF 需配套给出蛋白。
    """
    if not input_spec:
        raise ValueError("input required (protein.faa / accession / local files)")
    if input_spec.lower().startswith(("gcf_", "gca_")):
        raise NotImplementedError(
            "accession 下载二期待支持：请先用 datasets CLI 或 NCBI 下载 protein.faa，再传入本地路径")
    p = input_spec
    low = p.lower()
    if low.endswith((".faa", ".fasta", ".fa", ".faa.gz", ".fasta.gz")):
        # 粗略检查是否为蛋白（首行 > 后首个氨基酸不是 ATGC 开头）
        return p
    raise ValueError(f"unsupported input type: {p}（首版请提供 protein.faa）")


def run_carve(proteins, out_xml, venv=None, progress_path=None, timeout=3600):
    exe = _carve_exe(venv)
    cmd = [exe, "--input", proteins, "--output", out_xml, "--gapfill", "universal"]
    _log(progress_path, {"event": "carve_start", "cmd": " ".join(cmd)})
    st = time.time()
    env = dict(os.environ)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _log(progress_path, {"event": "carve_timeout", "s": int(timeout)})
        raise
    dt = time.time() - st
    if r.returncode != 0:
        _log(progress_path, {"event": "carve_fail", "rc": r.returncode,
                             "stderr_tail": (r.stderr or "")[-800:]})
        raise RuntimeError(f"carve failed rc={r.returncode}: {(r.stderr or '')[-800:]}")
    _log(progress_path, {"event": "carve_done", "s": round(dt, 1)})
    return out_xml


def build(input_spec, name=None, medium=None, venv=None, out_dir=None, progress_path=None):
    """主入口：输入 -> carve -> validate -> gapfill 闭环 -> 模型卡。"""
    if progress_path is None:
        progress_path = os.path.join(tempfile.gettempdir(), "gem_build.progress.jsonl")
    _log(progress_path, {"event": "build_start", "input": input_spec})
    proteins = resolve_input(input_spec, progress_path)
    name = name or os.path.splitext(os.path.basename(proteins))[0]
    out_dir = out_dir or MODEL_ROOT
    os.makedirs(out_dir, exist_ok=True)
    out_xml = os.path.join(out_dir, name + ".xml")

    # 1) carve
    st = time.time()
    if not (os.path.exists(out_xml) and os.path.getmtime(out_xml) > os.path.getmtime(proteins)):
        run_carve(proteins, out_xml, venv=venv, progress_path=progress_path)
    else:
        _log(progress_path, {"event": "carve_skip_cached"})
    # 2) validate G1-G3
    from validate import validate_model
    rep = validate_model(out_xml, medium=medium, reference_growth=None)
    g3 = rep["g3"]
    _log(progress_path, {"event": "validate", "overall": rep["overall"],
                         "g3": g3["status"], "growth": g3.get("growth_medium")})
    # 3) gapfill 闭环（G3 失败且给了 medium）
    gapfixes = []
    if g3["status"] == "FAIL" and medium:
        _log(progress_path, {"event": "gapfill_start"})
        from gapfill import apply_fixes
        gf = apply_fixes(out_xml, medium=medium, max_add=20,
                         out=out_xml[:-4] + "_gf.xml")
        gapfixes = gf.get("applied", [])
        if gapfixes:
            rep = validate_model(gf["out"], medium=medium, reference_growth=None)
            out_xml = gf["out"]
            _log(progress_path, {"event": "gapfill_done", "applied": len(gapfixes),
                                 "g3_after": rep["g3"]["status"],
                                 "growth_after": rep["g3"].get("growth_medium")})
    dt = round(time.time() - st, 1)
    # 4) 模型卡
    card = {
        "name": name, "engine": "carveme", "engine_version": _carve_version(venv),
        "carve_cmd": "carve --input ... --output ... --gapfill",
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": dt, "model": out_xml,
        "validations": {k: rep[k]["status"] for k in ("g1", "g2", "g3")},
        "growth_g3": rep["g3"].get("growth_medium"),
        "gapfixes": gapfixes, "medium": medium,
        "mapping": {"protein_input": proteins},
    }
    card_path = os.path.join(out_dir, name + ".card.json")
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    _log(progress_path, {"event": "build_done", "model": out_xml, "card": card_path, "s": dt})
    return {"model": out_xml, "card": card_path, "validations": card["validations"],
            "growth_g3": card["growth_g3"], "gapfixes_applied": len(gapfixes),
            "elapsed_s": dt}


def _carve_version(venv=None):
    try:
        venv = venv or DEFAULT_CARVE_VENV
        py = os.path.join(venv, "Scripts", "python.exe")
        r = subprocess.run([py, "-c",
                            "import importlib.metadata as im; print(im.version('carveme'))"],
                           capture_output=True, text=True, timeout=60)
        return (r.stdout or "").strip() or "unknown"
    except Exception:
        return "unknown"


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--name")
    ap.add_argument("--medium-json")
    ap.add_argument("--out-dir")
    ap.add_argument("--progress")
    a = ap.parse_args()
    medium = json.loads(a.medium_json) if a.medium_json else None
    try:
        res = build(a.input, name=a.name, medium=medium, out_dir=a.out_dir,
                    progress_path=a.progress)
        print(json.dumps({"ok": True, "result": res}, ensure_ascii=False))
    except Exception as e:
        import traceback
        sys.stderr.write("Traceback (most recent call last):\n")
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": True, "result": None,
                          "error_hint": f"build failed: {type(e).__name__}: {e}"},
                         ensure_ascii=False))