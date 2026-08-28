// dsh-bio-gem — Cordis 插件主模块
// 注入 tools（5 语义化工具：gem_report/validate/gapfind/gapfill/build）。
import { registerTools } from './tools.js'

/** Cordis 插件名（cordis.patch.yml row id 同名）。 */
export const name = 'dsh-bio-gem'

/** 需要的服务。M1 只注册工具（无浏览器半/无自带 skill/无 server 路由）。 */
export const inject = ['tools']

/**
 * 装配插件。
 * @param {import('@deepseek-ai/cordis').Context} ctx
 */
export function apply(ctx) {
  registerTools(ctx)
}