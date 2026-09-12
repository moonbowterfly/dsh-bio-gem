/**
 * Integration protocol unit tests.
 *
 * Run: node test/integration.js
 */
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, rmSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'

let integration
try {
  integration = await import('../src/integration.js')
} catch {
  integration = null
}

let failed = 0

async function test(name, fn) {
  try {
    await fn()
    console.log(`✓ ${name}`)
  } catch (error) {
    failed += 1
    console.error(`✗ ${name}`)
    console.error(error.stack || error.message)
  }
}

async function invokeRoute(handler, options = {}) {
  let status = null
  let headers = null
  let payload = null
  const req = {
    method: options.method ?? 'GET',
    socket: { remoteAddress: options.remoteAddress ?? '127.0.0.1' },
    headers: {
      host: options.host ?? '127.0.0.1:3080',
      ...(options.headers ?? {}),
    },
  }
  const res = {
    writeHead(nextStatus, nextHeaders) {
      status = nextStatus
      headers = nextHeaders
    },
    end(nextPayload) { payload = nextPayload },
  }
  await handler(req, res)
  return { status, headers, body: JSON.parse(payload) }
}

await test('health exposes the frozen protocol identity without a runtime probe', async () => {
  assert.equal(typeof integration?.createIntegrationService, 'function')
  let runtimeProbes = 0
  const service = integration.createIntegrationService({
    probePython: async () => { runtimeProbes += 1; return { selected: null, candidates: [] } },
    probeGapseq: async () => { runtimeProbes += 1; return { available: false } },
  })
  const response = await service.health()

  assert.deepEqual(response, {
    ok: true,
    value: {
      pluginId: 'dsh-bio-gem',
      pluginVersion: '0.1.11',
      protocolMajor: 1,
      protocolMinors: [0],
      features: [
        'status',
        'model-store',
        'ledger',
        'exports',
        'carveme-runtime',
        'gapseq-probe',
      ],
    },
  })
  assert.equal(runtimeProbes, 0)
})

await test('status reports the three required checks and bounded asset summaries', async () => {
  assert.equal(typeof integration?.createIntegrationService, 'function')
  const dataRoot = mkdtempSync(join(tmpdir(), 'gem-integration-'))
  try {
    mkdirSync(join(dataRoot, 'models'))
    mkdirSync(join(dataRoot, 'ledger'))
    mkdirSync(join(dataRoot, 'exports'))
    mkdirSync(join(dataRoot, 'venv-carveme', 'Scripts'), { recursive: true })
    writeFileSync(join(dataRoot, 'models', 'C58.xml'), '<sbml />')
    for (let index = 0; index < 50; index += 1) {
      writeFileSync(join(dataRoot, 'models', `extra-${index}.xml`), '<sbml />')
    }
    writeFileSync(join(dataRoot, 'ledger', 'C58.jsonl'), '{"prediction_id":"P0001"}\n')
    writeFileSync(join(dataRoot, 'exports', 'targets.json'), '[]')
    writeFileSync(join(dataRoot, 'venv-carveme', 'Scripts', 'carve.exe'), '')
    writeFileSync(join(dataRoot, 'venv-carveme', 'Scripts', 'diamond.exe'), '')

    const service = integration.createIntegrationService({
      dataRoot,
      now: () => 1_700_000_000_000,
      probePython: async () => ({
        selected: { path: 'python', source: 'PATH', cobraVersion: '0.32.1' },
        candidates: [{ path: 'python', source: 'PATH', exists: true }],
      }),
      probeGapseq: async () => ({ available: true, detail: 'gapseq 2.1.0' }),
    })
    const response = await service.status()

    assert.equal(response.ok, true)
    assert.equal(response.value.state, 'ready')
    assert.deepEqual(response.value.checks.map((check) => [check.id, check.status]), [
      ['python.cobra', 'ok'],
      ['runtime.carveme', 'ok'],
      ['runtime.gapseq', 'ok'],
    ])
    assert.equal(response.value.data.models.count, 51)
    assert.equal(response.value.data.models.items.length, 50)
    assert.equal(response.value.data.ledger.count, 1)
    assert.equal(response.value.data.ledger.totalEntries, 1)
    assert.equal(response.value.data.exports.count, 1)
    assert.deepEqual(response.value.env.python.selected, {
      path: 'python', source: 'PATH', cobraVersion: '0.32.1',
    })
    assert.equal(response.value.env.engines.carveme.available, true)
    assert.equal(response.value.env.engines.gapseq.available, true)
    assert.deepEqual(response.value.remediations, [])
  } finally {
    rmSync(dataRoot, { recursive: true, force: true })
  }
})

await test('status caches expensive runtime probes for no more than sixty seconds', async () => {
  assert.equal(typeof integration?.createIntegrationService, 'function')
  let clock = 1_700_000_000_000
  let pythonProbes = 0
  let gapseqProbes = 0
  const service = integration.createIntegrationService({
    now: () => clock,
    probePython: async () => {
      pythonProbes += 1
      return { selected: null, candidates: [] }
    },
    probeGapseq: async () => {
      gapseqProbes += 1
      return { available: false, detail: 'not configured' }
    },
  })

  await service.status()
  await service.status()
  assert.equal(pythonProbes, 1)
  assert.equal(gapseqProbes, 1)

  clock += 60_001
  await service.status()
  assert.equal(pythonProbes, 2)
  assert.equal(gapseqProbes, 2)
})

await test('degraded checks expose only controlled remediation codes and owners', async () => {
  assert.equal(typeof integration?.createIntegrationService, 'function')
  const dataRoot = mkdtempSync(join(tmpdir(), 'gem-integration-remediation-'))
  try {
    const service = integration.createIntegrationService({
      dataRoot,
      probePython: async () => ({ selected: null, candidates: [] }),
      probeGapseq: async () => ({ available: false, detail: 'not configured' }),
    })
    const response = await service.status()
    assert.equal(response.value.state, 'degraded')
    assert.deepEqual(response.value.remediations.map(({ code, owner }) => ({ code, owner })), [
      { code: 'genie.bootstrap-python', owner: 'genie' },
      { code: 'gem.install-carveme-runtime', owner: 'gem' },
      { code: 'genie.install-wsl-gapseq', owner: 'genie' },
    ])
  } finally {
    rmSync(dataRoot, { recursive: true, force: true })
  }
})

await test('status uses the ordered gem Python candidates to expose cobra availability', async () => {
  assert.equal(typeof integration?.createIntegrationService, 'function')
  const cobraCalls = []
  const service = integration.createIntegrationService({
    pythonCandidates: () => [
      { path: 'missing-python', source: 'GEM_PYTHON' },
      { path: 'usable-python', source: 'genie-hosted' },
    ],
    fileExists: (path) => path === 'usable-python',
    probeCobra: async (path) => {
      cobraCalls.push(path)
      return '0.32.1'
    },
    probeGapseq: async () => ({ available: true, detail: 'available' }),
  })

  const response = await service.status()
  assert.deepEqual(response.value.env.python.candidates, [
    { path: 'missing-python', source: 'GEM_PYTHON', exists: false },
    { path: 'usable-python', source: 'genie-hosted', exists: true },
  ])
  assert.deepEqual(response.value.env.python.selected, {
    path: 'usable-python', source: 'genie-hosted', cobraVersion: '0.32.1',
  })
  assert.deepEqual(cobraCalls, ['usable-python'])
})

await test('default gapseq probe uses a fixed read-only version command', async () => {
  assert.equal(typeof integration?.createIntegrationService, 'function')
  let invocation = null
  const service = integration.createIntegrationService({
    pythonCandidates: () => [],
    isWindows: true,
    runGapseqProbe: async (command, args) => {
      invocation = { command, args }
      return { ok: true, stdout: 'gapseq version: 2.1.0' }
    },
  })

  const response = await service.status()
  const gapseqCheck = response.value.checks.find((check) => check.id === 'runtime.gapseq')
  assert.equal(gapseqCheck.status, 'ok')
  assert.equal(response.value.env.engines.gapseq.available, true)
  assert.equal(invocation.command, 'wsl.exe')
  assert.deepEqual(invocation.args.slice(0, 7), ['-d', 'Ubuntu-22.04', '-u', 'root', '--', 'bash', '-lc'])
  assert.match(invocation.args[7], /gapseq -v/)
  assert.doesNotMatch(invocation.args[7], /install|update|download/i)
})

await test('integration routes allow loopback health and reject non-loopback callers', async () => {
  assert.equal(typeof integration?.registerIntegrationRoutes, 'function')
  const routes = []
  const ctx = {
    webServer: {
      register(route) {
        routes.push(route)
        return () => routes.splice(routes.indexOf(route), 1)
      },
    },
  }
  const dispose = integration.registerIntegrationRoutes(ctx, {
    service: integration.createIntegrationService(),
  })
  try {
    const health = routes.find((route) => route.path.endsWith('/health'))
    assert.ok(health)
    const allowed = await invokeRoute(health.handler)
    assert.equal(allowed.status, 200)
    assert.equal(allowed.body.ok, true)

    const denied = await invokeRoute(health.handler, { remoteAddress: '203.0.113.7' })
    assert.equal(denied.status, 403)
    assert.deepEqual(denied.body, {
      ok: false,
      code: 'loopback-required',
      message: 'loopback requests only',
    })
  } finally {
    dispose()
  }
})

await test('status route returns a safe failure envelope when a probe fails', async () => {
  assert.equal(typeof integration?.registerIntegrationRoutes, 'function')
  const routes = []
  const ctx = { webServer: { register: (route) => { routes.push(route); return () => {} } } }
  const dispose = integration.registerIntegrationRoutes(ctx, {
    service: {
      health: async () => ({ ok: true, value: {} }),
      status: async () => { throw new Error('GEM_API_TOKEN=should-not-leak') },
    },
  })
  try {
    const status = routes.find((route) => route.path.endsWith('/v1/status'))
    let response
    try {
      response = await invokeRoute(status.handler)
    } catch (error) {
      response = { error }
    }
    assert.equal(response.status, 500)
    assert.deepEqual(response.body, {
      ok: false,
      code: 'internal',
      message: 'integration endpoint failed',
    })
    assert.doesNotMatch(JSON.stringify(response.body), /should-not-leak/)
  } finally {
    dispose()
  }
})

if (failed) process.exitCode = 1
