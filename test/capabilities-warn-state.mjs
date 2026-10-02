/**
 * warn（探测进行中 / 未完成）不得被折成 unavailable。
 *
 * gem 的 runtime.gapseq 在 WSL 冷启动期间就是 warn（契约语义：null = 尚未探测）。
 * buildCapabilitiesReport 原先把所有非 ok 的状态（含 warn）都算进 missingDeps，
 * 于是"还在探测"被对外报成"gapseq 不可用"——与galatea 侧的同类失真同源。
 *
 * 变异锚点：把 warn 重新并回 missingDeps，本测试必须报红。
 */
import assert from 'node:assert/strict'
import { buildCapabilitiesReport, TOOLS_MANIFEST } from '../src/capabilities.js'

const ALL_IDS = ['python.cobra', 'runtime.carveme', 'runtime.gapseq']

function reportFor(statusById) {
  return buildCapabilitiesReport({
    pluginVersion: '0.0.0',
    checks: ALL_IDS.map((id) => ({ id, status: statusById[id] })),
  })
}

function toolOf(report, name) {
  return report.tools.find((tool) => tool.name === name)
}

/** manifest 声明的基线状态（避免在测试里臆造 'ready'）。 */
function declaredStatus(name) {
  return TOOLS_MANIFEST.find((tool) => tool.name === name).status
}

let passed = 0
async function test(name, run) {
  await run()
  passed += 1
  console.log(`  ok   ${name}`)
}

await test('gapseq 探测中（warn）→ 依赖它的工具为 unknown，不是 unavailable', () => {
  const report = reportFor({
    'python.cobra': 'ok',
    'runtime.carveme': 'ok',
    'runtime.gapseq': 'warn',
  })
  const tool = toolOf(report, 'gem_gapseq')
  assert.ok(tool, 'manifest 应含 gem_gapseq')
  assert.equal(tool.status, 'unknown')
})

await test('gapseq 确认缺失 → unavailable（原语义不被削弱）', () => {
  const report = reportFor({
    'python.cobra': 'ok',
    'runtime.carveme': 'ok',
    'runtime.gapseq': 'missing',
  })
  assert.equal(toolOf(report, 'gem_gapseq').status, 'unavailable')
})

await test('gapseq 就绪 → 沿用 manifest 原状态（零误报，不被改写成 ready）', () => {
  const report = reportFor({
    'python.cobra': 'ok',
    'runtime.carveme': 'ok',
    'runtime.gapseq': 'ok',
  })
  // manifest 把 gem_gapseq 标为 experimental（数据未初始化），依赖满足时不得篡改它。
  assert.equal(toolOf(report, 'gem_gapseq').status, declaredStatus('gem_gapseq'))
})

await test('gapseq warn 不连坐不依赖它的工具', () => {
  const report = reportFor({
    'python.cobra': 'ok',
    'runtime.carveme': 'ok',
    'runtime.gapseq': 'warn',
  })
  const unrelated = toolOf(report, 'gem_quality')
  assert.equal(unrelated.status, declaredStatus('gem_quality'))
})

await test('全 ok → 无 unknown / unavailable', () => {
  const report = reportFor({
    'python.cobra': 'ok',
    'runtime.carveme': 'ok',
    'runtime.gapseq': 'ok',
  })
  const bad = report.tools.filter((t) => t.status === 'unknown' || t.status === 'unavailable')
  assert.deepEqual(bad, [], `不应有降级工具：${bad.map((t) => t.name).join(', ')}`)
})

console.log(`capabilities-warn-state: ${passed} passed, 0 failed`)