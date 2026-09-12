import { spawn } from 'node:child_process'
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import os from 'node:os'
import { join } from 'node:path'
import { pythonCandidates } from './python.js'

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
export const INTEGRATION_FEATURES = [
  'status',
  'model-store',
  'ledger',
  'exports',
  'carveme-runtime',
  'gapseq-probe',
]

const PLUGIN_ID = 'dsh-bio-gem'
const PLUGIN_VERSION = '0.1.11'

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
      const child = spawn(command, args, {
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'ignore'],
      })
      child.stdout?.on('data', (chunk) => {
        if (stdout.length < 512) stdout += chunk.toString('utf8').slice(0, 512 - stdout.length)
      })
      child.on('error', () => finish({ ok: false }))
      child.on('close', (code) => finish({ ok: code === 0, stdout }))
      timer = setTimeout(() => {
        try { child.kill() } catch { /* already exited */ }
        finish({ ok: false })
      }, 15_000)
    } catch {
      finish({ ok: false })
    }
  })
}

async function probeGapseqEnvironment({ isWindows, distro, runner }) {
  if (!isWindows) {
    return { available: false, detail: 'gapseq 仅支持 Windows WSL 的只读探测。' }
  }
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
    return { available: false, detail: 'WSL/gapseq 只读探测未就绪。' }
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
  let cachedRuntime = null
  let cachedRuntimeAt = 0
  let pendingRuntime = null

  function readRuntime() {
    const current = now()
    if (cachedRuntime && current - cachedRuntimeAt < RUNTIME_PROBE_CACHE_MS) return Promise.resolve(cachedRuntime)
    if (pendingRuntime) return pendingRuntime
    pendingRuntime = Promise.all([probePython(), probeGapseq()])
      .then((value) => {
        cachedRuntime = { python: value[0], gapseq: value[1] }
        cachedRuntimeAt = now()
        return cachedRuntime
      })
      .finally(() => { pendingRuntime = null })
    return pendingRuntime
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

    async status() {
      const { python, gapseq } = await readRuntime()
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
          gapseq.available ? 'ok' : 'missing',
          gapseq.detail ?? (gapseq.available ? 'gapseq 只读探测通过。' : 'gapseq 只读探测未就绪。'),
        ),
      ]
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
