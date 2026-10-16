/**
 * 结果与评估 feature API 客户端(直接消费唯一后端 DTO 契约)。
 *
 * 规范:
 * - 消费对应 contracts 中定义的具体响应 DTO, 不做 oneOf/asList/asRecord 形状猜测;
 * - 错误由底层 request 统一直达标准 ApiError 信封;
 * - 不设立模块级业务缓存, 遵循 0.14 退出标准。
 */

import { request } from '../../shared/http'
import { collectHourlyValues } from '../../api/hourlyPages'
import type {
  AssessEnvelopeDto,
  AssessRequestDto,
  AssessmentListEnvelopeDto,
  HourlyResponseDto,
  ResultViewEnvelopeDto,
  SelectResultEnvelopeDto,
  SelectResultRequestDto,
} from './contracts'
import type {
  AssessmentType,
  HourlyRead,
  HourlySeriesMeta,
  ResultAssessment,
  ResultSelection,
  TaskResult,
} from './model'
import {
  mapAssessmentDtoToModel,
  mapResultViewDtoToModel,
  mapSelectionDtoToModel,
} from './mappers'

/**
 * 获取任务结果视图(四维结论、指标摘要、财务评价与快照引用)。
 */
export async function getResult(projectId: number, taskId: number): Promise<TaskResult> {
  const envelope = await request<ResultViewEnvelopeDto>(
    `/projects/${projectId}/tasks/${taskId}/result`,
  )
  return mapResultViewDtoToModel(envelope.result)
}

/**
 * 获取任务评估历史(不可变追加式, 时间倒序)。
 */
export async function listAssessments(
  projectId: number,
  taskId: number,
): Promise<ResultAssessment[]> {
  const envelope = await request<AssessmentListEnvelopeDto>(
    `/projects/${projectId}/tasks/${taskId}/result/assessments`,
  )
  return envelope.items.map(mapAssessmentDtoToModel)
}

/**
 * 触发系统四维检查(创建新评估记录)。
 */
export async function assess(
  projectId: number,
  taskId: number,
  assessmentType: AssessmentType = 'full',
): Promise<ResultAssessment> {
  const body: AssessRequestDto = { assessment_type: assessmentType }
  const envelope = await request<AssessEnvelopeDto>(
    `/projects/${projectId}/tasks/${taskId}/result/assess`,
    {
      method: 'POST',
      body,
    },
  )
  return mapAssessmentDtoToModel(envelope.assessment)
}

/**
 * 选择结果(保存所选解标识)。
 */
export async function select(
  projectId: number,
  taskId: number,
  input: { result_index_id: number; reason?: string },
): Promise<ResultSelection> {
  const body: SelectResultRequestDto = {
    solution_id: input.result_index_id,
    selection_type: 'adopt',
    reason: input.reason ?? null,
  }
  const envelope = await request<SelectResultEnvelopeDto>(
    `/projects/${projectId}/tasks/${taskId}/result/select`,
    {
      method: 'POST',
      body,
    },
  )
  return mapSelectionDtoToModel(envelope.selection)
}

/**
 * 逐时结果读取(单序列分页查询, 声明列表全量消费)。
 */
export async function readHourly(projectId: number, taskId: number): Promise<HourlyRead> {
  const envelope = await request<ResultViewEnvelopeDto>(
    `/projects/${projectId}/tasks/${taskId}/result`,
  )
  const rawRefs = envelope.result.hourly_refs
  if (rawRefs === null || rawRefs.length === 0) return { absent: true }
  const ref = rawRefs[0]
  if (ref.series.length === 0) return { absent: true }

  const series: HourlySeriesMeta[] = ref.series.map((s) => ({
    series_id: s.series_id,
    instance_id: s.instance_id,
    variable_id: s.variable_id,
    unit: s.unit,
  }))
  const rows = ref.rows

  const pages = await Promise.all(
    series.map(async (meta) => {
      const values = await collectHourlyValues(
        async (start, limit) => {
          const page = await request<HourlyResponseDto>(
            `/projects/${projectId}/tasks/${taskId}/result/hourly`,
            {
              query: { field: meta.series_id, start, end: rows, limit },
            },
          )
          return { values: page.values, next_start: page.next_start }
        },
        rows,
      )
      return [meta.series_id, values] as const
    }),
  )

  const flows: Record<string, number[]> = {}
  for (const [fieldId, values] of pages) {
    flows[fieldId] = values
  }
  return { absent: false, flows, series }
}
