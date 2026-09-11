// python.js — dsh-bio-gem Python 子进程调用器（JSON stdin 协议）
// bridge 契约同 dsh-bio-genie：stdout 最后一行是 JSON；stderr 含
// "Traceback (most recent call last)" 头 = 代码级失败（恒 ok:true 时靠它判定）。
import { spawn, spawnSync } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'
import os from 'node:os'

const PYTHON_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'python')

/**
 * 候选解释器，按优先级（**通用化，不写死任何本机路径**）：
 *
 *   1. `GEM_PYTHON`            — 用户显式指定，最高优先级
 *   2. 宿主插件自举环境         — `$DSH_HOME/dsh-bio-genie/python-env`
 *      dsh-bio-genie 的环境引导把 cobra 装在**第一层依赖**里，gem 可直接复用，
 *      因此用户只需安装 genie 即可获得代谢建模能力，无需自备 conda/cobra。
 *      （2026-09-11 实测：用该环境的 python 跑通 gem 的 ledger/validate/report op）
 *   3. `CONDA_PREFIX`          — 当前激活的 conda 环境（通用信号，非硬编码路径）
 *   4. `python`                — PATH 兜底
 */
function candidates() {
  const list = []
  if (process.env.GEM_PYTHON) list.push(process.env.GEM_PYTHON)

  const dshHome = process.env.DSH_HOME ?? join(os.homedir(), '.dsh')
  const hosted = join(dshHome, 'dsh-bio-genie', 'python-env')
  list.push(process.platform === 'win32'
    ? join(hosted, 'Scripts', 'python.exe')
    : join(hosted, 'bin', 'python'))

  if (process.env.CONDA_PREFIX) {
    list.push(process.platform === 'win32'
      ? join(process.env.CONDA_PREFIX, 'python.exe')
      : join(process.env.CONDA_PREFIX, 'bin', 'python'))
  }

  list.push('python')
  return list
}

/** cobra 是本插件除 gem_build 外全部 op 的硬依赖：探测解释器能否 import cobra。 */
function hasCobra(exe) {
  try {
    const r = spawnSync(exe, ['-I', '-c', 'import cobra'], {
      timeout: 30_000, windowsHide: true, stdio: 'ignore',
    })
    return r.status === 0
  } catch {
    return false
  }
}

let cachedExe = null
const probeLog = []

/**
 * 选定解释器（进程内缓存）。
 *
 * 不做「路径存在即采用」的浅判断——落在一个没有 cobra 的解释器上时，
 * 工具只会抛 ModuleNotFoundError 而用户无从判断该装到哪里（README 曾专门
 * 警告此坑）。这里逐个探测 import cobra，让选择结果可解释。
 */
export function pythonExe() {
  if (cachedExe) return cachedExe
  for (const c of candidates()) {
    if (!c) continue
    if (c !== 'python' && !existsSync(c)) {
      probeLog.push({ exe: c, skipped: 'path-not-found' })
      continue
    }
    if (hasCobra(c)) {
      cachedExe = c
      probeLog.push({ exe: c, selected: true })
      return c
    }
    probeLog.push({ exe: c, skipped: 'no-cobra' })
  }
  cachedExe = 'python'
  probeLog.push({ exe: 'python', selected: true, note: 'fallback: 所有候选均无 cobra' })
  return cachedExe
}

/** 环境诊断（供工具/日志说明「为什么选了这个解释器」）。 */
export function pythonDiagnostics() {
  const exe = pythonExe()
  return {
    selected: exe,
    required_package: 'cobra',
    candidates_probed: probeLog,
    hint: exe === 'python'
      ? '未找到含 cobra 的解释器：安装 dsh-bio-genie 插件（其自举环境已含 cobra），或用 GEM_PYTHON 指向一个装了 cobra 的解释器'
      : undefined,
  }
}

/** op 名 → 对外工具名（多数同名；特例是 model_info 与 biomass 的两个 op）。 */
const OP_TOOL = {
  model_info: 'gem_report',
  biomass_inspect: 'gem_biomass',
  biomass_apply: 'gem_biomass',
}

function toolNameFor(op) {
  return OP_TOOL[op] ?? `gem_${op}`
}

/**
 * 与 dsh-bio-genie 的溯源契约对齐：工具输出挂 `_provenance` 背书字段。
 *
 * genie 侧的语义化工具（bio_*）都带该字段，其计算防火墙台账据此与回复里的
 * 数值声明对账；gem 的工具此前不带，两边口径不一致。统一在**唯一出口**
 * （callGem）盖章，避免逐个工具遗漏。不改动已有 _provenance（幂等）。
 */
export function stampProvenance(tool, value) {
  if (value && typeof value === 'object' && !Array.isArray(value) && value._provenance === undefined) {
    value._provenance = { tool, at: new Date().toISOString() }
  }
  return value
}

/** 调用 gem_ops.py（op 协议）：{op, args} -> result；异常/代码级失败抛 Error。 */
export function callGem(op, args, opts = {}) {
  return new Promise((resolve, reject) => {
    const py = pythonExe()
    const script = join(PYTHON_DIR, 'gem_ops.py')
    const cp = spawn(py, ['-I', script], { cwd: PYTHON_DIR, windowsHide: true })
    let out = ''
    let err = ''
    cp.stdout.on('data', (d) => { out += d })
    cp.stderr.on('data', (d) => { err += d })
    cp.on('error', (e) => reject(new Error(`python spawn failed (${py}): ${e.message}`)))
    const timer = opts.timeoutMs
      ? setTimeout(() => { cp.kill(); reject(new Error(`gem op ${op} timeout after ${opts.timeoutMs}ms`)) }, opts.timeoutMs)
      : null
    cp.on('close', (code) => {
      if (timer) clearTimeout(timer)
      const lines = out.trim().split(/\r?\n/).filter(Boolean)
      if (!lines.length) {
        return reject(new Error(`gem_ops.py produced no output (op=${op}, python=${py}); stderr: ${err.slice(-400)}`))
      }
      if (err.includes('Traceback (most recent call last)')) {
        return reject(new Error(`gem op ${op} code-level failure: ${err.slice(-400)}`))
      }
      let parsed
      try {
        parsed = JSON.parse(lines[lines.length - 1])
      } catch (e) {
        return reject(new Error(`gem op ${op} bad JSON: ${lines[lines.length - 1].slice(0, 300)}`))
      }
      if (parsed.ok === false) return reject(new Error(parsed.error || `gem op ${op} failed (ok:false)`))
      resolve(stampProvenance(toolNameFor(op), parsed.result))
    })
    cp.stdin.write(JSON.stringify({ op, args }))
    cp.stdin.end()
  })
}

export { PYTHON_DIR }
