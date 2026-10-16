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
 * 运行（仓库根目录；只在 Docker 构建和执行，复用前端已有 esbuild）：
 *   docker build --target build -f frontend/Dockerfile -t pies-focused-tests .
 *   docker run --rm pies-focused-tests sh -ec './node_modules/.bin/esbuild tests/result_view_no_synthetic_diagnostics.test.mjs --bundle --platform=node --format=esm --outfile=/tmp/focused.mjs; node /tmp/focused.mjs'
 * 验证后删除自建 pies-focused-tests 镜像，不删除基础镜像。
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

// 3. 后端明确的未完成/无证据状态：保留 null，不制造 id=0 或空指标成功。
{
  installStub({
    evidence_status: 'no_evidence', evidence: null, metrics_summary: null,
    financial_evaluation: null, financial_absence: 'outcome_not_normal',
    candidates: null, planning_solution: null, snapshot_refs: null,
  })
  const res = await api.results.result(5, 7)
  check('未完成状态原样保留', res.evidence_status === 'no_evidence')
  check('无证据无伪造id', res.evidence_package_id === null)
  check('无指标保留null', res.metrics === null)
  check('未完成缺席原因保留', res.financialAbsence === 'outcome_not_normal')
  check('无候选是空集合', res.candidates.length === 0)
}

if (failures > 0) {
  console.error(`${failures} check(s) FAILED`)
  process.exitCode = 1
} else {
  console.log('all result view no-synthetic-diagnostics checks passed')
}
