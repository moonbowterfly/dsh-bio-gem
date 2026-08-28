// dsh-bio-gem — Cordis 插件主模块
// 注入 tools（5 语义化工具：gem_report/validate/gapfind/gapfill/build）+ skills（gem-expert）。
import { registerTools } from './tools.js'
import { registerSkills } from './skills.js'

/** Cordis 插件名（cordis.patch.yml row id 同名）。 */
export const name = 'dsh-bio-gem'

/** 需要的服务。M1：tools + skills（无浏览器半/无 server 路由）。 */
export const inject = ['tools', 'skills']

/**
 * 装配插件。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 */
export function apply(ctx) {
  registerTools(ctx)
  registerSkills(ctx)
}