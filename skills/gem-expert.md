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
| gapfind 判 L3（内部路径）后自动补洞（白名单/MILP）| `gem_l3_fix`（model + medium + substrates；allow_math=true 才放数学连接；补后自动跑 G6 防能量循环）|
| 看/改 biomass（FBA 目标函数）| `gem_biomass`（action=inspect 只读组分/对照参考；apply 显式 profile + 三联对照，原文件不动可回滚）|
| 按缺口自动补洞（缺交换/转运规则修复）| `gem_gapfill`（model + medium）→ 补完重跑 `gem_validate` |
| 从细菌基因组（蛋白 FASTA 或核苷酸 .fna）构建模型 | `gem_build`（input + 可选 target_medium；fna 自动注释）|
| 只有裸基因组 .fna，先要蛋白序列 | `gem_annotate`（fna → faa；官方优先 + pyrodigal 兜底）|
| 需要模型的全量必需基因清单 | `gem_essentiality`（FVA 预筛 + 手工敲除；medium 推荐 AB）|
| 跨条件通量对比（哪个反应真变了）| `gem_fluxscan`（区间制：FVA 区间+pFBA 点值，区间分离=硬结论，overlap=伪影禁止引用）|
| 量化模型不确定性（biomass/GAM 扰动下预测稳不稳）| `gem_sensitivity`（22 组合网格+稳定性三分类+单组分漂移；action=probe 秒级探测）|
| 查询/更新模型预测（必需性/表型预测追踪与实验兑现）| `gem_ledger`（list/query/update；预测默认 unverified，兑现后回填状态）|
| 两个模型规范对比（论文级基准表）| `gem_benchmark`（model_a+model_b：六关并列/生长/biomass 断供探针/必需性对比[退化侧只报结构]/表型/账本回填；export_md 落盘）|
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

0. **外部事实断言必须带证据**（2026-08-29 采纳 GLM 教训）：凡涉及"某物种/文献/工具是否存在、是否已发表"等**模型外事实断言**，agent 必须真实检索并附来源（如 PubMed/PMC 链接）；无法检索时降级表述为"未验证假设 [假设]"，不得由记忆下断言。此项与 EVIDENCE 分级同位：**事实断言也有证据分级**（检索+来源=high；仅记忆=unverified）。
1. **培养基一律用自然名成分**：`{"D-Glucose": -5, "NH3": -10, "O2": -12.5, ...}`。插件跨引擎自动解析（gapseq EX_cpdXXXXXX_e0 与 CarveMe/BiGG EX_glc__D_e 命名空间不同，硬编码 ID 会失配）。
2. **数字必须来自工具输出**：agent 报告生长值/基因数/复制子数时引用工具 result，不要凭模型知识猜测（防幻觉铁律）。**生长值单位是 mmol/gDW/h（FBA 通量），不是 h⁻¹/μ**——汇报时不要换算成比生长速率。
3. **CarveMe 模型的介质边界**：`gem_build` 默认在 M9 最小培养基验证（生长阳性）；**AB 等自定义培养基若 FAIL 且 gapfind 判定 L3（内部路径），规则补洞修不了**——此时如实报告「模型可用介质=M9，目标介质需要 L3 级文献补充」，绝不要假装自愈。
4. **缺口 90% 是 L1/L2**（缺交换/转运，非缺分解酶）——先诊断再补，别直接加反应。
5. **多复制子正常**：质粒/多染色体基因都在同一模型内（C58 实测 4 复制子：967/434/65/18 基因分布于 ChrⅠ/ChrⅡ/pTi/pAt）。
6. **通量区间制（阶段A-M4 硬规则）**：无区间不点数——任何条件间通量对比必须消费 `gem_fluxscan` 的区间分离判定；两条件区间分离才是解空间无关硬结论（a_higher/b_higher）；overlap 反应的任何点值 diff 必须标注"伪影，禁止引用"。单点 FBA 生长/通量值（gem_validate/gem_phenotype/gem_essentiality 等输出）只是该条件下的一个解，跨条件直接 diff 是求解器伪影。

## C58 回归锚（验证工具是否正常）

- 未知 → `gem_validate` 对 C58.xml（AB 自然名介质）→ G1 PASS / G3 PASS 生长 **0.519981** / 无碳源 0
- gapfind 对 C58.xml + 蔗糖 → L1（缺 EX_cpd00076_e0，fixable yes）
- gapfill 后蔗糖生长 **0.97077**（与手工 P1 补洞一致）
- gem_build 对 C58 protein.faa → M9 G3 PASS（growth>0），全流程 ~70s
- gem_fluxscan（区间制）对 C58.xml：条件 {AB} growth **0.519981**；C58_P1.xml + 蔗糖 supplement growth **0.97077**；输出每反应 fva_min/fva_max/pfba，条件对比只认区间分离判定（overlap=伪影禁止引用）
- gem_sensitivity 对 C58.xml（AB）：基准组合（biomass×1.0, GAM=orig 40.0）**精确复现 essential_scan 的 155**（155 vs 155 EXACT MATCH）；22 组合网格必需性恒 155
- gem_ledger：C58 essentiality 155 条 + phenotype 19 条入账，**幂等**——同参复跑 appended=0/skipped=174（账本行数不变）
- 对不上这些锚点 = 环境/模型被改动，先查再继续。

## 常见坑

- **gem_build 别误判超时**：1-2 分钟是正常的（C58 实测 70s），等待完成。
- medium 自然名拼写：D-Glucose / NH3 / O2 / Phosphate / Sulfate / Mn2+ / Fe3+ 等；别名表已内置（malic acid、gluconate 等）。
- G2 报告 WARN 且仅 1 个反应不平衡（如 bio1）→ 生物质方程簿记特性，不是模型坏了。
- 产物模型文件优先给绝对路径；工作区管理沿用 dsh 会话工作区。