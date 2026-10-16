/**
 * 计算配置/校验 feature 纯 mapper 定向测试(无浏览器依赖)。
 *
 * 覆盖 `src/features/config/mappers.ts` 与 `src/features/validation/mappers.ts`:
 * 1. 配置信封 {calculation_config, revision} 精确消费(缺 revision/非对象正文抛错);
 * 2. 校验报告 {report, stored} 精确消费(blocks_submit 口径、诊断数组直通);
 * 3. 基准确认只消费 confirmed(确认人/时间声明备查, 不伪装诊断)。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/cvb1 &&
 *     cp /app/src/features/config/mappers.ts /tmp/cvb1/config-mappers.ts &&
 *     cp /app/src/features/config/contracts.ts /tmp/cvb1/config-contracts.ts &&
 *     cp /app/src/features/validation/mappers.ts /tmp/cvb1/validation-mappers.ts &&
 *     cp /app/src/features/validation/contracts.ts /tmp/cvb1/validation-contracts.ts &&
 *     cp /app/src/types.ts /tmp/cvb1/types.ts &&
 *     cp /app/tests/config_validation_mappers.test.mjs /tmp/cvb1/t.mjs &&
 *     sed -i \"s#from './contracts'#from './config-contracts.ts'#; s#from '../../types'#from './types.ts'#\" /tmp/cvb1/config-mappers.ts &&
 *     sed -i \"s#from './contracts'#from './validation-contracts.ts'#; s#from '../../types'#from './types.ts'#\" /tmp/cvb1/validation-mappers.ts &&
 *     sed -i \"s#'../src/features/config/mappers.ts'#'./config-mappers.ts'#; s#'../src/features/validation/mappers.ts'#'./validation-mappers.ts'#\" /tmp/cvb1/t.mjs &&
 *     node /tmp/cvb1/t.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀并消歧同名文件，逻辑与仓库文件一致。）
 */

import { configFromServer } from '../src/features/config/mappers.ts'
import { baselineConfirmFromServer, validationResultFromServer } from '../src/features/validation/mappers.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}
function throws(name, fn) {
  let ok = false
  try {
    fn()
  } catch {
    ok = true
  }
  check(name, ok)
}

// 1. 配置信封
{
  const body = { mode: 'planning', generator: 'g', solver: 'highs' }
  const c = configFromServer({ calculation_config: body, revision: 3 }, 4)
  check('配置信封直读', c.project_id === 4 && c.revision === 3 && c.calculation_config === body)
  throws('配置缺 revision 抛错', () => configFromServer({ calculation_config: body }, 4))
  throws('配置正文非对象抛错', () => configFromServer({ calculation_config: null, revision: 1 }, 4))
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
  const blocked = validationResultFromServer({ report: { status: 'blocked', blocks_submit: true, diagnostics: [diag] }, stored: true })
  check('阻断口径', blocked.valid === false && blocked.diagnostics.length === 1 && blocked.diagnostics[0] === diag)
  const ok = validationResultFromServer({ report: { status: 'ok', blocks_submit: false, diagnostics: [] }, stored: true })
  check('通过口径', ok.valid === true && ok.diagnostics.length === 0)
}

// 3. 基准确认
{
  const r = baselineConfirmFromServer({ confirmed: true, confirmed_by: 3, confirmed_at: '2026-10-16T10:00:00Z' })
  check('确认映射', r.confirmed === true && Array.isArray(r.diagnostics) && r.diagnostics.length === 0)
  const n = baselineConfirmFromServer({ confirmed: false, confirmed_by: null, confirmed_at: null })
  check('未确认映射', n.confirmed === false)
}

if (failures > 0) {
  console.error(`FAILURES: ${failures}`)
  process.exit(1)
}
console.log('ALL CONFIG/VALIDATION MAPPER CHECKS PASSED')
