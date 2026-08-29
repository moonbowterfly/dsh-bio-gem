// test/smoke.js — dsh-bio-gem 回归冒烟（node 直测 Python 层，不依赖 dsh）
// 用法: node test/smoke.js [--skip-build]      （build 单测默认跑，耗时 ~70s）
// 断言（C58 回归锚）：
//   model_info  : 1084 基因 / 2492 反应
//   validate    : G1 PASS / G3 PASS 0.519981（AB 自然名介质）
//   gapfind     : 蔗糖缺口 L1 检出 EX_cpd00076_e0
//   gapfill     : 自动补洞 >=1 项，修复后蔗糖可生长
//   build      : protein.faa -> M9 G3 PASS（growth>0）[--skip-build 可跳过]
import { spawn } from 'node:child_process'
import { existsSync, writeFileSync, mkdtempSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'

const PY = process.env.GEM_PYTHON || 'C:/Users/shuai/miniconda3/python.exe'
const REPO = join(fileURLToPath(new URL('.', import.meta.url)), '..')
const PYDIR = join(REPO, 'python')
const C58 = 'F:/A_NGJ plan/Zcode/models/gapseq_C58/C58.xml'
const C58P1 = 'F:/A_NGJ plan/Zcode/models/gapseq_C58/C58_P1.xml'
const FAA = 'D:/Program/hermes/temp/gem_build_test/C58_protein.faa'

const AB = {
  'D-Glucose': -5, 'NH3': -10, 'O2': -12.5, 'CO2': -15, 'H+': -20, 'H2O': -100,
  'Phosphate': -10, 'Sulfate': -10, 'Cl-': -10, 'Mn2+': -10, 'Zn2+': -10, 'Co2+': -10,
  'Ni2+': -1, 'Fe3+': -0.1, 'Fe2+': -10, 'Ca2+': -10, 'Cu2+': -10, 'K+': -10,
  'Mg2+': -10, 'Na+': -10,
}

let passed = 0
let failed = 0
function check(name, cond, detail = '') {
  if (cond) { passed++; console.log(`  ✅ ${name}`) }
  else { failed++; console.log(`  ❌ ${name} ${detail}`) }
}

function runPy(script, payload, viaFile = false) {
  return new Promise((resolve, reject) => {
    const pf = join(tmpdir(), `gem-smoke-${Date.now()}-${Math.random().toString(36).slice(2, 6)}.json`)
    const args = ['-I', join(PYDIR, script)]
    if (viaFile) {
      writeFileSync(pf, JSON.stringify(payload))
      args.push(pf)
    }
    const cp = spawn(PY, args, { cwd: PYDIR, windowsHide: true })
    let out = ''
    let err = ''
    cp.stdout.on('data', (d) => { out += d })
    cp.stderr.on('data', (d) => { err += d })
    cp.on('close', (code) => {
      if (err.includes('Traceback')) return reject(new Error(err.slice(-500)))
      const txt = out.trim()
      let parsed = null
      try {
        parsed = JSON.parse(txt)          // 整段（gapfind/gapfill 多行 indent JSON）
      } catch {
        const lines = txt.split(/\r?\n/).filter(Boolean)
        try {
          parsed = JSON.parse(lines[lines.length - 1])  // 单行 bridge 协议
        } catch (e) {
          return reject(new Error(`bad JSON from ${script}: ${txt.slice(-300)}`))
        }
      }
      resolve(parsed)
    })
    cp.on('error', reject)
    if (!viaFile) {
      cp.stdin.write(JSON.stringify(payload))
      cp.stdin.end()
    }
  })
}

async function main() {
  const skipBuild = process.argv.includes('--skip-build')
  console.log('dsh-bio-gem smoke (C58 回归锚)')

  // 1) model_info —— 注意：断言目标为 C58_P1.xml（P1 补洞版，2492 反应）；VM系 C58.xml 为 2485（见 zcode 验证报告 D7）
  const mi = await runPy('gem_ops.py', { op: 'model_info', args: { model: C58P1 } })
  check('model_info: 1084 基因', mi?.result?.genes === 1084, `got ${mi?.result?.genes}`)
  check('model_info: 2492 反应', mi?.result?.reactions === 2492, `got ${mi?.result?.reactions}`)
  check('model_info: 4 复制子', Object.keys(mi?.result?.replicons ?? {}).length === 4,
    JSON.stringify(mi?.result?.replicons))

  // 2) validate
  const v = await runPy('gem_ops.py', { op: 'validate', args: { model: C58, medium: AB, reference_growth: 0.519981 } })
  const g3 = v?.result?.g3 ?? {}
  check('validate: G1 PASS', v?.result?.g1?.status === 'PASS', v?.result?.g1?.status)
  check('validate: G3 PASS + 0.519981', g3.status === 'PASS' && Math.abs(g3.growth_medium - 0.519981) < 1e-4,
    `${g3.status} ${g3.growth_medium}`)
  check('validate: 无碳源 = 0', Math.abs(g3.growth_no_carbon) < 1e-6, g3.growth_no_carbon)

  // 3) gapfind（蔗糖缺口）— viaFile（gapfind 读 argv 文件）
  const gf = await runPy('gapfind.py', { model: C58, medium: { ...AB, Sucrose: -10 }, substrates: ['Sucrose'] }, true)
  const l1missing = (gf?.L1 ?? []).some((x) => x.exchange === 'EX_cpd00076_e0' || x.type === 'exchange_unresolved_name' || x.type === 'exchange_missing_name')
  check('gapfind: 蔗糖 L1 缺口检出', l1missing, JSON.stringify(gf?.L1))

  // 3b) 跨引擎介质解析护栏：O2 不得误配到 Acetoin（2026-08-29 回归：子串回退防误伤）
  const cev = await runPy('gapfind.py', {
    model: 'D:/Program/hermes/temp/gem_build_test/C58_carveme_test.xml',
    medium: { medium_name: 'AB' },
  }, true)
  const resolvedIds = Object.keys((cev?._debug?.resolved) || {})
  const usesTempModel = existsSync('D:/Program/hermes/temp/gem_build_test/C58_carveme_test.xml')
  if (usesTempModel) {
    const rx = cev?.resolved_exchanges ?? []
    check('解析护栏: 含 EX_o2_e', rx.includes('EX_o2_e'), JSON.stringify(rx))
    check('解析护栏: 不含 EX_actn__R_e（O2 误配回归）', !rx.includes('EX_actn__R_e'), JSON.stringify(rx))
    check('解析护栏: medium_unresolved 空', (cev?.medium_unresolved ?? []).length === 0,
      JSON.stringify(cev?.medium_unresolved))
  } else {
    console.log('  ⚠️ 跳过 CarveMe 解析护栏（临时模型不存在）')
  }

  // 4) gapfill（补洞 + 修复后生长验证）
  const tmp = mkdtempSync(join(tmpdir(), 'gem-smoke-'))
  const out = join(tmp, 'gf.xml')
  const gfill = await runPy('gapfill.py', { model: C58, medium: { ...AB, Sucrose: -10 }, out }, true)
  const applied = (gfill?.applied ?? []).length
  check('gapfill: 自动补洞 >=1', applied >= 1, `applied=${applied}`)
  if (gfill?.out && existsSync(gfill.out)) {
    const v2 = await runPy('gem_ops.py', { op: 'validate', args: { model: gfill.out, medium: { ...AB, Sucrose: -10 } } })
    const g3b = v2?.result?.g3 ?? {}
    check('gapfill 后: 蔗糖介质 G3 PASS', g3b.status === 'PASS' && g3b.growth_medium > 0.1,
      `${g3b.status} ${g3b.growth_medium}`)
    check('gapfill 后: 蔗糖生长≈0.97077', Math.abs(g3b.growth_medium - 0.97077) < 0.01,
      `${g3b.growth_medium}`)
  }

  // 5) build（可用 --skip-build 跳过；完整 ~30-80s）
  if (!skipBuild && existsSync(FAA)) {
    const tmp2 = mkdtempSync(join(tmpdir(), 'gem-smoke-build-'))
    const pr = join(tmp2, 'progress.jsonl')
    const b = await new Promise((resolve, reject) => {
      const cp = spawn(PY, ['-u', join(PYDIR, 'build.py'), '--input', FAA, '--name', 'smoke_test',
        '--out-dir', tmp2, '--progress', pr])
      let out = ''
      let err = ''
      cp.stdout.on('data', (d) => { out += d })
      cp.stderr.on('data', (d) => { err += d })
      cp.on('close', (code) => {
        try {
          const lines = out.trim().split(/\r?\n/).filter(Boolean)
          resolve(JSON.parse(lines[lines.length - 1]))
        } catch (e) { reject(new Error(`build parse fail: ${out.slice(-300)} ${err.slice(-300)}`)) }
      })
      cp.on('error', reject)
    })
    const r = b?.result
    check('build: M9 G3 PASS', r?.validations_m9?.g3 === 'PASS', JSON.stringify(r?.validations_m9))
    check('build: M9 growth > 0', (r?.growth_g3_m9 ?? 0) > 0, r?.growth_g3_m9)
    check('build: 模型卡生成', !!r?.card, r?.card)
  } else if (!skipBuild) {
    console.log('  ⚠️ build 跳过：C58 protein.faa 不存在')
  }

  // 6) l3_fix（B' 后半）：op 协议 + 防过补第五闸门 + 工具注册计数（不跑 L3 MILP，保持冒烟秒级）
  const l3p = await runPy('gem_ops.py', { op: 'l3_fix', args: {} })
  check('l3_fix: op 协议（缺 model 明确报错）',
    l3p?.ok === false && /model file not found/.test(l3p?.error || ''), JSON.stringify(l3p))
  const bg = await runPy('budget.py', { n_reactions: 2485, prior_added: 123, planned: 2 })
  check('l3_fix: 防过补第五闸门（124 预算超限 confirm_required）',
    bg?.error === 'budget_exceeded' && bg?.confirm_required === true, JSON.stringify(bg))
  const toolsSrc = readFileSync(join(REPO, 'src', 'tools.js'), 'utf8')
  const nReg = (toolsSrc.match(/ctx\.tools\.register\(/g) || []).length
  check('tools: 15 个语义化工具注册', nReg === 15, `got ${nReg}`)

  // 7) Q2 工程质量件：SBML 往返保真（GPR 防丢）+ 模型卡 v2 selftest
  const rt = await runPy('roundtrip_check.py', { model: C58 })
  check('往返保真: 计数一致 + GPR 无丢失（≥5 复合 GPR 精确对比）',
    rt?.ok === true && rt?.gpr_diffs === 0 && (rt?.gpr_compared ?? 0) >= 5,
    JSON.stringify(rt?.counts) + ` gpr=${rt?.gpr_total} compared=${rt?.gpr_compared} diffs=${rt?.gpr_diffs}`)
  const cardOk = await new Promise((resolve, reject) => {
    const cp = spawn(PY, ['-I', join(PYDIR, 'model_card.py'), '--selftest'], { cwd: PYDIR, windowsHide: true })
    let o = ''
    cp.stdout.on('data', (d) => { o += d })
    cp.on('close', () => {
      try { resolve(JSON.parse(o.trim().split(/\r?\n/).filter(Boolean).pop())) }
      catch (e) { reject(new Error('card selftest parse fail: ' + o.slice(-200))) }
    })
    cp.on('error', reject)
  })
  check('模型卡 v2: selftest（init/append 版本递增/legacy 迁移/phenotype/essential 形状）',
    cardOk?.ok === true && cardOk?.result?.selftest === 'pass', JSON.stringify(cardOk))

  // 8) M1 fluxscan：区间分离判定单测（锁定公式）+ op 协议 + 真实单条件锚点（~35s）
  const fsSelf = await new Promise((resolve, reject) => {
    const cp = spawn(PY, ['-I', join(PYDIR, 'fluxscan.py'), '--selftest'], { cwd: PYDIR, windowsHide: true })
    let o = ''
    cp.stdout.on('data', (d) => { o += d })
    cp.on('close', () => {
      try { resolve(JSON.parse(o.trim().split(/\r?\n/).filter(Boolean).pop())) }
      catch (e) { reject(new Error('fluxscan selftest parse fail: ' + o.slice(-200))) }
    })
    cp.on('error', reject)
  })
  check('fluxscan: 区间分离判定单测（锁定公式 分离/重叠/零通量/负向/精确边界/容差）',
    fsSelf?.ok === true && fsSelf?.result?.selftest === 'pass' && (fsSelf?.result?.cases ?? 0) >= 10,
    JSON.stringify(fsSelf))
  const fsp = await runPy('gem_ops.py', { op: 'fluxscan', args: {} })
  check('fluxscan: op 协议（缺 model 明确报错）',
    fsp?.ok === false && /model file not found/.test(fsp?.error || ''), JSON.stringify(fsp))
  const fsx = await runPy('gem_ops.py', {
    op: 'fluxscan',
    args: { model: C58, conditions: [{ name: 'AB', medium: { medium_name: 'AB' } }] },
  })
  check('fluxscan: AB 单条件 growth 0.519981（区间制路径）',
    fsx?.result?.conditions?.[0] && Math.abs(fsx.result.conditions[0].growth - 0.519981) < 1e-9,
    JSON.stringify(fsx?.result?.conditions?.[0]))
  check('fluxscan: 输出口径声明（units + fraction 0.9999）',
    fsx?.result?.units === 'mmol/gDW/h' && fsx?.result?.fraction_of_optimum === 0.9999,
    JSON.stringify({ units: fsx?.result?.units, f: fsx?.result?.fraction_of_optimum }))

  // 9) M2 sensitivity：稳定性三分类 selftest + probe（GAM 载体定位，秒级）
  const sensSelf = await new Promise((resolve, reject) => {
    const cp = spawn(PY, ['-I', join(PYDIR, 'sensitivity.py'), '--selftest'], { cwd: PYDIR, windowsHide: true })
    let o = ''
    cp.stdout.on('data', (d) => { o += d })
    cp.on('close', () => {
      try { resolve(JSON.parse(o.trim().split(/\r?\n/).filter(Boolean).pop())) }
      catch (e) { reject(new Error('sensitivity selftest parse fail: ' + o.slice(-200))) }
    })
    cp.on('error', reject)
  })
  check('sensitivity: 稳定性三分类 selftest（always/conditional/never）',
    sensSelf?.ok === true && sensSelf?.result?.selftest === 'pass', JSON.stringify(sensSelf))
  const sensProbe = await runPy('gem_ops.py', {
    op: 'sensitivity', args: { model: C58, action: 'probe' },
  })
  check('sensitivity: probe 定位 GAM 载体（bio1 内部 stub，GAM_ORIG=40）',
    sensProbe?.result?.biomass_rxn === 'bio1' && sensProbe?.result?.carrier_type === 'inside_biomass'
    && Math.abs((sensProbe?.result?.gam_orig ?? 0) - 40.0) < 1e-6,
    JSON.stringify(sensProbe?.result))

  // 10) M3 ledger：幂等/容错 selftest + gem_report 账本摘要 e2e（临时账本，含坏行）
  const ledSelf = await new Promise((resolve, reject) => {
    const cp = spawn(PY, ['-I', join(PYDIR, 'ledger.py'), '--selftest'], { cwd: PYDIR, windowsHide: true })
    let o = ''
    cp.stdout.on('data', (d) => { o += d })
    cp.on('close', () => {
      try { resolve(JSON.parse(o.trim().split(/\r?\n/).filter(Boolean).pop())) }
      catch (e) { reject(new Error('ledger selftest parse fail: ' + o.slice(-200))) }
    })
    cp.on('error', reject)
  })
  check('ledger: selftest（登记/幂等/前缀 query/update/坏行容错）',
    ledSelf?.ok === true && ledSelf?.result?.selftest === 'pass', JSON.stringify(ledSelf))
  const tmpLedgerDir = mkdtempSync(join(tmpdir(), 'gem-smoke-ledger-'))
  const tmpLedger = join(tmpLedgerDir, 'predictions.jsonl')
  writeFileSync(tmpLedger, [
    JSON.stringify({ prediction_id: 'P0001', type: 'essentiality', content: 'g1 在 AB 培养基下必需',
      model: C58, condition: 'AB', status: 'unverified', evidence_tier: 'EVIDENCE_rule' }),
    JSON.stringify({ prediction_id: 'P0002', type: 'phenotype', content: '底物 X 预测生长',
      model: C58, condition: 'AB/sole', status: 'literature_supported', evidence_tier: 'EVIDENCE_literature' }),
    '{"corrupt line...\n',
  ].join('\n'))
  const miLedger = await runPy('gem_ops.py', { op: 'model_info', args: { model: C58P1, ledger_path: tmpLedger } })
  check('gem_report: ledger_summary（2 行 + by_status/by_type + corrupt 1 不阻塞）',
    miLedger?.result?.ledger_summary?.total === 2
    && miLedger?.result?.ledger_summary?.corrupt_rows === 1
    && miLedger?.result?.ledger_summary?.by_status?.unverified === 1
    && miLedger?.result?.ledger_summary?.by_status?.literature_supported === 1
    && typeof miLedger?.result?.ledger_context === 'string',
    JSON.stringify(miLedger?.result?.ledger_summary))
  const ledUpdate = await runPy('gem_ops.py', { op: 'ledger', args: { action: 'update',
    prediction_id: 'P0002', status: 'experimentally_verified', ledger_path: tmpLedger } })
  check('gem_ledger: op update（P0002 -> experimentally_verified）',
    ledUpdate?.ok === true && ledUpdate?.result?.row?.status === 'experimentally_verified',
    JSON.stringify(ledUpdate))
  const ledQuery = await runPy('gem_ops.py', { op: 'ledger', args: { action: 'query',
    status: 'experimentally_verified', ledger_path: tmpLedger } })
  check('gem_ledger: op query（更新后可过滤）',
    ledQuery?.ok === true && ledQuery?.result?.matched === 1, JSON.stringify(ledQuery?.result?.matched))

  console.log(`\n结果: ${passed} 通过 / ${failed} 失败`)
  process.exit(failed ? 1 : 0)
}

main().catch((e) => { console.error('smoke 异常:', e.message); process.exit(1) })