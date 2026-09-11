// jobs.js — dsh-bio-gem 后台长任务管理（M1 基建，ESM）
// gem_build 是 1-2 分钟级任务：startBuild(args) -> jobId（立即返回）；
// jobStatus(jobId) -> 进度/结果（轮询）。进度事件落 <jobDir>/progress.jsonl，
// 完成时 build.py 写 result.json（进程消失也可恢复读取）。
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
// 解释器选择与 PYTHON_DIR 统一到 python.js：本文件此前另有一份硬编码 miniconda
// 路径的 pythonExe 副本，会造成「gem_build 与其余 op 使用不同解释器」的隐性分裂
// （2026-09-11 统一；候选顺序见 python.js：GEM_PYTHON → 宿主 dsh-bio-genie
// 自举环境 → CONDA_PREFIX → PATH，且逐个探测 import cobra）。
import { pythonExe, PYTHON_DIR } from './python.js'

const ROOT = path.join(os.homedir(), '.dsh', 'dsh-bio-gem')
const JOBS_DIR = path.join(ROOT, 'jobs')

const jobs = new Map() // jobId -> {cp, jobDir, ...}

export function startBuild({ input, name, engine, medium, outDir }) {
  const jobId = 'gem_' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)
  const jobDir = path.join(JOBS_DIR, jobId)
  fs.mkdirSync(jobDir, { recursive: true })
  const progressFile = path.join(jobDir, 'progress.jsonl')
  const stdoutFile = path.join(jobDir, 'stdout.txt')
  const stderrFile = path.join(jobDir, 'stderr.txt')

  const py = pythonExe()
  const args = ['-u', path.join(PYTHON_DIR, 'build.py'),
    '--input', input, '--progress', progressFile]
  if (engine) args.push('--engine', engine)
  if (name) args.push('--name', name)
  if (medium) args.push('--medium-json', JSON.stringify(medium))
  if (outDir) args.push('--out-dir', outDir)

  const cp = spawn(py, args, { cwd: PYTHON_DIR, windowsHide: true })
  const so = fs.createWriteStream(stdoutFile, { flags: 'a' })
  const se = fs.createWriteStream(stderrFile, { flags: 'a' })
  cp.stdout.pipe(so)
  cp.stderr.pipe(se)

  const rec = {
    jobId, jobDir, progressFile, stdoutFile, stderrFile,
    cp, done: false, result: null, error: null, started: Date.now(),
  }
  jobs.set(jobId, rec)

  cp.on('exit', (code) => {
    se.end()
    so.end()
    try {
      const txt = fs.readFileSync(stdoutFile, 'utf8')
      const lines = txt.trim().split(/\r?\n/).filter(Boolean)
      const last = JSON.parse(lines[lines.length - 1])
      rec.result = last
      rec.done = true
      rec.code = code
    } catch (e) {
      rec.error = `parse result failed: ${e.message}`
      rec.done = true
      rec.code = code
    }
    fs.writeFileSync(path.join(jobDir, 'result.json'),
      JSON.stringify({ result: rec.result, error: rec.error, code: rec.code }, null, 2))
    jobs.delete(jobId)
  })
  cp.on('error', (e) => {
    rec.error = e.message
    rec.done = true
    jobs.delete(jobId)
  })
  return { jobId, jobDir, progressFile }
}

export function readProgress(jobId) {
  const pf = path.join(JOBS_DIR, jobId, 'progress.jsonl')
  if (!fs.existsSync(pf)) return []
  try {
    return fs.readFileSync(pf, 'utf8').trim().split(/\r?\n/)
      .filter(Boolean)
      .map((l) => JSON.parse(l))
  } catch {
    return []
  }
}

// 阶段D-P2：失败透明化——job 目录内容摘要 + stderr 尾部（成功路径不计算不返回）
function jobDetail(jobDir) {
  let files = []
  try {
    files = fs.readdirSync(jobDir).map((f) => {
      let bytes = 0
      try { bytes = fs.statSync(path.join(jobDir, f)).size } catch { /* ignore */ }
      return { name: f, bytes }
    })
  } catch { /* ignore */ }
  let stderr_tail = ''
  try {
    const sp = path.join(jobDir, 'stderr.txt')
    if (fs.existsSync(sp)) stderr_tail = fs.readFileSync(sp, 'utf8').slice(-400)
  } catch { /* ignore */ }
  return { job_dir: jobDir, job_dir_files: files, stderr_tail }
}

// 轻量 in-memory 任务视图（进程存活期）；磁盘持久视图用 readProgress/result.json
export function jobStatus(jobId) {
  const m = jobs.get(jobId)
  const jobDir = path.join(JOBS_DIR, jobId)
  const resultFile = path.join(jobDir, 'result.json')
  const events = readProgress(jobId)
  const last = events.length ? events[events.length - 1] : null
  let result = null
  let error = null
  let done = false
  let code = null
  if (fs.existsSync(resultFile)) {
    try {
      const r = JSON.parse(fs.readFileSync(resultFile, 'utf8'))
      done = true; result = r.result; error = r.error; code = r.code
    } catch { /* ignore */ }
  } else if (m) {
    done = m.done; result = m.result; error = m.error; code = m.code
  } else {
    const stderr = path.join(jobDir, 'stderr.txt')
    error = fs.existsSync(stderr) ? fs.readFileSync(stderr, 'utf8').slice(-800) : 'job vanished'
    done = true
  }
  // build.py 失败信封：{ok:true, result:null, error_hint}（exit 0）——也按失败透出 detail
  const envelopeFailed = !!(result && result.error_hint && result.result == null)
  const failed = !!error || envelopeFailed || (done && code !== 0 && !result)
  return {
    jobId, done, code, result, error, lastEvent: last, events, jobDir,
    ...(failed ? { detail: jobDetail(jobDir) } : {}),
    elapsed_ms: Date.now() - (m ? m.started : Date.now()),
  }
}