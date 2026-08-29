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


def diamond_whitelist(faa, db_out, out_tsv, out_dir=None, evalue=1e-5, min_bitscore=60):
    """makedb + blastp -> 命中反应集。返回 {reactions: {rxn_id: [seq_hits]}, hits}"""
    r = subprocess_run([DIAMOND, "makedb", "--in", "-", "--db", db_out])
    pass


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