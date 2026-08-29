---
language: mixed
---

# dsh-bio-gem 主指引：基因组 → 可验证代谢模型（GEM）

任何「代谢模型 / GEM / 基因组建模型 / 模型验证 / 模型补洞」需求先加载本 skill。

## 工具分层（按任务选，别乱用）

| 任务 | 工具 |
|---|---|
| 已有 SBML 模型文件，想知道概要（基因/反应/复制子）| `gem_report`（model 参数=绝对路径）|
| 验证模型质量（五道关卡：加载/元素平衡/生长真实性/表型/必需性抽检）| `gem_validate`（model + medium）|
| 模型在目标培养基不长，想知道为什么 | `gem_gapfind`（model + medium + substrates）|
| 按缺口自动补洞（缺交换/转运规则修复）| `gem_gapfill`（model + medium）→ 补完重跑 `gem_validate` |
| 从细菌基因组（蛋白 FASTA 或核苷酸 .fna）构建模型 | `gem_build`（input + 可选 target_medium；fna 自动注释）|
| 只有裸基因组 .fna，先要蛋白序列 | `gem_annotate`（fna → faa；官方优先 + pyrodigal 兜底）|
| 需要模型的全量必需基因清单 | `gem_essentiality`（FVA 预筛 + 手工敲除；medium 推荐 AB）|
| 用 Biolog/文献表型表校准模型（提升匹配率）| `gem_phenotype`（phenotype_table + medium；自动补 L1/L2）|
| 把自然名培养基转成模型交换（消费方统一入口）| `gem_media_resolve`（model + medium → EX ID 列表）|
| gapseq 质量重建（需 WSL2）| `gem_gapseq`：setup → launch →（循环 status 直到 done）→ fetch，agent 编排 |
| 需要跑 FBA/必需性/生产包络线分析 | 用 dsh-bio-genie 的 `bio_fba` / `bio_gene_knockout` / `bio_production_envelope` 等（模型文件直接用）|

## 推荐工作流

```
基因组（蛋白 FASTA）
  └─ gem_build(engine=carveme) ──► SBML + 模型卡（内置 M9 介质验证 G1-G3；target_medium 可选）
        ├─ 通过 → gem_report 看概要 / gem_validate 全检 / 交给 bio_fba 等分析
        └─ 目标介质 FAIL → gem_gapfind 分级诊断
              ├─ L1 缺交换 / L2 缺转运 → gem_gapfill 自动补 → 重验
              └─ L3 内部路径 → 诚实报告（需文献反应，不自动补）

基因组（核苷酸 .fna，质量档）
  └─ gem_gapseq 原子编排：setup（探测）→ launch（后台 doall，30-60min 不等）→
     status 循环（2-5min/次，running 时不要干等可并行处理其他）→ fetch（产物拷回）
     → gem_validate / gem_gapfind / gem_gapfill 继续质量闭环

裸基因组（.fna）纯 Windows
  └─ gem_annotate（fna→faa）→ gem_build(engine=carveme) → 验证/补洞闭环
     （gem_build 内部自动调注释层；也可先 gem_annotate 单独出蛋白）

模型质量进阶
  └─ gem_essentiality（全量必需基因→模型卡章节）
  └─ gem_phenotype（表型回填迭代，按 Biolog/文献表校准）
  └─ gem_media_resolve（介质解析——任何下游要跑 FBA 前先解析介质）
```

## 硬规则（实测，违反会拿错结果）

1. **培养基一律用自然名成分**：`{"D-Glucose": -5, "NH3": -10, "O2": -12.5, ...}`。插件跨引擎自动解析（gapseq EX_cpdXXXXXX_e0 与 CarveMe/BiGG EX_glc__D_e 命名空间不同，硬编码 ID 会失配）。
2. **数字必须来自工具输出**：agent 报告生长值/基因数/复制子数时引用工具 result，不要凭模型知识猜测（防幻觉铁律）。**生长值单位是 mmol/gDW/h（FBA 通量），不是 h⁻¹/μ**——汇报时不要换算成比生长速率。
3. **CarveMe 模型的介质边界**：`gem_build` 默认在 M9 最小培养基验证（生长阳性）；**AB 等自定义培养基若 FAIL 且 gapfind 判定 L3（内部路径），规则补洞修不了**——此时如实报告「模型可用介质=M9，目标介质需要 L3 级文献补充」，绝不要假装自愈。
4. **缺口 90% 是 L1/L2**（缺交换/转运，非缺分解酶）——先诊断再补，别直接加反应。
5. **多复制子正常**：质粒/多染色体基因都在同一模型内（C58 实测 4 复制子：967/434/65/18 基因分布于 ChrⅠ/ChrⅡ/pTi/pAt）。

## C58 回归锚（验证工具是否正常）

- 未知 → `gem_validate` 对 C58.xml（AB 自然名介质）→ G1 PASS / G3 PASS 生长 **0.519981** / 无碳源 0
- gapfind 对 C58.xml + 蔗糖 → L1（缺 EX_cpd00076_e0，fixable yes）
- gapfill 后蔗糖生长 **0.97077**（与手工 P1 补洞一致）
- gem_build 对 C58 protein.faa → M9 G3 PASS（growth>0），全流程 ~70s
- 对不上这些锚点 = 环境/模型被改动，先查再继续。

## 常见坑

- **gem_build 别误判超时**：1-2 分钟是正常的（C58 实测 70s），等待完成。
- medium 自然名拼写：D-Glucose / NH3 / O2 / Phosphate / Sulfate / Mn2+ / Fe3+ 等；别名表已内置（malic acid、gluconate 等）。
- G2 报告 WARN 且仅 1 个反应不平衡（如 bio1）→ 生物质方程簿记特性，不是模型坏了。
- 产物模型文件优先给绝对路径；工作区管理沿用 dsh 会话工作区。