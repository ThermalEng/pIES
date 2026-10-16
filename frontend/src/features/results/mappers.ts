/** 结果 DTO 的纯展示映射；null 按公开缺席语义保留，不补业务值。 */
import type { ResultAssessmentDto, ResultSelectionDto, TaskResultViewDto } from './contracts'
import type { ResultAssessment, ResultSelection, TaskResult } from './model'

export function mapAssessmentDtoToModel(dto: ResultAssessmentDto): ResultAssessment {
  return {
    id: dto.id,
    evidence_package_id: dto.evidence_package_id,
    assessor: dto.assessor,
    assessed_by: dto.assessed_by,
    dimension_physical: dto.dimensions.physical,
    dimension_optimality: dto.dimensions.optimality,
    dimension_financial: dto.dimensions.financial,
    dimension_reliability: dto.dimensions.reliability,
    overall_score: dto.overall_score,
    comment: dto.comment,
    detail: dto.detail,
    created_at: dto.created_at,
  }
}

export function mapSelectionDtoToModel(dto: ResultSelectionDto): ResultSelection {
  return { ...dto }
}

export function mapResultViewDtoToModel(dto: TaskResultViewDto): TaskResult {
  return {
    evidence_status: dto.evidence_status,
    evidence_package_id: dto.evidence === null ? null : dto.evidence.id,
    metrics: dto.metrics_summary,
    financial: dto.financial_evaluation,
    financialAbsence: dto.financial_absence,
    candidates: dto.candidates === null ? [] : dto.candidates.map((c) => ({ index: c.index })),
    planningSolution: dto.planning_solution,
    snapshotRefs: dto.snapshot_refs,
  }
}
