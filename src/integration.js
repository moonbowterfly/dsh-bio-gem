import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import os from 'node:os'
import { join } from 'node:path'
import { pythonCandidates } from './python.js'
import { TOOLS_MANIFEST, buildCapabilitiesReport } from './capabilities.js'

/**
 * dsh-bio-gem — hosted-domain integration protocol v1 (read-only batch).
 *
 * This module deliberately has no import-time probes: loading the plugin and
 * serving /health must never spawn Python or modify the data directory.
 */

export const INTEGRATION_PREFIX = '/api/dsh-bio-gem/integration'
export const PROTOCOL_MAJOR = 1
export const PROTOCOL_MINORS = [0]
export const RUNTIME_PROBE_CACHE_MS = 60_000
/** WSL/gapseq 探测更重（wsl.exe 冷启动可达十秒级），缓存更久且**非阻塞**。 */
export const GAPSEQ_PROBE_CACHE_MS = 300_000
export const INTEGRATION_FEATURES = [
  'status',
  'capabilities',
  'model-store',
  'ledger',
  'exports',
  'carveme-runtime',
  'gapseq-probe',
]

const PLUGIN_ID = 'dsh-bio-gem'
// 版本号从 package.json 实时读，避免 bump 版本时漏改此处导致 /health 自报旧版本
// （第二真值源曾导致 galatea 报 0.1.2 而磁盘已是 0.1.3；重启无效、非缓存）。
const PLUGIN_VERSION = JSON.parse(
  readFileSync(new URL('../package.json', import.meta.url), 'utf8'),
).version

function defaultDataRoot() {
  const dshHome = process.env.DSH_HOME ?? join(os.homedir(), '.dsh')
  return join(dshHome, 'dsh-bio-gem')
}

function listFiles(dir, predicate = () => true) {
  try {
    return readdirSync(dir, { withFileTypes: true })
      .filter((entry) => entry.isFile() && predicate(entry.name))
      .map((entry) => {
        const fullPath = join(dir, entry.name)
        const stat = statSync(fullPath)
        return {
          name: entry.name,
          sizeBytes: stat.size,
          modifiedAt: stat.mtime.toISOString(),
        }
      })
      .sort((a, b) => b.modifiedAt.localeCompare(a.modifiedAt))
  } catch {
    return []
  }
}

function boundedSummary(dir, predicate) {
  const all = listFiles(dir, predicate)
  return { count: all.length, items: all.slice(0, 50) }
}

function ledgerSummary(root) {
  const dir = join(root, 'ledger')
  const files = listFiles(dir, (name) => name.endsWith('.jsonl'))
  const ledgers = files.map((file) => {
    let entries = 0
    try {
      entries = readFileSync(join(dir, file.name), 'utf8').split(/\r?\n/).filter((line) => line.trim()).length
    } catch { /* unreadable ledgers are represented as zero entries */ }
    return {
      model: file.name.replace(/\.jsonl$/, ''),
      entries,
      modifiedAt: file.modifiedAt,
    }
  })
  return {
    count: ledgers.length,
    totalEntries: ledgers.reduce((total, ledger) => total + ledger.entries, 0),
    dir,
    ledgers: ledgers.slice(0, 50),
  }
}

function carvemeStatus(root) {
  const scripts = join(root, 'venv-carveme', 'Scripts')
  const carve = existsSync(join(scripts, 'carve.exe'))
  const diamond = existsSync(join(scripts, 'diamond.exe'))
  return {
    available: carve && diamond,
    hint: carve && diamond
      ? 'carve.exe 与 diamond.exe 均可读。'
      : 'gem_build(carveme) 需要私有 venv 中的 carve.exe 与 diamond.exe。',
  }
}

function statusCheck(id, status, detail) {
  return { id, status, detail }
}

function remediationsFor(checks) {
  const codes = new Set(checks.filter((check) => check.status !== 'ok').map((check) => check.id))
  const remediations = []
  if (codes.has('python.cobra')) {
    remediations.push({
      code: 'genie.bootstrap-python', owner: 'genie',
      detail: '准备 BioGenie 共享 Python/cobra 环境后重新探测。',
    })
  }
  if (codes.has('runtime.carveme')) {
    remediations.push({
      code: 'gem.install-carveme-runtime', owner: 'gem',
      detail: '准备 gem 私有 CarveMe 运行时（含 diamond）后重新探测。',
    })
  }
  if (codes.has('runtime.gapseq')) {
    remediations.push({
      code: 'genie.install-wsl-gapseq', owner: 'genie',
      detail: '准备 WSL/gapseq 共享前置能力后重新探测。',
    })
  }
  return remediations
}

/** Read only the installed cobra version; output and failures stay local. */
function probeCobraVersion(executable) {
  return new Promise((resolve) => {
    let settled = false
    let timer = null
    const finish = (value) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(value)
    }
    try {
      const child = spawn(executable, ['-I', '-c', 'import cobra; print(cobra.__version__)'], {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'ignore'],
      })
      let stdout = ''
      child.stdout?.on('data', (chunk) => { stdout += chunk.toString('utf8') })
      child.on('error', () => finish(null))
      child.on('close', (code) => finish(code === 0 ? stdout.trim() || 'available' : null))
      timer = setTimeout(() => {
        try { child.kill() } catch { /* already exited */ }
        finish(null)
      }, 20_000)
    } catch {
      finish(null)
    }
  })
}

async function probePythonEnvironment(candidateProvider, fileExists, probeCobra) {
  const candidates = candidateProvider().map((candidate) => ({
    ...candidate,
    exists: candidate.path === 'python' ? true : fileExists(candidate.path),
  }))
  let selected = null
  for (const candidate of candidates) {
    if (!candidate.exists) continue
    let cobraVersion = null
    try {
      cobraVersion = await probeCobra(candidate.path)
    } catch { /* individual candidate failures are an expected degraded state */ }
    if (cobraVersion) {
      selected = {
        path: candidate.path,
        source: candidate.source,
        cobraVersion,
      }
      break
    }
  }
  return {
    selected,
    candidates,
    note: selected ? undefined : '所有候选均未通过 import cobra 的只读探测。',
  }
}

/** Execute a fixed, read-only WSL command and retain only a tiny version response. */
function runGapseqVersionCommand(command, args) {
  return new Promise((resolve) => {
    let settled = false
    let stdout = ''
    let timer = null
    const finish = (result) => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(result)
    }
    try {
      // stdin 必须保持 pipe（并立即 end）：实测 wsl.exe 在 stdin=ignore 下会极慢
      // （同一条 `echo ok`：ignore 14.9s vs pipe 0.26s，约 50 倍），
      // 这是之前 gapseq 探测频繁超时的真正根因。
      const child = spawn(command, args, {
        windowsHide: true,
        stdio: ['pipe', 'pipe', 'ignore'],
      })
      child.stdin?.end()
      child.stdout?.on('data', (chunk) => {
        if (stdout.length < 512) stdout += chunk.toString('utf8').slice(0, 512 - stdout.length)
      })
      child.on('error', () => finish({ ok: false }))
      child.on('close', (code) => finish({ ok: code === 0, stdout }))
      timer = setTimeout(() => {
        try { child.kill() } catch { /* already exited */ }
        finish({ ok: false, timeout: true })
      }, 30_000)
    } catch {
      finish({ ok: false })
    }
  })
}

async function probeGapseqEnvironment({ isWindows, distro, runner }) {
  if (!isWindows) {
    return { available: false, detail: 'gapseq 仅支持 Windows WSL 的只读探测。' }
  }
  // 快速预检：先确认目标发行版存在（wsl.exe -l -q 亚秒级），避免发行版缺失时
  // 白等一次 bash 长命令直到超时刹车（实测：bash 启动即 ~3s）。
  try {
    const listed = await runner('wsl.exe', ['-l', '-q'])
    const names = String(listed?.stdout ?? '')
      .replace(/\u0000/g, '')
      .split(/\r?\n/)
      .map((name) => name.trim())
      .filter(Boolean)
    if (listed?.ok && !names.includes(distro)) {
      return {
        available: false,
        detail: names.length > 0
          ? `WSL 发行版 ${distro} 不存在（现有：${names.join(', ')}）。`
          : `WSL 未发现发行版（期望 ${distro}）。`,
      }
    }
  } catch { /* 预检失败不阻断后续只读探测 */ }
  let result
  try {
    result = await runner('wsl.exe', [
      '-d', distro, '-u', 'root', '--', 'bash', '-lc',
      'source /opt/miniforge3/etc/profile.d/conda.sh && conda activate gapseq && gapseq -v',
    ])
  } catch {
    return { available: false, detail: 'WSL/gapseq 只读探测未完成。' }
  }
  const output = String(result?.stdout ?? '').slice(0, 512)
  if (!result?.ok || !/\bgapseq\b/i.test(output)) {
    return {
      available: false,
      detail: result?.timeout
        ? 'gapseq 探测超时（WSL 冷启动可能超过 30s），后台将自动重试。'
        : 'WSL/gapseq 只读探测未就绪。',
    }
  }
  const version = output.match(/gapseq(?:\s+version)?\s*[:v]?\s*([0-9][0-9.]*)/i)?.[1]
  return {
    available: true,
    detail: version ? `gapseq ${version}（WSL 只读探测通过）。` : 'gapseq（WSL 只读探测通过）。',
  }
}

function writeJson(res, status, body) {
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'referrer-policy': 'no-referrer',
  })
  res.end(JSON.stringify(body))
}

/**
 * Loopback + same-origin guard copied from the host integration boundary.
 *
 * It independently verifies socket address, Host, browser cross-site intent,
 * and Origin when one is present. Loopback alone is not treated as a general
 * authorization mechanism for future write routes.
 */
export function isLoopbackRequest(req) {
  const address = req.socket?.remoteAddress
  if (address !== '127.0.0.1' && address !== '::1' && address !== '::ffff:127.0.0.1') return false
  const host = req.headers?.host
  if (typeof host !== 'string') return false
  let hostUrl
  try {
    hostUrl = new URL(`http://${host}`)
  } catch {
    return false
  }
  if (hostUrl.hostname !== '127.0.0.1' && hostUrl.hostname !== 'localhost' && hostUrl.hostname !== '[::1]') return false
  if (req.headers?.['sec-fetch-site'] === 'cross-site') return false
  const origin = req.headers?.origin
  if (origin === undefined) return true
  try {
    return new URL(origin).host === hostUrl.host
  } catch {
    return false
  }
}

function guardedGet(handler) {
  return async (req, res) => {
    if (!isLoopbackRequest(req)) {
      return writeJson(res, 403, {
        ok: false,
        code: 'loopback-required',
        message: 'loopback requests only',
      })
    }
    if (req.method !== 'GET') {
      return writeJson(res, 405, {
        ok: false,
        code: 'method-not-allowed',
        message: `method not allowed: ${req.method}`,
      })
    }
    try {
      const response = await handler()
      if (response?.ok !== true) {
        return writeJson(res, 500, {
          ok: false,
          code: 'internal',
          message: 'integration endpoint failed',
        })
      }
      return writeJson(res, 200, response)
    } catch {
      return writeJson(res, 500, {
        ok: false,
        code: 'internal',
        message: 'integration endpoint failed',
      })
    }
  }
}

/** Register the two fixed read-only integration routes when webServer exists. */
export function registerIntegrationRoutes(ctx, options = {}) {
  const webServer = options.webServer ?? ctx?.webServer
  if (!webServer?.register) return () => {}
  const service = options.service ?? createIntegrationService(options)
  const routes = [
    {
      kind: 'exact',
      path: `${INTEGRATION_PREFIX}/health`,
      handler: guardedGet(() => service.health()),
    },
    {
      kind: 'exact',
      path: `${INTEGRATION_PREFIX}/v1/status`,
      handler: guardedGet(() => service.status()),
    },
    {
      kind: 'exact',
      path: `${INTEGRATION_PREFIX}/v1/capabilities`,
      handler: guardedGet(() => service.capabilities()),
    },
  ]
  const disposers = routes.map((route) => webServer.register(route))
  return () => {
    for (const dispose of disposers) dispose?.()
  }
}

/**
 * Create the stateless portion of the integration API.
 *
 * Options exist solely to make protocol behavior testable without a dsh host;
 * production callers use the package defaults.
 */
export function createIntegrationService(options = {}) {
  const pluginVersion = options.pluginVersion ?? PLUGIN_VERSION
  const dataRoot = options.dataRoot ?? defaultDataRoot()
  const now = options.now ?? Date.now
  const candidateProvider = options.pythonCandidates ?? pythonCandidates
  const fileExists = options.fileExists ?? existsSync
  const probeCobra = options.probeCobra ?? probeCobraVersion
  const probePython = options.probePython
    ?? (() => probePythonEnvironment(candidateProvider, fileExists, probeCobra))
  const probeGapseq = options.probeGapseq
    ?? (() => probeGapseqEnvironment({
      isWindows: options.isWindows ?? process.platform === 'win32',
      distro: options.gapseqDistro ?? process.env.GEM_GAPSEQ_DISTRO ?? 'Ubuntu-22.04',
      runner: options.runGapseqProbe ?? runGapseqVersionCommand,
    }))
  let cachedPython = null
  let cachedPythonAt = 0
  let pendingPython = null
  let cachedGapseq = null
  let cachedGapseqAt = 0
  let pendingGapseq = null

  /** Python 探测快（≈3s）且有 60s 缓存：保持同步等待，语义简单。 */
  function readPython() {
    const current = now()
    if (cachedPython && current - cachedPythonAt < RUNTIME_PROBE_CACHE_MS) return Promise.resolve(cachedPython)
    if (pendingPython) return pendingPython
    pendingPython = Promise.resolve()
      .then(probePython)
      .then((value) => {
        cachedPython = value
        cachedPythonAt = now()
        return value
      })
      .finally(() => { pendingPython = null })
    return pendingPython
  }

  /**
   * gapseq 探测**非阻塞**（stale-while-revalidate）：缓存未过期直接返回；
   * 过期时立即返回旧值并后台刷新；从未探测过则返回 `available: null` 占位
   * （契约语义：null = 尚未探测，探测在后台进行，后续请求即得布尔结果）。
   * 这样 status 永远不会被 WSL 冷启动拖到消费端超时。
   */
  function readGapseq() {
    const current = now()
    // 失败结果（含超时）只缓存 60s，让后台尽快重试（WSL 冷启动是暂时性状态）。
    const ttl = cachedGapseq?.available === false ? 60_000 : GAPSEQ_PROBE_CACHE_MS
    if (cachedGapseq && current - cachedGapseqAt < ttl) return cachedGapseq
    if (!pendingGapseq) {
      pendingGapseq = Promise.resolve()
        .then(probeGapseq)
        .then((value) => {
          cachedGapseq = value
          cachedGapseqAt = now()
          return value
        })
        .catch(() => cachedGapseq)
        .finally(() => { pendingGapseq = null })
    }
    return cachedGapseq ?? {
      available: null,
      probing: true,
      detail: 'gapseq 只读探测进行中（WSL 冷启动可能需数秒），稍后刷新可见结果。',
    }
  }

  /** 依赖检查（status 与 capabilities 共用；语义与既有 status 一致）。 */
  async function collectChecks() {
    const python = await readPython()
    const gapseq = readGapseq()
    const carveme = carvemeStatus(dataRoot)
    const checks = [
      statusCheck(
        'python.cobra',
        python.selected ? 'ok' : 'missing',
        python.selected
          ? `cobra ${python.selected.cobraVersion ?? 'available'} @ ${python.selected.path}`
          : '未找到可 import cobra 的 Python 解释器。',
      ),
      statusCheck(
        'runtime.carveme',
        carveme.available ? 'ok' : 'missing',
        carveme.hint,
      ),
      statusCheck(
        'runtime.gapseq',
        gapseq.available === true ? 'ok' : gapseq.available === null ? 'warn' : 'missing',
        gapseq.detail ?? (gapseq.available ? 'gapseq 只读探测通过。' : 'gapseq 只读探测未就绪。'),
      ),
    ]
    return { python, gapseq, carveme, checks }
  }

  return {
    async health() {
      return {
        ok: true,
        value: {
          pluginId: PLUGIN_ID,
          pluginVersion,
          protocolMajor: PROTOCOL_MAJOR,
          protocolMinors: PROTOCOL_MINORS,
          features: INTEGRATION_FEATURES,
        },
      }
    },

    async capabilities() {
      const { checks } = await collectChecks()
      return { ok: true, value: buildCapabilitiesReport({ pluginVersion, checks }) }
    },

    async status() {
      const { python, gapseq, carveme, checks } = await collectChecks()
      return {
        ok: true,
        value: {
          state: checks.every((check) => check.status === 'ok') ? 'ready' : 'degraded',
          generatedAt: new Date(now()).toISOString(),
          pluginVersion,
          features: INTEGRATION_FEATURES,
          checks,
          data: {
            models: boundedSummary(join(dataRoot, 'models'), (name) => name.endsWith('.xml')),
            ledger: ledgerSummary(dataRoot),
            exports: boundedSummary(join(dataRoot, 'exports')),
          },
          env: { python, engines: { carveme, gapseq } },
          remediations: remediationsFor(checks),
        },
      }
    },
  }
}
