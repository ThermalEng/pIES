/**
 * 任务 feature 公开入口定向测试(无浏览器依赖)。
 *
 * 覆盖 `src/features/tasks` 公开入口的纯映射与表单装配:
 * 1. 摘要/列表/详情/冻结输入/载入/证据的精确信封消费(具名键直读, 无并集猜测);
 * 2. 真实可空语义透传(idempotency_key null/completed 三项缺席 null/
 *    progress 可空 null/诊断 code 可空展示标签, 无快照为 null);
 * 3. 表单↔工作输入往返与提交装配(评价顶层字段、幂等键形态、空串不静默变 0);
 * 4. 冻结摘要文本与缺席占位。
 * 不测试畸形信封: 可信内部交接无运行时守卫, 缺席自然抛错, 不设重复校验。
 *
 * 纯 Node 环境(无浏览器/无 React): 用 esbuild 把公开入口打包为 CJS 后
 * 直接调用(与 tests/modeling_mappers.test.mjs 同一模式)。
 *
 * 运行(在 frontend 目录, Docker 内):
 *   npx esbuild src/features/tasks/index.ts --bundle --format=cjs \
 *     --outfile=/tmp/b1-tasks.cjs
 *   node tests/tasks_mappers.test.mjs
 */

import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

let failures = 0
function check(name, cond, detail = '') {
  if (!cond) failures++
  console.log(`[${cond ? 'PASS' : 'FAIL'}] ${name}${detail ? ` — ${detail}` : ''}`)
}

const tk = require('/tmp/b1-tasks.cjs')

// ---------------------------------------------------------------------------
// 后端形态桩(与 application/tasks/views.py 同形)
// ---------------------------------------------------------------------------

function summary(over = {}) {
  return {
    id: 7,
    type: 'optimization',
    status: 'completed',
    business_outcome: 'normal_completion',
    priority: 0,
    calc_snapshot_id: 41,
    requested_by: 3,
    requested_at: '2026-10-16T10:00:00Z',
    attempt_count: 1,
    max_attempts: 3,
    idempotency_key: 'sub-optimization-r2-abc',
    superseded_by_task_id: null,
    created_at: '2026-10-16T10:00:01Z',
    updated_at: '2026-10-16T10:05:00Z',
    result_available: true,
    summary: { attempt_no: 1, percent: 100, stage: 'done', queue_position: null },
    ...over,
  }
}

// 1. 摘要直读
{
  const t = tk.taskFromSummary(summary(), 4)
  check('summary 直读 id/type/status', t.id === 7 && t.type === 'optimization' && t.status === 'completed')
  check('summary project_id 取请求作用域', t.project_id === 4)
  check('summary 进度块透传', t.summary !== undefined && t.summary.percent === 100)
  check('summary deadline 无键恒 null', t.deadline === null)
  check('summary completed_at 缺席 null', t.completed_at === null && t.latest_package_id === null)
  check('summary result_available 直读', t.result_available === true)
}
// 1b. idempotency_key 可空事实透传(历史行真实 null)
{
  const t = tk.taskFromSummary(summary({ idempotency_key: null }), 4)
  check('idempotency null 透传', t.idempotency_key === null)
}
// 2. 列表项三项
{
  const t = tk.taskFromListItem(
    { ...summary(), completed_at: '2026-10-16T10:05:00Z', latest_package_id: 9, latest_evidence_status: 'complete' },
    4,
  )
  check('列表三项透传', t.completed_at === '2026-10-16T10:05:00Z' && t.latest_package_id === 9 && t.latest_evidence_status === 'complete')
  const u = tk.taskFromListItem({ ...summary(), status: 'running', completed_at: null, latest_package_id: null, latest_evidence_status: null }, 4)
  check('非完成三项恒 null', u.completed_at === null && u.latest_package_id === null)
}
// 3. 列表信封精确 {items, next_cursor}
{
  const items = tk.taskListFromServer({ items: [summary(), summary({ id: 8 })], next_cursor: 8 }, 4)
  check('列表信封直读', items.length === 2 && items[1].id === 8 && items[1].project_id === 4)
}
// 4. 详情全量
{
  const d = tk.taskDetailFromServer(
    {
      task: {
        ...summary(),
        calc_snapshot: { id: 41, random_seed: 7, config_revision: 2, config_project_id: 4 },
        attempts: [{ id: 11, attempt_no: 1, status: 'succeeded', worker_id: 'w1', stop_reason: null, started_at: '2026-10-16T10:00:02Z', finished_at: '2026-10-16T10:05:00Z' }],
        current_lease: null,
        progress: { attempt_no: 1, percent: 100, stage: 'done', detail: {}, updated_at: '2026-10-16T10:05:00Z', source: 'pg' },
        diagnostics: [{ id: 1, level: 'error', code: 'TASK-DATA-001', message: 'boom', stack_trace: null, context: null, attempt_id: 11, created_at: '2026-10-16T10:05:01Z' }],
        batch: { parent_task_id: null, child_task_count: 0, children: [] },
      },
    },
    4,
  )
  check('详情尝试映射', d.attempts.length === 1 && d.attempts[0].status === 'succeeded' && d.attempts[0].worker_id === 'w1')
  check('详情进度合成 task/status', d.progress.task_id === 7 && d.progress.status === 'completed' && d.progress.source === 'pg')
  check('详情诊断映射', d.diagnostics.length === 1 && d.diagnostics[0].code === 'TASK-DATA-001' && d.diagnostics[0].source === 'task')
  check('详情 evidence 缺席为空数组', Array.isArray(d.evidence) && d.evidence.length === 0)
  check('详情 outcome_note 无键 null', d.outcome_note === null)
  check('详情快照透传', d.calc_snapshot !== null && d.calc_snapshot.config_revision === 2)
}
// 4b. 进度可空事实透传
{
  const d = tk.taskDetailFromServer(
    {
      task: {
        ...summary(),
        calc_snapshot: null,
        attempts: [],
        current_lease: null,
        progress: { attempt_no: null, percent: null, stage: null, detail: null, updated_at: null, source: 'pg' },
        diagnostics: [],
        batch: { parent_task_id: null, child_task_count: 0, children: [] },
      },
    },
    4,
  )
  check('进度 attempt_no null 透传', d.progress.attempt_no === null)
  check('进度 updated_at null 透传', d.progress.updated_at === null)
}
// 5. 诊断生产者口径(level/message/created_at 非空直读, code 可空展示标签)
{
  const b = tk.taskDiagFromServer({ id: 1, level: 'blocking', code: null, message: 'm', stack_trace: null, context: null, attempt_id: null, created_at: '2026-10-16T10:00:00Z' })
  check('blocking 口径', b.severity === 'blocking' && b.blocking === true)
  check('code 可空展示标签', b.code === 'TASK-DIAG')
  check('created_at 直读', b.occurred_at === '2026-10-16T10:00:00Z')
  check('message 直读', b.params.message === 'm')
  const w = tk.taskDiagFromServer({ id: 2, level: 'warning', code: 'X', message: 'n', stack_trace: null, context: null, attempt_id: null, created_at: '2026-10-16T10:00:00Z' })
  check('warning 口径', w.severity === 'warning' && w.blocking === false && w.code === 'X')
}
// 6. 提交/取消/重试只消费 task 键
{
  check('提交映射', tk.taskFromCreateResponse({ task: summary(), replayed: false, hint: null }, 4).id === 7)
  check('取消映射', tk.taskFromCancelResponse({ task: summary(), cancel_status: 'cancelled', diagnostic: 'cancel_ok' }, 4).id === 7)
  check('重试映射', tk.taskFromRetryResponse({ task: summary() }, 4).id === 7)
}
// 7. 冻结输入
{
  const snap = {
    id: 41,
    project_version_id: 3,
    dataset_version_ids: [5, 6],
    calc_config_snapshot: { mode: 'planning' },
    device_models: { hp1: {} },
    evaluation: { evaluation_years: 2 },
    schema_version: '5.0.0',
    random_seed: null,
  }
  const full = tk.taskInputsFromServer({
    inputs: {
      task: { id: 7, type: 'optimization', status: 'completed', calc_snapshot_id: 41 },
      snapshot: snap,
      frozen_refs: { planning_revision: 9, finance_profile_id: 'rg', finance_revision: 2 },
    },
  })
  check('冻结快照透传', full.snapshot !== null && full.snapshot.schema_version === '5.0.0' && full.snapshot.dataset_version_ids.length === 2)
  check('冻结引用透传', full.frozen_refs.planning_revision === 9 && full.frozen_refs.finance_profile_id === 'rg')
  const none = tk.taskInputsFromServer({
    inputs: {
      task: { id: 8, type: 'report', status: 'completed', calc_snapshot_id: null },
      snapshot: null,
      frozen_refs: { planning_revision: null, finance_profile_id: null, finance_revision: null },
    },
  })
  check('无快照为 null', none.snapshot === null)
}
// 8. 载入
{
  const r = tk.loadDraftFromServer({ draft: { revision: 12 }, config_revision: 3 })
  check('载入双 revision', r.draftRevision === 12 && r.configRevision === 3)
  const n = tk.loadDraftFromServer({ draft: { revision: 13 }, config_revision: null })
  check('载入配置缺席 null', n.configRevision === null)
}
// 9. 证据身份
{
  const e = tk.evidenceFromServer({ id: 9, status: 'complete', object_id: 77 })
  check('证据身份三键', e.package_id === 9 && e.object_id === 77 && e.status === 'complete')
}

// ---------------------------------------------------------------------------
// 表单↔工作输入与提交装配
// ---------------------------------------------------------------------------
const PAYMENTS = JSON.stringify([{ t: 0, ratio: '0.6' }, { t: 1, ratio: '0.4' }])

// 10. working 往返(calc)
{
  const f = { ...tk.DEFAULT_FORM, constructionPeriod: '1', operationYears: '1', discountRate: '0.08', constructionPayments: PAYMENTS, maintenanceExpenses: '[]' }
  const w = tk.workingOfForm(f)
  check('working 键 snake_case', w.task_type === 'calc' && w.construction_period === '1' && !('dataset_ids' in w))
  const back = tk.formFromWorking(w)
  check('working 往返文本', back.constructionPeriod === '1' && back.discountRate === '0.08')
  check('付款文本原文往返', back.constructionPayments === PAYMENTS)
  const frozenBack = tk.formFromWorking({ ...w, construction_payments: [{ t: 0, ratio: '0.6' }] })
  check('冻结 ratio 定点串转 JSON 数', frozenBack.constructionPayments.includes('"ratio":0.6'))
}
// 11. 空串不静默变 0(解析前判空)
{
  const f = { ...tk.DEFAULT_FORM, constructionPeriod: '', operationYears: '1', discountRate: '0.08', constructionPayments: PAYMENTS, maintenanceExpenses: '[]' }
  check('起始年空值拒绝', tk.parseEvaluation(f) === null)
  const g = { ...f, constructionPeriod: '0', discountRate: '' }
  check('折现率空值拒绝', tk.parseEvaluation(g) === null)
}
// 12. 评价解析口径
{
  const f = { ...tk.DEFAULT_FORM, constructionPeriod: '1', operationYears: '2', discountRate: '0.080', constructionPayments: PAYMENTS, maintenanceExpenses: '[]' }
  const p = tk.parseEvaluation(f)
  check('E=K+N 派生', p !== null && p.evaluation_years === 3 && p.construction_period === 1 && p.operation_years === 2)
  check('折现率 0..1 接受', p !== null && p.discount_rate === 0.08)
  const bad = { ...f, discountRate: '1.5' }
  check('折现率越界拒绝', tk.parseEvaluation(bad) === null)
  const badN = { ...f, operationYears: '0' }
  check('运营年限 <1 拒绝', tk.parseEvaluation(badN) === null)
  const badJ = { ...f, constructionPayments: 'not-json' }
  check('付款非 JSON 拒绝', tk.parseEvaluation(badJ) === null)
}
// ISSUE-06 S1: JSON 维护关联项在工作态、提交态与载入态保留同年顺序。
{
  const expenses = [
    { year: 2, amount: '0', currency: 'CNY', terms: [
      { kind: 'scale_unit_cost', instance_id: 'bat1', property: 'capacity_kwh', unit_cost: { value: '3.25', unit: 'CNY/kWh' } },
      { kind: 'cost_component_share', instance_id: 'bat1', component: 'upfront_capex', ratio: 0.2 },
    ] },
    { year: 2, amount: '100', currency: 'CNY', terms: [] },
  ]
  const f = { ...tk.DEFAULT_FORM, constructionPeriod: '0', operationYears: '2', discountRate: '0.08', constructionPayments: '[{"t":0,"ratio":1}]', maintenanceExpenses: JSON.stringify(expenses) }
  const parsed = tk.parseEvaluation(f)
  check('关联项提交保持有序列表', parsed !== null && JSON.stringify(parsed.maintenance_expenses) === JSON.stringify(expenses))
  const working = tk.workingOfForm(f)
  check('编辑态原文无损保存', working.maintenance_expenses === f.maintenanceExpenses)
  const restored = tk.formFromWorking({ ...working, maintenance_expenses: expenses })
  check('冻结列表载入再次提交不丢关联项', JSON.stringify(tk.parseEvaluation(restored)?.maintenance_expenses) === JSON.stringify(expenses))
}
// 13. 提交装配
{
  const f = { ...tk.DEFAULT_FORM, constructionPeriod: '1', operationYears: '1', discountRate: '0.08', constructionPayments: PAYMENTS, maintenanceExpenses: '[]' }
  const input = tk.buildInput(f, 4, 2, 'nonce1')
  check('提交顶层 evaluation', input.evaluation !== undefined && input.evaluation.evaluation_years === 2)
  check('提交 config_revision', input.config_revision === 2)
  check('calc 无 params', input.params === undefined)
  check('幂等键形态', typeof input.idempotency_key === 'string' && input.idempotency_key.includes('nonce1'))
  const noCfg = tk.buildInput(f, 4, null, 'nonce1')
  check('无配置不带 revision', noCfg.config_revision === undefined)
  const u = { ...f, type: 'uncertainty', seed: '42' }
  const ui = tk.buildInput(u, 4, 2, 'nonce1')
  check('不确定性 params', ui.params !== undefined && ui.params.mode === 'fixed_reliability' && ui.params.seed === 42)
}
// 14. 签名稳定('+1' 与 '1' 同签名; 非法表单仍有界)
{
  const a = { ...tk.DEFAULT_FORM, constructionPeriod: '+1', operationYears: '1', discountRate: '0.08', constructionPayments: PAYMENTS, maintenanceExpenses: '[]' }
  const b = { ...a, constructionPeriod: '1' }
  check('同数值同签名', tk.logicalSigOf(a, 2) === tk.logicalSigOf(b, 2))
  check('签名字符集', /^[A-Za-z0-9._:-]+$/.test(tk.logicalSigOf(a, 2)))
  check('stableHash 确定', tk.stableHash('x') === tk.stableHash('x') && tk.serializeParams({ b: 1, a: 2 }) === '{"a":2,"b":1}')
  check('nonce 形态', /^[a-z0-9]+$/.test(tk.newSubmitNonce()))
}
// 15. 冻结摘要
{
  check(
    '评价摘要',
    tk.describeFrozenEvaluation({ evaluation_years: 2, construction_period: 1, operation_years: 1, discount_rate: 0.08, construction_payments: [{}, {}], maintenance_expenses: [], confirmed_by: 3, confirmed_at: '2026-10-16T10:00:00Z' }) ===
      'E2 · K1 · N1 · r0.08 · 付款2笔 · 维护0笔 · 确认人#3 · 2026-10-16T10:00:00Z',
  )
  check('评价缺席占位', tk.describeFrozenEvaluation({}) === '—')
  check('配置摘要', tk.describeFrozenConfig({ mode: 'planning', generator: 'g', solver: 'highs', config_ref: { revision: 2 } }) === 'planning · g · highs · revision v2')
  check('财务摘要', tk.describeFrozenFinance('rg', 2) === 'rg · revision v2' && tk.describeFrozenFinance(null, null) === '—')
}
// 16. hasSavedConfig
{
  check('配置存在性', tk.hasSavedConfig({ project_id: 4, calculation_config: {}, revision: 2 }) === true)
  check('配置缺席', tk.hasSavedConfig(null) === false)
}
// 17. 不确定性/批量 working 形态
{
  const u = tk.workingOfForm({ ...tk.DEFAULT_FORM, type: 'uncertainty', seed: ' 7 ' })
  check('不确定性内层', u.task_params.params.mode === 'fixed_reliability' && u.task_params.params.seed === '7')
  const back = tk.formFromWorking(u)
  check('seed 文本往返', back.seed === '7')
  const an = tk.workingOfForm({ ...tk.DEFAULT_FORM, type: 'analysis', sweeps: [{ path: 'a.b', values: '1,2', unit: '' }] })
  check('批量 sweeps 原文', an.task_params.params.sweeps[0].values === '1,2')
  const bp = tk.buildParams({ ...tk.DEFAULT_FORM, type: 'analysis', sweeps: [{ path: 'a.b', values: '1, 2', unit: 'kW' }] })
  check('批量提交转数值', bp.sweeps[0].values[1] === 2 && bp.sweeps[0].unit === 'kW')
}

// Field diagnostics and submission parsing share one basic input boundary.
{
  const valid = { ...tk.DEFAULT_FORM, constructionPeriod: '0', operationYears: '1',
    discountRate: '0.08', constructionPayments: '[{"t":0,"ratio":1}]', maintenanceExpenses: '[]' }
  check('empty form identifies all five required fields', Object.keys(tk.evaluationFieldErrors(tk.DEFAULT_FORM)).length === 5)
  check('valid fields have no diagnostics', Object.keys(tk.evaluationFieldErrors(valid)).length === 0)
  for (const [field, value] of [['constructionPeriod', '-1'], ['operationYears', '1.5'],
    ['discountRate', 'Infinity'], ['constructionPayments', '{}'], ['maintenanceExpenses', 'not json']]) {
    const form = { ...valid, [field]: value }
    const errors = tk.evaluationFieldErrors(form)
    check(`invalid ${field} is located and blocks parsing`, Object.keys(errors).length === 1 && !!errors[field] && tk.parseEvaluation(form) === null)
  }
}

if (failures > 0) {
  console.error(`FAILURES: ${failures}`)
  process.exit(1)
}
console.log('ALL TASK MAPPER CHECKS PASSED')
