/**
 * 结果与评估 feature 前端领域模型。
 *
 * 与 HTTP 响应 DTO 区分:
 * - DTO 保留后端原始 dimensions 嵌套与 snake_case 字段;
 * - 模型按界面消费诉求扁平化维度字段, 剥离信封结构。
 */

import type {
  AssessmentGrade,
  EvidenceCandidate,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  PlanningSolution,
  ResultAssessment,
  ResultSelection,
  SnapshotRefs,
} from '../../types'

export type {
  AssessmentGrade,
  EvidenceCandidate,
  FinancialAbsence,
  FinancialEvaluationBlock,
  MetricValue,
  PlanningSolution,
  ResultAssessment,
  ResultSelection,
  SnapshotRefs,
}

export type AssessmentType = 'full' | 'physical' | 'optimality' | 'financial' | 'reliability'

/** 逐时序列公开元数据(与后端已封存 series 同一身份)。 */
export interface HourlySeriesMeta {
  series_id: string
  instance_id: string
  variable_id: string
  unit: string
}

/** 任务计算结果前端模型。 */
export interface TaskResult {
  /** available=已提交证据包; no_evidence=任务尚无证据包(未完成), 内容字段为 null。 */
  evidence_status: 'available' | 'no_evidence'
  evidence_package_id: number | null
  metrics: Record<string, MetricValue> | null
  /** 已封存财务评价(同一证据内容原样投影, 无块为 null)。 */
  financial: FinancialEvaluationBlock | null
  /** 缺块原因(有块为 null; 后端按可观察事实判定)。 */
  financialAbsence: FinancialAbsence
  /** 真实候选列表(选择用 index)。 */
  candidates: EvidenceCandidate[]
  /** 规划解读取(无可用解为 null)。 */
  planningSolution: PlanningSolution | null
  /** 原快照资源引用。 */
  snapshotRefs: SnapshotRefs | null
}

/** 逐时序列读取模型。 */
export type HourlyRead =
  | { absent: true }
  | { absent: false; flows: Record<string, number[]>; series: HourlySeriesMeta[] }
