/**
 * R7 导出不隐式创建评估定向测试（显式任务身份契约）。
 *
 * 覆盖 `src/api/client.ts` exports.excel 经 resolveAssessmentId 的读取语义：
 * 调用方必须显式传入所选任务 id（与所选证据同源）；评估按该任务真实读取，
 * 不存在包→任务隐式反查，不依赖任何 tasks.get 预热：
 * 1. 新任务无既有评估 → 缺评估业务错，且零写请求（评估 GET 会真实发出）；
 * 2. 评估历史为空 → 缺评估错，不 POST /result/assess、不 POST /exports/excel；
 * 3. 有既有评估 → 引用最新一条 id 发 Excel 请求，不 POST /result/assess；
 *    两组不同任务/包各自走自己的评估 GET 路径，Excel 请求体带精确评估/证据 id；
 * 4. 评估列表读取失败(500) → 原错透传，不伪装成缺评估，不写请求；
 * 5. 项目包下载会话语义不变。
 * fetch 用桩代替（node22 原生 Response/fetch），桩按精确方法+路径匹配，
 * 未知请求一律 500 可见失败，不做子串兜底。
 *
 * 运行（Docker，复用已缓存的 playwright 镜像，不新增镜像/依赖）：
 *   docker run --rm -v "$PWD/frontend:/app:ro" --entrypoint sh \
 *     mcr.microsoft.com/playwright:v1.54.2-noble -c "
 *     mkdir -p /tmp/r7/api /tmp/r7/features/datasets && cp /app/src/api/client.ts /app/src/api/hourlyPages.ts /tmp/r7/api/ &&
 *     cp /app/src/features/datasets/contracts.ts /app/src/features/datasets/mappers.ts /tmp/r7/features/datasets/ &&
 *     cp /app/src/types.ts /tmp/r7/types.ts && cp /app/tests/export_no_implicit_assessment.test.mjs /tmp/r7/ &&
 *     sed -i \"s#'../src/api/client.ts'#'./api/client.ts'#\" /tmp/r7/export_no_implicit_assessment.test.mjs &&
 *     sed -i \"s#from '../types'#from '../types.ts'#; s#from './hourlyPages'#from './hourlyPages.ts'#; s#from '../features/datasets/\(contracts\|mappers\)'#from '../features/datasets/\1.ts'#\" /tmp/r7/api/client.ts &&
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

/**
 * 桩：按精确方法+路径匹配；未知请求一律 500 可见失败。
 * assessments 列表内容与状态由调用方给定（按任务 id 区分两组）。
 */
function installStub({ assessmentsByTask = {}, assessmentsStatus = 200 }) {
  const calls = []
  globalThis.fetch = async (url, init = {}) => {
    const text = String(url)
    const method = String(init.method ?? 'GET').toUpperCase()
    const entry = `${method} ${text}`
    const m = text.match(/\/projects\/(\d+)\/tasks\/(\d+)\/result\/assessments$/)
    if (method === 'GET' && m) {
      calls.push(entry)
      const taskId = Number(m[2])
      if (assessmentsStatus !== 200) {
        return new Response('{"detail":"boom"}', { status: assessmentsStatus })
      }
      return new Response(JSON.stringify({ items: assessmentsByTask[taskId] ?? [] }), { status: 200 })
    }
    if (method === 'POST' && /\/projects\/\d+\/result\/assess$/.test(text)) {
      calls.push(entry)
      return new Response(JSON.stringify({ assessment: assessmentItem(99, 0) }), { status: 201 })
    }
    if (method === 'POST' && /\/projects\/\d+\/exports\/excel$/.test(text)) {
      calls.push(entry)
      return new Response(JSON.stringify({ token: 'tok', file_name: 'report.xlsx' }), { status: 200 })
    }
    if (method === 'POST' && /\/projects\/\d+\/exports\/package$/.test(text)) {
      calls.push(entry)
      return new Response(JSON.stringify({ token: 'pkt', file_name: 'project-package.zip' }), { status: 200 })
    }
    if (method === 'GET' && text.includes('/exports/package/download')) {
      calls.push(entry)
      return new Response(new Uint8Array([80, 75, 3, 4]), { status: 200 })
    }
    if (method === 'GET' && text.includes('/exports/excel/download')) {
      calls.push(entry)
      return new Response(new Uint8Array([1, 2, 3]), { status: 200 })
    }
    calls.push(`${entry} [UNMATCHED-STUB-500]`)
    return new Response('{"error":"unexpected"}', { status: 500 })
  }
  return calls
}

function excelInput(taskId, packageId) {
  return { project_id: 5, task_id: taskId, evidence_package_id: packageId, include_hourly: true, lang: 'zh' }
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

// 1. 新任务无既有评估 → 缺评估错；评估 GET 真实发出（显式任务身份驱动），零写请求。
{
  const calls = installStub({ assessmentsByTask: {} })
  await expectNoAssessment('新任务缺评估', () => api.exports.excel(excelInput(7, 26)), calls)
  check(
    '评估GET走显式任务路径',
    calls.some((c) => c === 'GET /api/projects/5/tasks/7/result/assessments'),
    calls.join(';'),
  )
}

// 2. 评估历史为空 → 缺评估错，不建评估、不导出。
{
  const calls = installStub({ assessmentsByTask: { 7: [] } })
  await expectNoAssessment('空历史缺评估', () => api.exports.excel(excelInput(7, 26)), calls)
}

// 3. 两组不同任务/包：各自走自己的评估 GET 路径，Excel 请求体带精确评估/证据 id，不建评估。
for (const [taskId, pkgId, assessId] of [[7, 26, 41], [8, 27, 42]]) {
  const calls = installStub({ assessmentsByTask: { [taskId]: [assessmentItem(assessId, pkgId)] } })
  let sentBody = ''
  const stubFetch = globalThis.fetch
  globalThis.fetch = async (url, init = {}) => {
    const text = String(url)
    if (String(init.method ?? 'GET').toUpperCase() === 'POST' && /\/exports\/excel$/.test(text)) {
      sentBody = String(init.body ?? '')
    }
    return stubFetch(url, init)
  }
  const downloadSessionId = await api.exports.excel(excelInput(taskId, pkgId))
  globalThis.fetch = stubFetch
  check(`任务${taskId}正常导出(本地会话id)`, typeof downloadSessionId === 'number' && downloadSessionId > 0, String(downloadSessionId))
  check(
    `任务${taskId}评估GET路径精确`,
    calls.some((c) => c === `GET /api/projects/5/tasks/${taskId}/result/assessments`),
    calls.join(';'),
  )
  const body = JSON.parse(sentBody)
  check(
    `任务${taskId}请求体精确`,
    body.assessment_id === assessId && body.evidence_package_id === pkgId,
    sentBody,
  )
  check(`任务${taskId}零assess创建请求`, !calls.some((c) => c.startsWith('POST') && c.includes('/result/assess')), calls.join(';'))
}

// 4. 评估列表 500 → 原错透传，不伪装缺评估，不写请求。
{
  const calls = installStub({ assessmentsStatus: 500 })
  const before = calls.length
  try {
    await api.exports.excel(excelInput(9, 28))
    check('列表失败直接抛错', false, '未抛错')
  } catch (err) {
    check('列表失败直接抛错', err?.message_key !== 'ies.error.no_assessment_available', `message_key=${err?.message_key},status=${err?.status}`)
    check('列表失败零写请求', postsOf(calls, before).length === 0, calls.slice(before).join(';'))
  }
}

// 5. 项目包同样返回本地下载会话 id, download() 按此 id 取回文件。
{
  const calls = installStub({})
  const sessionId = await api.exports.package({ project_id: 5, include_versions: true })
  check('项目包返回本地会话id', typeof sessionId === 'number' && sessionId > 0, String(sessionId))
  const got = await api.exports.download(sessionId)
  const bytes = new Uint8Array(await got.blob.arrayBuffer())
  check(
    '会话id可下载包文件',
    got.filename === 'project-package.zip' && bytes.length === 4 && bytes[0] === 80,
    `${got.filename}/${bytes.length}`,
  )
  check('包导出零assess请求', !calls.some((c) => c.includes('/result/assess')), calls.join(';'))
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all export no-implicit-assessment checks passed')
}
