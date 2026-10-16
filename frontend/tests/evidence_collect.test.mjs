/**
 * R4 证据收集失败可见定向测试。
 *
 * 覆盖 `src/features/tasks` 公开入口 getTaskDetail 的补充 /result 读取语义：
 * 1. 任务详情 GET 失败 → 直接 reject（真实 HTTP 状态可见，不返回无证据详情）；
 * 2. result_available=true 但 /result 补充读取失败 → 直接 reject，
 *    不吞错置空 evidence 伪装详情正常无结果；
 * 3. result_available=false → 成功返回且 evidence 为空，不发任何 /result 请求；
 * 4. 成功 → evidence 包身份（package_id/object_id/status）与桩完全一致。
 * 成功桩与后端 application/tasks/views.py task_summary/task_detail 同形：
 * 摘要全集（priority/attempt_count/max_attempts/created_at 等）、summary 块、
 * progress 全量对象、batch、calc_snapshot。
 * fetch 用桩代替（node22 原生 Response/fetch），业务值全为桩内真实内容，
 * 不 mock 成功语义本身之外的任何行为。
 *
 * 运行（仓库根目录；只在 Docker 构建和执行，复用前端已有 esbuild）：
 *   docker build --target build -f frontend/Dockerfile -t pies-focused-tests .
 *   docker run --rm pies-focused-tests sh -ec './node_modules/.bin/esbuild tests/evidence_collect.test.mjs --bundle --platform=node --format=esm --outfile=/tmp/focused.mjs; node /tmp/focused.mjs'
 * 验证后删除自建 pies-focused-tests 镜像，不删除基础镜像。
 */

import { getTaskDetail } from '../src/features/tasks/index.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

/** 与 task_detail 生产者同形的成功桩体（摘要全集 + 详情专有块）。 */
function taskBody({ id, resultAvailable }) {
  return {
    task: {
      id,
      type: 'optimization',
      status: 'completed',
      business_outcome: 'normal_completion',
      priority: 0,
      calc_snapshot_id: 3,
      requested_by: 1,
      requested_at: '2026-10-13T00:00:00Z',
      attempt_count: 1,
      max_attempts: 3,
      idempotency_key: `k-${id}`,
      superseded_by_task_id: null,
      created_at: '2026-10-13T00:00:01Z',
      updated_at: '2026-10-13T00:05:00Z',
      result_available: resultAvailable,
      summary: { attempt_no: 1, percent: 100, stage: 'done', queue_position: null },
      calc_snapshot: { id: 3, random_seed: 7, config_revision: 1, config_project_id: 5 },
      attempts: [],
      current_lease: null,
      progress: {
        attempt_no: 1,
        percent: 100,
        stage: 'done',
        detail: {},
        updated_at: '2026-10-13T00:05:00Z',
        source: 'pg',
      },
      diagnostics: [],
      batch: { parent_task_id: null, child_task_count: 0, children: [] },
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

async function expectApiError(name, fn, { status, code }) {
  try {
    await fn()
  } catch (err) {
    check(`${name}抛错`, true)
    check(`${name}真实状态`, err?.status === status, `status=${err?.status}`)
    check(`${name}真实错误码`, err?.code === code, `code=${err?.code}`)
    return
  }
  check(`${name}抛错`, false, '未抛错')
}

// 1. 详情 GET 失败 → reject，真实 500/API-UNKNOWN 可见。
{
  installStub({ detailStatus: 500 })
  await expectApiError('详情失败', () => getTaskDetail(5, 7), { status: 500, code: 'API-UNKNOWN' })
}

// 2. /result 补充读取失败 → reject，不置空伪装。
{
  installStub({ resultAvailable: true, resultStatus: 500 })
  await expectApiError('可用时result失败', () => getTaskDetail(5, 7), { status: 500, code: 'API-UNKNOWN' })
}

// 3. result_available=false → 成功、无证据、零 /result 请求，生产者字段同步。
{
  const calls = installStub({ resultAvailable: false })
  const d = await getTaskDetail(5, 7)
  check('不可用仍成功', d.id === 7 && d.result_available === false)
  check('不可用诚实无证据', Array.isArray(d.evidence) && d.evidence.length === 0)
  check('不可用零result请求', calls() === 0, String(calls()))
  check('摘要priority同步', d.priority === 0, `priority=${d.priority}`)
  check('摘要尝试数同步', d.attempt_count === 1 && d.max_attempts === 3)
  check('摘要创建时间同步', d.created_at === '2026-10-13T00:00:01Z', `created_at=${d.created_at}`)
  check(
    'progress对象同步',
    d.progress !== null
      && d.progress.attempt_no === 1
      && d.progress.percent === 100
      && d.progress.stage === 'done',
    JSON.stringify(d.progress),
  )
  check('batch同步', d.batch !== null && d.batch.child_task_count === 0, JSON.stringify(d.batch))
  check('快照引用同步', d.calc_snapshot !== null && d.calc_snapshot.id === 3, JSON.stringify(d.calc_snapshot))
}

// 4. 成功 → 证据身份精确。
{
  installStub({ resultAvailable: true, resultStatus: 200 })
  const d = await getTaskDetail(5, 7)
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
