# dsh-bio-gem

**基因组尺度代谢模型（GEM）构建插件**：输入细菌全基因组（支持多质粒/多染色体），自动构建 → 验证 → 补洞 → 出报告（标准 SBML + 模型卡），产出后可由 dsh-bio-genie 现有消费工具（FBA / 基因必需性 / 生产包络线 / 模型管理面板）直接加载使用。

Genome-scale metabolic model builder for dsh: genome in, validated SBML out.

## 工具（20）

| 工具 | 作用 | 状态 |
|---|---|---|
| `gem_report` | 模型摘要（基因/反应/区室/复制子分布）+ 预测账本基率摘要（ledger_summary）| ✅ |
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
| `gem_fluxscan` | 通量区间制（FVA 区间+pFBA 点值；条件对比区间分离判定，overlap=伪影禁止引用）| ✅ C58 AB 0.519981 / 蔗糖 supplement 0.97077 |
| `gem_sensitivity` | 结构性灵敏度（GAM×biomass 22 组合全量+稳定性三分类+单组分漂移+模型卡鲁棒性 v3）| ✅ C58 基准复现 155 |
| `gem_ledger` | 预测账本（essentiality/phenotype 自动登记；幂等；list/query/update；基率追踪）| ✅ C58 155+19 条幂等复跑 |
| `gem_benchmark` | 通用基准对比（两模型六关并列/生长/biomass 断供探针/必需性对比[退化护栏]/表型/账本回填；介质层两级策略；支持 bigg:&lt;id&gt; 下载）| ✅ B1 自检 C58 vs C58_P1；B2 C58 vs iNX1344_v4；B3 bigg:iML1515 |
| `gem_secretion` | 可分泌代谢物谱（production envelope 扫描；边界声明=纯拓扑 LP；wt<=EPS 退化护栏）| ✅ C58: 182 候选 85 可分泌 |
| `gem_double_knockout` | 双敲 v1 合成致死（GPR 穷尽先验+全扫预算；假设生成声明内置）| ✅ C58: Atu3364↔Atu4682 对应命中 |
| `gem_enrichment` | 必需基因通路富集（超几何+BH FDR；通路源=SBML groups；无注释诚实兜底）| ✅ C58: 388 通路 55 显著 |
| `gem_targets` | 靶点清单规范导出（账本三类 -> 锁定 schema CSV/JSON；计数闭合）| ✅ C58: 258 行三类闭合 |

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