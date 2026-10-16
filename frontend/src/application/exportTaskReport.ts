/** 所选任务报告用例：读取真实评估，再导出同一任务的证据。 */
import { ApiError } from '../types'
import type { ExcelExportInput } from '../types'
import { listAssessments } from '../features/results'
import { exportExcel } from '../features/exports'

export function isNoAssessmentError(err: unknown): err is ApiError {
  return err instanceof ApiError && err.message_key === 'ies.error.no_assessment_available'
}

export async function exportTaskReport(input: ExcelExportInput): Promise<number> {
  const assessments = await listAssessments(input.project_id, input.task_id)
  const latest = assessments[0]
  if (!latest) throw new ApiError(404, null, 'ies.error.no_assessment_available')
  return exportExcel(input.project_id, {
    evidence_package_id: input.evidence_package_id,
    assessment_id: latest.id,
    lang: input.lang ?? 'zh',
  })
}
