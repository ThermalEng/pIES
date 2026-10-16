/**
 * 计算配置/校验 feature 公开入口定向测试(无浏览器依赖)。
 *
 * 覆盖 `src/features/config` 与 `src/features/validation` 公开入口:
 * 1. 配置信封 {calculation_config, revision} 精确消费;
 * 2. 校验报告 {report, stored} 精确消费(blocks_submit 口径、诊断数组直通);
 * 3. 基准确认只消费 confirmed(确认人/时间声明备查, 不伪装诊断)。
 * 不测试畸形信封: 可信内部交接无运行时守卫, 缺席自然抛错, 不设重复校验。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把公开入口打包为 CJS 后
 * 直接调用(与 tests/modeling_mappers.test.mjs 同一模式)。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild src/features/config/index.ts --bundle --format=cjs \
 *     --outfile=/tmp/b1-config.cjs
 *   npx esbuild src/features/validation/index.ts --bundle --format=cjs \
 *     --outfile=/tmp/b1-validation.cjs
 *   node tests/config_validation_mappers.test.mjs
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const cfg = require('/tmp/b1-config.cjs')
const val = require('/tmp/b1-validation.cjs')

// 1. 配置信封
{
  const body = { mode: 'planning', generator: 'g', solver: 'highs' }
  const c = cfg.configFromServer({ calculation_config: body, revision: 3 }, 4)
  check('配置信封直读', c.project_id === 4 && c.revision === 3 && c.calculation_config === body)
}

// 2. 校验报告
{
  const diag = {
    code: 'DATA-VAL-001',
    message_key: 'ies.diag.data.row',
    severity: 'blocking',
    blocking: true,
    params: {},
    location: null,
    fix_hint_key: '',
    ref_ids: [],
    occurred_at: '2026-10-16T10:00:00Z',
    source: 'validation',
    trace_id: '',
    project_id: '4',
    task_id: '',
    suppressed: false,
  }
  const blocked = val.validationResultFromServer({ report: { status: 'blocked', blocks_submit: true, diagnostics: [diag] }, stored: true })
  check('阻断口径', blocked.valid === false && blocked.diagnostics.length === 1 && blocked.diagnostics[0] === diag)
  const ok = val.validationResultFromServer({ report: { status: 'ok', blocks_submit: false, diagnostics: [] }, stored: true })
  check('通过口径', ok.valid === true && ok.diagnostics.length === 0)
}

// 3. 基准确认
{
  const r = val.baselineConfirmFromServer({ confirmed: true, confirmed_by: 3, confirmed_at: '2026-10-16T10:00:00Z' })
  check('确认映射', r.confirmed === true && Array.isArray(r.diagnostics) && r.diagnostics.length === 0)
  const n = val.baselineConfirmFromServer({ confirmed: false, confirmed_by: null, confirmed_at: null })
  check('未确认映射', n.confirmed === false)
}

if (failures > 0) {
  console.error(`FAILURES: ${failures}`)
  process.exit(1)
}
console.log('ALL CONFIG/VALIDATION MAPPER CHECKS PASSED')
