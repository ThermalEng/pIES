/**
 * F4 最小前端适配定向单测：并联评价器结果、报告块、新缺块语义与单评价器重试接口。
 */

import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
globalThis.require = createRequire(import.meta.url)

const { createElement } = await import('react')
const { renderToStaticMarkup } = await import('react-dom/server')
const { api } = await import('../src/api/client.ts')
const { mapResultViewDtoToModel } = await import('../src/features/results/mappers.ts')
const { selectDefaultTaskId, selectEvidencePackage } = await import('../src/lib/taskSelection.ts')
const {
  FinancialAbsenceAlert,
  FinancialTabView,
  SealedFinancialCore,
  TaxDetailsTabView,
  buildAnnualOperationChartData,
  buildCashflowChartData,
  buildVatPoolChartData,
  formatEvaluatorDetail,
} = await import('../src/features/results/index.ts')
const { I18nProvider } = await import('../src/i18n/index.ts')
const { messagesEn } = await import('../src/i18n/messages_en.ts')
const { messagesZh } = await import('../src/i18n/messages_zh.ts')

// 1. i18n keys exist in both zh & en for F4 config & results
for (const key of [
  'ies.config.evaluators',
  'ies.config.unsupported_evaluators',
  'ies.config.hint_evaluators',
  'ies.config.hint_options',
  'ies.config.invalid_json_array',
  'ies.config.invalid_json_object',
  'ies.result.fin_absence_evaluators_empty',
  'ies.result.fin_absence_evaluator_failed',
  'ies.result.evaluators_title',
  'ies.result.evaluator_status_succeeded',
  'ies.result.evaluator_status_failed',
  'ies.result.evaluator_status_timed_out',
  'ies.result.evaluator_status_cancelled',
  'ies.result.evaluator_status_skipped',
  'ies.result.evaluator_retry_btn',
  'ies.result.evaluator_retry_ok',
  'ies.result.eval_reports_title',
]) {
  assert.ok(messagesZh[key], `missing zh key ${key}`)
  assert.ok(messagesEn[key], `missing en key ${key}`)
}
assert.equal(messagesZh['ies.config.convergence'], undefined)
assert.equal(messagesEn['ies.config.convergence'], undefined)

// 2. mapResultViewDtoToModel preserves evaluator_results & evaluation_reports
{
  const evaluator_results = [
    {
      evaluator: 'ies.evaluator.finance@1.0.0',
      status: 'failed',
      contract: 'ies.financial-evaluation@1.0.0',
      error: { code: 'EVAL-ERR-001', message: 'finance boom', detail: 'bad binding' },
      attempt_id: 12,
      finished_at: '2026-11-26T10:00:00Z',
    },
    {
      evaluator: 'acme.evaluator.carbon@1.0.0',
      status: 'succeeded',
      contract: 'ies.evaluation-report@1.0.0',
      error: null,
      attempt_id: 12,
      finished_at: '2026-11-26T10:00:01Z',
    },
  ]
  const evaluation_reports = [
    {
      evaluator: 'acme.evaluator.carbon@1.0.0',
      schema: 'ies.evaluation-report',
      schema_version: '1.0.0',
      title: 'Carbon Report',
      metrics: [{ id: 'co2', label: 'CO2', value: 12.5, unit: 'tCO2' }],
      charts: [],
      tables: [],
    },
  ]
  const model = mapResultViewDtoToModel({
    task: { id: 7, type: 'optimization', status: 'completed', business_outcome: 'normal_completion', calc_snapshot_id: 3 },
    evidence_status: 'available',
    evidence: { id: 26, status: 'partial', object_id: 99, attempt_id: 12, created_at: '2026-11-26T10:00:02Z' },
    assessment: null,
    metrics_summary: null,
    solver_summary: null,
    financial_evaluation: null,
    financial_absence: 'evaluator_failed',
    candidates: [],
    planning_solution: null,
    snapshot_refs: null,
    best: null,
    plan_summary: null,
    hourly_refs: null,
    selection: null,
    evaluator_results,
    evaluation_reports,
  })
  assert.deepEqual(model.evaluatorResults, evaluator_results)
  assert.deepEqual(model.evaluationReports, evaluation_reports)
  assert.equal(model.financialAbsence, 'evaluator_failed')
}

// 3. FinancialAbsenceAlert renders new absence reasons
for (const reason of ['evaluators_empty', 'evaluator_failed']) {
  const html = renderToStaticMarkup(
    createElement(
      I18nProvider,
      null,
      createElement(FinancialAbsenceAlert, { finAbsence: reason, outcome: 'normal_completion' }),
    ),
  )
  assert.ok(html.includes(messagesZh[`ies.result.fin_absence_${reason}`]), `missing rendered text for ${reason}`)
}

// 4. Single-evaluator retry API sends exact path & payload and handles child analysis retry response
{
  let captured = null
  globalThis.fetch = async (url, init = {}) => {
    captured = { url: String(url), method: init.method, body: JSON.parse(String(init.body)) }
    return new Response(
      JSON.stringify({
        task: {
          id: 42,
          type: 'analysis',
          status: 'queued',
          business_outcome: null,
          priority: 0,
          calc_snapshot_id: 3,
          requested_by: 1,
          requested_at: '2026-11-26T10:05:00Z',
          attempt_count: 0,
          max_attempts: 1,
          idempotency_key: 'eval-retry-7-abc-123',
          superseded_by_task_id: null,
          created_at: '2026-11-26T10:05:00Z',
          updated_at: '2026-11-26T10:05:00Z',
          result_available: false,
          summary: { attempt_no: 0, percent: 0, stage: 'queued', queue_position: 1 },
        },
        source_task_id: 7,
        evaluator: 'ies.evaluator.finance@1.0.0',
        replayed: false,
      }),
      { status: 201 },
    )
  }
  const res = await api.results.retryEvaluator(5, 7, {
    evaluator: 'ies.evaluator.finance@1.0.0',
    idempotency_key: 'eval-retry-7-abc-123',
  })
  assert.equal(captured.url, '/api/projects/5/tasks/7/result/evaluators/retry')
  assert.equal(captured.method, 'POST')
  assert.deepEqual(captured.body, {
    evaluator: 'ies.evaluator.finance@1.0.0',
    idempotency_key: 'eval-retry-7-abc-123',
  })
  assert.equal(res.task.id, 42)
  assert.equal(res.task.type, 'analysis')
  assert.equal(res.task.project_id, 5)
  assert.equal(res.source_task_id, 7)
  assert.equal(res.evaluator, 'ies.evaluator.finance@1.0.0')
  assert.equal(res.replayed, false)
  assert.equal(res.hint, null)
}

// 6. Child analysis retry task never hijacks default pickedTaskId / evidence selection from source task
{
  const history = [
    { id: 7, type: 'optimization', completed_at: '2026-11-26T10:00:00Z' },
    { id: 42, type: 'analysis', completed_at: '2026-11-26T10:10:00Z' },
  ]
  const defaultWinnerId = selectDefaultTaskId(
    history.filter((tk) => tk.type === 'calc' || tk.type === 'optimization' || tk.type === 'uncertainty'),
  )
  assert.equal(defaultWinnerId, 7)
  const sourceDetail = {
    id: 7,
    type: 'optimization',
    status: 'completed',
    attempts: [{ attempt_no: 1, finished_at: '2026-11-26T10:00:00Z' }],
    evidence: [{ package_id: 26, status: 'available' }],
  }
  const retryDetail = {
    id: 42,
    type: 'analysis',
    status: 'completed',
    attempts: [{ attempt_no: 1, finished_at: '2026-11-26T10:10:00Z' }],
    evidence: [],
  }
  assert.deepEqual(selectEvidencePackage([sourceDetail], null), { taskId: 7, packageId: 26 })
  assert.deepEqual(selectEvidencePackage([sourceDetail, retryDetail], 7), { taskId: 7, packageId: 26 })
}

assert.equal(formatEvaluatorDetail(null), null)
assert.equal(formatEvaluatorDetail(''), null)
assert.equal(formatEvaluatorDetail('raw text'), 'raw text')
assert.equal(
  formatEvaluatorDetail({ exit_code: 1, termination_reason: 'failed' }),
  '{"exit_code":1,"termination_reason":"failed"}',
)

// 5. Partial & malformed nested financial blocks render safely without crashing or fabricating comparison.mode
{
  const malformedFin = {
    evaluation: { rows: [null, {}] },
    annual_operation: [null, {}],
    payments_applied: [null, {}],
    net_cashflows: [null, {}],
    monthly_demand: [null, {}],
    cost_items: [null, {}],
    amounts: [null, { axis: 'scalar', unit: 'CNY', values: [null] }],
    assets: [null, { book_values: [null] }],
    tax_cash: {
      vat: [null, {}],
      income: [null, {}],
      depreciation: [null, {}],
    },
    comparison: {},
  }
  for (const [label, finCandidate] of [
    ['empty', {}],
    ['malformed', malformedFin],
  ]) {
    const coreHtml = renderToStaticMarkup(
      createElement(
        I18nProvider,
        null,
        createElement(SealedFinancialCore, { fin: finCandidate }),
      ),
    )
    if (label === 'empty') {
      assert.equal(coreHtml, '')
    } else {
      assert.ok(!coreHtml.includes('fin_pb_undefined'), 'missing payback status must not leak undefined i18n key')
    }
    assert.deepEqual(buildCashflowChartData({}), { categories: [], bars: [], lines: [] })
    assert.doesNotThrow(() => buildCashflowChartData(finCandidate))
    assert.doesNotThrow(() => buildAnnualOperationChartData(finCandidate))
    assert.doesNotThrow(() => buildVatPoolChartData(finCandidate))

    const finHtml = renderToStaticMarkup(
      createElement(
        I18nProvider,
        null,
        createElement(FinancialTabView, {
          fin: finCandidate,
          finAbsence: null,
          outcome: 'normal_completion',
          metrics: null,
          currency: 'CNY',
          onOpenDetail: () => {},
        }),
      ),
    )
    assert.ok(finHtml.length > 0)
    assert.ok(finHtml.includes('—'), 'missing comparison.mode must render explicit absence marker')
    assert.ok(!finHtml.includes('absolute'), 'missing comparison.mode must not default to absolute')

    const taxHtml = renderToStaticMarkup(
      createElement(
        I18nProvider,
        null,
        createElement(TaxDetailsTabView, {
          fin: finCandidate,
          finAbsence: null,
          outcome: 'normal_completion',
          onOpenDetail: () => {},
        }),
      ),
    )
    assert.ok(taxHtml.length > 0)
  }
}

console.log('ALL F4 RESULTS/EVALUATOR CHECKS PASSED')
