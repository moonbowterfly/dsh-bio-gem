# annotate.py — 路线 P0 注释步骤（纯 Windows）
# 策略（GLM 二轮 Q1 采纳 + 验证协议）：官方注释优先 + pyrodigal 兜底
#   1) 同目录 *_protein.faa 存在（NCBI dataset 常见）→ 直接用
#   2) 同目录 *.gff（含 CDS）→ 解析坐标从 fna 提取 + 翻译（transl_table 11）
#   3) 仅 .fna → pyrodigal（多序列模式；总长 <100kb 时 meta 模式）
# 验证：predict 蛋白集合 vs 官方蛋白的完全一致率应 85-92%（C58 验证见 logs）。
import os
import sys
import glob
import pyrodigal

# 标准遗传密码子表（transl_table 11，杆菌默认；零依赖实现）
CODON_TABLE = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L", "TCT": "S", "TCC": "S", "TCA": "S",
    "TCG": "S", "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*", "TGT": "C", "TGC": "C",
    "TGA": "*", "TGG": "W", "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L", "CCT": "P",
    "CCC": "P", "CCA": "P", "CCG": "P", "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R", "ATT": "I", "ATC": "I", "ATA": "I",
    "ATG": "M", "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T", "AAT": "N", "AAC": "N",
    "AAA": "K", "AAG": "K", "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V", "GCT": "A", "GCC": "A", "GCA": "A",
    "GCG": "A", "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E", "GGT": "G", "GGC": "G",
    "GGA": "G", "GGG": "G",
}


def translate_nt(dna):
    """DNA -> 蛋白（标准表 11；到 stop 截止）。"""
    s = dna.upper().replace("U", "T")
    out = []
    for i in range(0, len(s) - 2, 3):
        aa = CODON_TABLE.get(s[i:i + 3], "X")
        if aa == "*":
            break
        out.append(aa)
    return "".join(out)


def read_fasta(path):
    recs = []
    cur, cur_id = [], None
    with open(path, encoding="utf-8", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if cur_id is not None:
                    recs.append((cur_id, "".join(cur)))
                cur_id = line[1:].split()[0]
                cur = []
            else:
                cur.append(line)
    if cur_id is not None:
        recs.append((cur_id, "".join(cur)))
    return recs


def _translate_cds_file(cds_fna, out_faa):
    """官方 CDS 核酸 -> 蛋白（transl_table 11 直译）。"""
    n = 0
    with open(out_faa, "w", encoding="utf-8") as fo:
        for rid, seq in read_fasta(cds_fna):
            prot = translate_nt(seq)
            if prot:
                fo.write(f">{rid}\n{prot}\n")
                n += 1
    return out_faa, n


def nucleotide_to_protein(fna_path, out_faa=None, prefer_existing=True):
    """.fna -> .faa（官方优先 + pyrodigal 兜底）。返回 (faa_path, source, stats)。"""
    d = os.path.dirname(fna_path) or "."
    base = os.path.splitext(os.path.basename(fna_path))[0]
    if out_faa is None:
        out_faa = os.path.join(d, base + ".gem_annot.faa")

    # 1) 优先已有的官方蛋白
    if prefer_existing:
        cands = [f for f in glob.glob(os.path.join(d, "*_protein.faa"))]
        cands += [f for f in glob.glob(os.path.join(d, base + ".faa"))]
        if cands:
            best = cands[0]
            n = sum(1 for l in open(best, encoding="utf-8", errors="ignore") if l.startswith(">"))
            if n > 50:
                return best, "official_protein", {"seqs": n, "note": f"现有蛋白 {os.path.basename(best)}"}

    # 2) 官方 CDS（cds_from_genomic.fna）直译蛋白
    cds = os.path.join(d, "cds_from_genomic.fna")
    if os.path.exists(cds):
        faa, n = _translate_cds_file(cds, out_faa)
        if n > 50:
            return faa, "cds_translate", {"seqs": n}

    # 3) GFF 官方注释解析翻译
    gffs = glob.glob(os.path.join(d, "*.gff")) + glob.glob(os.path.join(d, "*.gff3"))
    if gffs and os.path.exists(fna_path):
        faa, n = _translate_from_gff(fna_path, gffs[0], out_faa)
        if faa and n > 50:
            return faa, "gff_translate", {"seqs": n}

    # 4) pyrodigal 兜底
    n = _pyrodigal_predict(fna_path, out_faa)
    return out_faa, "pyrodigal", {"seqs": n}


def _pyrodigal_predict(fna_path, out_faa):
    """pyrodigal 多序列预测（总长 <100kb 强制 meta 模式）。"""
    model = pyrodigal.GeneFinder(meta=True)
    seqs = []
    with open(fna_path, encoding="utf-8", errors="ignore") as f:
        cur, cur_id = [], None
        for line in f:
            line = line.strip()
            if not line:
                continue
            if line.startswith(">"):
                if cur_id is not None:
                    seqs.append((cur_id, "".join(cur).upper()))
                cur_id = line[1:].split()[0]
                cur = []
            else:
                cur.append(line)
        if cur_id is not None:
            seqs.append((cur_id, "".join(cur).upper()))
    total = sum(len(s) for _, s in seqs)
    n_out = 0
    with open(out_faa, "w", encoding="utf-8") as fo:
        for rec_id, seq in seqs:
            genes = model.find_genes(seq)
            for g in genes:
                fo.write(f">{rec_id}_{g.begin}_{g.end}_{'+' if g.strand == 1 else '-'}\n")
                fo.write(g.translate() + "\n")
                n_out += 1
    return n_out


def _translate_from_gff(fna_path, gff_path, out_faa):
    """从 GFF 提取 CDS 坐标并在 fna 上翻译（零依赖：内嵌密码子表）。"""
    _RC = str.maketrans("ACGTNacgtn", "TGCANtgcan")
    genome = {rid: seq for rid, seq in read_fasta(fna_path)}
    n = 0
    with open(out_faa, "w", encoding="utf-8") as fo:
        for line in open(gff_path, encoding="utf-8", errors="ignore"):
            if line.startswith("#"):
                continue
            p = line.rstrip("\n").split("\t")
            if len(p) < 9 or p[2] != "CDS":
                continue
            seqid, start, end, strand = p[0], int(p[3]), int(p[4]), p[6]
            seq = genome.get(seqid)
            if not seq:
                continue
            cds = seq[start - 1:end]
            if strand == "-":
                cds = cds.translate(_RC)[::-1]
            prot = translate_nt(cds)
            fo.write(f">{seqid}_{start}_{end}_{strand}\n{prot}\n")
            n += 1
    return out_faa, n


if __name__ == "__main__":
    import json
    args = json.loads(open(sys.argv[1], encoding="utf-8").read()) if len(sys.argv) > 1 else {}
    faa, src, stats = nucleotide_to_protein(args["fna"], args.get("out"))
    print(json.dumps({"faa": faa, "source": src, "stats": stats}, ensure_ascii=False, indent=2))