import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { TOOLS_MANIFEST, buildCapabilitiesReport } from '../src/capabilities.js'
import { createIntegrationService } from '../src/integration.js'

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const fixture = JSON.parse(readFileSync(new URL('./fixtures/capabilities-check-states.json', import.meta.url), 'utf8'))
const requiredIds = [...new Set(TOOLS_MANIFEST.flatMap((tool) => tool.requires ?? []))]
const allOkChecks = requiredIds.map((id) => ({ id, status: 'ok' }))

// requires 必须与真实采集项闭合，防止新增依赖后永久显示 unknown。
const service = createIntegrationService({
  dataRoot: fileURLToPath(new URL('./fixtures/nonexistent-runtime-data', import.meta.url)),
  probePython: async () => ({ selected: { path: 'python', cobraVersion: 'test' }, candidates: [] }),
  probeGapseq: async () => ({ available: true, detail: 'test' }),
})
const status = await service.status()
assert.equal(status.ok, true)
assert.deepEqual(
  [...new Set(status.value.checks.map((check) => check.id))].sort(),
  [...requiredIds].sort(),
  'manifest requires 与 integration 实际采集 check id 必须一致',
)
assert.equal(status.value.checks.length, requiredIds.length, '采集检查项不得重复')

const baseline = buildCapabilitiesReport({ pluginVersion: pkg.version, checks: allOkChecks })
assert.equal(baseline.plugin_version, pkg.version)
assert.equal(baseline.tool_count, TOOLS_MANIFEST.length)
for (const [index, tool] of baseline.tools.entries()) {
  assert.equal(tool.name, TOOLS_MANIFEST[index].name)
  assert.equal(tool.status, TOOLS_MANIFEST[index].status ?? 'ready', `${tool.name}: 全 ok 不得误报`)
  assert.equal(tool.missing_dependencies, undefined)
  assert.equal(tool.unknown_dependencies, undefined)
}
console.log(`✓ all-ok: ${baseline.tools.length} 个工具状态与 manifest 一致，零误报`)

for (const scenario of fixture.cases) {
  assert.ok(requiredIds.includes(scenario.dependency), `${scenario.name}: 测试依赖必须由 manifest 声明`)
  const checks = allOkChecks
    .filter((check) => !scenario.omit || check.id !== scenario.dependency)
    .map((check) => check.id === scenario.dependency
      ? { ...check, status: scenario.check_status }
      : check)
  const report = buildCapabilitiesReport({ pluginVersion: pkg.version, checks })
  let affected = 0
  for (const [index, tool] of report.tools.entries()) {
    const declared = TOOLS_MANIFEST[index]
    if (declared.requires?.includes(scenario.dependency)) {
      affected += 1
      assert.equal(tool.status, scenario.expected_status, `${scenario.name}: ${tool.name}`)
      assert.deepEqual(tool[scenario.dependency_field], [scenario.dependency], `${scenario.name}: ${tool.name}`)
    } else {
      assert.equal(tool.status, baseline.tools[index].status, `${scenario.name}: ${tool.name} 不应受影响`)
    }
  }
  assert.ok(affected > 0, `${scenario.name}: 至少覆盖一个依赖该项的工具`)
  console.log(`✓ ${scenario.name}: ${affected} 个依赖工具标为 ${scenario.expected_status}`)
}
