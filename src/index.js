// dsh-bio-gem — Cordis 插件主模块
// 注入 tools（5 语义化工具：gem_report/validate/gapfind/gapfill/build）+ skills（gem-expert）。
import { registerTools } from './tools.js'
import { registerSkills } from './skills.js'
import { registerIntegrationRoutes, createIntegrationService } from './integration.js'

/** Cordis 插件名（cordis.patch.yml row id 同名）。 */
export const name = 'dsh-bio-gem'

/**
 * 静态注入只列**必选**服务（数组形式是 cordis 唯一支持的「服务名列表」写法；
 * `{ required, optional }` 的对象形式会被 cordis 当成「服务名 → 配置」字典，
 * 导致插件永远 pending —— 实测：dsh boot 报
 * `@dsh-bio/dsh-bio-gem: pending (waiting for services: required, optional)`）。
 *
 * `webServer` 是**可选**服务（非 web 部署不提供），改用 apply 内的动态注入
 * `ctx.inject(['webServer'], cb)`（官方 dsh 插件同款模式）：服务可用时注册
 * 只读 integration 路由，不可用时 21 个工具与 skill 照常注册。
 */
export const inject = ['tools', 'skills']

/**
 * 装配插件。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 */
export function apply(ctx) {
  registerTools(ctx)
  registerSkills(ctx)

  const service = createIntegrationService()
  ctx.inject(['webServer'], (webCtx) => {
    webCtx.effect(() => registerIntegrationRoutes(webCtx, { service }), 'dsh-bio-gem: integration API routes')
    // 延迟预热（**不在插件加载期**执行，不阻塞启动）：启动 ~8s 后后台跑一次只读探测，
    // 让面板首次打开即命中 60s 缓存，避免首次冷探测（含 WSL 探测）被消费端超时截断。
    const warmup = setTimeout(() => { void service.status().catch(() => {}) }, 8_000)
    webCtx.effect(() => () => clearTimeout(warmup), 'dsh-bio-gem: runtime probe warm-up')
  })
}
