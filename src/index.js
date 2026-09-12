// dsh-bio-gem — Cordis 插件主模块
// 注入 tools（5 语义化工具：gem_report/validate/gapfind/gapfill/build）+ skills（gem-expert）。
import { registerTools } from './tools.js'
import { registerSkills } from './skills.js'
import { registerIntegrationRoutes } from './integration.js'

/** Cordis 插件名（cordis.patch.yml row id 同名）。 */
export const name = 'dsh-bio-gem'

/**
 * `webServer` is optional: GEM tools and skill registration must stay active
 * in non-web dsh deployments, while web deployments gain the read-only
 * hosted-domain integration routes.
 */
export const inject = {
  required: ['tools', 'skills'],
  optional: ['webServer'],
}

/**
 * 装配插件。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 */
export function apply(ctx) {
  registerTools(ctx)
  registerSkills(ctx)

  // Cordis re-evaluates this effect when optional services appear. Access the
  // property inside the effect so a missing webServer never blocks tools.
  ctx.effect(() => {
    if (!ctx.webServer) return undefined
    return registerIntegrationRoutes(ctx)
  }, 'dsh-bio-gem: integration API routes')
}
