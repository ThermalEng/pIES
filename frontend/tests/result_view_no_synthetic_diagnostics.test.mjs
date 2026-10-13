/**
 * R1/R2 结果视图无合成诊断定向测试。
 *
 * 覆盖 `src/api/client.ts` results.result 的读取语义：
 * 1. 后端结果视图不带 diagnostics 字段时，适配层不得合成 `diagnostics: []`
 *    伪装“无诊断”；返回对象无该键。
 * 2. 有封存财务块时，financial 原样透传（含 evaluation.irr_status），
 *    financialAbsence 为 null。
 * 3. 无封存财务块时，financial 为 null 且 financialAbsence 原样透传，
 *    同样不合成 diagnostics。
 * fetch 用桩代替（node22 原生 Response/fetch），业务值全为桩内真实内容，
 * 不 mock 成功语义本身之外的任何行为。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/rv/api && cp /app/src/api/client.ts /tmp/rv/api/ &&
 *     cp /app/src/types.ts /tmp/rv/types.ts && cp /app/tests/result_view_no_synthetic_diagnostics.test.mjs /tmp/rv/ &&
 *     sed -i \"s#'../src/api/client.ts'#'./api/client.ts'#\" /tmp/rv/result_view_no_synthetic_diagnostics.test.mjs &&
 *     sed -i \"s#from '../types'#from '../types.ts'#\" /tmp/rv/api/client.ts &&
 *     node /tmp/rv/result_view_no_synthetic_diagnostics.test.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀，逻辑与仓库文件一致；
 * client.ts 顶层无浏览器副作用。）
 */

import { api } from '../src/api/client.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

/** 桩：结果视图正文由调用方给定；注意正文里没有 diagnostics 键。 */
function installStub(resultBody) {
  globalThis.fetch = async (url) => {
    const text = String(url)
    if (text.includes('/tasks/7/result')) {
      return new Response(JSON.stringify({ result: resultBody }), { status: 200 })
    }
    return new Response('{"error":"unexpected"}', { status: 500 })
  }
}

// 1. 有封存财务块：financial 原样透传，不合成 diagnostics。
{
  installStub({
    evidence_status: 'available',
    evidence: { id: 26, object_id: 99, status: 'complete' },
    metrics_summary: {},
    financial_evaluation: {
      evaluation: { irr_status: 'normal' },
    },
    financial_absence: null,
    candidates: [],
    planning_solution: null,
    snapshot_refs: null,
  })
  const res = await api.results.result(5, 7)
  check('结果视图无diagnostics合成键', !('diagnostics' in res), Object.keys(res).join(','))
  check(
    '封存irr_status原样透传',
    res.financial?.evaluation?.irr_status === 'normal',
    JSON.stringify(res.financial),
  )
  check('有块时缺席为null', res.financialAbsence === null, String(res.financialAbsence))
}

// 2. 无封存财务块：缺席原因透传，同样不合成 diagnostics。
{
  installStub({
    evidence_status: 'available',
    evidence: { id: 27, object_id: 100, status: 'complete' },
    metrics_summary: {},
    financial_evaluation: null,
    financial_absence: 'solution_not_requested',
    candidates: [],
    planning_solution: null,
    snapshot_refs: null,
  })
  const res = await api.results.result(5, 7)
  check('无块时亦无diagnostics合成键', !('diagnostics' in res), Object.keys(res).join(','))
  check('无块时financial为null', res.financial === null, String(res.financial))
  check(
    '缺席原因原样透传',
    res.financialAbsence === 'solution_not_requested',
    String(res.financialAbsence),
  )
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all result view no-synthetic-diagnostics checks passed')
}
