// skills.js — dsh-bio-gem skill 注册（M1：gem-expert 主 skill）
// 工具选择决策树 + 工作流 + 实测坑位；遵循 dsh-bio-genie 注册模式（ctx.skills.register）。
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SKILLS_DIR = join(dirname(fileURLToPath(import.meta.url)), '..', 'skills')

export function registerSkills(ctx) {
  const disposers = []
  let content = ''
  try {
    content = readFileSync(join(SKILLS_DIR, 'gem-expert.md'), 'utf8')
  } catch {
    content = `Skill body missing from plugin package (skills/gem-expert.md)。`
  }
  disposers.push(ctx.skills.register({
    name: 'gem-expert',
    description:
      '基因组尺度代谢模型（GEM）主指引：工具分层选择（gem_report/validate/gapfind/gapfill/build）、构建→验证→补洞→报告工作流、' +
      '跨引擎培养基自然名规则、CarveMe M9 介质边界、C58 回归锚。任何代谢模型需求先加载本 skill。',
    whenToUse:
      '用户提出代谢模型/GEM/基因组建模型/模型验证/模型补洞/为什么模型不长/FBA 模型准备/底盘代谢分析等需求时。',
    source: 'custom',
    provider: 'dsh-bio-gem',
    content,
  }))
  return () => disposers.forEach((d) => d())
}