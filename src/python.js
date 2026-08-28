// python.js — dsh-bio-gem Python 子进程调用器（JSON stdin 协议）
// bridge 契约同 dsh-bio-genie：stdout 最后一行是 JSON；stderr 含
// "Traceback (most recent call last)" 头 = 代码级失败（恒 ok:true 时靠它判定）。
import { spawn } from 'node:child_process'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'

const PYTHON_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'python')

// 运行时探测 Python：优先 miniconda（本机分析环境，cobra 已装），回退 env GEM_PYTHON / PATH
function pythonExe() {
  const cands = [
    process.env.GEM_PYTHON,
    'C:/Users/shuai/miniconda3/python.exe',
    'python',
  ]
  for (const c of cands) {
    if (!c) continue
    try {
      if (c === 'python' || existsSync(c)) return c
    } catch { /* ignore */ }
  }
  return 'python'
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
    cp.on('error', (e) => reject(new Error(`python spawn failed: ${e.message}`)))
    const timer = opts.timeoutMs
      ? setTimeout(() => { cp.kill(); reject(new Error(`gem op ${op} timeout after ${opts.timeoutMs}ms`)) }, opts.timeoutMs)
      : null
    cp.on('close', (code) => {
      if (timer) clearTimeout(timer)
      const lines = out.trim().split(/\r?\n/).filter(Boolean)
      if (!lines.length) {
        return reject(new Error(`gem_ops.py produced no output (op=${op}); stderr: ${err.slice(-400)}`))
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
      resolve(parsed.result)
    })
    cp.stdin.write(JSON.stringify({ op, args }))
    cp.stdin.end()
  })
}

export { pythonExe, PYTHON_DIR }