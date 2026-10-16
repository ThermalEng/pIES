/**
 * 结果与评估 feature 前后端契约(与后端 HTTP JSON 字段一一对应, snake_case)。
 *
 * 端点(与后端 iesplan/api/results.py 一致):
 *   GET  /api/projects/{pid}/tasks/{tid}/result             结果视图 → {result: {...}}
 *   GET  /api/projects/{pid}/tasks/{tid}/result/assessments 评估历史 → {items: [...], total: number}
 *   POST /api/projects/{pid}/tasks/{tid}/result/assess      触发新评估 → {assessment: {...}}
 *   POST /api/projects/{pid}/tasks/{tid}/result/select      选择结果 → {selection: {...}, diff: ...}
 *   GET  /api/projects/{pid}/tasks/{tid}/result/diff        选中差异预览 → {diff: ...}
 *   GET  /api/projects/{pid}/tasks/{tid}/result/hourly      逐时结果查询 → 裸对象
 */

import type {
  AssessmentGrade,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  PlanningSolution,
  SnapshotRefs,
} from '../../types'

export type {
  AssessmentGrade,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  PlanningSolution,
  SnapshotRefs,
}

/** 评估记录 DTO(后端 assessment_to_dict 输出)。 */
export interface ResultAssessmentDto {
  id: number
  evidence_package_id: number
  assessor: 'human' | 'system'
  assessed_by: number | null
  created_at: string
  dimensions: {
    physical: AssessmentGrade
    optimality: AssessmentGrade
    financial: AssessmentGrade
    reliability: AssessmentGrade
  }
  fine_states?: Record<string, string>
  summary?: string
  overall_score: number | null
  comment: string | null
  detail: Record<string, unknown> | null
}

/** 评估历史响应 DTO(GET .../result/assessments)。 */
export interface AssessmentListEnvelopeDto {
  items: ResultAssessmentDto[]
  total: number
}

/** 触发评估请求体(POST .../result/assess)。 */
export interface AssessRequestDto {
  assessment_type: 'full' | 'physical' | 'optimality' | 'financial' | 'reliability'
}

/** 触发评估响应 DTO(POST .../result/assess)。 */
export interface AssessEnvelopeDto {
  assessment: ResultAssessmentDto
}

/** 选择结果请求体(POST .../result/select)。 */
export interface SelectResultRequestDto {
  solution_id: number
  selection_type: 'adopt' | 'reference'
  reference_rule?: string | null
  reason?: string | null
}

/** 结果选择记录 DTO。 */
export interface ResultSelectionDto {
  id: number
  project_id: number
  result_index_id: number
  selected_by: number
  selected_at: string
  reason: string | null
  is_current: boolean
}

/** 选择结果响应 DTO(POST .../result/select)。 */
export interface SelectResultEnvelopeDto {
  selection: ResultSelectionDto
  diff: unknown | null
}

/** 逐时序列元数据 DTO(封存在结果视图 hourly_refs[].series)。 */
export interface HourlySeriesMetaDto {
  series_id: string
  instance_id: string
  variable_id: string
  unit: string
}

/** 逐时引用组 DTO。 */
export interface HourlyRefGroupDto {
  series: HourlySeriesMetaDto[]
  rows: number
}

/** 候选解 DTO 条目。 */
export interface EvidenceCandidateDto {
  index: number
}

/** 结果视图主体 DTO(后端 results_app.result_view 输出)。 */
export interface TaskResultViewDto {
  task: {
    id: number
    type: string
    status: string
    business_outcome: string | null
    calc_snapshot_id: number | null
  }
  evidence_status: 'no_evidence' | 'available'
  evidence: {
    id: number
    status: string
    object_id: number | null
    attempt_id: number | null
    created_at: string
  } | null
  assessment: ResultAssessmentDto | null
  metrics_summary: Record<string, MetricValue> | null
  financial_evaluation: FinancialEvaluationBlock | null
  financial_absence: FinancialAbsence
  candidates: EvidenceCandidateDto[] | null
  planning_solution: PlanningSolution | null
  snapshot_refs: SnapshotRefs | null
  best: unknown | null
  plan_summary: unknown | null
  hourly_refs: HourlyRefGroupDto[] | null
  selection: Omit<ResultSelectionDto, 'project_id' | 'is_current'> | null
}

/** 结果视图响应 DTO(GET .../result)。 */
export interface ResultViewEnvelopeDto {
  result: TaskResultViewDto
}

/** 逐时分页响应 DTO(GET .../result/hourly)。 */
export interface HourlyResponseDto {
  field: string
  unit: string
  instance_id: string
  variable_id: string
  start: number
  end: number
  values: number[]
  next_start: number | null
  total_rows: number
}
