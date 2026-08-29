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

# Python -I isolated 模式下脚本目录不进 sys.path——显式插入以导入同目录模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


def resolve_input(input_spec, progress_path=None, engine="carveme"):
    """输入归一化。
    engine=carveme: *.faa（蛋白）；engine=gapseq: *.fna（核苷酸）——gapseq 吃 DNA。
    accession 下载二期待支持。"""
    if not input_spec:
        raise ValueError("input required (protein.faa / accession / local files)")
    if input_spec.lower().startswith(("gcf_", "gca_")):
        raise NotImplementedError(
            "accession 下载二期待支持：请先用 datasets CLI 或 NCBI 下载蛋白/基因组，再传入本地路径")
    p = input_spec
    if not os.path.exists(p):
        d = os.path.dirname(p)
        cands = []
        if os.path.isdir(d):
            cands = [f for f in os.listdir(d)
                     if f.lower().endswith((".fna", ".faa", ".fasta", ".fa"))][:6]
        raise ValueError(f"输入文件不存在: {p}；目录内候选: {cands or '无'}")
    low = p.lower()
    if engine == "gapseq":
        if low.endswith((".fna", ".fasta", ".fa", ".fna.gz", ".fasta.gz")):
            return p
        raise ValueError(f"gapseq 引擎需要核苷酸 fasta（.fna/.fasta）：{p}")
    if low.endswith((".faa", ".fasta", ".fa", ".faa.gz", ".fasta.gz")):
        return p
    if engine == "carveme" and low.endswith((".fna", ".fna.gz")):
        # 路线 P0：裸/带注释基因组 -> 注释层 -> 蛋白（官方优先 + pyrodigal 兜底）
        from annotate import nucleotide_to_protein
        faa, src, stats = nucleotide_to_protein(p)
        print(f"[annotate] source={src} seqs={stats.get('seqs')} -> {faa}")
        return faa
    raise ValueError(f"unsupported input type: {p}（carveme 请提供 protein.faa 或 genomic.fna；gapseq 请提供 genomic.fna）")


def run_carve(proteins, out_xml, venv=None, progress_path=None, timeout=3600, gapfill_medium="M9"):
    exe = _carve_exe(venv)
    cmd = [exe, proteins, "-o", out_xml, "-g", gapfill_medium]
    _log(progress_path, {"event": "carve_start", "cmd": " ".join(cmd)})
    st = time.time()
    env = dict(os.environ)
    # carve 从 PATH 找 diamond（Windows venv 不激活时 Scripts 不在 PATH）——显式注入
    script_dir = os.path.join(venv or DEFAULT_CARVE_VENV, "Scripts")
    if script_dir and script_dir not in env.get("PATH", ""):
        env["PATH"] = script_dir + os.pathsep + env.get("PATH", "")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           env=env, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        _log(progress_path, {"event": "carve_timeout", "s": int(timeout)})
        raise
    dt = time.time() - st
    if r.returncode != 0:
        _log(progress_path, {"event": "carve_fail", "rc": r.returncode,
                             "stderr_tail": (r.stderr or "")[-800:],
                             "stdout_tail": (r.stdout or "")[-400:]})
        raise RuntimeError(f"carve failed rc={r.returncode}: {(r.stderr or '')[-800:]}")
    _log(progress_path, {"event": "carve_done", "s": round(dt, 1),
                         "stdout_tail": (r.stdout or "")[-300:]})
    return out_xml


def _active_medium(m):
    """模型当前打开的交换 -> {EX_id: lb}（carve gapfill 实际设置的介质，可溯源）。
    注意：CarveMe 默认把所有 EX 设成开放(-1000)，故全开交换数≠真介质成分；
    精确介质请用 _media_db_exchanges()。"""
    return {r.id: r.lower_bound for r in m.reactions
            if r.id.startswith("EX_") and r.lower_bound < 0}


def _media_db_exchanges(m, medium_name="M9", default_lb=-10.0):
    """从 carveme 自带 media_db.tsv 提取介质成分 -> 模型 EX 交换字典（精确介质）。"""
    import csv
    db = os.path.join(DEFAULT_CARVE_VENV, "Lib", "site-packages", "carveme",
                      "data", "input", "media_db.tsv")
    if not os.path.exists(db):
        return _active_medium(m)  # 退化：全部开放交换
    comps = set()
    with open(db, encoding="utf-8") as f:
        rd = csv.DictReader(f, delimiter="\t")
        for row in rd:
            if row.get("medium") == medium_name and row.get("compound"):
                comps.add(row["compound"].strip())
    out = {}
    for c in sorted(comps):
        exid = "EX_" + c + "_e"
        if exid in m.reactions:
            out[exid] = default_lb
    return out or _active_medium(m)


def build(input_spec, name=None, medium=None, venv=None, out_dir=None, progress_path=None,
          engine="carveme"):
    """主入口。engine:
      carveme（默认）: protein.faa -> carve(M9 gapfill) -> validate M9 -> 目标介质闭环
      gapseq（M2）  : genomic.fna -> WSL2 gapseq doall（30-60min，需探测 OK）-> 模型 -> 目标介质验证
    """
    if progress_path is None:
        progress_path = os.path.join(tempfile.gettempdir(), "gem_build.progress.jsonl")
    _log(progress_path, {"event": "build_start", "input": input_spec, "engine": engine})
    if engine == "gapseq":
        return _build_gapseq(input_spec, name=name, medium=medium, out_dir=out_dir,
                             progress_path=progress_path)
    proteins = resolve_input(input_spec, progress_path, engine="carveme")
    name = name or os.path.splitext(os.path.basename(proteins))[0]
    out_dir = out_dir or MODEL_ROOT
    os.makedirs(out_dir, exist_ok=True)
    out_xml = os.path.join(out_dir, name + ".xml")

    # 1) carve（自带 M9 gapfill，CarveMe 原生最小培养基）
    st = time.time()
    if not (os.path.exists(out_xml) and os.path.getmtime(out_xml) > os.path.getmtime(proteins)):
        run_carve(proteins, out_xml, venv=venv, progress_path=progress_path,
                  gapfill_medium="M9")
    else:
        _log(progress_path, {"event": "carve_skip_cached"})

    from silentio import silent_read_sbml
    m1 = silent_read_sbml(out_xml)
    med_m9 = _media_db_exchanges(m1, "M9")  # 精确 M9 成分（非全开近似）

    # 2) validate G1-G3：M9 介质（构建产物实测）
    from validate import validate_model
    rep_m9 = validate_model(out_xml, medium=med_m9, reference_growth=None)
    g3_m9 = rep_m9["g3"]
    _log(progress_path, {"event": "validate_m9", "overall": rep_m9["overall"],
                         "g3": g3_m9["status"], "growth": g3_m9.get("growth_medium"),
                         "exch": len(med_m9)})

    # 3) 用户目标介质（可选）：preset 展开 + resolve -> G3；FAIL 时 L1/L2 规则补洞闭环
    target = None
    user_rep = None
    if medium:
        from gapfind import resolve_medium, expand_medium
        medium_exp, preset_used = expand_medium(medium)
        resolved, unresolved = resolve_medium(m1, medium_exp)
        _log(progress_path, {"event": "target_medium", "preset": preset_used,
                             "resolved": len(resolved), "unresolved": unresolved})
        user_rep = validate_model(out_xml, medium=resolved, reference_growth=None)
        g3_user = user_rep["g3"]
        gapfixes = []
        if g3_user["status"] == "FAIL" and resolved:
            _log(progress_path, {"event": "gapfill_start"})
            from gapfill import apply_fixes
            gf = apply_fixes(out_xml, medium=resolved, max_add=20,
                             out=out_xml[:-4] + "_gf.xml")
            gapfixes = gf.get("applied", [])
            if gapfixes:
                user_rep = validate_model(gf["out"], medium=resolved, reference_growth=None)
                out_xml = gf["out"]
                g3_user = user_rep["g3"]
                _log(progress_path, {"event": "gapfill_done", "applied": len(gapfixes),
                                     "g3_after": g3_user["status"],
                                     "growth_after": g3_user.get("growth_medium")})
        target = {
            "medium": medium, "resolved_exchanges": len(resolved),
            "unresolved": unresolved,
            "g3": user_rep["g3"]["status"],
            "growth": user_rep["g3"].get("growth_medium"),
            "gapfixes_applied": len(gapfixes if 'gapfixes' in dir() else []),
        }
        # 若 M9 已 PASS 而用户介质 FAIL：诚实保留（模型可用介质=M9）
    dt = round(time.time() - st, 1)
    # 4) 模型卡
    card = {
        "name": name, "engine": "carveme", "engine_version": _carve_version(venv),
        "carve_cmd": "carve INPUT -o OUT -g M9",
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": dt, "model": out_xml,
        "validations_m9": {k: rep_m9[k]["status"] for k in ("g1", "g2", "g3")},
        "growth_g3_m9": g3_m9.get("growth_medium"),
        "m9_exchanges": len(med_m9),
        "target": target,
        "mapping": {"protein_input": proteins},
    }
    card_path = os.path.join(out_dir, name + ".card.json")
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    _log(progress_path, {"event": "build_done", "model": out_xml, "card": card_path, "s": dt})
    return {"model": out_xml, "card": card_path,
            "validations_m9": card["validations_m9"],
            "growth_g3_m9": card["growth_g3_m9"],
            "target": target or {"note": "no target medium provided"},
            "elapsed_s": dt}


def _build_gapseq(input_fna, name=None, medium=None, out_dir=None, progress_path=None):
    """gapseq 引擎：WSL2 桥 doall（30-60min）→ 模型拷回 → 目标介质验证闭环 → 模型卡。"""
    from gapseq_wsl import probe, run_gapseq
    from silentio import silent_read_sbml
    from validate import validate_model
    from gapfind import expand_medium, resolve_medium

    _log(progress_path, {"event": "gapseq_probe"})
    p = probe()
    if not p.get("capable"):
        _log(progress_path, {"event": "gapseq_unavailable", "level": p.get("level"),
                             "detail": (p.get("detail") or "")[:300]})
        raise RuntimeError(
            f"gapseq 引擎不可用（level={p.get('level')}）：{(p.get('detail') or '')[:300]}。"
            "请先配置 WSL2 + gapseq 环境，或改用 carveme 引擎。")

    input_fna = resolve_input(input_fna, progress_path, engine="gapseq")
    name = name or os.path.splitext(os.path.basename(input_fna))[0]
    out_dir = out_dir or MODEL_ROOT
    os.makedirs(out_dir, exist_ok=True)
    _log(progress_path, {"event": "gapseq_start", "note": "doall 30-60min，后台等待不误判超时"})
    st = time.time()
    model = None
    try:
        model, log_tail = run_gapseq(input_fna, out_dir, name=name,
                                     progress=(lambda ev: _log(progress_path, ev)))
    except Exception as e:
        _log(progress_path, {"event": "gapseq_fail", "err": str(e)[:300]})
        raise
    dt = round(time.time() - st, 1)
    _log(progress_path, {"event": "gapseq_done", "model": model, "s": dt})

    # 目标介质验证（gapseq 模型用 AB 自然名/用户 medium；无 medium 时 M9 兜底）
    target = None
    med_exp, preset_used = expand_medium(medium) if medium else ({}, None)
    if medium:
        m1 = silent_read_sbml(model)
        resolved, unresolved = resolve_medium(m1, med_exp)
        rep = validate_model(model, medium=resolved, reference_growth=None)
        g3 = rep["g3"]
        gapfixes = []
        if g3["status"] == "FAIL" and resolved:
            _log(progress_path, {"event": "gapseq_gapfill_start"})
            from gapfill import apply_fixes
            gf = apply_fixes(model, medium=resolved, max_add=20,
                             out=model[:-4] + "_gf.xml")
            gapfixes = gf.get("applied", [])
            if gapfixes:
                model = gf["out"]
                rep = validate_model(model, medium=resolved, reference_growth=None)
                g3 = rep["g3"]
            _log(progress_path, {"event": "gapseq_gapfill_done", "applied": len(gapfixes),
                                 "g3_after": g3["status"]})
        target = {"medium": medium, "preset": preset_used, "g3": g3["status"],
                  "growth": g3.get("growth_medium"), "unresolved": unresolved,
                  "gapfixes_applied": len(gapfixes)}
    else:
        _log(progress_path, {"event": "gapseq_no_medium"})
        rep = None

    card = {
        "name": name, "engine": "gapseq", "engine_version": p.get("gapseq_version"),
        "gapseq_probe": p.get("level"),
        "started": datetime.datetime.now().isoformat(timespec="seconds"),
        "elapsed_s": dt, "model": model,
        "validations": {k: rep[k]["status"] for k in ("g1", "g2", "g3")} if rep else None,
        "growth_g3": rep["g3"].get("growth_medium") if rep else None,
        "target": target,
        "mapping": {"genome_input": input_fna},
    }
    card_path = os.path.join(out_dir, name + ".card.json")
    with open(card_path, "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)
    _log(progress_path, {"event": "build_done", "model": model, "card": card_path, "s": dt})
    return {"model": model, "card": card_path,
            "validations": card["validations"], "growth_g3": card["growth_g3"],
            "target": target or {"note": "no target medium provided"},
            "engine": "gapseq", "elapsed_s": dt}


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
    ap.add_argument("--engine", default="carveme", choices=["carveme", "gapseq"])
    ap.add_argument("--medium-json")
    ap.add_argument("--out-dir")
    ap.add_argument("--progress")
    a = ap.parse_args()
    medium = json.loads(a.medium_json) if a.medium_json else None
    try:
        res = build(a.input, name=a.name, medium=medium, out_dir=a.out_dir,
                    progress_path=a.progress, engine=a.engine)
        print(json.dumps({"ok": True, "result": res}, ensure_ascii=False))
    except Exception as e:
        import traceback
        sys.stderr.write("Traceback (most recent call last):\n")
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"ok": True, "result": None,
                          "error_hint": f"build failed: {type(e).__name__}: {e}"},
                         ensure_ascii=False))