/**
 * R3 逐时缺席/失败语义定向测试。
 *
 * 覆盖 `src/api/client.ts` results.hourly 的整组失败与合法缺席区分：
 * 1. `hourly_refs: null` → `{absent:true}`，不发任何 `/result/hourly` 请求；
 * 2. `hourly_refs: []` → `{absent:true}`，不发分页请求；
 * 3. 引用存在但 `series: []`（空集合）→ `{absent:true}`；
 * 4. 引用存在、单序列分页 500 → 直接 reject（不返回空 flows 缩水成功）；
 * 5. 引用存在、两序列中第二序列第二页 500 → 直接 reject（无部分 flows）；
 * 6. 引用存在、对象缺失导致分页 404 → 直接 reject（不误判为缺席）；
 * 7. 引用存在、两序列成功 → `{absent:false}` 且值与身份完整。
 * fetch 用桩代替（node22 原生 Response/fetch），业务值全为桩内真实数组，
 * 不 mock 成功语义本身之外的任何行为。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/ha/api && cp /app/src/api/client.ts /app/src/api/hourlyPages.ts /tmp/ha/api/ &&
 *     cp /app/src/types.ts /tmp/ha/types.ts && cp /app/tests/hourly_absence.test.mjs /tmp/ha/ &&
 *     sed -i \"s#'../src/api/client.ts'#'./api/client.ts'#\" /tmp/ha/hourly_absence.test.mjs &&
 *     sed -i \"s#from '../types'#from '../types.ts'#; s#from './hourlyPages'#from './hourlyPages.ts'#\" /tmp/ha/api/client.ts &&
 *     node /tmp/ha/hourly_absence.test.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀，逻辑与仓库文件一致；
 * client.ts 顶层无浏览器副作用。）
 */

import { api } from '../src/api/client.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const SERIES = [
  { series_id: 'a.x', instance_id: 'a', variable_id: 'x', unit: 'kW', values: [1.5, -2, 0, 3.25] },
  { series_id: 'b.y', instance_id: 'b', variable_id: 'y', unit: 'kWh', values: [10, 20, 30, 40] },
]
const ROWS = 4

/** 桩场景：缺席形态 + 分页失败注入（failAt: {field, start} 命中即定值失败）。 */
function installStub({ refs, failAt = null, missingObject = false }) {
  const pageCalls = []
  const byId = new Map(SERIES.map((s) => [s.series_id, s]))
  globalThis.fetch = async (url) => {
    const text = String(url)
    if (text.includes('/result/hourly')) {
      const query = new URL(text, 'http://x').searchParams
      const field = query.get('field')
      const start = Number(query.get('start') ?? 0)
      pageCalls.push({ field, start })
      if (missingObject) return new Response('{"detail":"missing"}', { status: 404 })
      if (failAt && failAt.field === field && failAt.start === start) {
        return new Response('{"detail":"boom"}', { status: 500 })
      }
      // 按后端 MAX_HOURLY_LIMIT 语义截断，迫使客户端走 next_start 多页。
      const PAGE_MAX = 2
      const full = byId.get(field).values
      const eff = Math.min(Math.max(1, Number(query.get('limit') ?? ROWS)), PAGE_MAX)
      const end = Math.min(ROWS, start + eff)
      return new Response(JSON.stringify({
        field, start, end, values: full.slice(start, end),
        next_start: end < ROWS ? end : null, total_rows: ROWS,
      }), { status: 200 })
    }
    if (text.endsWith('/result')) {
      return new Response(JSON.stringify({ result: { hourly_refs: refs } }), { status: 200 })
    }
    return new Response('{"error":"unexpected"}', { status: 500 })
  }
  return pageCalls
}

function refOf(series = SERIES) {
  return [{
    object_id: 7,
    fields: series.map((s) => s.series_id),
    rows: ROWS,
    series: series.map(({ series_id, instance_id, variable_id, unit }) => ({
      series_id, instance_id, variable_id, unit,
    })),
  }]
}

async function expectReject(name, fn) {
  try {
    await fn()
  } catch {
    check(name, true)
    return
  }
  check(name, false, '未抛错')
}

// 1. null 引用 → 缺席，不发分页请求。
{
  const calls = installStub({ refs: null })
  const out = await api.results.hourly(7, 9)
  check('null引用判缺席', out.absent === true, JSON.stringify(out))
  check('缺席不发分页请求', calls.length === 0, String(calls.length))
}

// 2. 空数组引用 → 缺席。
{
  const calls = installStub({ refs: [] })
  const out = await api.results.hourly(11, 3)
  check('空数组引用判缺席', out.absent === true, JSON.stringify(out))
  check('空数组不发分页请求', calls.length === 0, String(calls.length))
}

// 3. 空序列集合 → 缺席。
{
  const calls = installStub({ refs: refOf([]) })
  const out = await api.results.hourly(11, 4)
  check('空序列集合判缺席', out.absent === true, JSON.stringify(out))
  check('空序列不发分页请求', calls.length === 0, String(calls.length))
}

// 4. 单序列分页失败 → 整组 reject，不缩水。
{
  installStub({ refs: refOf([SERIES[0]]), failAt: { field: 'a.x', start: 0 } })
  await expectReject('单序列失败整组抛错', () => api.results.hourly(7, 10))
}

// 5. 第二序列后续页失败 → 整组 reject，无部分成功。
{
  installStub({ refs: refOf(), failAt: { field: 'b.y', start: 2 } })
  // 首轮两序列各发 start=0；b.y 第二页 start=2 失败。
  await expectReject('后续页失败整组抛错', () => api.results.hourly(7, 11))
}

// 6. 对象缺失 404 → 读取失败，不误判缺席。
{
  installStub({ refs: refOf(), missingObject: true })
  await expectReject('对象缺失404判失败非缺席', () => api.results.hourly(7, 12))
}

// 7. 成功 → 非缺席且值身份完整。
{
  installStub({ refs: refOf() })
  const out = await api.results.hourly(7, 13)
  check('成功非缺席', out.absent === false, JSON.stringify(out.absent))
  check(
    '成功值身份完整',
    out.absent === false
    && JSON.stringify(out.flows['a.x']) === JSON.stringify([1.5, -2, 0, 3.25])
    && JSON.stringify(out.flows['b.y']) === JSON.stringify([10, 20, 30, 40])
    && out.series.length === 2,
  )
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all hourly absence checks passed')
}
