# dsh-bio-gem

**基因组尺度代谢模型（GEM）构建插件**：输入细菌全基因组（支持多质粒/多染色体），自动构建 → 验证 → 补洞 → 出报告（标准 SBML + 模型卡），产出后可由 dsh-bio-genie 现有消费工具（FBA / 基因必需性 / 生产包络线 / 模型管理面板）直接加载使用。

Genome-scale metabolic model builder for dsh: genome in, validated SBML out.

## 工具（M1）

| 工具 | 作用 | 状态 |
|---|---|---|
| `gem_report` | 模型摘要（基因/反应/区室/复制子分布）| ✅ |
| `gem_validate` | 五道验证关卡 G1-G5（加载/元素平衡/生长真实性/表型(条件)/必需性抽检(条件)）| ✅ |
| `gem_gapfind` | 缺口分级诊断（L1 缺交换 / L2 缺转运 / L3 内部路径）| ✅ |
| `gem_gapfill` | 规则级补洞（L1/L2 自动，provenance 打标，防过补四闸门）| ✅ |
| `gem_build` | CarveMe/gapseq 双引擎基因组→SBML 构建（后台 job + 进度；M9 或目标介质验证闭环）| ✅ carveme 70s / gapseq 实测中 |

## 架构

见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)（M1 定稿：决策记录、关卡规格、补洞闸门、模型卡 schema、验收线）。

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