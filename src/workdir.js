/**
 * dsh-bio-gem — 工作区解析（工具执行的工作目录）
 *
 * 优先级：
 *   1. 会话工作区 `exec.agent.session.header.cwd`（与 dsh 内置 fs 工具一致：
 *      每个会话操作自己的工作区，而不是服务器启动目录）
 *   2. 保底工作区 `~/.dsh/sessions/default`（自动创建）
 *
 * 背景（2026-10-04 E2E 实测）：python 子进程此前固定 cwd=插件 python 目录，
 * 工具参数里的相对路径（如 gem_fluxscan 的 export_csv）因此解析到插件目录、
 * 写入失败（agent 被迫改用绝对路径自愈）。本模块对齐 dsh-bio-genie 的
 * workdir 语义：相对路径基于会话工作区解析。
 *
 * 说明：dsh 对未显式指定 cwd 的会话会把 header.cwd 填充为服务器启动目录
 * （process.cwd()），此时视为「未指定工作区」，走保底链。
 *
 * @module dsh-bio-gem/workdir
 */
import { existsSync, mkdirSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

/** 保底工作区目录（自动创建；仅无会话 cwd 时使用）。 */
export function fallbackWorkspace() {
  return join(homedir(), '.dsh', 'sessions', 'default')
}

/**
 * 会话工作区目录。
 * @param {object} [exec] 工具执行上下文（defineTool execute 的第二参数）。
 * @returns {string|undefined} 会话 cwd；未指定或不可读时返回 undefined。
 */
export function sessionWorkspace(exec) {
  const cwd = exec?.agent?.session?.header?.cwd
  if (typeof cwd !== 'string' || cwd.length === 0) return undefined
  if (!existsSync(cwd)) return undefined
  return cwd
}

/** 幂等确保目录存在（失败静默——由调用方决定回退）。 */
function ensureDir(dir) {
  if (existsSync(dir)) return true
  try {
    mkdirSync(dir, { recursive: true })
    return existsSync(dir)
  } catch {
    return false
  }
}

/**
 * 解析一次工具调用的工作目录（供 python 子进程 cwd 使用）。
 * @param {object} [exec] 工具执行上下文。
 * @returns {string} 解析后的绝对路径（保底可用）。
 */
export function resolveWorkdir(exec) {
  const serverCwd = process.cwd()
  const rawSessionCwd = sessionWorkspace(exec)
  const sessionCwd = rawSessionCwd && rawSessionCwd !== serverCwd ? rawSessionCwd : undefined
  if (sessionCwd) {
    ensureDir(sessionCwd)
    return sessionCwd
  }
  const fb = fallbackWorkspace()
  ensureDir(fb)
  return fb
}
