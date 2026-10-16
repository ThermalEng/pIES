/**
 * 任务 feature 纯 mapper: 后端任务 DTO → 前端领域模型。
 *
 * 全部函数为纯函数: 不访问网络、缓存或 React 状态, 不导入 HTTP 层。
 * 可信响应直接消费具名键: 列表/详情/提交/取消/重试/冻结输入/载入均为
 * 唯一精确信封, 不做 {items}/{key}/裸数组并集猜测(oneOf/asItems 已删除),
 * 不以 ??0/'' 补后端非空列(与 application/tasks/views.py 同形)。
 * 仅在后端真正可空处保留缺席语义(与旧行为一致):
 * business_outcome/calc_snapshot_id/deadline…为 null、
 * idempotency_key 可空列缺席为空串(历史行真实存在 null, 非伪造值)、
 * completed_at/latest_* 仅列表项携带(详情/提交一律 null, 不反查)、
 * 无 project_id/deadline/outcome_note 键(后端未发布, 不猜测: project_id
 * 取请求作用域入参, deadline/outcome_note 恒为 null)。
 * 畸形体自然抛错可见, 不伪装成功。
 */

import type {
  Diagnostic,
  Severity,
  Task,
  TaskAttempt,
  TaskDetail,
  TaskInputs,
} from '../../types'
import type {
  TaskCancelResponseDto,
  TaskCreateResponseDto,
  TaskDetailResponseDto,
  TaskDiagDto,
  TaskEvidenceDto,
  TaskInputsResponseDto,
  TaskListItemDto,
  TaskListResponseDto,
  TaskRetryResponseDto,
  TaskSummaryDto,
} from './contracts'

/** 后端任务摘要(task_summary) → 前端 Task(列表项三项另见 taskFromListItem)。 */
export function taskFromSummary(s: TaskSummaryDto, projectId: number): Task {
  return {
    id: s.id,
    project_id: projectId,
    type: s.type,
    status: s.status,
    business_outcome: s.business_outcome,
    // idempotency_key 列可空: 历史行真实存在 null, 缺席为空串(旧行为保留)。
    idempotency_key: s.idempotency_key ?? '',
    calc_snapshot_id: s.calc_snapshot_id,
    requested_by: s.requested_by,
    requested_at: s.requested_at,
    priority: s.priority,
    // 后端摘要/详情均无 deadline 键: 恒为 null, 不猜测。
    deadline: null,
    superseded_by_task_id: s.superseded_by_task_id,
    attempt_count: s.attempt_count,
    max_attempts: s.max_attempts,
    created_at: s.created_at,
    updated_at: s.updated_at,
    result_available: s.result_available === true,
    // 列表预取三项仅列表项携带: 摘要默认 null(详情/提交与旧行为一致)。
    completed_at: null,
    latest_package_id: null,
    latest_evidence_status: null,
    summary: s.summary,
  }
}

/** 后端任务列表项(含批量预取三项) → 前端 Task。 */
export function taskFromListItem(item: TaskListItemDto, projectId: number): Task {
  return {
    ...taskFromSummary(item, projectId),
    completed_at: item.completed_at,
    latest_package_id: item.latest_package_id,
    latest_evidence_status: item.latest_evidence_status,
  }
}

/** 后端任务列表信封 {items, next_cursor} → 列表项模型(直接消费具名键)。 */
export function taskListFromServer(body: TaskListResponseDto, projectId: number): Task[] {
  return body.items.map((t) => taskFromListItem(t, projectId))
}

/** 后端任务诊断行 → 前端 Diagnostic(错误路径映射, 与旧行为逐键一致)。 */
export function taskDiagFromServer(d: TaskDiagDto): Diagnostic {
  const level = String(d.level ?? 'error')
  const severity: Severity =
    level === 'blocking' ? 'blocking' : level === 'warning' ? 'warning' : level === 'info' ? 'info' : 'error'
  return {
    code: String(d.code ?? 'TASK-DIAG'),
    message_key: 'ies.diag.raw',
    params: { message: String(d.message ?? '') },
    severity,
    blocking: level === 'blocking' || level === 'error',
    location: null,
    fix_hint_key: null,
    ref_ids: [],
    occurred_at:
      d.created_at === null || d.created_at === undefined ? new Date().toISOString() : String(d.created_at),
    source: 'task',
    // 任务诊断行无 trace_id 键(见 task_detail 诊断装配): 恒为 null。
    trace_id: null,
    project_id: null,
    task_id: null,
    suppressed: false,
  }
}

/** 后端任务详情 {task} → 前端 TaskDetail(直接消费具名键)。 */
export function taskDetailFromServer(body: TaskDetailResponseDto, projectId: number): TaskDetail {
  const d = body.task
  const base = taskFromSummary(d, projectId)
  const attempts: TaskAttempt[] = d.attempts.map((a) => ({
    attempt_no: a.attempt_no,
    worker_id: a.worker_id,
    status: a.status,
    stop_reason: a.stop_reason,
    started_at: a.started_at,
    finished_at: a.finished_at,
  }))
  const p = d.progress
  return {
    ...base,
    attempts,
    current_lease: d.current_lease,
    progress: {
      task_id: base.id,
      status: base.status,
      // 进度摘要 attempt_no 可空(尚无尝试): 缺席为 0(旧行为保留)。
      attempt_no: p.attempt_no ?? 0,
      percent: p.percent,
      stage: p.stage,
      detail: p.detail ?? {},
      // updated_at 可空: 缺席为空串(旧行为保留, 无消费者依赖其值)。
      updated_at: p.updated_at ?? '',
      source: p.source === 'redis' ? 'redis' : 'pg',
      retry: false,
    },
    batch: d.batch,
    // 后端详情无 outcome_note 键: 恒为 null, 不猜测。
    outcome_note: null,
    diagnostics: d.diagnostics.map(taskDiagFromServer),
    // 详情体无 evidence 键: 空数组(补充读取见 api.get, 不在此探测)。
    evidence: [],
    calc_snapshot: d.calc_snapshot,
  }
}

/** 提交/取消/重试响应 {task, ...} → 前端 Task(只消费 task 具名键)。 */
export function taskFromCreateResponse(res: TaskCreateResponseDto, projectId: number): Task {
  return taskFromSummary(res.task, projectId)
}

/** 取消响应 {task, ...} → 前端 Task。 */
export function taskFromCancelResponse(res: TaskCancelResponseDto, projectId: number): Task {
  return taskFromSummary(res.task, projectId)
}

/** 重试响应 {task} → 前端 Task。 */
export function taskFromRetryResponse(res: TaskRetryResponseDto, projectId: number): Task {
  return taskFromSummary(res.task, projectId)
}

/** 冻结输入 {inputs} → 前端 TaskInputs(直接消费具名键, 无快照为 null)。 */
export function taskInputsFromServer(body: TaskInputsResponseDto): TaskInputs {
  const v = body.inputs
  const snap = v.snapshot
  return {
    task: {
      id: v.task.id,
      type: v.task.type,
      status: v.task.status,
      calc_snapshot_id: v.task.calc_snapshot_id,
    },
    snapshot:
      snap === null
        ? null
        : {
            id: snap.id,
            project_version_id: snap.project_version_id,
            dataset_version_ids: snap.dataset_version_ids,
            calc_config_snapshot: snap.calc_config_snapshot,
            device_models: snap.device_models,
            evaluation: snap.evaluation,
            schema_version: snap.schema_version,
            random_seed: snap.random_seed,
          },
    frozen_refs: {
      planning_revision: v.frozen_refs.planning_revision,
      finance_profile_id: v.frozen_refs.finance_profile_id,
      finance_revision: v.frozen_refs.finance_revision,
    },
  }
}

/** 载入响应 {draft, config_revision} → 新草稿 revision 与配置 revision。 */
export function loadDraftFromServer(body: { draft: { revision: number }; config_revision: number | null }): {
  draftRevision: number
  configRevision: number | null
} {
  return {
    draftRevision: body.draft.revision,
    configRevision: body.config_revision,
  }
}

/** 冻结数值 → 表单文本(有限 number 或数值字符串才映射, 其余返回 null 表缺席)。 */
export function frozenNumText(v: unknown): string | null {
  if (typeof v === 'number' && Number.isFinite(v)) return String(v)
  if (typeof v === 'string' && v.trim() !== '' && Number.isFinite(Number(v))) return v.trim()
  return null
}

/** 冻结字符串(非空字符串才采用, 其余返回 null 表缺席)。 */
export function frozenStr(v: unknown): string | null {
  return typeof v === 'string' && v !== '' ? v : null
}

/** 冻结评价确认块 → 单行摘要(缺席项以 — 占位, 不猜测)。 */
export function describeFrozenEvaluation(evaluation: Record<string, unknown>): string {
  const parts: string[] = []
  const years = frozenNumText(evaluation.evaluation_years)
  const period = frozenNumText(evaluation.construction_period)
  const operation = frozenNumText(evaluation.operation_years)
  const rate = frozenNumText(evaluation.discount_rate)
  if (years !== null) parts.push(`E${years}`)
  if (period !== null) parts.push(`K${period}`)
  if (operation !== null) parts.push(`N${operation}`)
  if (rate !== null) parts.push(`r${rate}`)
  if (Array.isArray(evaluation.construction_payments)) {
    parts.push(`付款${evaluation.construction_payments.length}笔`)
  }
  if (Array.isArray(evaluation.maintenance_expenses)) {
    parts.push(`维护${evaluation.maintenance_expenses.length}笔`)
  }
  const by = evaluation.confirmed_by
  if (typeof by === 'number' && Number.isFinite(by)) parts.push(`确认人#${by}`)
  const at = frozenStr(evaluation.confirmed_at)
  if (at !== null) parts.push(at)
  return parts.length > 0 ? parts.join(' · ') : '—'
}

/** 冻结计算配置正文 → 单行摘要(缺席以 — 占位, 不猜测)。 */
export function describeFrozenConfig(body: Record<string, unknown>): string {
  const parts = [frozenStr(body.mode), frozenStr(body.generator), frozenStr(body.solver)].filter(
    (x): x is string => x !== null,
  )
  const ref = body.config_ref
  if (typeof ref === 'object' && ref !== null) {
    const rev = (ref as Record<string, unknown>).revision
    if (typeof rev === 'number' && Number.isFinite(rev)) parts.push(`revision v${rev}`)
  }
  return parts.length > 0 ? parts.join(' · ') : '—'
}

/** 冻结财务引用 → 单行摘要(缺席以 — 占位, 不猜测)。 */
export function describeFrozenFinance(profileId: string | null, revision: number | null): string {
  if (profileId === null && revision === null) return '—'
  const parts: string[] = []
  if (profileId !== null) parts.push(profileId)
  if (revision !== null) parts.push(`revision v${revision}`)
  return parts.join(' · ')
}

/** 证据身份 → 前端证据包摘要行(tasks.get 补充读取专用)。 */
export function evidenceFromServer(evidence: TaskEvidenceDto): {
  package_id: number
  object_id: number
  status: 'complete' | 'partial' | 'invalid'
} {
  return {
    package_id: evidence.id,
    object_id: evidence.object_id,
    status: evidence.status,
  }
}
