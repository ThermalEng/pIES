/**
 * R5/R6 逐时适配消费回归：17 条序列全量可达。
 *
 * 覆盖 `src/api/client.ts` results.hourly 对已封存 `hourly_refs.series`
 * 的直接类型化消费：声明列表有多少取多少（无静默截断），每条的值、
 * 身份、单位与桩给定的真实内容一致；第 17 条必须真实返回。
 * fetch 用桩代替（node22 原生 Response/fetch），业务值全为桩内真实数组，
 * 不 mock 成功语义本身之外的任何行为。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/hc/api && cp /app/src/api/client.ts /app/src/api/hourlyPages.ts /tmp/hc/api/ &&
 *     cp /app/src/types.ts /tmp/hc/types.ts && cp /app/tests/hourly_consume.test.mjs /tmp/hc/ &&
 *     sed -i \"s#'../src/api/client.ts'#'./api/client.ts'#\" /tmp/hc/hourly_consume.test.mjs &&
 *     sed -i \"s#from '../types'#from '../types.ts'#; s#from './hourlyPages'#from './hourlyPages.ts'#\" /tmp/hc/api/client.ts &&
 *     node /tmp/hc/hourly_consume.test.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀，逻辑与仓库文件一致；
 * client.ts 顶层无浏览器副作用。）
 */

import { api } from '../src/api/client.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

// 17 条公开序列：身份/单位各异，值各异（含正/零/负）。
const SERIES = Array.from({ length: 17 }, (_, i) => ({
  series_id: `inst${i}.var${i}`,
  instance_id: `inst${i}`,
  variable_id: `var${i}`,
  unit: i % 3 === 0 ? 'kW' : i % 3 === 1 ? 'kWh' : '1',
  values: Array.from({ length: 10 }, (_, t) => (i - 8) * 100 + t * (i % 2 === 0 ? 1 : -1)),
}))
const ROWS = 10
const requestedFields = []

const byId = new Map(SERIES.map((s) => [s.series_id, s]))

globalThis.fetch = async (url) => {
  const text = String(url)
  if (text.includes('/result/hourly')) {
    const query = new URL(text, 'http://x').searchParams
    const field = query.get('field')
    const start = Number(query.get('start') ?? 0)
    const end = Math.min(Number(query.get('end') ?? ROWS), ROWS)
    const limit = Math.max(1, Number(query.get('limit') ?? ROWS))
    requestedFields.push(field)
    const full = byId.get(field).values
    const chunkEnd = Math.min(end, start + limit)
    const body = {
      field,
      unit: byId.get(field).unit,
      instance_id: byId.get(field).instance_id,
      variable_id: byId.get(field).variable_id,
      start,
      end: chunkEnd,
      values: full.slice(start, chunkEnd),
      next_start: chunkEnd < end ? chunkEnd : null,
      total_rows: ROWS,
    }
    return new Response(JSON.stringify(body), { status: 200 })
  }
  if (text.endsWith('/result')) {
    return new Response(
      JSON.stringify({
        result: {
          hourly_refs: [{
            object_id: 7,
            fields: SERIES.map((s) => s.series_id),
            rows: ROWS,
            series: SERIES.map(({ series_id, instance_id, variable_id, unit }) => ({
              series_id, instance_id, variable_id, unit,
            })),
          }],
        },
      }),
      { status: 200 },
    )
  }
  return new Response('{"error": "unexpected "}', { status: 500 })
}

const out = await api.results.hourly(7, 9)

check('17 条全部返回', Object.keys(out.flows).length === 17, String(Object.keys(out.flows).length))
check('请求覆盖全部声明', new Set(requestedFields).size === 17, JSON.stringify(requestedFields))
const last = SERIES[16]
check(
  '第17条真实值不丢',
  JSON.stringify(out.flows[last.series_id]) === JSON.stringify(last.values),
)
let identitiesOk = true
for (const s of SERIES) {
  const meta = out.series.find((m) => m.series_id === s.series_id)
  if (!meta || meta.instance_id !== s.instance_id || meta.variable_id !== s.variable_id || meta.unit !== s.unit) {
    identitiesOk = false
    break
  }
  if (JSON.stringify(out.flows[s.series_id]) !== JSON.stringify(s.values)) {
    identitiesOk = false
    break
  }
}
check('17 条值与身份单位一致', identitiesOk)

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all hourly consume checks passed')
}
