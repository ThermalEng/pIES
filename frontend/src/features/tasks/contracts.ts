/**
 * 任务 feature 前后端契约(与后端 application/tasks/views.py 同形, snake_case)。
 *
 * 端点(与后端 /api 路由一致):
 *   GET  /api/projects/{pid}/tasks                    任务列表 {items, next_cursor}
 *   POST /api/projects/{pid}/tasks                    提交任务 {task, replayed, hint}
 *   GET  /api/projects/{pid}/tasks/{tid}              任务详情 {task}
 *   GET  /api/projects/{pid}/tasks/{tid}/inputs       任务冻结输入 {inputs}
 *   POST /api/projects/{pid}/tasks/{tid}/load-draft   冻结输入载入草稿 {draft, config_revision}
 *   POST /api/projects/{pid}/tasks/{tid}/cancel       取消任务 {task, cancel_status, diagnostic}
 *   POST /api/projects/{pid}/tasks/{tid}/retry        手动重试 {task}
 *   GET  /api/projects/{pid}/tasks/{tid}/result        结果视图 {result}(tasks.get 仅读 evidence 身份)
 *
 * 列表/详情/提交/取消/重试的任务体均为同一 task_summary 形态
 * (backend/iesplan/application/tasks/views.py:task_summary); 列表项另带
 * 批量预取的三项(_task_summary_prefetched, 非 completed 任务恒为 null)。
 * 本文件只声明后端实际发布的键: 无 project_id/deadline/outcome_note 键
 * (调用方不得猜测, 缺席语义见 mappers)。
 *
 * 依赖方向: features/tasks/contracts → ../../types(共享领域类型);
 * mappers/api 只依赖 contracts/types/shared, 不导入 api/client 聚合器。
 */

import type { TaskOutcome, TaskStatus, TaskType } from '../../types'

// ---------------------------------------------------------------------------
// 任务摘要(task_summary, 列表/详情/提交/取消/重试共用)
// ---------------------------------------------------------------------------

/** 进度摘要块(列表项内嵌; detail 另有 progress 全量块, 此处无)。 */
export interface TaskSummaryBlockDto {
  attempt_no: number | null
  percent: number | null
  stage: string | null
  queue_position: number | null
}

/** 任务摘要 DTO(与 task_summary 键一一对应; 无 project_id/deadline 键)。 */
export interface TaskSummaryDto {
  id: number
  type: TaskType
  status: TaskStatus
  business_outcome: TaskOutcome | null
  priority: number
  calc_snapshot_id: number | null
  requested_by: number
  requested_at: string
  attempt_count: number
  max_attempts: number
  /** DB 列可空(历史行可为 null); 缺席语义见 mappers(空串, 非猜测值)。 */
  idempotency_key: string | null
  superseded_by_task_id: number | null
  created_at: string
  updated_at: string | null
  result_available: boolean
  summary: TaskSummaryBlockDto
  /** 仅部分行携带(链路追踪); 映射不消费。 */
  trace_id?: string
}

/** 列表项 DTO(摘要 + 批量预取三项; 非 completed 恒为 null)。 */
export interface TaskListItemDto extends TaskSummaryDto {
  completed_at: string | null
  latest_package_id: number | null
  latest_evidence_status: string | null
}

/** 列表响应: 精确 {items, next_cursor} 信封, 直接消费具名键。 */
export interface TaskListResponseDto {
  items: TaskListItemDto[]
  next_cursor: number | null
}

// ---------------------------------------------------------------------------
// 任务详情({task})
// ---------------------------------------------------------------------------

/** 尝试行(与 task_detail attempts 条目同形)。 */
export interface TaskAttemptDto {
  id: number
  attempt_no: number
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'stopped'
  worker_id: string | null
  stop_reason: string | null
  started_at: string | null
  finished_at: string | null
}

/** 当前租约(只暴露 acquired_by/renewed_at/expires_at, 无 token)。 */
export interface TaskLeaseDto {
  attempt_no: number | null
  acquired_by: string
  renewed_at: string
  expires_at: string
}

/** 进度全量块(与 task_detail progress 同形)。 */
export interface TaskProgressDto {
  attempt_no: number | null
  percent: number | null
  stage: string | null
  detail: Record<string, unknown> | null
  updated_at: string | null
  source: string
}

/**
 * 任务诊断行(与 task_diagnostics 表公开视图同形; level/message/created_at
 * 非空, code 可空; 非验证诊断形态)。
 */
export interface TaskDiagDto {
  id: number
  level: 'blocking' | 'error' | 'warning' | 'info'
  code: string | null
  message: string
  stack_trace: string | null
  context: Record<string, unknown> | null
  attempt_id: number | null
  created_at: string
}

/** 快照摘要(与 task_detail calc_snapshot 同形)。 */
export interface CalcSnapshotSummaryDto {
  id: number
  random_seed: number | null
  config_revision: number | null
  config_project_id: number | null
}

/**
 * 批量关系(与 task_detail batch 同形; 子任务状态取任务状态机封闭词汇,
 * 与 TaskStatus 同集, 不另设猜测)。
 */
export interface TaskBatchDto {
  parent_task_id: number | null
  child_task_count: number
  children: Array<{ id: number; status: TaskStatus }>
}

/** 任务详情体(摘要全集 + 详情专有块; 无 outcome_note 键)。 */
export interface TaskDetailDto extends TaskSummaryDto {
  calc_snapshot: CalcSnapshotSummaryDto | null
  attempts: TaskAttemptDto[]
  current_lease: TaskLeaseDto | null
  progress: TaskProgressDto
  diagnostics: TaskDiagDto[]
  batch: TaskBatchDto
}

/** 详情响应: 精确 {task} 信封。 */
export interface TaskDetailResponseDto {
  task: TaskDetailDto
}

// ---------------------------------------------------------------------------
// 提交/取消/重试({task, ...})
// ---------------------------------------------------------------------------

/** 提交响应: 精确 {task, replayed, hint} 信封。 */
export interface TaskCreateResponseDto {
  task: TaskSummaryDto
  replayed: boolean
  hint: string | null
}

/** 取消响应: {task} + 本层状态/诊断标记(与 cancel_task_endpoint 同形)。 */
export interface TaskCancelResponseDto {
  task: TaskSummaryDto
  cancel_status: string
  diagnostic: string
}

/** 重试响应: 精确 {task} 信封。 */
export interface TaskRetryResponseDto {
  task: TaskSummaryDto
}

/** 提交请求体(与后端 TaskCreateRequest 同形; evaluation 为顶层类型化输入)。 */
export interface TaskCreateRequestDto {
  task_type: TaskType
  config: Record<string, unknown> | null
  evaluation?: Record<string, unknown>
  idempotency_key: string | null
  parent_task_id: number | null
}

// ---------------------------------------------------------------------------
// 冻结输入({inputs})与载入({draft, config_revision})
// ---------------------------------------------------------------------------

/** 冻结快照行投影(与 task_inputs snapshot 同形)。 */
export interface TaskFrozenSnapshotDto {
  id: number
  project_version_id: number
  dataset_version_ids: number[]
  calc_config_snapshot: Record<string, unknown>
  device_models: Record<string, unknown>
  evaluation: Record<string, unknown>
  schema_version: string
  random_seed: number | null
}

/** 冻结输入响应体(与 task_inputs 返回同形; 无快照时 snapshot=null)。 */
export interface TaskInputsBodyDto {
  task: {
    id: number
    type: TaskType
    status: TaskStatus
    calc_snapshot_id: number | null
  }
  snapshot: TaskFrozenSnapshotDto | null
  frozen_refs: {
    planning_revision: number | null
    finance_profile_id: string | null
    finance_revision: number | null
  }
}

/** 冻结输入响应: 精确 {inputs} 信封。 */
export interface TaskInputsResponseDto {
  inputs: TaskInputsBodyDto
}

/** 载入响应: 精确 {draft, config_revision}(draft 为 draft_to_dict, 只消费 revision)。 */
export interface LoadDraftResponseDto {
  draft: {
    revision: number
  }
  config_revision: number | null
}

// ---------------------------------------------------------------------------
// 结果证据身份(tasks.get 补充读取, 与 result_view evidence 同形)
// ---------------------------------------------------------------------------

/** 证据包身份(与 result_view evidence 条目同形; 只消费三键)。 */
export interface TaskEvidenceDto {
  id: number
  status: 'complete' | 'partial' | 'invalid'
  object_id: number
}

/**
 * 结果视图响应中 tasks.get 补充读取消费的子集(非结果家族通用 result_view
 * 契约): 仅在任务元数据 result_available 明确为 true 时读取, 与可用性判定
 * 同查询族, 证据包必然存在; 直接消费, 缺席即自然抛错可见。
 */
export interface TaskResultEnvelopeDto {
  result: {
    evidence: TaskEvidenceDto
  }
}
