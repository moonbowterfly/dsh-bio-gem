// dsh-bio-gem — 工具层（defineTool 注册，5 语义化工具，M1）
// 全部执行走 python/gem_ops.py（JSON stdin 协议）；gem_build 走 build.py 长任务。
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
      '从细菌全基因组（蛋白 FASTA 或本地文件）构建基因组尺度代谢模型（GEM）。' +
      '流程：CarveMe 引擎（-g M9 gapfill）→ 精确 M9 介质验证（G1-G3）→ 若给 target_medium（自然名成分如 D-Glucose/NH3/O2）则解析并验证目标介质，' +
      '失败时自动执行 L1/L2 规则补洞（缺交换/转运），L3 内部路径缺口诚实报告。' +
      '输出标准 SBML（fbc v2）+ 模型卡（sidecar JSON：引擎版本/验证结果/补洞记录）。' +
      '耗时约 1-2 分钟（C58 实测 70s），请等待完成，不要误判为超时。' +
      '触发词：构建代谢模型、基因组转模型、建GSMM、carveme 建模。',
    parameters: {
      input: {
        type: 'string', required: true,
        description: '蛋白 FASTA 绝对路径（*.faa，NCBI protein.faa 或翻译产物）。accession 下载二期待支持。',
      },
      name: { type: 'string', description: '模型命名（如 C58），缺省用文件名' },
      out_dir: { type: 'string', description: '输出目录，缺省 ~/.dsh/dsh-bio-gem/models' },
      target_medium: {
        type: 'object', additionalProperties: true,
        description: '目标培养基（自然名 → lower_bound，如 {"D-Glucose": -5, "NH3": -10, "O2": -12.5}）。跨引擎自动解析（gapseq/BiGG 命名均可）。',
      },
    },
    timeoutMs: 900_000,
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => [{ type: 'text', text: JSON.stringify(value, null, 2) }],
    },
    async execute(args) {
      requirePath(args.input, 'input')
      const job = startBuild({
        input: args.input,
        name: args.name,
        medium: args.target_medium,
        outDir: args.out_dir,
      })
      // 轮询进度直到 done（build.py 内部已写 result.json）
      const deadline = Date.now() + 840_000
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
      medium: { type: 'object', additionalProperties: true, description: '培养基：自然名 → lower_bound' },
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
      medium: { type: 'object', additionalProperties: true, description: '培养基：自然名 → lower_bound' },
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
      medium: { type: 'object', additionalProperties: true, description: '培养基（自然名），驱动缺什么补什么' },
      substrates: { type: 'array', items: { type: 'string' }, description: '可选：要支持的底物名列表' },
      max_add: { type: 'integer', description: '单次最多新增反应数（默认 20）' },
      out: { type: 'string', description: '输出模型路径（缺省 <model>_gf.xml）' },
    },
    op: 'gapfill',
  })))

  disposers.push(ctx.tools.register(buildTool()))

  return () => disposers.forEach((d) => d())
}

export const gemToolNames = ['gem_report', 'gem_validate', 'gem_gapfind', 'gem_gapfill', 'gem_build']