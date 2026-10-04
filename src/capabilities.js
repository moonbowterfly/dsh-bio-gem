// dsh-bio-gem — capabilities 单源（single source of truth for tool manifest & capability metadata）
//
// 目的（2026-09-21 设计共识）：
//   1. 工具清单/能力分级/成本与副作用元数据集中一处，供 integration API（/v1/capabilities）
//      与 genie 宿主侧动态消费——消灭「工具数变化需同步 ≥12 处」的手工漂移。
//   2. 校验脚本 scripts/check-capabilities.mjs 强制本 MANIFEST 与 tools.js 真实注册集合一致；
//      文档计数（README/persona/面板）另有 count-audit 校验。
//
// 同步纪律：新增/删除工具时同步本文件（对应 category / capability / cost_class / network /
// mutability / summary），否则 check-capabilities 会报红。
//
// cost_class:  light  (<10s)  | medium (10s–2min) | heavy (>2min)
// network:     none | optional | required
// mutability:  read_only | writes_output | model_mutating
// status:      ready | experimental | data-not-initialized  （依赖级状态由 integration 动态覆盖为 unavailable / unknown）
// requires:    动态依赖 id 列表（对齐 integration status 的 check id；缺省即 ready）

export const TOOLS_MANIFEST = [
  // ---- build ----
  { name: 'gem_build', capability: 'gem.build.genome-to-model', category: 'build',
    cost_class: 'heavy', network: 'optional', mutability: 'writes_output', status: 'ready',
    requires: ['python.cobra', 'runtime.carveme'],
    summary: '基因组→GEM 构建（CarveMe 自动自举 / gapseq WSL 档）；后台 job + 进度' },
  { name: 'gem_annotate', capability: 'gem.build.annotate', category: 'build',
    cost_class: 'medium', network: 'none', mutability: 'writes_output', status: 'ready',
    summary: '基因组注释→蛋白 FASTA（官方优先 + pyrodigal 兜底，纯 Windows）' },
  { name: 'gem_gapseq', capability: 'gem.build.gapseq-wsl', category: 'build',
    cost_class: 'heavy', network: 'none', mutability: 'writes_output', status: 'experimental',
    requires: ['runtime.gapseq'],
    summary: 'gapseq 引擎原子四步（setup/launch/status/fetch，WSL2 依赖，实验性）' },

  // ---- validate ----
  { name: 'gem_validate', capability: 'gem.validate.six-gate', category: 'validate',
    cost_class: 'medium', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '六道验证关卡 G0-G6（加载/平衡/生长/表型/必需性/ATP 泄漏）' },
  { name: 'gem_quality', capability: 'gem.validate.quality-report', category: 'validate',
    cost_class: 'medium', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '模型质量报告（gem-qi-v1：blocked/环路/平衡/孤儿/覆盖/连通性 + 启发式聚合分）' },

  // ---- repair ----
  { name: 'gem_gapfind', capability: 'gem.repair.gap-diagnose', category: 'repair',
    cost_class: 'medium', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '缺口分级诊断 L1 缺交换 / L2 缺转运 / L3 内部路径' },
  { name: 'gem_gapfill', capability: 'gem.repair.gap-fill', category: 'repair',
    cost_class: 'medium', network: 'none', mutability: 'writes_output', status: 'ready',
    requires: ['python.cobra'],
    summary: 'L1/L2 规则级自动补洞（provenance 打标 + 防过补四闸门）' },
  { name: 'gem_l3_fix', capability: 'gem.repair.l3-pathway', category: 'repair',
    cost_class: 'heavy', network: 'none', mutability: 'writes_output', status: 'ready',
    requires: ['python.cobra'],
    summary: 'L3 内部路径补洞（白名单 + BiGG 反应式移植 + 证据分级 + G6 回滚）' },
  { name: 'gem_precursor_scan', capability: 'gem.repair.precursor-scan', category: 'repair',
    cost_class: 'medium', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '阻塞前体分析（不生长时逐前体移除测试定位阻塞点）' },
  { name: 'gem_phenotype', capability: 'gem.repair.phenotype-calibrate', category: 'repair',
    cost_class: 'heavy', network: 'none', mutability: 'writes_output', status: 'ready',
    requires: ['python.cobra'],
    summary: '表型回填迭代（Biolog/文献表校准 + L1/L2 自动修复 + 匹配率对比）' },
  { name: 'gem_biomass', capability: 'gem.repair.biomass-refine', category: 'repair',
    cost_class: 'heavy', network: 'none', mutability: 'writes_output', status: 'ready',
    requires: ['python.cobra'],
    summary: 'biomass 精修（inspect 组分对照 / apply 覆盖表 + 三联对照 + 回滚）' },

  // ---- analysis ----
  { name: 'gem_essentiality', capability: 'gem.analysis.essentiality', category: 'analysis',
    cost_class: 'heavy', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '全量必需基因扫描（FVA 预筛 + 手工敲除；带 GPR 覆盖警告）' },
  { name: 'gem_fluxscan', capability: 'gem.analysis.flux-interval', category: 'analysis',
    cost_class: 'heavy', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '通量区间制（FVA 区间 + pFBA 点值；条件对比只认区间分离）' },
  { name: 'gem_sensitivity', capability: 'gem.analysis.sensitivity', category: 'analysis',
    cost_class: 'heavy', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '结构性灵敏度（GAM×biomass 网格 + 稳定性三分类）' },
  { name: 'gem_secretion', capability: 'gem.analysis.secretion', category: 'analysis',
    cost_class: 'medium', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '可分泌代谢物谱（production envelope 扫描；纯拓扑边界声明）' },
  { name: 'gem_double_knockout', capability: 'gem.analysis.double-knockout', category: 'analysis',
    cost_class: 'heavy', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '双敲合成致死（GPR 穷尽先验 + 全扫预算）' },
  { name: 'gem_enrichment', capability: 'gem.analysis.enrichment', category: 'analysis',
    cost_class: 'light', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '必需基因通路富集（超几何 + BH FDR；通路源 = SBML groups）' },
  { name: 'gem_sample', capability: 'gem.analysis.sampling', category: 'analysis',
    cost_class: 'heavy', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '通量空间采样（ACHR/OptGP；growth_floor 受限空间；边界声明）' },

  // ---- export ----
  { name: 'gem_targets', capability: 'gem.export.targets', category: 'export',
    cost_class: 'light', network: 'none', mutability: 'writes_output', status: 'ready',
    summary: '靶点清单规范导出（11 字段锁定 schema；计数闭合）' },

  // ---- assets ----
  { name: 'gem_media_resolve', capability: 'gem.asset.medium-resolve', category: 'assets',
    cost_class: 'light', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '介质解析（自然名 → EX ID；跨引擎命名空间）' },
  { name: 'gem_ledger', capability: 'gem.asset.ledger', category: 'assets',
    cost_class: 'light', network: 'none', mutability: 'writes_output', status: 'ready',
    summary: '预测账本（list/query/update；幂等；基率追踪）' },
  { name: 'gem_benchmark', capability: 'gem.asset.benchmark', category: 'assets',
    cost_class: 'heavy', network: 'optional', mutability: 'writes_output', status: 'ready',
    requires: ['python.cobra'],
    summary: '两模型基准对比（六关并列/生长/必需性/表型/账本回填；支持 bigg: 下载）' },
  { name: 'gem_report', capability: 'gem.asset.report', category: 'assets',
    cost_class: 'light', network: 'none', mutability: 'read_only', status: 'ready',
    requires: ['python.cobra'],
    summary: '模型摘要 + 账本基率' },
]

export const CONTRACT_VERSION = '1'

/** 组装 capabilities 报告（静态 manifest + 动态依赖状态覆盖）。 */
export function buildCapabilitiesReport({ pluginVersion, checks = [] } = {}) {
  const checkStatus = new Map(checks.map((c) => [c.id, c.status]))
  const tools = TOOLS_MANIFEST.map((t) => {
    const required = t.requires ?? []
    // 三态与 status 层对齐：missing = 确认缺；warn = 探测未完成或进行中
    // （runtime.gapseq 在 WSL 冷启动期间就是 warn）；未出现在 checks = 未观测到。
    // warn 不可归入 missing——否则"还在探测"会被说成"gapseq 不可用"。
    const missingDeps = required.filter((id) => checkStatus.get(id) === 'missing')
    const unprobedDeps = required.filter((id) => {
      const st = checkStatus.get(id)
      return st !== undefined && st !== 'ok' && st !== 'missing'
    })
    const unknownDeps = required.filter((id) => !checkStatus.has(id))
    const effectiveStatus = missingDeps.length > 0 ? 'unavailable'
      : unprobedDeps.length > 0 ? 'unknown'
        : unknownDeps.length > 0 ? 'unknown' : (t.status ?? 'ready')
    return {
      name: t.name,
      capability: t.capability,
      category: t.category,
      cost_class: t.cost_class,
      network: t.network,
      mutability: t.mutability,
      status: effectiveStatus,
      summary: t.summary,
      ...(t.requires ? { requires: t.requires } : {}),
      ...(missingDeps.length > 0 ? { missing_dependencies: missingDeps } : {}),
      // 未观测到（check缺席）与探测未完成（warn）都归入 unknown_dependencies：
      // 两者都无法证明依赖缺失，前端据此提示"待确认"而非"不可用"。
      ...(unknownDeps.length + unprobedDeps.length > 0
        ? { unknown_dependencies: [...unprobedDeps, ...unknownDeps] }
        : {}),
    }
  })
  return {
    contract_version: CONTRACT_VERSION,
    plugin_id: 'dsh-bio-gem',
    plugin_version: pluginVersion,
    tool_count: tools.length,
    tools,
  }
}
