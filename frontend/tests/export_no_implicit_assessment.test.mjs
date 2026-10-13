/**
 * R7 导出不隐式创建评估定向测试。
 *
 * 覆盖 `src/api/client.ts` exports.excel 经 resolveAssessmentId 的读取语义：
 * 1. 从未见过的证据包 → 直接抛缺评估业务错，且零 HTTP 请求；
 * 2. 评估历史为空 → 抛缺评估业务错，不 POST /result/assess、不 POST /exports/excel；
 * 3. 有既有评估 → 引用最新一条 id 发 Excel 请求，不 POST /result/assess；
 * 4. 评估列表读取失败(500) → 原错透传，不伪装成缺评估，不 POST /result/assess、不 POST /exports/excel。
 * fetch 用桩代替（node22 原生 Response/fetch），业务值全为桩内真实内容，
 * 不 mock 成功语义本身之外的任何行为。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/r7/api && cp /app/src/api/client.ts /app/src/api/hourlyPages.ts /tmp/r7/api/ &&
 *     cp /app/src/types.ts /tmp/r7/types.ts && cp /app/tests/export_no_implicit_assessment.test.mjs /tmp/r7/ &&
 *     sed -i \"s#'../src/api/client.ts'#'./api/client.ts'#\" /tmp/r7/export_no_implicit_assessment.test.mjs &&
 *     sed -i \"s#from '../types'#from '../types.ts'#; s#from './hourlyPages'#from './hourlyPages.ts'#\" /tmp/r7/api/client.ts &&
 *     node /tmp/r7/export_no_implicit_assessment.test.mjs"
 * （node22 自带类型剥离；仅给相对导入补机械后缀，逻辑与仓库文件一致；
 * client.ts 顶层无浏览器副作用。）
 */

import { api } from '../src/api/client.ts'

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

function taskDetail(id) {
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
      result_available: true,
      attempts: [],
      progress: null,
      batch: null,
      outcome_note: null,
      diagnostics: [],
      evidence: [],
    },
  }
}

/** 任务→证据包映射(模块级缓存按包隔离, 每用例独立包)。 */
const PKG_OF_TASK = { 7: 26, 8: 27, 9: 28 }

function resultView(pkgId) {
  return {
    result: { evidence: { id: pkgId, object_id: 90 + pkgId, status: 'complete' } },
  }
}

function assessmentItem(id, pkgId) {
  return {
    id,
    evidence_package_id: pkgId,
    assessor: 'system',
    assessed_by: 1,
    dimensions: { physical: 'pass', optimality: 'pass', financial: 'pass', reliability: 'pass' },
    overall_score: 90,
    comment: null,
    detail: null,
    created_at: '2026-10-13T00:00:00Z',
  }
}

/** 桩：记录全部请求；assessments 列表内容与状态由调用方给定。 */
function installStub({ assessmentsItems = [], assessmentsStatus = 200 }) {
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    const text = String(url)
    const method = String(init.method ?? 'GET').toUpperCase()
    calls.push(`${method} ${text}`)
    if (method === 'POST' && text.includes('/result/assess')) {
      return new Response(JSON.stringify({ assessment: assessmentItem(99, 0) }), { status: 201 })
    }
    if (method === 'POST' && text.includes('/exports/excel')) {
      return new Response(JSON.stringify({ token: 'tok', file_name: 'report.xlsx' }), { status: 200 })
    }
    if (text.includes('/result/assessments')) {
      if (assessmentsStatus !== 200) return new Response('{"detail":"boom"}', { status: assessmentsStatus })
      return new Response(JSON.stringify({ items: assessmentsItems }), { status: 200 })
    }
    for (const [taskId, pkgId] of Object.entries(PKG_OF_TASK)) {
      if (text.includes(`/tasks/${taskId}/result`)) {
        return new Response(JSON.stringify(resultView(Number(pkgId))), { status: 200 })
      }
      if (text.endsWith(`/tasks/${taskId}`)) {
        return new Response(JSON.stringify(taskDetail(Number(taskId))), { status: 200 })
      }
    }
    return new Response('{"error":"unexpected"}', { status: 500 })
  }
  return calls
}

function excelInput(packageId) {
  return { project_id: 5, evidence_package_id: packageId, include_hourly: true, lang: 'zh' }
}

/** 经真实 tasks.get 填充 证据包→任务 映射(与页面加载效应同一路径)。 */
async function fillMapping(taskId, pkgId) {
  const d = await api.tasks.get(5, taskId)
  check(
    `详情填充映射(包${pkgId})`,
    d.evidence.length === 1 && d.evidence[0].package_id === pkgId,
    JSON.stringify(d.evidence),
  )
}

function postsOf(calls, from) {
  return calls.slice(from).filter((c) => c.startsWith('POST'))
}

async function expectNoAssessment(name, fn, calls) {
  const before = calls.length
  try {
    await fn()
  } catch (err) {
    const isBusinessSignal = err?.message_key === 'ies.error.no_assessment_available'
    check(name, isBusinessSignal, `message_key=${err?.message_key}`)
    check(`${name}(零写请求)`, postsOf(calls, before).length === 0, calls.slice(before).join(';'))
    return
  }
  check(name, false, '未抛错')
}

// 1. 从未见过的证据包 → 缺评估错，零 HTTP 请求。
{
  const calls = installStub({})
  await expectNoAssessment('未映射包直接缺评估', () => api.exports.excel(excelInput(99)), calls)
  check('未映射包零请求', calls.length === 0, calls.join(';'))
}

// 2. 评估历史为空 → 缺评估错，不建评估、不导出(包26)。
{
  installStub({})
  await fillMapping(7, 26)
  const calls = installStub({ assessmentsItems: [] })
  await expectNoAssessment('空历史缺评估', () => api.exports.excel(excelInput(26)), calls)
}

// 3. 有既有评估 → 引用最新一条 id，不建评估(包26)。
{
  const calls = installStub({ assessmentsItems: [assessmentItem(41, 26)] })
  const report = await api.exports.excel(excelInput(26))
  check('有评估正常导出', report.report_type === 'excel', JSON.stringify(report.report_type))
  check('发一次Excel请求', calls.filter((c) => c.includes('/exports/excel')).length === 1, calls.join(';'))
  check('零assess创建请求', !calls.some((c) => c.includes('/result/assess') && !c.includes('/assessments')), calls.join(';'))
}

// 3b. Excel 请求体携带该评估 id（独立包27，避免缓存串扰）。
{
  installStub({})
  await fillMapping(8, 27)
  const calls = installStub({ assessmentsItems: [assessmentItem(42, 27)] })
  void calls
  let sentBody = ''
  const stubFetch = globalThis.fetch
  globalThis.fetch = async (url, init = {}) => {
    const text = String(url)
    if (String(init.method ?? 'GET').toUpperCase() === 'POST' && text.includes('/exports/excel')) {
      sentBody = String(init.body ?? '')
    }
    return stubFetch(url, init)
  }
  await api.exports.excel(excelInput(27))
  const body = JSON.parse(sentBody)
  check('请求体引用最新评估id', body.assessment_id === 42 && body.evidence_package_id === 27, sentBody)
}

// 4. 评估列表 500 → 原错透传，不伪装缺评估，不建评估、不导出(独立包28)。
{
  installStub({})
  await fillMapping(9, 28)
  const calls = installStub({ assessmentsStatus: 500 })
  const before = calls.length
  try {
    await api.exports.excel(excelInput(28))
    check('列表失败直接抛错', false, '未抛错')
  } catch (err) {
    check('列表失败直接抛错', err?.message_key !== 'ies.error.no_assessment_available', `message_key=${err?.message_key},status=${err?.status}`)
    check('列表失败零写请求', postsOf(calls, before).length === 0, calls.slice(before).join(';'))
  }
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all export no-implicit-assessment checks passed')
}
