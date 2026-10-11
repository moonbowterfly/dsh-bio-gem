// run-gem-pytest.mjs — 用 gem 运行时解析出的解释器（GEM_PYTHON → 宿主 genie env → PATH）
// 跑一个 Python 测试文件，退出码透传（测试失败 → 非零退出，形成闸门）。
// 用法：node test/run-gem-pytest.mjs test/test_g6_atp_maintenance.py
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { pythonExe } from '../src/python.js'

const testFile = process.argv[2]
if (!testFile) {
  console.error('usage: node test/run-gem-pytest.mjs <test.py>')
  process.exit(2)
}
const abs = join(dirname(fileURLToPath(import.meta.url)), '..', testFile)
const exe = pythonExe()
const r = spawnSync(exe, ['-I', '-B', abs], {
  cwd: join(dirname(fileURLToPath(import.meta.url)), '..'),
  stdio: 'inherit',
  env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
})
if (r.status !== 0) {
  console.error(`\n[run-gem-pytest] ${testFile} FAILED (interpreter=${exe}, exit=${r.status})`)
  process.exit(r.status ?? 1)
}
console.log(`\n[run-gem-pytest] ${testFile} PASS (interpreter=${exe})`)
