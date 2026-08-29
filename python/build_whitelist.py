# build_whitelist.py — B' 白名单 B0/B1：gapseq rxn 库 -> 反应白名单（本地基准，license 守则：不分发）
# B0: 聚合 rxn/*.fasta（非空）→ rxn_proteins.fa + mapping（序列header -> 反应ID）
# B1: diamond makedb + blastp 目标物种 faa → 命中反应集（EVIDENCE_sequence 候选池）
import os
import sys
import time

SEQDB = os.environ.get("GEM_GAPSEQ_DB", r"F:\Datasets\gapseq\db\Bacteria")
DIAMOND = r"C:\Users\shuai\.dsh\dsh-bio-gem\venv-carveme\Scripts\diamond.exe"


def build_rxn_fasta(out_fa, out_map=None, min_size=100):
    """聚合 rxn/ 非空文件（文件名=反应 ID）→ 序列 fasta + mapping。返回 (seqs, files_used)。"""
    rxn_dir = os.path.join(SEQDB, "rxn")
    n_seq = 0
    n_file = 0
    with open(out_fa, "w", encoding="utf-8", errors="ignore") as fo:
        if out_map:
            fm = open(out_map, "w", encoding="utf-8")
            fm.write("reaction_id\tseq_id\n")
        for fn in os.listdir(rxn_dir):
            if not fn.endswith(".fasta"):
                continue
            p = os.path.join(rxn_dir, fn)
            if os.path.getsize(p) < min_size:
                continue
            rxn_id = fn[:-6]  # 文件名去 .fasta -> 反应 ID
            with open(p, encoding="utf-8", errors="ignore") as f:
                cur = None
                buf = []
                for line in f:
                    line = line.rstrip("\n")
                    if line.startswith(">"):
                        if cur is not None:
                            fo.write(f">{cur}\n{''.join(buf)}\n")
                            if out_map:
                                fm.write(f"{rxn_id}\t{cur}\n")
                            n_seq += 1
                        cur = f"{rxn_id}|{line[1:].split()[0]}"
                        buf = []
                    elif line.strip():
                        buf.append(line.strip())
                if cur is not None and buf:
                    fo.write(f">{cur}\n{''.join(buf)}\n")
                    if out_map:
                        fm.write(f"{rxn_id}\t{cur}\n")
                    n_seq += 1
            n_file += 1
        if out_map:
            fm.close()
    return n_seq, n_file


def diamond_whitelist(faa, out_dir, db_path=None, rxn_fa=None, out_tsv=None,
                      evalue=1e-5, min_bitscore=60, max_target_seqs=5):
    """B1: diamond blastp 目标物种 faa vs rxn_all 数据库 -> 命中反应集。
    db_path 缺省用 out_dir/rxn_all.dmnd（无则由 rxn_fa 建，rxn_fa 再缺则用 GEM_GAPSEQ_DB 的 B0 产物）。
    返回 {"rxn_hits": {rxn_id: [seq_hit_desc...]}, "n_hits": N, "hits_tsv": path, "db": path}。
    License 守则: rxn 库/命中集仅本地使用，不进 git/发布包（调用方负责落在 ~/.dsh 下）。"""
    import subprocess
    os.makedirs(out_dir, exist_ok=True)
    if not os.path.exists(DIAMOND):
        raise FileNotFoundError(f"diamond not found: {DIAMOND}")
    if db_path is None:
        db_path = os.path.join(out_dir, "rxn_all.dmnd")
    if rxn_fa is None:
        rxn_fa = os.path.join(out_dir, "rxn_all.fa")
    if not os.path.exists(db_path):
        if not os.path.exists(rxn_fa):
            # B0 现场聚合（SEQDB rxn/ 目录 -> rxn_all.fa）
            n_seq, n_file = build_rxn_fasta(rxn_fa)
            if n_seq == 0:
                raise FileNotFoundError(f"no rxn fasta built from {SEQDB}/rxn (GEM_GAPSEQ_DB?)")
        rc, so, se, secs = subprocess_run([DIAMOND, "makedb", "--in", rxn_fa, "--db", db_path])
        if rc != 0:
            raise RuntimeError(f"diamond makedb failed rc={rc}: {se}")
    if out_tsv is None:
        out_tsv = os.path.join(out_dir, os.path.splitext(os.path.basename(faa))[0] + "_hits.tsv")
    rc, so, se, secs = subprocess_run(
        [DIAMOND, "blastp", "-d", db_path, "-q", faa, "-o", out_tsv,
         "--evalue", str(evalue), "--max-target-seqs", str(max_target_seqs),
         "--outfmt", "6", "qseqid", "sseqid", "pident", "evalue", "bitscore"],
        timeout=3600)
    if rc != 0:
        raise RuntimeError(f"diamond blastp failed rc={rc}: {se}")
    rxn_hits = {}
    with open(out_tsv, encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = line.rstrip("\r\n").split("\t")
            if len(p) < 5:
                continue
            qseqid, sseqid = p[0], p[1]
            try:
                if float(p[4]) < min_bitscore:
                    continue
            except ValueError:
                continue
            rxn_id = sseqid.split("|")[0].strip()  # header 约定: RXNID|uniprot...
            if not rxn_id:
                continue
            rxn_hits.setdefault(rxn_id, [])
            if qseqid not in rxn_hits[rxn_id]:
                rxn_hits[rxn_id].append(qseqid)
    return {"rxn_hits": rxn_hits, "n_hits": len(rxn_hits),
            "hits_tsv": out_tsv, "db": db_path, "evalue": evalue,
            "min_bitscore": min_bitscore}


def subprocess_run(cmd, timeout=3600):
    import subprocess
    st = time.time()
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, (r.stdout or b"").decode("utf-8", "ignore")[-800:], (r.stderr or b"").decode("utf-8", "ignore")[-800:], round(time.time() - st, 1)


if __name__ == "__main__":
    import json
    fa = sys.argv[1] if len(sys.argv) > 1 else r"D:\Program\hermes\temp\gem_whitelist\rxn_all.fa"
    out_dir = os.path.dirname(fa) or "."
    os.makedirs(out_dir, exist_ok=True)
    map_p = os.path.join(out_dir, "rxn_map.tsv")
    t0 = time.time()
    n_seq, n_file = build_rxn_fasta(fa, map_p)
    print(json.dumps({"seqs": n_seq, "files_used": n_file, "out_fa": fa,
                      "map": map_p, "elapsed_s": round(time.time() - t0, 1)},
                     ensure_ascii=False, indent=2))
    print(f"文件大小: {os.path.getsize(fa)/1e6:.1f} MB")