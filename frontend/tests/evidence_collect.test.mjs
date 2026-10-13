/**
 * R4 证据收集失败可见定向测试。
 *
 * 覆盖 `src/api/client.ts` tasks.get 的补充 /result 读取语义：
 * 1. 任务详情 GET 失败 → 直接 reject（不返回无证据详情）；
 * 2. result_available=true 但 /result 补充读取失败 → 直接 reject，
 *    不吞错置空 evidence 伪装详情正常无结果；
 * 3. result_available=false → 成功返回且 evidence 为空，不发任何 /result 请求；
 * 4. 成功 → evidence 包身份（package_id/object_id/status）与桩完全一致。
 * fetch 用桩代替（node22 原生 Response/fetch），业务值全为桩内真实内容，
 * 不 mock 成功语义本身之外的任何行为。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/ec/api && cp /app/src/api/client.ts /app/src/api/hourlyPages.ts /tmp/ec/api/ &&
 *     cp /app/src/types.ts /tmp/ec/types.ts && cp /app/tests/evidence_collect.test.mjs /tmp/ec/ &&
 *     sed -i \"s#'../src/api/client.ts'#'./api/client.ts'#\" /tmp/ec/evidence_collect.test.mjs &&
 *     sed -i \"s#from '../types'#from '../types.ts'#; s#from './hourlyPages'#from './hourlyPages.ts'#\" /tmp/ec/api/client.ts &&
 *     node /tmp/ec/evidence_collect.test.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀，逻辑与仓库文件一致；
 * client.ts 顶层无浏览器副作用。）
 */

import { api } from '../src/api/client.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

function taskBody({ id, resultAvailable }) {
  return {
    task: {
      id,
      project_id: 5,
      type: 'optimization',
      status: 'completed',
      business_outcome: 'normal_completion',
      idempotency_key: `k-${id}`,
      calc_snapshot_id: 3,
      requested_by: 1,
      requested_at: '2026-10-13T00:00:00Z',
      result_available: resultAvailable,
      attempts: [],
      progress: null,
      batch: null,
      outcome_note: null,
      diagnostics: [],
      evidence: [],
    },
  }
}

/** 桩：按任务 id 决定详情/结果行为；记录 /result 请求次数。 */
function installStub({ detailStatus = 200, resultAvailable = true, resultStatus = 200 }) {
  let resultCalls = 0
  globalThis.fetch = async (url) => {
    const text = String(url)
    if (text.includes('/tasks/7/result')) {
      resultCalls++
      if (resultStatus !== 200) return new Response('{"detail":"boom"}', { status: resultStatus })
      return new Response(JSON.stringify({
        result: { evidence: { id: 26, object_id: 99, status: 'complete' } },
      }), { status: 200 })
    }
    if (text.endsWith('/tasks/7')) {
      if (detailStatus !== 200) return new Response('{"detail":"boom"}', { status: detailStatus })
      return new Response(JSON.stringify(taskBody({ id: 7, resultAvailable })), { status: 200 })
    }
    return new Response('{"error":"unexpected"}', { status: 500 })
  }
  return () => resultCalls
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

// 1. 详情 GET 失败 → reject。
{
  installStub({ detailStatus: 500 })
  await expectReject('详情失败直接抛错', () => api.tasks.get(5, 7))
}

// 2. /result 补充读取失败 → reject，不置空伪装。
{
  installStub({ resultAvailable: true, resultStatus: 500 })
  await expectReject('可用时result失败直接抛错', () => api.tasks.get(5, 7))
}

// 3. result_available=false → 成功、无证据、零 /result 请求。
{
  const calls = installStub({ resultAvailable: false })
  const d = await api.tasks.get(5, 7)
  check('不可用仍成功', d.id === 7 && d.result_available === false)
  check('不可用诚实无证据', Array.isArray(d.evidence) && d.evidence.length === 0)
  check('不可用零result请求', calls() === 0, String(calls()))
}

// 4. 成功 → 证据身份精确。
{
  installStub({ resultAvailable: true, resultStatus: 200 })
  const d = await api.tasks.get(5, 7)
  check(
    '成功证据身份精确',
    d.evidence.length === 1
    && d.evidence[0].package_id === 26
    && d.evidence[0].object_id === 99
    && d.evidence[0].status === 'complete',
    JSON.stringify(d.evidence),
  )
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all evidence collect checks passed')
}
