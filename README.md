# dsh-bio-gem

**基因组尺度代谢模型（GEM）构建插件**：输入细菌全基因组（支持多质粒/多染色体），自动构建 → 验证 → 补洞 → 出报告（标准 SBML + 模型卡），产出后可由 dsh-bio-genie 现有消费工具（FBA / 基因必需性 / 生产包络线 / 模型管理面板）直接加载使用。

Genome-scale metabolic model builder for dsh: genome in, validated SBML out.

## 工具（10）

| 工具 | 作用 | 状态 |
|---|---|---|
| `gem_report` | 模型摘要（基因/反应/区室/复制子分布）| ✅ |
| `gem_validate` | 六道关卡 G1-G6（加载/元素平衡/生长真实性/表型(条件)/必需性抽检(条件)/ATP 泄漏）| ✅ |
| `gem_gapfind` | 缺口分级诊断（L1 缺交换 / L2 缺转运 / L3 内部路径）| ✅ |
| `gem_gapfill` | 规则级补洞（L1/L2 自动，provenance 打标，防过补四闸门）| ✅ |
| `gem_l3_fix` | L3 内部路径补洞（L3a 连通性/L3b 白名单+BiGG 反应式；证据分级 + 防过补第五闸门）| ✅ C58 阿拉伯糖 0→0.851 |
| `gem_biomass` | biomass 精修（inspect 组分/对照参考；apply 覆盖表+三联对照+回滚）| ✅ 复位 delta 0.0 |
| `gem_phenotype` | 表型回填迭代（G4 sole 检测 → L1/L2 修复 → L3 报告 → 匹配率对比）| ✅ |
| `gem_essentiality` | 全量必需基因扫描（FVA 预筛 + 手工敲除）| ✅ C58: 必需 155 |
| `gem_annotate` | 基因组→蛋白（官方优先 + pyrodigal 兜底，纯 Windows）| ✅ pyrodigal 5330 |
| `gem_build` | CarveMe/gapseq 双引擎构建（fna/faa；后台 job + 进度；M9 或目标介质验证闭环）| ✅ carveme 70s / fna 全链 63.5s / gapseq 实测中 |
| `gem_gapseq` | gapseq 原子四步（WSL 可选：setup/launch/status/fetch）| ✅ 本机全通 |
| `gem_media_resolve` | 跨引擎介质解析 RPC（自然名→EX ID；消费侧统一入口）| ✅ AB→20 EX |

架构/决策见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)、[docs/DECISIONS-2026-08-29.md](docs/DECISIONS-2026-08-29.md)。

## 开发速查

```bash
# Python 层直测（需 Windows Python + cobra）
echo '{"op":"model_info","args":{"model":"path.xml"}}' | python -I python/gem_ops.py
python -u python/build.py --input proteins.faa --name X --progress p.jsonl --medium-json '{...}'

# 独立脚本模式（gapfind/gapfill）
python -I python/gapfind.py payload.json
python -I python/gapfill.py payload.json
```

依赖：`cobra`（分析，miniconda 已有）；`carveme`（构建，独立 venv `~/.dsh/dsh-bio-gem/venv-carveme`）。

## 验收（C58 回归锚）

- `gem_validate` 对 C58.xml：G1 PASS / G2 WARN(仅 bio1 已知边界) / G3 PASS(0.519981) / G4 PASS(17/19) / G5 PASS
- gapfind→gapfill 闭环：原版 C58.xml + 蔗糖培养基 → 自动补交换/转运 → 蔗糖生长 0.97077（与手工 P1 补洞一致）
- gem_build 端到端（C58 protein.faa）：carve M9 gapfill 54s → 精确 M9 介质 G3 PASS 0.782 → AB 目标介质 resolve 20/20，G3 FAIL（L3 内部路径，已记录为 CarveMe 已知边界）

## 许可

MIT（工程层）；引擎 CarveMe/gapseq 以独立子进程调用分发，版本记录入模型卡，自带许可边界。