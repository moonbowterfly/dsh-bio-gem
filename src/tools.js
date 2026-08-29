// dsh-bio-gem — 工具层（defineTool 注册，10 语义化工具，2026-08-29）
// 全部执行走 python/gem_ops.py（JSON stdin 协议）或 build.py CLI（gem_build 长任务）。
// op 与工具对照：9 op + build CLI；详见 docs/ARCHITECTURE.md §3。
import { defineTool } from '@deepseek-ai/dsh-tools'
import { join } from 'node:path'
import { dirname, isAbsolute } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { callGem, pythonExe, PYTHON_DIR } from './python.js'
import { startBuild, jobStatus } from './jobs.js'

const PY = pythonExe()

/** 校验输入存在（绝对路径或用户给定路径）。 */
function requirePath(v, label) {
  if (!v) throw new Error(`${label} required`)
  if (!isAbsolute(v)) throw new Error(`${label} 必须是绝对路径: ${v}`)
  return v
}

/** gem_ops 通用工具工厂（同步 op：report/validate/gapfind/gapfill）。 */
function gemTool(opts) {
  return defineTool({
    name: opts.name,
    description: opts.description,
    parameters: opts.parameters,
    timeoutMs: opts.timeoutMs ?? 300_000,
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      return callGem(opts.op, args, { timeoutMs: opts.timeoutMs ?? 300_000 })
    },
  })
}

/** gem_build：spawn build.py（可 60-120s），await 完成返回 {model, card, ...}。 */
function buildTool() {
  return defineTool({
    name: 'gem_build',
    description:
      '从细菌全基因组构建基因组尺度代谢模型（GEM）。' +
      'engine=carveme（默认，纯 Windows 快）：输入蛋白 FASTA（*.faa），CarveMe -g M9 gapfill → M9 介质验证 → 目标介质 L1/L2 补洞，' +
      '约 1-2 分钟（C58 实测 70s）。' +
      'engine=gapseq（质量档，需本机 WSL2 gapseq 环境）：输入核苷酸 FASTA（*.fna），WSL 桥 gapseq doall → 模型拷回 → 目标介质验证，' +
      '约 30-60 分钟（后台进度日志旁观，不要误判超时）。' +
      '输出标准 SBML（fbc v2）+ 模型卡（sidecar JSON：引擎版本/验证结果/补洞记录）。' +
      '触发词：构建代谢模型、基因组转模型、建GSMM、carveme 建模、gapseq 建模。',
    parameters: {
      input: {
        type: 'string', required: true,
        description: '输入绝对路径：engine=carveme 用蛋白 FASTA（*.faa）；engine=gapseq 用核苷酸 FASTA（*.fna）。',
      },
      name: { type: 'string', description: '模型命名（如 C58），缺省用文件名' },
      engine: { type: 'string', enum: ['carveme', 'gapseq'], description: '构建引擎：carveme（默认，纯 Windows 快出稿）或 gapseq（WSL2，质量档 30-60min）' },
      out_dir: { type: 'string', description: '输出目录，缺省 ~/.dsh/dsh-bio-gem/models' },
      target_medium: {
        type: 'object', additionalProperties: true,
        description: '目标培养基：可传 {"medium_name": "AB"/"M9"}（内置完整成分）或自然名成分字典 {"D-Glucose": -5, "NH3": -10, ...}。跨引擎自动解析。',
      },
    },
    timeoutMs: 3_600_000,
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      requirePath(args.input, 'input')
      const job = startBuild({
        input: args.input,
        name: args.name,
        engine: args.engine,
        medium: args.target_medium,
        outDir: args.out_dir,
      })
      // 轮询进度直到 done（build.py 内部已写 result.json）
      const deadline = Date.now() + 3_540_000
      while (Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 2000))
        const st = jobStatus(job.jobId)
        if (st.done) {
          if (st.error) throw new Error(`gem_build failed: ${st.error}`)
          if (!st.result || st.result.ok === false) {
            throw new Error(`gem_build failed: ${st.result?.error_hint ?? 'result missing'}`)
          }
          return st.result.result
        }
      }
      throw new Error('gem_build timeout (840s)')
    },
  })
}

export function registerTools(ctx) {
  const disposers = []
  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_report',
    description:
      '读取 SBML 代谢模型文件，输出模型摘要：基因/反应/代谢物/区室/复制子分布（多质粒/多染色体分离统计）、交换数。' +
      '用于快速检查模型文件是否可加载、规模、是否为多复制子。模型文件不存在或非有效 SBML 会明确报错。' +
      '触发词：看模型信息、模型摘要、有多少基因。',
    parameters: { model: { type: 'string', required: true, description: 'SBML 文件绝对路径' } },
    op: 'model_info',
  })))

  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_validate',
    description:
      '对 SBML 代谢模型执行五道验证关卡：G1 加载统计（+多复制子 ID 检查 + GPR 覆盖）、' +
      'G2 内部反应元素平衡（C/N/P/S 必须为 0，H/O 单独报告）、G3 生长真实性（声明培养基上有碳源>0、无碳=0、全关=0）、' +
      'G4 底物表型对照（需 phenotype_table 路径，条件执行）、G5 必需基因抽检（需 essential_test 基因列表，条件执行）。' +
      'medium 用自然名成分（如 D-Glucose/NH3/O2），跨引擎自动解析。G2 的已知生物质方程簿记偏差（如 bio1）报 WARN 不阻塞。' +
      '触发词：验证模型、质量检查、五道关卡。',
    parameters: {
      model: { type: 'string', required: true, description: 'SBML 文件绝对路径' },
      medium: { type: 'object', additionalProperties: true, description: '培养基：可传 {"medium_name": "AB"}（推荐，内置完整 AB 成分含金属）或自然名成分字典 {"D-Glucose": -5, "NH3": -10, ...}' },
      reference_growth: { type: 'number', description: '回归锚：已知野生型生长值（用于 G3 ratio 判定）' },
      phenotype_table: { type: 'string', description: '可选：底物表型 TSV（substrate<TAB>published 0/1）' },
      essential_test: { type: 'array', items: { type: 'string' }, description: '可选：抽检基因 ID 列表' },
      carbon_mode: { type: 'string', enum: ['supplement', 'sole'], description: 'G4 语义（默认 supplement=基准+底物）' },
    },
    op: 'validate',
    timeoutMs: 300_000,
  })))

  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_gapfind',
    description:
      '代谢模型缺口分级诊断：L1 缺胞外交换（培养基成分无对应 EX）、L2 缺转运（e0 代谢物无入胞出口）、' +
      'L3 内部路径（有交换+转运但 FBA 不生长）。输出分级缺口清单 + 每条是否规则可修（fixable）。' +
      '已知规律：多数「不能利用某碳源」缺口是 L1/L2 而非 L3。medium 支持自然名（跨引擎解析）。' +
      '触发词：诊断缺口、为什么不能用这个碳源、gapfind。',
    parameters: {
      model: { type: 'string', required: true, description: 'SBML 文件绝对路径' },
      medium: { type: 'object', additionalProperties: true, description: '培养基：可传 {"medium_name": "AB"}（推荐，内置完整 AB 成分含金属）或自然名成分字典 {"D-Glucose": -5, "NH3": -10, ...}' },
      substrates: { type: 'array', items: { type: 'string' }, description: '可选：待检底物名列表（如 Sucrose）' },
    },
    op: 'gapfind',
  })))

  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_gapfill',
    description:
      '按 gapfind 分级结果自动补洞：L1 补胞外交换（EX_ 反应 + 胞外代谢物）、L2 补转运（e0→c0，GPR 留空标注）。' +
      '每条新增反应打 provenance 标记（source=gem-gapfill + 原因）；max_add 封顶防过补；out 缺省生成 <model>_gf.xml（原文件不覆盖，' +
      '就地覆盖才备份 .bak）。L3 内部路径不自动补（需文献反应）。补洞后建议重跑 gem_validate 确认生长恢复。' +
      '触发词：补洞、修复缺口、gapfill、加交换。',
    parameters: {
      model: { type: 'string', required: true, description: 'SBML 文件绝对路径' },
      medium: { type: 'object', additionalProperties: true, description: '培养基：可传 {"medium_name": "AB"/"M9"}（内置完整成分）或自然名成分字典' },
      substrates: { type: 'array', items: { type: 'string' }, description: '可选：要支持的底物名列表' },
      max_add: { type: 'integer', description: '单次最多新增反应数（默认 20）' },
      out: { type: 'string', description: '输出模型路径（缺省 <model>_gf.xml）' },
    },
    op: 'gapfill',
  })))

  disposers.push(ctx.tools.register(buildTool()))

  // gem_gapseq：gapseq 引擎原子步骤（agent 编排 setup->launch->status*->fetch）
  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_gapseq',
    description:
      'gapseq 引擎（WSL2）原子步骤工具——长任务由 agent 按编排推进：\n' +
      'action=setup：能力探测（wsl/发行版/gapseq 版本/序列库注册）→ capability OK 才能继续。\n' +
      'action=launch：输入核苷酸 FASTA（*.fna 绝对路径）→ 后台启动 gapseq doall（30-60min），立即返回工作目录；不要等它完成。\n' +
      'action=status：查 doall 状态 → {state: running|done|failed, log_tail}；running 时 2-5 分钟后再查，可多轮。\n' +
      'action=fetch：done 后把产物（XML/faa.gz/tbl/日志）拷回 Windows 输出目录 → {model}。\n' +
      '编排模式：setup → launch →（循环 status 直到 done）→ fetch → 对 model 跑 gem_validate/缺口补洞。' +
      '失败时（failed/COPY_FAIL）根据 hint 自纠后重试 launch。触发词：gapseq 建模、doall、质量重建。',
    parameters: {
      action: { type: 'string', enum: ['setup', 'launch', 'status', 'fetch'], required: true, description: '原子步骤' },
      input: { type: 'string', description: 'launch 用：核苷酸 .fna 绝对路径' },
      name: { type: 'string', description: '模型名（产物 basename），缺省 model' },
      out_dir: { type: 'string', description: '产物输出目录，缺省 ~/.dsh/dsh-bio-gem/models' },
    },
    op: 'gapseq',
    timeoutMs: 180_000,
  })))

  // gem_phenotype：表型回填迭代（G4 驱动，路线 A3）
  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_phenotype',
    description:
      '表型回填迭代：对模型跑 G4 表型对照（phenotype_table：substrate<TAB>published 0/1，如 Biolog/文献表），' +
      '对「应生长但模型不长」的底物逐个 gapfind 分级 → L1/L2 交换/转运规则自动补洞（累积修复）→ L3 内部路径列候选清单 → 重跑 G4 对比匹配率。' +
      'medium 推荐 {"medium_name": "AB"}。输出 before/after 匹配率 + 修复清单 + L3 待处理项。' +
      '触发词：表型回填、提高表型匹配、Biolog 校准、为什么这个底物不长。',
    parameters: {
      model: { type: 'string', required: true, description: 'SBML 文件绝对路径（将基于副本修复，原文件不动）' },
      phenotype_table: { type: 'string', required: true, description: '表型表 TSV 绝对路径：substrate<TAB>published(0/1)' },
      medium: { type: 'object', additionalProperties: true, description: '培养基：{"medium_name": "AB"} 或自然名成分字典' },
      max_add: { type: 'integer', description: '每底物最多新增反应数（默认 20）' },
      out: { type: 'string', description: '修复后模型输出路径（缺省 <model>_pf.xml）' },
    },
    op: 'phenotype_fix',
    timeoutMs: 300_000,
  })))

  // gem_essentiality：G5 全量必需基因扫描（FVA 预筛 + 手工敲除）
  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_essentiality',
    description:
      '对代谢模型做全量必需基因扫描：FVA（全范围）预筛出可通量的反应关联基因（死基因免敲，通常省 30-50% 计算），' +
      '再对候选逐一手工敲除（with m: 循环）判定是否必需（敲除后生长<1e-6）。' +
      'medium 推荐 {"medium_name": "AB"}。输出必需基因列表 + 数量 + wt 生长 + 耗时统计。' +
      '结果可用于模型卡"必需基因"章节（对照文献/实验必需基因集即召回率）。' +
      '触发词：必需基因扫描、全量必要基因、essentiality scan、敲除全扫。',
    parameters: {
      model: { type: 'string', required: true, description: 'SBML 文件绝对路径' },
      medium: { type: 'object', additionalProperties: true, description: '培养基：{"medium_name": "AB"} 或自然名成分字典' },
      gene_subset: { type: 'array', items: { type: 'string' }, description: '可选：只扫描指定基因（限制范围）' },
    },
    op: 'essential_scan',
    timeoutMs: 600_000,
  })))

  // gem_annotate：基因组注释（纯 Windows；官方优先 + pyrodigal 兜底）
  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_annotate',
    description:
      '把细菌基因组核苷酸 FASTA（.fna）转成蛋白 FASTA（.faa），供 gem_build(CarveMe) 使用。' +
      '优先级：同目录 *_protein.faa（官方蛋白）→ 同目录 cds_from_genomic.fna 直译 → 同目录 *.gff 解析翻译 → pyrodigal 预测（兜底）。' +
      '返回 {faa, source, stats}。触发词：注释、基因组转蛋白、建蛋白序列、pyrodigal。',
    parameters: {
      fna: { type: 'string', required: true, description: '基因组核苷酸 fasta 绝对路径（.fna）' },
      out: { type: 'string', description: '输出蛋白 fasta 路径（缺省同目录 <base>.gem_annot.faa）' },
    },
    op: 'annotate',
    timeoutMs: 300_000,
  })))

  // gem_media_resolve：跨引擎介质解析 RPC（genie 消费侧统一入口；防介质语义漂移三次假象重演）
  disposers.push(ctx.tools.register(gemTool({
    name: 'gem_media_resolve',
    description:
      '把自然名培养基（如 {"medium_name": "AB"} 或 {"D-Glucose": -5, "NH3": -10, ...}）解析到指定模型的实际交换反应（EX ID 列表）。' +
      '这是全插件统一的介质解析入口——任何消费方（包括 dsh-bio-genie 的 FBA 等）应通过本工具/同款解析层获得交换 ID，' +
      '不要自行实现介质名匹配（已因三次"模型不生长假象"加固）。返回 {resolved_exchanges, unresolved, medium_preset}。' +
      '触发词：介质解析、培养基转交换、media resolve。',
    parameters: {
      model: { type: 'string', required: true, description: '目标模型 SBML 绝对路径' },
      medium: { type: 'object', additionalProperties: true, description: '自然名培养基（medium_name 或成分字典）' },
    },
    op: 'media_resolve',
    timeoutMs: 120_000,
  })))

  return () => disposers.forEach((d) => d())
}

export const gemToolNames = ['gem_report', 'gem_validate', 'gem_gapfind', 'gem_gapfill', 'gem_build']