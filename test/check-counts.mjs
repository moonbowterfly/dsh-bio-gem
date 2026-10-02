/**
 * check-counts.mjs — 文档-代码计数一致性检查（防「改一处漏三处」）。
 *
 * 真值来源（不硬编码）：
 *   工具数 = src/capabilities.js 的 TOOLS_MANIFEST.length（单源）
 *   op 数  = python/gem_ops.py 的 OPS 注册数（OPS dict 字面量 + OPS["x"]= 赋值，去重）
 *
 * 断言各处文档声称与真值一致；skill 工具表按名称覆盖率检查，不要求人工计数句。
 * 计数表述「没找到」报 WARN（显式化，不静默通过）。
 * Run: node test/check-counts.mjs
 */
import { readFileSync, existsSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { TOOLS_MANIFEST } from '../src/capabilities.js'

const ROOT = join(fileURLToPath(import.meta.url), '..', '..')

function read(p) {
  return existsSync(join(ROOT, p)) ? readFileSync(join(ROOT, p), 'utf8') : ''
}

// ---- 真值 ----
const toolCount = TOOLS_MANIFEST.length

const opsSrc = read('python/gem_ops.py')
const opsNames = new Set([
  ...[...opsSrc.matchAll(/^\s*"([a-z0-9_]+)":\s*op_/gm)].map((m) => m[1]),   // OPS = { "x": op_x, ... }
  ...[...opsSrc.matchAll(/OPS\["([a-z0-9_]+)"\]\s*=/g)].map((m) => m[1]),      // OPS["x"] = op_x
])
const opCount = opsNames.size

console.log('代码实证真值：')
console.log(`  语义化工具 = ${toolCount}（capabilities.js TOOLS_MANIFEST）`)
console.log(`  op 数      = ${opCount}（gem_ops.py OPS 注册）`)
console.log()

let pass = 0
let fail = 0
let warned = 0
let skipped = 0

function assertCount(file, re, truth, label) {
  const txt = read(file)
  if (!txt) {
    console.log(`SKIP  ${label}（文件不存在: ${file}）`)
    skipped += 1
    return
  }
  const found = [...txt.matchAll(re)].map((m) => Number(m[1]))
  const uniq = [...new Set(found)]
  if (uniq.length === 0) {
    console.log(`WARN  ${label}：${file} 未出现该计数表述（本项检查未覆盖，非通过）`)
    warned += 1
    return
  }
  const bad = uniq.filter((n) => n !== truth)
  if (bad.length === 0) {
    console.log(`PASS  ${label}：出现 [${uniq.join(', ')}]，与真值 ${truth} 一致`)
    pass += 1
  } else {
    console.log(`FAIL  ${label}：出现 [${uniq.join(', ')}]，其中 ${bad.join(', ')} ≠ 真值 ${truth}`)
    fail += 1
  }
}

// ---- 断言点 ----
assertCount('README.md', /除 `gem_build` 外的 (\d+) 个工具/g, toolCount - 1, 'README 工具数（除 gem_build）')
assertCount('README.md', /其余 (\d+) 个工具/g, toolCount - 1, 'README 工具数（其余）')
assertCount('docs/ARCHITECTURE.md', /共 \*\*(\d+) 个 op\*\*/g, opCount, 'ARCHITECTURE op 数')
assertCount('docs/ARCHITECTURE.md', /工具数（(\d+)）与 op 数（(\d+)）/g, toolCount, 'ARCHITECTURE 工具数（工具/op 关系句）')
assertCount('docs/ARCHITECTURE.md', /仍照常注册 (\d+) 个 `gem_\*` 工具/g, toolCount, 'ARCHITECTURE 无 webServer 工具数')
assertCount('src/index.js', /(\d+) 个工具与 skill/g, toolCount, 'index.js 工具数（注释）')
assertCount('src/tools.js', /，(\d+) 语义化工具/g, toolCount, 'tools.js 头注释工具数')
assertCount('src/tools.js', /op 与工具对照：(\d+) op/g, opCount, 'tools.js op 计数')

// skill 是按任务选工具的表，不承诺工具总数；逐名覆盖比添加一个容易漂移的计数句更有用。
const skillFile = 'skills/gem-expert.md'
const skillText = read(skillFile)
const skillTools = new Set([...skillText.matchAll(/\bgem_[a-z0-9_]+\b/g)].map((match) => match[0]))
const manifestTools = new Set(TOOLS_MANIFEST.map((tool) => tool.name))
const missingInSkill = [...manifestTools].filter((name) => !skillTools.has(name))
const staleInSkill = [...skillTools].filter((name) => !manifestTools.has(name))
if (!skillText || missingInSkill.length > 0 || staleInSkill.length > 0) {
  console.log(`FAIL  gem-expert skill 工具覆盖：缺 ${missingInSkill.join(', ') || '无'}；过时 ${staleInSkill.join(', ') || '无'}`)
  fail += 1
} else {
  console.log(`PASS  gem-expert skill 工具覆盖：${skillTools.size} 个工具与 manifest 逐名一致`)
  pass += 1
}

console.log()
console.log(`check-counts: ${pass} pass / ${fail} fail / ${warned} warn / ${skipped} skip`)
if (fail > 0) process.exitCode = 1
