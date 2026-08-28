// dsh-bio-gem 工具注册表（M1 语义化工具 · 2026-08-29 定稿）
// 结构同族 dsh-bio-genie/src/tools.js：name/description/inputSchema/dual_use
// 实现状态：model_info 已通（gem_report 底层）；validate/gapfind/gapfill/build 挂 TODO（骨架）

const gemTools = [
  {
    name: "gem_report",
    description:
      "读取一个 SBML 代谢模型文件，输出模型摘要：基因/反应/代谢物数、区室、复制子分布（多染色体/质粒统计）、交换反应数。用于快速检查模型文件是否可加载、规模多大、是否多复制子。如果模型文件不存在或不是有效 SBML，会明确报错。",
    inputSchema: {
      type: "object",
      properties: {
        model: { type: "string", description: "SBML 文件的绝对路径（.xml）" }
      },
      required: ["model"],
      additionalProperties: true
    },
    dual_use: { category: "read", desc: "读取模型元数据" },
    impl: "todo"
  },
  {
    name: "gem_validate",
    description:
      "对 SBML 代谢模型执行五道验证关卡：G1 加载统计（基因/反应/代谢物 + 多复制子 locus_tag 唯一性 + GPR 覆盖）、G2 内部反应元素平衡（排除 EX/DM/SK/boundary；C/N/P/S 必须为 0，H/charge 单独报告）、G3 生长真实性（声明培养基上有碳源>0、无碳源=0、全关=0）、G4 底物表型对照（需提供参照表，条件执行）、G5 必需基因抽检（需参照必需基因集，条件执行，默认抽 ≤30 基因）。输出每关 PASS/WARN/FAIL 与证据值。",
    inputSchema: {
      type: "object",
      properties: {
        model: { type: "string", description: "SBML 文件绝对路径" },
        medium: { type: "object", description: "培养基交换 ID -> lower_bound 字典（如 {\"EX_cpd00027_e0\": -5}），缺省用内置默认" },
        phenotype_table: { type: "string", description: "可选：底物表型参照表路径（G4 用）" },
        essential_test: { type: "array", items: { type: "string" }, description: "可选：抽检基因 ID 列表（G5 用，默认取内置参考集前 30）" }
      },
      required: ["model"],
      additionalProperties: true
    },
    dual_use: { category: "analysis", desc: "模型质量验证" },
    impl: "todo"
  },
  {
    name: "gem_gapfind",
    description:
      "代谢模型缺口分级诊断：L1 检查培养基中已声明成分是否缺少对应胞外交换反应（集合差）；L2 检查胞外/胞内区室连通性（缺转运反应）；L3 检查内部路径缺失（某底物无法进入中心代谢）。输出分级缺口清单，每条含证据（缺失的交换 ID 或断开的代谢物）。",
    inputSchema: {
      type: "object",
      properties: {
        model: { type: "string", description: "SBML 文件绝对路径" },
        medium: { type: "object", description: "培养基定义（交换 ID -> lower_bound）" },
        substrates: { type: "array", items: { type: "string" }, description: "可选：待检底物名列表；缺省用培养基成分" }
      },
      required: ["model"],
      additionalProperties: true
    },
    dual_use: { category: "analysis", desc: "缺口诊断" },
    impl: "todo"
  },
  {
    name: "gem_gapfill",
    description:
      "按 gapfind 的分级结果自动补洞：L1 补胞外交换（EX_ 反应）、L2 补转运反应（e0→c0）、L3 内部路径（需人工/文献反应，M1 为可选）。每条新增反应打 provenance 标记（来源、原因、是否借自模板），修改前自动备份原模型文件为 .bak。补洞后应重跑 gem_validate 确认生长恢复。",
    inputSchema: {
      type: "object",
      properties: {
        model: { type: "string", description: "SBML 文件绝对路径" },
        medium: { type: "object", description: "培养基定义（交换 ID -> lower_bound）" },
        fixes: { type: "array", items: { type: "object" }, description: "可选：仅应用指定修复；缺省应用全部自动可修复项" },
        max_add: { type: "integer", description: "单次最多新增反应数（防过补，默认 20）" }
      },
      required: ["model"],
      additionalProperties: true
    },
    dual_use: { category: "write", desc: "模型补洞（修改副本）" },
    impl: "todo"
  },
  {
    name: "gem_build",
    description:
      "从细菌全基因组构建代谢模型（长任务，后台执行+进度回报）：输入 NCBI accession（如 GCF_000092025.1）或本地基因组（genomic.fna + genomic.gff 或 protein.faa），经 CarveMe 引擎重建 → 自动补洞闭环 → 输出标准 SBML（fbc v2）+ 模型卡（sidecar JSON：引擎、版本、库版本、命令行、验证结果、补洞记录）。产出模型自动存入 ~/.dsh/dsh-bio-gem/models/ 并可由 gem_report/gem_validate 消费。",
    inputSchema: {
      type: "object",
      properties: {
        input: { type: "string", description: "NCBI accession（GCF_/GCA_）或本地基因组文件路径（.fna/.gff/.faa）" },
        name: { type: "string", description: "模型命名（如 C58），缺省用 accession 或文件名" },
        medium: { type: "object", description: "可选：目标培养基，构建后自动验证生长" },
        engine: { type: "string", enum: ["carveme", "gapseq", "auto"], description: "构建引擎；auto=CarveMe 快出稿，可选 gapseq 增强（M2）" }
      },
      required: ["input"],
      additionalProperties: true
    },
    dual_use: { category: "write", desc: "构建模型（长任务）" },
    impl: "todo"
  }
];

module.exports = { gemTools };